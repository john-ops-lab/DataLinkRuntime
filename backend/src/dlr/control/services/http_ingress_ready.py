"""Bounded scheduling of sealed HTTP groups; members keep their original permits."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal

from fastapi import HTTPException

from dlr.control.services.http_ingress_group import HttpInput

READY_WAIT_SECONDS = 1.0
GroupResults = list[tuple[int, dict[str, Any]] | HTTPException]


@dataclass(eq=False)
class ReadyGroup:
    public_id: str
    items: list[HttpInput]
    loop: asyncio.AbstractEventLoop
    deadline: float
    state: Literal["ready", "submitting", "running", "done"] = "ready"


@dataclass
class _Route:
    ready: deque[ReadyGroup] = field(default_factory=deque)
    active: ReadyGroup | None = None


class HttpReadyQueue:
    """One active group per route, sharing the existing global native budget.

    Only metadata/references are queued. Each member already owns its request
    and body permit, bounding all route/group state by those existing permits.
    The state lock never surrounds submit or callbacks, including immediate
    callbacks from an already-completed concurrent Future.
    """

    def __init__(
        self,
        acquire: Callable[[], bool],
        submit: Callable[[ReadyGroup], Future[GroupResults]],
        finish: Callable[[ReadyGroup, Future[GroupResults] | HTTPException, bool], None],
    ) -> None:
        self._acquire, self._submit, self._finish = acquire, submit, finish
        self._lock = threading.Lock()
        self._routes: dict[str, _Route] = {}
        self._order: deque[str] = deque()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._scheduled = False
        self._timer: asyncio.TimerHandle | None = None
        self._closed = False

    def enqueue(self, public_id: str, items: list[HttpInput]) -> None:
        loop = items[0].future.get_loop()
        group = ReadyGroup(public_id, items, loop, loop.time() + READY_WAIT_SECONDS)
        with self._lock:
            rejected = self._closed or (self._loop is not None and self._loop is not loop)
            if rejected:
                group.state = "done"
            else:
                self._loop = loop
                route = self._routes.setdefault(public_id, _Route())
                if not route.ready:
                    self._order.append(public_id)
                route.ready.append(group)
        if rejected:
            self._finish(group, self._closing(), False)
        else:
            self.wake()

    @staticmethod
    def _closing() -> HTTPException:
        return HTTPException(
            503, {"code": "ingress_unavailable", "message": "Reception is closing"}
        )

    @staticmethod
    def _busy() -> HTTPException:
        return HTTPException(
            429, {"code": "ingress_capacity_exceeded", "message": "Reception resources are busy"}
        )

    def wake(self) -> None:
        """Coalesce hints from any native completion thread into one loop callback."""
        with self._lock:
            loop = self._loop
            if self._closed or not self._order or self._scheduled or loop is None:
                return
            self._scheduled = True
        try:
            loop.call_soon_threadsafe(self._pump)
        except RuntimeError:
            # A closing loop must not turn a native release into a new failure.
            with self._lock:
                self._scheduled = False

    def _take(self) -> tuple[ReadyGroup | None, list[ReadyGroup]]:
        expired: list[ReadyGroup] = []
        with self._lock:
            if self._closed or self._loop is None:
                return None, expired
            now = self._loop.time()
            for _ in range(len(self._order)):
                key = self._order.popleft()
                route = self._routes[key]
                while route.ready and route.ready[0].deadline <= now:
                    group = route.ready.popleft()
                    group.state = "done"
                    expired.append(group)
                if route.ready:
                    self._order.append(key)
                elif route.active is None:
                    del self._routes[key]
            if expired:
                return None, expired
            for _ in range(len(self._order)):
                key = self._order.popleft()
                route = self._routes[key]
                if route.active is not None:
                    self._order.append(key)
                    continue
                if not self._acquire():
                    self._order.appendleft(key)
                    return None, expired
                group = route.ready.popleft()
                # Claim before submit. Close/expiry cannot release this group
                # while submit is in progress, even before a Future is returned.
                group.state = "submitting"
                route.active = group
                if route.ready:
                    self._order.append(key)
                return group, expired
        return None, expired

    def _pump(self) -> None:
        with self._lock:
            self._scheduled = False
        while True:
            group, expired = self._take()
            for old in expired:
                self._finish(old, self._busy(), False)
            if group is None:
                if expired:
                    continue
                break
            try:
                future = self._submit(group)
            except RuntimeError:
                self._settle(group, self._closing())
            else:
                with self._lock:
                    group.state = "running"
                # add_done_callback can invoke _settle synchronously here.
                future.add_done_callback(partial(self._settle, group))
        self._arm_timer()

    def _settle(
        self, group: ReadyGroup, result: Future[GroupResults] | HTTPException
    ) -> None:
        with self._lock:
            if group.state == "done":
                return
            group.state = "done"
            route = self._routes[group.public_id]
            route.active = None
            if not route.ready:
                del self._routes[group.public_id]
        self._finish(group, result, True)
        self.wake()

    def _arm_timer(self) -> None:
        with self._lock:
            if self._closed or self._loop is None or not self._order:
                return
            deadline = min(self._routes[key].ready[0].deadline for key in self._order)
            # Retain an earlier wake rather than cancel/rearm a timer for every
            # completed group. There is at most one live expiry timer per pool.
            if self._timer is not None:
                return
            self._timer = self._loop.call_at(deadline, self._deadline)

    def _deadline(self) -> None:
        with self._lock:
            self._timer = None
        self.wake()

    def close(self) -> None:
        rejected: list[ReadyGroup] = []
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for key, route in list(self._routes.items()):
                while route.ready:
                    group = route.ready.popleft()
                    group.state = "done"
                    rejected.append(group)
                if route.active is None:
                    del self._routes[key]
            self._order.clear()
            timer, self._timer = self._timer, None
            loop = self._loop
        if timer is not None and loop is not None and not loop.is_closed():
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(timer.cancel)
        for group in rejected:
            self._finish(group, self._closing(), False)
