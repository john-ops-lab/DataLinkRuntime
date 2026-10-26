"""Async collection before native work; every waiter retains its own permit."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException


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
    timer: asyncio.TimerHandle | None = None


class HttpCollector:
    def __init__(self, dispatch: Callable[[str, list[HttpInput]], None]) -> None:
        self.dispatch = dispatch
        self.pending: dict[str, _Group] = {}

    def add(self, public_id: str, item: HttpInput) -> None:
        loop = asyncio.get_running_loop()
        group = self.pending.get(public_id)
        if group is None:
            group = self.pending[public_id] = _Group()
            group.timer = loop.call_later(0.005, self.flush, public_id)
        group.items.append(item)
        if len(group.items) == 4:
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
