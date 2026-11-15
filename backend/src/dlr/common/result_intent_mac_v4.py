"""Canonical keyed result-body digest shared by Control and Worker.

The byte ceiling bounds the encoded body, not JSON parsing, sorting, object
copies, process RSS, or concurrent requests.
"""

from __future__ import annotations

import hmac
import json
import math
from collections.abc import Callable
from typing import cast

RESULT_INTENT_MAC_CONTEXT = b"dlr/result-intent/body/v1\x00"


class ResultBodyMacError(ValueError):
    """A bounded, non-sensitive result encoding failure."""


def _check_json_lower_bound(value: object, limit: int) -> None:
    remaining = limit
    active: set[int] = set()

    def take(size: int) -> None:
        nonlocal remaining
        if size > remaining:
            raise ValueError
        remaining -= size

    def walk(item: object) -> None:
        if type(item) is str:
            take(len(item) + 2)
        elif item is None or type(item) in (bool, int):
            take(1)
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError
            take(1)
        elif type(item) in (dict, list, tuple):
            if id(item) in active:
                raise ValueError
            active.add(id(item))
            try:
                if type(item) is dict:
                    take(2 + max(0, len(item) - 1) + len(item))
                    for key, child in item.items():
                        if type(key) is not str:
                            raise ValueError
                        walk(key)
                        walk(child)
                else:
                    sequence = cast("list[object] | tuple[object, ...]", item)
                    take(2 + max(0, len(sequence) - 1))
                    for child in sequence:
                        walk(child)
            finally:
                active.remove(id(item))
        else:
            raise ValueError

    walk(value)


def _validate_arguments(body: object, claim_token: object, max_body_bytes: object) -> None:
    if (
        type(body) is not dict
        or type(claim_token) is not str
        or not 1 <= len(claim_token) <= 512
        or type(max_body_bytes) is not int
        or not 0 < max_body_bytes < 2**63
    ):
        raise ResultBodyMacError("result intent unavailable")


def _encode_json(item: object, emit: Callable[[bytes], None], remaining: Callable[[], int]) -> None:
    """Emit the same canonical JSON used by Control without a whole-body copy."""
    if item is None:
        emit(b"null")
    elif item is True:
        emit(b"true")
    elif item is False:
        emit(b"false")
    elif type(item) is str:
        emit(b'"')
        for offset in range(0, len(item), 4096):
            part = json.encoder.encode_basestring(item[offset : offset + 4096])
            emit(part[1:-1].encode("utf-8"))
        emit(b'"')
    elif type(item) is int:
        if item.bit_length() > remaining() * 4:
            raise ValueError
        emit(str(item).encode("ascii"))
    elif type(item) is float:
        emit(json.dumps(item, allow_nan=False).encode("ascii"))
    elif type(item) is dict:
        emit(b"{")
        for index, key in enumerate(sorted(item)):
            if type(key) is not str:
                raise ValueError
            if index:
                emit(b",")
            _encode_json(key, emit, remaining)
            emit(b":")
            _encode_json(item[key], emit, remaining)
        emit(b"}")
    elif type(item) in (list, tuple):
        emit(b"[")
        sequence = cast("list[object] | tuple[object, ...]", item)
        for index, child in enumerate(sequence):
            if index:
                emit(b",")
            _encode_json(child, emit, remaining)
        emit(b"]")
    else:
        raise ValueError


def result_body_mac(body: dict[str, object], claim_token: str, max_body_bytes: int) -> bytes:
    """Hash canonical JSON without first serializing an oversized result."""
    _validate_arguments(body, claim_token, max_body_bytes)
    try:
        _check_json_lower_bound(body, max_body_bytes)
        digest = hmac.new(claim_token.encode("utf-8"), RESULT_INTENT_MAC_CONTEXT, "sha256")
        remaining = max_body_bytes

        def emit(data: bytes) -> None:
            nonlocal remaining
            if len(data) > remaining:
                raise ValueError
            remaining -= len(data)
            digest.update(data)

        _encode_json(body, emit, lambda: remaining)
        return digest.digest()
    except Exception:
        raise ResultBodyMacError("result intent unavailable") from None


def stream_result_request(
    body: dict[str, object],
    claim_token: str,
    max_body_bytes: int,
    write: Callable[[bytes], None],
    *,
    _binding_fields: frozenset[str] | None = None,
    _binding_digest: hmac.HMAC | None = None,
) -> tuple[bytes, int]:
    """Write canonical request bytes and hash the same bytes excluding its token.

    The writer must consume each complete chunk or raise; partially written
    output must never be sent. The returned byte count includes the token.
    This bounds encoded bytes only, not memory or the lifetime of the output.
    """
    _validate_arguments(body, claim_token, max_body_bytes)
    if (
        "claim_token" in body
        or not callable(write)
        or (_binding_fields is None) != (_binding_digest is None)
        or (
            _binding_fields is not None
            and (
                type(_binding_fields) is not frozenset
                or not _binding_fields
                or "claim_token" in _binding_fields
                or not _binding_fields.issubset(body)
                or type(_binding_digest) is not hmac.HMAC
            )
        )
    ):
        raise ResultBodyMacError("result intent unavailable")
    try:
        # Reject an obviously oversized mapping before even its shallow copy.
        _check_json_lower_bound(body, max_body_bytes)
        fields = dict(body)
        fields["claim_token"] = claim_token
        _check_json_lower_bound(fields, max_body_bytes)
        digest = hmac.new(claim_token.encode("utf-8"), RESULT_INTENT_MAC_CONTEXT, "sha256")
        remaining = max_body_bytes

        def emit(data: bytes) -> None:
            nonlocal remaining
            if len(data) > remaining:
                raise ValueError
            write(data)
            remaining -= len(data)

        def emit_both(data: bytes) -> None:
            emit(data)
            digest.update(data)

        def emit_bound(data: bytes) -> None:
            emit_both(data)
            assert _binding_digest is not None
            _binding_digest.update(data)

        if _binding_digest is not None:
            _binding_digest.update(b"{")
        emit_both(b"{")
        body_index = 0
        bound_index = 0
        for index, key in enumerate(sorted(fields)):
            if type(key) is not str:
                raise ValueError
            if index:
                emit(b",")
            if key == "claim_token":
                _encode_json(key, emit, lambda: remaining)
                emit(b":")
                _encode_json(claim_token, emit, lambda: remaining)
            else:
                if body_index:
                    digest.update(b",")
                bound = _binding_fields is not None and key in _binding_fields
                if bound:
                    assert _binding_digest is not None
                    if bound_index:
                        _binding_digest.update(b",")
                    bound_index += 1
                field_emit = emit_bound if bound else emit_both
                _encode_json(key, field_emit, lambda: remaining)
                field_emit(b":")
                _encode_json(fields[key], field_emit, lambda: remaining)
                body_index += 1
        emit_both(b"}")
        if _binding_digest is not None:
            _binding_digest.update(b"}")
        return digest.digest(), max_body_bytes - remaining
    except Exception:
        raise ResultBodyMacError("result intent unavailable") from None
