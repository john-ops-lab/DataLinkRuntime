"""Async collection before native work; every waiter retains its own permit."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

MAX_ITEMS = 16
MAX_PAYLOAD_BYTES = 1024**2
MAX_WAIT_SECONDS = 0.025


def estimated_input_bytes(raw_bytes: int) -> int:
    """Bound raw plus the fixed HTTP Envelope without parsing on the loop.

    Accepted JSON float tokens use >=3 bytes and encode to <=24 bytes;
    other JSON tokens do not grow. Fixed metadata fits within 16 KiB.
    The existing native 256 KiB Envelope limit remains authoritative.
    """
    return raw_bytes + min(256 * 1024, 8 * raw_bytes + 16 * 1024)


@dataclass
class HttpInput:
    authorization: str | None
    key: str | None
    body: bytes
    release: Callable[[], None]
    future: asyncio.Future[tuple[int, dict[str, Any]]]


@dataclass
class _Group:
    items: list[HttpInput] = field(default_factory=list)
    estimated_bytes: int = 0
    timer: asyncio.TimerHandle | None = None
    deadline: float = 0


class HttpCollector:
    def __init__(self, dispatch: Callable[[str, list[HttpInput]], None]) -> None:
        self.dispatch = dispatch
        self.pending: dict[str, _Group] = {}

    def add(self, public_id: str, item: HttpInput) -> None:
        loop = asyncio.get_running_loop()
        group = self.pending.get(public_id)
        if group is not None and loop.time() >= group.deadline:
            # A delayed event-loop timer cannot extend the collection window
            # by admitting a fresh request before its callback gets a turn.
            self.flush(public_id)
            group = None
        size = estimated_input_bytes(len(item.body))
        if group is not None and group.estimated_bytes + size > MAX_PAYLOAD_BYTES:
            self.flush(public_id)
            group = None
        if group is None:
            group = self.pending[public_id] = _Group(deadline=loop.time() + MAX_WAIT_SECONDS)
            group.timer = loop.call_later(MAX_WAIT_SECONDS, self.flush, public_id)
        group.items.append(item)
        group.estimated_bytes += size
        if len(group.items) == MAX_ITEMS or group.estimated_bytes >= MAX_PAYLOAD_BYTES:
            self.flush(public_id)

    def flush(self, public_id: str) -> None:
        group = self.pending.pop(public_id, None)
        if group is None:
            return
        if group.timer is not None:
            group.timer.cancel()
        self.dispatch(public_id, group.items)

    def close(self) -> None:
        """Flush admitted bodies before the executor is shut down."""
        for public_id in tuple(self.pending):
            self.flush(public_id)


def complete(item: HttpInput, result: tuple[int, dict[str, Any]] | HTTPException) -> None:
    if item.future.done():
        return
    if isinstance(result, HTTPException):
        item.future.set_exception(result)
    else:
        item.future.set_result(result)
