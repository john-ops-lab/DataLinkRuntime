"""Bounded physical work survives request-loop cancellation with an explicit owner."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TypeVar

import httpx2
from dlr.control.ai import providers

from guards import Rejected, strict_json

T = TypeVar("T")


class WorkOwner:
    """Own concurrent futures, independent of any request's asyncio event loop."""

    def __init__(self, workers: int):
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dlr-owned")
        self.slots = threading.BoundedSemaphore(workers)

    def submit(self, call: Callable[[], T]) -> Future[T]:
        if not self.slots.acquire(blocking=False):
            raise Rejected("work_capacity", "full")

        def execute() -> T:
            try:
                return call()
            finally:
                self.slots.release()

        try:
            return self.pool.submit(execute)
        except BaseException:
            self.slots.release()
            raise

    async def result(self, future: Future[T]) -> T:
        # asyncio.run() can close immediately: no default-executor DNS job or
        # task owns the physical work. Cancelling the request never cancels it.
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda value: None if value.cancelled() else value.exception())
        return await asyncio.shield(wrapped)

    def close(self) -> None:
        self.pool.shutdown(wait=True)


NETWORK_WORK = WorkOwner(40)


class OwnedTransport(httpx2.AsyncBaseTransport):
    """Use the public sync HTTP transport inside bounded, independently owned work.

    DNS, socket I/O and bounded body reading share one physical lifetime. Closing
    an async SDK client only requests closure; a live worker closes its transport
    and returns the shared permit after the actual I/O finishes.
    """

    def __init__(
        self,
        *,
        permits: threading.BoundedSemaphore | None = None,
        owner: WorkOwner = NETWORK_WORK,
        capacity_timeout: float = 2,
        request_limit: int = 32 << 20,
        response_limit: int = 4 << 20,
    ):
        self.inner = httpx2.HTTPTransport(retries=0, trust_env=False)
        self.permits = permits if permits is not None else providers._TRANSPORT_PERMITS
        self.owner = owner
        self.capacity_timeout = capacity_timeout
        self.request_limit = request_limit
        self.response_limit = response_limit
        self.lock = threading.Lock()
        self.active = 0
        self.closing = False
        self.closed = threading.Event()

    def finish(self) -> None:
        with self.lock:
            self.active -= 1
            if self.closing and self.active == 0:
                self.inner.close()
                self.closed.set()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        body = await request.aread()
        if len(body) > self.request_limit:
            raise Rejected("http_request", "body_limit")
        deadline = time.monotonic() + self.capacity_timeout
        while not self.permits.acquire(blocking=False):
            if time.monotonic() >= deadline:
                raise Rejected("capacity", "timeout")
            await asyncio.sleep(0.005)
        with self.lock:
            if self.closing:
                self.permits.release()
                raise Rejected("transport", "closed")
            self.active += 1
        physical_request = httpx2.Request(
            request.method,
            request.url,
            headers=request.headers,
            content=body,
            extensions=request.extensions,
        )

        def read() -> httpx2.Response:
            try:
                response = self.inner.handle_request(physical_request)
                try:
                    raw = bytearray()
                    for chunk in response.iter_raw():
                        if len(raw) + len(chunk) > self.response_limit:
                            raise Rejected("http_envelope", "body_limit")
                        raw.extend(chunk)
                    # Preserve HTTP status precedence for non-JSON gateway
                    # errors. Successful envelopes are inspected before SDK parsing.
                    if 200 <= response.status_code < 300:
                        strict_json(bytes(raw), "http_envelope")
                    return httpx2.Response(
                        response.status_code,
                        headers=response.headers,
                        content=bytes(raw),
                        request=request,
                    )
                finally:
                    response.close()
            finally:
                try:
                    self.finish()
                finally:
                    self.permits.release()

        try:
            future = self.owner.submit(read)
        except BaseException:
            self.finish()
            self.permits.release()
            raise
        return await self.owner.result(future)

    async def aclose(self) -> None:
        with self.lock:
            self.closing = True
            if self.active == 0 and not self.closed.is_set():
                self.inner.close()
                self.closed.set()
