"""Bounded passive HTTP ingress. Blocking work has its own finite pool.

Cancellation stops waiting for a response, never the responsibility transaction.
The native worker owns its permit and byte reservation until it really finishes.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import ClientDisconnect

from dlr.common.config import settings
from dlr.control.security import AuthorizationHeader
from dlr.control.services import http_ingress as service
from dlr.control.services.adapter import domain_error
from dlr.control.services.subscription_source import ApplicationBufferBudget

router = APIRouter()
BODY_RESERVATION = service.MAX_BODY_BYTES + service.MAX_ENVELOPE_BYTES


class IngressResources:
    def __init__(
        self,
        *,
        buffer: ApplicationBufferBudget,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self.requests = threading.BoundedSemaphore(32)
        self.work = threading.BoundedSemaphore(8)
        self.buffer = buffer.scope(8 * 1024**2)
        self.executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dlr-ingress")
        self.engine: Engine | None = None
        if session_factory is None:
            self.engine = create_engine(
                settings.database_url,
                pool_size=8,
                max_overflow=0,
                pool_timeout=1,
                pool_pre_ping=True,
                connect_args={
                    "connect_timeout": 5,
                    "options": "-c statement_timeout=5000 -c lock_timeout=1000",
                    "keepalives": 1,
                    "keepalives_idle": 2,
                    "keepalives_interval": 1,
                    "keepalives_count": 3,
                    "tcp_user_timeout": 5000,
                },
            )
            self.session_factory: Callable[[], Session] = sessionmaker(
                bind=self.engine, expire_on_commit=False
            )
        else:
            self.session_factory = session_factory

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)
        if self.engine:
            self.engine.dispose()


_resources: IngressResources | None = None
_resources_lock = threading.Lock()


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
        if int(length) > service.MAX_BODY_BYTES:
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
        if not pool.work.acquire(blocking=False):
            _busy()
        work_acquired = True
        future = pool.executor.submit(
            _run_sync, pool, public_id, authorization, body, key, receipt_id
        )

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
