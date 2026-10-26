"""Dedicated RabbitMQ source; never shares the internal dispatch connection or queue."""

from __future__ import annotations

import base64
import ssl
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import pika
from pika import frame

from dlr.control.schemas.subscription import RabbitMQSourceConfig
from dlr.control.services.subscription_queue import (
    Message,
    QueueSourceRunner,
    SourcePikaLogBoundary,
    classify,
)
from dlr.control.services.subscription_rabbit_budget import MAX_CREDIT, NativeBudget, NativeToken
from dlr.control.services.subscription_source import (
    ScopedBufferBudget,
    SourceFailure,
    SourceRunContext,
)


def typed_property(value: Any, depth: int = 0, budget: list[int] | None = None) -> Any:
    """AMQP field-table bytes/decimals/timestamps survive JSON without coercion."""
    budget = [4096] if budget is None else budget
    budget[0] -= 1
    if depth > 12 or budget[0] < 0:
        raise SourceFailure("source_properties_invalid")
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, bytes):
        return {"amqp_type": "bytes", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Decimal):
        return {"amqp_type": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        return {"amqp_type": "timestamp", "value": value.isoformat()}
    if isinstance(value, int):
        return {"amqp_type": "integer", "value": str(value)}
    if isinstance(value, float):
        import math

        if not math.isfinite(value):
            raise SourceFailure("source_properties_invalid")
        return {"amqp_type": "float", "value": repr(value)}
    if isinstance(value, list):
        return [typed_property(child, depth + 1, budget) for child in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        # A typed wrapper avoids collisions with producer dictionaries containing amqp_type.
        return {
            "amqp_type": "table",
            "value": {
                key: typed_property(child, depth + 1, budget) for key, child in value.items()
            },
        }
    raise SourceFailure("source_properties_invalid")


def guard_content(channel: Any) -> None:
    original = channel._handle_content_frame

    def bounded(value: Any) -> Any:
        # Pika 1.3.2 invokes this before ContentFrameAssembler buffers body fragments.
        if isinstance(value, frame.Header) and value.body_size > 512 * 1024:
            channel._dlr_content_fault = "event_payload_too_large"
            raise SourceFailure("event_payload_too_large")
        return original(value)

    channel._handle_content_frame = bounded


@dataclass(frozen=True)
class _RawDelivery:
    method: Any
    properties: Any
    body: bytes
    token: NativeToken


class RabbitMQTransport:
    def __init__(
        self,
        config: dict[str, Any],
        credentials: dict[str, str],
        *,
        diagnostic: bool,
        budget: ScopedBufferBudget | None = None,
    ):
        value = RabbitMQSourceConfig.model_validate(config)
        if not credentials.get("username") or not credentials.get("password"):
            raise SourceFailure("source_credential_unavailable")
        address = urlsplit(value.server)
        tls = None
        if address.scheme == "amqps":
            trust = ssl.create_default_context(cadata=value.ca_pem)
            tls = pika.SSLOptions(trust, address.hostname)
        parameters = pika.ConnectionParameters(
            host=address.hostname,
            port=address.port,
            virtual_host=value.vhost,
            credentials=pika.PlainCredentials(credentials["username"], credentials["password"]),
            ssl_options=tls,
            heartbeat=10,
            connection_attempts=1,
            retry_delay=0,
            socket_timeout=3,
            stack_timeout=5,
            blocked_connection_timeout=3,
            frame_max=131072,
        )
        self.log_boundary = SourcePikaLogBoundary()
        self.connection: Any = None
        self.channel: Any = None
        self.iterator: Any = None
        self.config = value.model_dump()
        self.micro_t1 = False
        self._native: NativeBudget | None = None
        self._raw_ready: deque[_RawDelivery] = deque()
        self._pending_failure: SourceFailure | None = None
        self.failed_message: Message | None = None
        self._consumer_started = False
        try:
            if (
                not diagnostic
                and budget is not None
                and self.config.get("mode") == "business_batch"
            ):
                self._native = NativeBudget(budget, None)
            self.connection = pika.BlockingConnection(parameters)
            with self.deadline():
                self.channel = self.connection.channel()
            if self._native is not None and NativeBudget.compatible(
                self.connection._impl, self.channel._impl
            ):
                self._native.channel = self.channel._impl
                self._native.install(self.connection._impl)
                self.micro_t1 = True
            else:
                if self._native is not None:
                    self._native.closed()
                    self._native = None
                guard_content(self.channel._impl)
            # Passive declaration performs no mutation. Non-durable queues are unsupported:
            # AMQP passive responses do not expose durable, so require it in source setup.
            with self.deadline():
                self.channel.queue_declare(queue=value.queue, passive=True)
            if not diagnostic:
                with self.deadline():
                    self.channel.basic_qos(
                        prefetch_count=MAX_CREDIT if self.micro_t1 else 1, global_qos=False
                    )
                if not self.micro_t1:
                    self.iterator = self.channel.consume(
                        queue=value.queue, auto_ack=False, exclusive=True, inactivity_timeout=0.2
                    )
        except Exception:
            self.close()
            raise

    @contextmanager
    def deadline(self) -> Iterator[None]:
        def abort() -> None:
            self.connection._impl._terminate_stream(
                pika.exceptions.StreamLostError("source operation deadline")
            )

        timer = self.connection.call_later(3, abort)
        try:
            yield
        finally:
            if not self.connection.is_closed:
                self.connection.remove_timeout(timer)

    def diagnostic(self) -> dict[str, Any]:
        return {
            "source_type": "rabbitmq",
            "cluster_id": self.config["cluster_id"],
            "group_available": True,
        }

    def _capture(self, _channel: Any, method: Any, properties: Any, body: bytes) -> None:
        # Native callback only transfers references. It never parses a payload,
        # runs user code, makes a DB call or ACKs a delivery.
        assert self._native is not None
        token = self._native.tokens.get(method.delivery_tag)
        if token is None or len(self._raw_ready) >= MAX_CREDIT or len(body) != token.raw_size:
            self._native.fail("source_buffer_capacity", capacity=True)
        self._raw_ready.append(_RawDelivery(method, properties, body, token))

    def poll_bounded(self, timeout_seconds: float) -> Message | None:
        if not self.micro_t1:
            raise SourceFailure("source_unavailable", "backoff")
        # A cohort only drains already complete messages. Zero wait is inside
        # the <=5ms collection bound, even when heartbeat/output is backpressured.
        # In particular, process_data_events(0) still waits for an outbound flush.
        if self._native is not None:
            self._native.pause_io(self.connection._impl)
        if self._raw_ready:
            raw: _RawDelivery | None = self._raw_ready.popleft()
            try:
                assert raw is not None
                message = self._message(raw.method, raw.properties, raw.body, raw.token)
            except SourceFailure as error:
                assert raw is not None
                self.failed_message = Message(
                    str(raw.properties.message_id or ""),
                    raw.body,
                    {},
                    raw.method.delivery_tag,
                    native_token=raw.token,
                )
                self._pending_failure = SourceFailure(error.code, error.disposition)
                error.__traceback__ = None
                error.__context__ = None
                error.__cause__ = None
            else:
                # _message's frame and the captured SDK properties must really
                # be gone before replacing their conversion-peak reservation.
                raw = None
                assert message.native_token is not None
                message.native_token.finish_properties(message.attributes, message.message_id)
                return message
        if self._pending_failure is not None:
            raise SourceFailure(self._pending_failure.code, self._pending_failure.disposition)
        return None

    def _poll_native(self, timeout_seconds: float) -> None:
        """First-message wait only; never wait for output after a body is ready."""
        if self._raw_ready or self._pending_failure is not None:
            return
        timer = None
        loop = self.connection._impl.ioloop
        try:
            assert self._native is not None
            self._native.resume_io()
            if not self._consumer_started:
                # The checked asynchronous Channel registers capture before its
                # Consume RPC. It neither waits for that RPC nor dispatches a
                # suspended BlockingChannel generator holding the last body.
                self.channel._impl.basic_consume(
                    queue=self.config["queue"],
                    auto_ack=False,
                    exclusive=True,
                    on_message_callback=self._capture,
                )
                self._consumer_started = True
            expires = time.monotonic() + max(0, timeout_seconds)
            expired = False

            def expire() -> None:
                nonlocal expired
                expired = True

            # Native timers bound poll(); BlockingConnection.call_later would
            # enqueue a timer event requiring an unrelated blocking dispatch.
            timer = loop.call_later(max(0, timeout_seconds), expire)
            while (
                not self._raw_ready
                and not expired
                and time.monotonic() < expires
                and not self.connection.is_closed
                and not self.connection._closed_result.ready
                and not self.channel._impl.is_closed
            ):
                loop.poll()
                loop.process_timeouts()
            if (
                self.connection.is_closed
                or self.connection._closed_result.ready
                or self.channel._impl.is_closed
            ):
                reason = getattr(self.channel._impl, "_closing_reason", None)
                failure = classify(reason) if isinstance(reason, Exception) else None
                self._pending_failure = (
                    SourceFailure(failure.code, failure.disposition)
                    if failure is not None
                    else SourceFailure("source_unavailable", "backoff")
                )
            if self._native is not None and self._native.fault is not None:
                # Pika's transport may catch the callback exception and close
                # the socket rather than rethrowing through IOLoop.poll().
                observed_fault = self._native.fault
                self._pending_failure = SourceFailure(
                    observed_fault.code, observed_fault.disposition
                )
        except Exception as error:
            native = self._native.fault if self._native else None
            if native is not None:
                self._pending_failure = SourceFailure(native.code, native.disposition)
            elif isinstance(error, SourceFailure):
                self._pending_failure = SourceFailure(error.code, error.disposition)
            else:
                self._pending_failure = SourceFailure("source_unavailable", "backoff")
            error.__traceback__ = None
            error.__context__ = None
            error.__cause__ = None
        finally:
            if timer is not None:
                loop.remove_timeout(timer)
            if self._native is not None:
                self._native.pause_io(self.connection._impl)

    def poll(self) -> Message | None:
        if self.micro_t1:
            self._poll_native(0.2)
            return self.poll_bounded(0)
        try:
            with self.deadline():
                method, properties, body = next(self.iterator)
        except Exception:
            if getattr(self.channel._impl, "_dlr_content_fault", None):
                raise SourceFailure("event_payload_too_large") from None
            raise
        if method is None:
            return None
        return self._message(method, properties, body)

    def _message(
        self, method: Any, properties: Any, body: bytes, token: NativeToken | None = None
    ) -> Message:
        if len(body) > 512 * 1024:
            raise SourceFailure("event_payload_too_large")
        if not properties.message_id:
            raise SourceFailure("source_message_id_required")
        if token is not None and not token.prepare_properties(properties, method):
            raise SourceFailure("source_buffer_capacity", "backoff")
        values = {
            name: typed_property(getattr(properties, name))
            for name in (
                "content_type",
                "content_encoding",
                "headers",
                "delivery_mode",
                "priority",
                "correlation_id",
                "reply_to",
                "expiration",
                "timestamp",
                "type",
                "user_id",
                "app_id",
                "cluster_id",
            )
        }
        attrs = {
            "cluster_id": self.config["cluster_id"],
            "vhost": self.config["vhost"],
            "queue": self.config["queue"],
            "exchange": method.exchange,
            "routing_key": method.routing_key,
            "properties": values,
        }
        return Message(
            properties.message_id, body, attrs, method.delivery_tag, properties.content_type, token
        )

    def ack(self, message: Message) -> None:
        with self.deadline():
            self.channel.basic_ack(delivery_tag=message.handle, multiple=False)
            # Flush the asynchronous AMQP ACK within the bounded I/O turn.
            self.connection.process_data_events(time_limit=0.01)

    def ack_handles(self, tags: list[int]) -> None:
        assert self._native is not None
        for tag in tags:
            self._native.retiring(tag)
        self._native.resume_io()
        with self.deadline():
            for tag in tags:
                self.channel._impl.basic_ack(delivery_tag=tag, multiple=False)
            # _flush_output waits for the outbound buffer even at time_limit=0.
            self.connection.process_data_events(time_limit=0)
        for tag in tags:
            self._native.released(tag)
        self._native.pause_io(self.connection._impl)

    def close(self) -> None:
        try:
            self._close()
        finally:
            self.log_boundary.close()

    def _close(self) -> None:
        connection = self.connection
        if connection is None or connection.is_closed:
            self._release_closed_buffers()
            return
        # Deliberately abort TCP: broker requeues any unacknowledged delivery. A graceful
        # Close/CloseOk handshake can wait indefinitely when the peer stops responding.
        connection._impl._terminate_stream(pika.exceptions.StreamLostError("source closed"))
        deadline = time.monotonic() + 2
        while not connection.is_closed and time.monotonic() < deadline:
            try:
                connection.process_data_events(time_limit=0.05)
            except pika.exceptions.AMQPError:
                break
        if not connection.is_closed:
            raise SourceFailure("source_close_unconfirmed", closed=False)
        self._release_closed_buffers()

    def _release_closed_buffers(self) -> None:
        self._raw_ready.clear()
        self.failed_message = None
        if self._native is None:
            return
        if self.connection is not None:
            self.connection._impl._frame_buffer = b""
            self.connection._impl._error = None
            self.connection._closed_result.reset()
        if self.channel is not None:
            self.channel._impl._content_assembler._reset()
            self.channel._impl._closing_reason = None
            self.channel._closing_reason = None
            self.channel._pending_events.clear()
        self._native.closed()


class RabbitMQSourceRunner(QueueSourceRunner):
    def open_context(self, context: SourceRunContext) -> RabbitMQTransport:
        return RabbitMQTransport(
            context.source_config,
            context.credential_fields,
            diagnostic=False,
            budget=context.buffer
            if context.source_config.get("mode") == "business_batch"
            else None,
        )

    def open(
        self, config: dict[str, Any], credentials: dict[str, str], *, diagnostic: bool
    ) -> RabbitMQTransport:
        return RabbitMQTransport(config, credentials, diagnostic=diagnostic)
