"""Process-local fingerprint of a validated v4 Claim task.

This detects changes between Claim and local preparation. It is not a signed
Control assertion, a persisted record, an execution authority or a memory
bound for Claim parsing.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import struct
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from dlr.control.schemas.reliable_runtime import ResourceProfile, V4TaskInputFile, V4TaskPayload

_MODEL_TAGS = {
    V4TaskPayload: b"task",
    V4TaskInputFile: b"input-file",
    ResourceProfile: b"resource-profile",
}


class TaskFingerprintError(ValueError):
    """A task cannot be compared without reflecting its contents."""


def fingerprint(task: V4TaskPayload, claim_token: str) -> bytes:
    """Hash all normalized task fields using bounded per-string chunks.

    Mapping insertion order is retained. A reordered but otherwise equivalent
    mapping may therefore be rejected; no different snapshot is accepted as a
    consequence. The caller must still enforce proof deadlines and ownership.
    """
    if type(task) is not V4TaskPayload or type(claim_token) is not str or not claim_token:
        raise TaskFingerprintError("task snapshot unavailable")
    try:
        digest = hmac.new(
            claim_token.encode("utf-8"), b"dlr/v4/task-snapshot/1\x00", hashlib.sha256
        )
        active: set[int] = set()
        stack: list[tuple[Iterator[object], int]] = []

        def number(value: int) -> None:
            digest.update(struct.pack(">Q", value))

        def text(value: str) -> None:
            digest.update(b"s")
            number(len(value))
            for offset in range(0, len(value), 4096):
                digest.update(value[offset : offset + 4096].encode("utf-8"))

        def mapping_items(value: dict[Any, Any]) -> Iterator[object]:
            for key, item in value.items():
                if type(key) is not str:
                    raise TaskFingerprintError("task snapshot unavailable")
                yield key
                yield item

        def model_items(value: BaseModel) -> Iterator[object]:
            for name in type(value).model_fields:
                yield name
                yield getattr(value, name)

        def visit(value: object) -> None:
            if value is None:
                digest.update(b"n")
            elif type(value) is bool:
                digest.update(b"t" if value else b"f")
            elif type(value) is int:
                digest.update(b"i")
                text(str(value))
            elif type(value) is float:
                if not math.isfinite(value):
                    raise TaskFingerprintError("task snapshot unavailable")
                digest.update(b"d" + struct.pack(">d", value))
            elif type(value) is str:
                text(value)
            elif type(value) is UUID:
                digest.update(b"u" + value.bytes)
            elif type(value) is datetime:
                if value.tzinfo is None or value.utcoffset() is None:
                    raise TaskFingerprintError("task snapshot unavailable")
                digest.update(b"z")
                text(value.astimezone(UTC).isoformat(timespec="microseconds"))
            else:
                identity = id(value)
                if identity in active:
                    raise TaskFingerprintError("task snapshot unavailable")
                if type(value) in _MODEL_TAGS:
                    assert isinstance(value, BaseModel)
                    digest.update(b"m" + _MODEL_TAGS[type(value)] + b"\x00")
                    number(len(type(value).model_fields))
                    iterator = model_items(value)
                elif type(value) is dict:
                    digest.update(b"o")
                    number(len(value))
                    iterator = mapping_items(value)
                elif type(value) is list:
                    digest.update(b"l")
                    number(len(value))
                    iterator = iter(value)
                else:
                    raise TaskFingerprintError("task snapshot unavailable")
                active.add(identity)
                stack.append((iterator, identity))

        visit(task)
        while stack:
            iterator, identity = stack[-1]
            try:
                item = next(iterator)
            except StopIteration:
                stack.pop()
                active.remove(identity)
                digest.update(b"e")
            else:
                visit(item)
        return digest.digest()
    except (AttributeError, OverflowError, RuntimeError, TypeError, UnicodeError, ValueError):
        raise TaskFingerprintError("task snapshot unavailable") from None
