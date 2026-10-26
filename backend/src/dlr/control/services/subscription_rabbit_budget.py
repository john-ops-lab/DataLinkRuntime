"""Opt-in Pika 1.3.2 byte credits, before decode and content assembly.

Credits belong to the existing owner/global buffer. They are memory accounting,
never source custody or an ACK authority. Unknown close retains every credit.
"""

from __future__ import annotations

import hashlib
import inspect
import struct
import sys
from dataclasses import dataclass
from typing import Any, NoReturn

import pika
from pika import frame, spec

from dlr.control.services.subscription_rabbit_metadata import (
    heap_size,
    preview_header,
    typed_scratch,
)
from dlr.control.services.subscription_source import ScopedBufferBudget, SourceFailure

Channel = pika.channel.Channel
ContentFrameAssembler = pika.channel.ContentFrameAssembler
Connection = pika.connection.Connection

MAX_CREDIT = 4
FRAME_MAX = 131072
RECV_BYTES = 4096
FRAME_WORKSPACE = 2 * FRAME_MAX + 2 * RECV_BYTES
MAX_RAW = 512 * 1024
MAX_INLINE = 256 * 1024
FRAGMENT_OVERHEAD = 64
METADATA_FACTOR = 128
TOKEN_OVERHEAD = 4096

# These are the inspected upstream 1.3.2 source functions, not a runtime cache
# of whatever an installed SDK happens to contain. A vendor patch, unavailable
# source or changed private layout falls back to the existing single credit.
_SDK_DIGESTS = (
    "6b9207ef5dd9b99c97a8a31ba2030d8b689cbe7a2d9a0da15b67c43f849fd60b",
    "73281f1e039f7b9e91634f2c426157822ae277eeef42639b6a0a5b4cc91fb7de",
    "085f97d1d21ad4952717923f1d7b986fae6b420ca4c87424d2b4b3341c63542d",
    "a1097d51e48baedcd98c9c9c3a8dc9e9516e6accdc9319d331fe0c3dbd06fe73",
    "ca9983e49aabc328389d080710e8ab5798a8f23a3fe7c7fc740d02a7f233cac3",
    "e6624d6ddd330fdd2486d1fa3375242d12904290f190ea106b303820f5274f96",
    "c5ffc807c8eff1153ec1c77fe70ac45e384da3814507292cda9342ac2170085e",
    "37d8cba32446445e2f5931c4ceda3e022cbd8cc3eb3e58d37a4b3d90da7a9fd1",
    "7af144e51539c7489298149a411dda8d898f05fe93e9617174df711149447b51",
    "365b750e2b9f263076181e30dfb88f65cb5dc9e4f0c6c00d33f6b2ca9c46bed0",
    "6bfb1e1ab9ed36f1ba22c5d34600ce9d4246c2fffc9b05281a6157f7e03afac2",
)


class Reservation:
    """One idempotent owner charge; growing fails before allocation."""

    def __init__(self, scope: ScopedBufferBudget):
        self.scope = scope
        self.amount = 0

    def resize(self, amount: int) -> bool:
        if type(amount) is not int or amount < 0:
            raise ValueError("Invalid native reservation")
        delta = amount - self.amount
        if delta > 0 and not self.scope.reserve(delta):
            return False
        if delta < 0:
            self.scope.release(-delta)
        self.amount = amount
        return True

    def release(self) -> None:
        self.resize(0)


@dataclass
class NativeToken:
    tag: int
    reservation: Reservation
    metadata: int
    wire_metadata: int
    raw_size: int | None = None
    fragments: int = 0
    assembled: bool = False
    working: int = 0
    retiring: bool = False

    def prepare_properties(self, properties: Any, method: Any) -> bool:
        assert self.raw_size is not None
        # Replace the predecode bound with the actual SDK graph before booking
        # typed wrappers. No decoded header object is silently left uncharged.
        actual = heap_size(vars(properties)) + heap_size(vars(method))
        scratch = sum(typed_scratch(value) for value in vars(properties).values())
        metadata = TOKEN_OVERHEAD + actual + scratch
        if not self.reservation.resize(metadata + self.raw_size):
            return False
        self.metadata = metadata
        return True

    def finish_properties(self, attributes: dict[str, Any], message_id: str) -> None:
        assert self.raw_size is not None
        # The caller drops its SDK delivery/properties before envelope building.
        # Shared strings/keys are double counted, never undercounted.
        metadata = TOKEN_OVERHEAD + heap_size(attributes) + heap_size(message_id)
        if not self.reservation.resize(metadata + self.raw_size):
            raise SourceFailure("source_buffer_capacity", "backoff")
        self.metadata = metadata

    def begin_processing(self, workspace: int | None = None) -> bool:
        if not self.assembled or self.raw_size is None:
            raise SourceFailure("source_buffer_capacity", "backoff")
        if self.working:
            return True
        encoded = 4 * ((self.raw_size + 2) // 3)
        metadata = min(64 * 1024, self.wire_metadata * 64 + 8192)
        # Envelope plus serializer text/UTF-8 scratch. SDK metadata is charged
        # separately; real RSS remains an independent acceptance requirement.
        working = 3 * min(MAX_INLINE, encoded + metadata) if workspace is None else workspace
        if not self.reservation.resize(self.metadata + self.raw_size + working):
            return False
        self.working = working
        return True

    def end_processing(self) -> None:
        if self.working:
            assert self.raw_size is not None
            self.reservation.resize(self.metadata + self.raw_size)
            self.working = 0


class NativeBudget:
    def __init__(self, scope: ScopedBufferBudget, channel: Any):
        self.scope, self.channel = scope, channel
        self.fixed = Reservation(scope)
        self.tokens: dict[int, NativeToken] = {}
        self.current: NativeToken | None = None
        self.fault: SourceFailure | None = None
        self._provisional: Reservation | None = None
        if not self.fixed.resize(FRAME_WORKSPACE):
            self.fail("source_buffer_capacity", capacity=True)

    def fail(self, code: str, *, capacity: bool = False) -> NoReturn:
        self.fault = SourceFailure(code, "backoff" if capacity else "blocked")
        raise SourceFailure(code, "backoff" if capacity else "blocked")

    def pause_io(self, connection: Any) -> None:
        # No SDK callbacks run while the owner builds an Envelope or runs T1.
        # Keep the actual incomplete wire frame, release ended read/copy peaks.
        self.fixed.resize(len(connection._frame_buffer) + 2 * RECV_BYTES)

    def resume_io(self) -> None:
        if not self.fixed.resize(FRAME_WORKSPACE):
            self.fail("source_buffer_capacity", capacity=True)

    @staticmethod
    def compatible(connection: Any, channel: Any) -> bool:
        try:
            functions = (
                frame.decode_frame,
                Connection._read_frame,
                Connection._on_data_available,
                Channel._handle_content_frame,
                ContentFrameAssembler.process,
                ContentFrameAssembler._handle_body_frame,
                ContentFrameAssembler._finish,
                spec.BasicProperties.decode,
                pika.data.decode_value,
                pika.data.decode_table,
                pika.data.decode_short_string,
            )
            return bool(
                pika.__version__ == "1.3.2"
                and sys.implementation.name == "cpython"
                and struct.calcsize("P") == 8
                and getattr(connection._read_frame, "__func__", None) is Connection._read_frame
                and getattr(channel._handle_content_frame, "__func__", None)
                is Channel._handle_content_frame
                and type(channel._content_assembler) is ContentFrameAssembler
                and pika.adapters.utils.io_services_utils._AsyncTransportBase._MAX_RECV_BYTES
                == RECV_BYTES
                and callable(channel.basic_consume)
                and all(
                    hashlib.sha256(inspect.getsource(function).encode()).hexdigest() == expected
                    for function, expected in zip(functions, _SDK_DIGESTS, strict=True)
                )
            )
        except (AttributeError, TypeError, OSError):
            return False

    def install(self, connection: Any) -> None:
        original_read = connection._read_frame
        original_content = self.channel._handle_content_frame

        def read() -> Any:
            data = connection._frame_buffer
            if len(data) < 7 or data.startswith(b"AMQP"):
                return original_read()
            kind, channel_number, payload = struct.unpack_from(">BHI", data)
            total = payload + 8
            if total > FRAME_MAX:
                self.fail("source_buffer_capacity", capacity=True)
            if len(data) < total or channel_number != self.channel.channel_number:
                return original_read()
            if kind == spec.FRAME_METHOD and payload >= 4:
                method_id = struct.unpack_from(">I", data, 7)[0]
                if method_id == spec.Basic.Deliver.INDEX:
                    active = sum(not token.retiring for token in self.tokens.values())
                    if active >= MAX_CREDIT:
                        self.fail("source_buffer_capacity", capacity=True)
                    charge = TOKEN_OVERHEAD + payload * METADATA_FACTOR
                    lease = Reservation(self.scope)
                    if not lease.resize(charge):
                        self.fail("source_buffer_capacity", capacity=True)
                    self._provisional = lease
                    consumed, value = original_read()
                    tag = value.method.delivery_tag
                    if tag in self.tokens:
                        self.fail("source_buffer_capacity", capacity=True)
                    created = NativeToken(tag, lease, charge, payload)
                    self.tokens[tag] = self.current = created
                    self._provisional = None
                    return consumed, value
            elif kind == spec.FRAME_HEADER:
                token = self.current
                if payload < 12 or token is None or token.raw_size is not None:
                    self.fail("source_buffer_capacity", capacity=True)
                raw_size = struct.unpack_from(">Q", data, 11)[0]
                if raw_size > MAX_RAW or 4 * ((raw_size + 2) // 3) > MAX_INLINE:
                    self.fail("event_payload_too_large")
                try:
                    extra = preview_header(data, payload)
                except SourceFailure as error:
                    self.fail(error.code)
                if not token.reservation.resize(token.metadata + extra + 2 * raw_size):
                    self.fail("source_buffer_capacity", capacity=True)
                token.metadata += extra
                token.wire_metadata += payload
                token.raw_size = raw_size
            elif kind == spec.FRAME_BODY:
                token = self.current
                if (
                    token is None
                    or token.raw_size is None
                    or token.assembled
                    or self.channel._content_assembler._seen_so_far + payload > token.raw_size
                ):
                    self.fail("source_buffer_capacity", capacity=True)
                if not token.reservation.resize(token.reservation.amount + FRAGMENT_OVERHEAD):
                    self.fail("source_buffer_capacity", capacity=True)
                token.fragments += 1
            return original_read()

        def content(value: Any) -> Any:
            token = self.current
            result = original_content(value)
            if isinstance(value, (frame.Header, frame.Body)) and token is not None:
                assembler = self.channel._content_assembler
                if assembler._header_frame is None and token.raw_size is not None:
                    # _finish has joined then cleared all fragments before it
                    # returns. Only the assembled raw body remains in capture.
                    token.assembled = True
                    token.reservation.resize(token.metadata + token.raw_size)
            return result

        connection._read_frame = read
        self.channel._handle_content_frame = content

    def retiring(self, tag: int) -> None:
        token = self.tokens[tag]
        # A source ACK may admit the next window during its I/O flush. Keep
        # this token's bytes, but don't miscount it as another native credit.
        token.retiring = True
        token.reservation.resize(TOKEN_OVERHEAD)
        token.working = 0

    def released(self, tag: int) -> None:
        token = self.tokens.pop(tag)
        token.reservation.release()
        if self.current is token:
            self.current = None

    def closed(self) -> None:
        # Caller first clears actual ready/content/frame references and proves
        # native close. There is no release operation for an unknown close.
        for token in self.tokens.values():
            token.reservation.release()
        self.tokens.clear()
        if self._provisional is not None:
            self._provisional.release()
            self._provisional = None
        self.current = None
        self.fixed.release()
