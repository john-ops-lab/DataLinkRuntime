"""Bounded passive HTTP ingress. Blocking work has its own finite pool.

Cancellation stops waiting for a response, never the responsibility transaction.
The native worker owns its permit and byte reservation until it really finishes.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect

from dlr.control.security import AuthorizationHeader
from dlr.control.services import http_ingress as service
from dlr.control.services.adapter import domain_error
from dlr.control.services.http_ingress_group import (
    HttpCollector,
    HttpInput,
    complete,
    estimated_input_bytes,
)
from dlr.control.services.http_ingress_ready import GroupResults, HttpReadyQueue, ReadyGroup
from dlr.control.services.subscription_source import ApplicationBufferBudget

router = APIRouter()
BODY_RESERVATION = service.MAX_BODY_BYTES + service.MAX_ENVELOPE_BYTES


class NativeWorkBudget:
    """Nonblocking permits and their read-only snapshot share one lock."""

    def __init__(self, limit: int, released: Callable[[], None] | None = None) -> None:
        self._limit = limit
        self._released = released
        self._used = 0
        self._semaphore = threading.BoundedSemaphore(limit)
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        with self._lock:
            if not self._semaphore.acquire(blocking=False):
                return False
            self._used += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._semaphore.release()
            self._used -= 1
        if self._released is not None:
            self._released()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def saturated(self) -> bool:
        with self._lock:
            return self._used == self._limit


class IngressResources:
    def __init__(
        self,
        *,
        buffer: ApplicationBufferBudget,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self.requests = threading.BoundedSemaphore(32)
        self.work = NativeWorkBudget(8, self._wake_ready)
        self.buffer = buffer.scope(8 * 1024**2)
        self.executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dlr-ingress")
        if session_factory is None:
            from dlr.control.services.subscription_runtime import get_runtime

            self.session_factory: Callable[[], Session] = get_runtime().intake_session_factory
            self.engine = get_runtime().intake_session_factory.kw["bind"]
        else:
            self.session_factory = session_factory
            self.engine = None
        self.collector = collector = HttpCollector(self.dispatch)

        def prepare(public_id: str, remaining_items: int, remaining_bytes: int) -> None:
            if self.ready is not ready or self.collector is not collector:
                return
            loop = asyncio.get_running_loop()
            if (
                ready._closed
                or collector.closed
                or ready._loop is not loop
                or collector._loop is not loop
                or self.work.saturated
            ):
                return
            pending = collector.pending.get(public_id)
            if (
                pending is None
                or not pending.items
                or pending.deadline <= loop.time()
                or remaining_items < 1
                or estimated_input_bytes(len(pending.items[0].body)) > remaining_bytes
            ):
                return
            collector.flush(public_id)

        self.ready = ready = HttpReadyQueue(
            self.work.acquire, self._submit_group, self._finish_group, prepare=prepare
        )

    def _wake_ready(self) -> None:
        self.ready.wake()

    def begin_close(self) -> None:
        self.ready.close()
        self.collector.close()

    def close(self) -> None:
        self.begin_close()
        self.executor.shutdown(wait=True, cancel_futures=False)

    def dispatch(self, public_id: str, items: list[HttpInput]) -> None:
        self.ready.enqueue(public_id, items)

    def _submit_group(self, group: ReadyGroup) -> Future[GroupResults]:
        return self.executor.submit(_run_group_sync, self, group.public_id, group.items)

    def _finish_group(
        self, group: ReadyGroup, result: Future[GroupResults] | HTTPException, claimed: bool
    ) -> None:
        try:
            if isinstance(result, HTTPException):
                results: GroupResults = [result for _ in group.items]
            else:
                try:
                    results = result.result()
                except Exception:
                    results = [
                        domain_error(
                            503,
                            "ingress_unavailable",
                            "Reception is temporarily unavailable; "
                            "retry with the same ID and bytes",
                        )
                        for _ in group.items
                    ]
        finally:
            if claimed:
                self.work.release()
            for item in group.items:
                item.body = b""
                item.release()
        for item, answer in zip(group.items, results, strict=True):
            if not group.loop.is_closed():
                with suppress(RuntimeError):
                    group.loop.call_soon_threadsafe(complete, item, answer)


_resources: IngressResources | None = None
_resources_lock = threading.Lock()


def admission_capacity_busy() -> bool:
    """Conservative admission hint, never a reservation or a pool initializer.

    The original request path still performs the authoritative acquire. Each
    snapshot can become stale after a native job completes; neither reading
    nor refusing a new request changes the lifetime of that job's reservation.
    """
    current = _resources
    if current is None:
        return False
    buffer = current.buffer
    return (
        current.work.saturated
        or buffer.used_bytes + BODY_RESERVATION > buffer.limit_bytes
        or buffer.parent.used_bytes + BODY_RESERVATION > buffer.parent.limit_bytes
    )


def resources() -> IngressResources:
    global _resources
    with _resources_lock:
        if _resources is None:
            from dlr.control.services.subscription_runtime import get_runtime

            _resources = IngressResources(buffer=get_runtime().buffer)
        return _resources


def shutdown_ingress_resources() -> None:
    global _resources
    with _resources_lock:
        current = _resources
        _resources = None
    if current is not None:
        current.close()


def flush_ingress_resources() -> None:
    if _resources is not None:
        _resources.begin_close()


def _busy() -> None:
    error = domain_error(429, "ingress_capacity_exceeded", "Reception resources are busy")
    error.headers = {"Retry-After": "1"}
    raise error


def _require_media(request: Request) -> None:
    if request.headers.get("content-encoding", "identity").lower().strip() != "identity":
        raise domain_error(
            415, "ingress_encoding_unsupported", "Compressed request bodies are not supported"
        )
    content_type = request.headers.get("content-type", "").lower()
    parts = [item.strip() for item in content_type.split(";")]
    if parts[0] != "application/json" or any(
        value not in {"charset=utf-8", 'charset="utf-8"'} for value in parts[1:]
    ):
        raise domain_error(415, "ingress_media_unsupported", "Use application/json with UTF-8")
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal():
            raise domain_error(422, "ingress_length_invalid", "Invalid Content-Length")
        normalized = length.lstrip("0") or "0"
        limit = str(service.MAX_BODY_BYTES)
        if len(normalized) > len(limit) or (len(normalized) == len(limit) and normalized > limit):
            raise domain_error(413, "ingress_body_too_large", "JSON body exceeds 64 KiB")


async def _read_body(request: Request) -> bytes:
    chunks: list[bytes] = []
    total = 0
    stream = request.stream().__aiter__()
    try:
        async with asyncio.timeout(5):
            while True:
                try:
                    chunk = await asyncio.wait_for(anext(stream), timeout=2)
                except StopAsyncIteration:
                    break
                total += len(chunk)
                if total > service.MAX_BODY_BYTES:
                    raise domain_error(413, "ingress_body_too_large", "JSON body exceeds 64 KiB")
                chunks.append(chunk)
    except TimeoutError:
        raise domain_error(
            408, "ingress_read_timeout", "Request body exceeded the read deadline"
        ) from None
    except ClientDisconnect:
        raise domain_error(499, "ingress_disconnected", "The sender disconnected") from None
    return b"".join(chunks)


def _notify_committed_delivery_work() -> None:
    """Wake only after a known new commit; the durable fallback remains authority."""
    try:
        from dlr.control.services.subscription_scheduler import notify_delivery_work

        notify_delivery_work()
    except Exception:
        # A best-effort hint cannot turn committed custody into a failed or
        # unknown response. The existing <=1s durable scan still finds it.
        pass


def _run_sync(
    pool: IngressResources,
    public_id: str,
    authorization: str | None,
    body: bytes | None,
    key: str | None,
    receipt_id: int | None,
) -> tuple[int, dict[str, Any]]:
    with pool.session_factory() as session:
        try:
            if receipt_id is not None:
                return 200, service.receipt_status(session, public_id, authorization, receipt_id)
            assert body is not None
            status, result = service.receive(session, public_id, authorization, key, body)
            session.commit()
            if status == 202:
                _notify_committed_delivery_work()
            return status, result
        except SQLAlchemyError:
            # Commit failure can be ambiguous. The caller must retain the same
            # ID and original bytes, then retry or query the durable identity.
            session.rollback()
            raise domain_error(
                503,
                "ingress_unavailable",
                "Reception is temporarily unavailable; retry with the same ID and bytes",
            ) from None


def _run_group_sync(
    pool: IngressResources, public_id: str, items: list[HttpInput]
) -> list[tuple[int, dict[str, Any]] | HTTPException]:
    with pool.session_factory() as session:
        try:
            result = service.receive_group(
                session,
                public_id,
                [service.HttpReception(item.authorization, item.key, item.body) for item in items],
            )
            if result is not None:
                session.commit()
                if any(not isinstance(item, HTTPException) and item[0] == 202 for item in result):
                    _notify_committed_delivery_work()
                return [_response_result(item) for item in result]
            session.rollback()
        except SQLAlchemyError:
            session.rollback()
            raise domain_error(
                503,
                "ingress_unavailable",
                "Reception is temporarily unavailable; retry with the same ID and bytes",
            ) from None
        except HTTPException as error:
            session.rollback()
            code = error.detail.get("code") if isinstance(error.detail, dict) else None
            if code in {
                "subscription_owner_stale",
                "subscription_config_conflict",
                "subscription_activation_missing",
                "source_not_available",
            }:
                error = domain_error(
                    503, "ingress_unavailable", "Reception configuration is not ready"
                )
            return [_response_result(error) for _ in items]
    # A stale hint cannot change per-event transaction/response semantics.
    results: list[tuple[int, dict[str, Any]] | HTTPException] = []
    for item in items:
        try:
            results.append(
                _run_sync(pool, public_id, item.authorization, item.body, item.key, None)
            )
        except HTTPException as error:
            results.append(_response_result(error))
    return results


def _response_result(
    result: tuple[int, dict[str, Any]] | HTTPException,
) -> tuple[int, dict[str, Any]] | HTTPException:
    # Do not retain a native validation traceback (and its parsed bodies) in
    # a client future after the corresponding body reservation is released.
    if isinstance(result, HTTPException):
        return HTTPException(result.status_code, result.detail, result.headers)
    return result


async def _request(
    public_id: str,
    request: Request,
    authorization: str | None,
    key: str | None = None,
    receipt_id: int | None = None,
) -> JSONResponse:
    pool = resources()
    if not pool.requests.acquire(blocking=False):
        _busy()
    reservation = BODY_RESERVATION if receipt_id is None else 0
    reserved = False
    work_acquired = False
    handed_off = False
    try:
        if not pool.buffer.reserve(reservation):
            _busy()
        reserved = True
        body = None
        if receipt_id is None:
            _require_media(request)
            service.require_key(key)
            body = await _read_body(request)
            from dlr.control.services.subscription_runtime import get_runtime

            if get_runtime().http_batch_hint(public_id):
                unused_raw = service.MAX_BODY_BYTES - len(body)
                if unused_raw:
                    pool.buffer.release(unused_raw)
                    reservation -= unused_raw
                loop = asyncio.get_running_loop()
                result_future: asyncio.Future[tuple[int, dict[str, Any]]] = loop.create_future()
                result_future.add_done_callback(
                    lambda done: None if done.cancelled() else done.exception()
                )

                def release_request() -> None:
                    pool.buffer.release(reservation)
                    pool.requests.release()

                pool.collector.add(
                    public_id, HttpInput(authorization, key, body, release_request, result_future)
                )
                body = None
                handed_off = True
                status, result = await asyncio.shield(result_future)
                return JSONResponse(result, status_code=status)
        if not pool.work.acquire():
            _busy()
        work_acquired = True
        try:
            future = pool.executor.submit(
                _run_sync, pool, public_id, authorization, body, key, receipt_id
            )
        except RuntimeError:
            raise domain_error(503, "ingress_unavailable", "Reception is closing") from None

        def release(_: Future[tuple[int, dict[str, Any]]]) -> None:
            pool.work.release()
            pool.buffer.release(reservation)
            pool.requests.release()

        future.add_done_callback(release)
        handed_off = True
        wrapped = asyncio.wrap_future(future)
        # Retrieving an eventual failure also avoids an unobserved task warning
        # when the HTTP client cancels while the native DB transaction finishes.
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        status, result = await asyncio.shield(wrapped)
        return JSONResponse(result, status_code=status)
    except HTTPException as error:
        if error.status_code in {429, 503}:
            error.headers = {**(error.headers or {}), "Retry-After": "1"}
        raise
    finally:
        if not handed_off:
            if work_acquired:
                pool.work.release()
            if reserved:
                pool.buffer.release(reservation)
            pool.requests.release()


@router.post("/api/ingress/{public_id}")
async def receive_ingress(
    public_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    return await _request(public_id, request, authorization, idempotency_key)


@router.get("/api/ingress/{public_id}/receipts/{receipt_id}")
async def get_ingress_receipt(
    public_id: str,
    receipt_id: int,
    request: Request,
    authorization: AuthorizationHeader = None,
) -> JSONResponse:
    return await _request(public_id, request, authorization, receipt_id=receipt_id)
