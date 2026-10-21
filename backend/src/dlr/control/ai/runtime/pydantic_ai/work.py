"""Bounded physical work owned independently of request event loops."""

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, TypeVar

from dlr.control.ai.provider_config import AiProviderError

T = TypeVar("T")


class WorkOwner:
    def __init__(self, capacity: int, name: str) -> None:
        self.slots = threading.BoundedSemaphore(capacity)
        self.name = name
        self.lock = threading.Lock()
        self.active: set[Future[Any]] = set()

    def submit(self, call: Callable[[], T]) -> Future[T]:
        if not self.slots.acquire(blocking=False):
            raise AiProviderError("ai_timeout")
        future: Future[T] = Future()

        def execute() -> None:
            try:
                future.set_result(call())
            except BaseException as error:
                future.set_exception(error)
            finally:
                with self.lock:
                    self.active.discard(future)
                self.slots.release()

        with self.lock:
            self.active.add(future)
        try:
            # Daemon threads have an explicit future owner, no default asyncio
            # executor and no unbounded atexit join of a stalled system DNS call.
            threading.Thread(target=execute, name=self.name, daemon=True).start()
        except BaseException:
            with self.lock:
                self.active.discard(future)
            self.slots.release()
            raise
        return future

    async def result(self, future: Future[T]) -> T:
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda value: None if value.cancelled() else value.exception())
        return await asyncio.shield(wrapped)


NETWORK_WORK = WorkOwner(40, "dlr-ai-sdk-http")
TOOL_WORK = WorkOwner(40, "dlr-ai-tool")
