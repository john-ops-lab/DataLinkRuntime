"""Final Webhook Adapter endpoints of the Control Node (M5.4.3).

Two routers with different authentication models:

- ``router`` (admin token): the singleton Webhook configuration API
  (GET/PUT /api/adapters/{adapter_id}/webhook).
- ``public_router`` (no admin token): the external ingress
  POST /api/hooks/{public_id}, authenticated only by its own Bearer token
  (the referenced token Credential).

The ingress reads the request body with a hard byte cap so an oversized
external request can never occupy unbounded memory; the service layer
re-validates the same limit, the compact JSON Execution input cap and
the JSONB-persistable string boundary.

Concurrency contract: the async route streams the body and observes results;
blocking database transactions run via ``asyncio.to_thread`` on its own session
(created and closed inside the same worker thread), so lock waits and
commits never block the Control event loop. No async ORM is introduced.
"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control import db
from dlr.control.models import Execution
from dlr.control.schemas.webhook import WebhookResponse, WebhookUpsert
from dlr.control.security import (
    AuthorizationHeader,
    Principal,
    require_business_principal,
    require_principal,
)
from dlr.control.services import adapter_access
from dlr.control.services import webhook as webhook_service
from dlr.control.services.adapter import domain_error
from dlr.control.services.execution import TERMINAL_STATUSES

router = APIRouter(dependencies=[Depends(require_business_principal)])
public_router = APIRouter()

DbSession = Annotated[Session, Depends(db.get_session)]
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


@router.get("/api/adapters/{adapter_id}/webhook", response_model=WebhookResponse)
def get_webhook(
    adapter_id: int, principal: CurrentPrincipal, session: DbSession
) -> WebhookResponse:
    """Return the Adapter's Webhook; 404 ``webhook_not_configured`` if absent.

    Never returns Credential plaintext or ciphertext.
    """
    adapter_access.require_adapter_access(session, adapter_id, principal, "read")
    return webhook_service.get_webhook(session, adapter_id)


@router.put("/api/adapters/{adapter_id}/webhook", response_model=WebhookResponse)
def put_webhook(
    adapter_id: int,
    payload: WebhookUpsert,
    principal: CurrentPrincipal,
    session: DbSession,
) -> WebhookResponse:
    """Replace stopped settings or start/stop Webhook receiving.

    ``public_id`` is editable while stopped. A referenced Credential must
    exist and be type ``token``; starting also enforces runtime readiness.
    """
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    return webhook_service.upsert_webhook(session, adapter_id, payload)


async def _read_capped_body(request: Request) -> bytes:
    """Read the request body with a hard cap at the Execution input limit.

    The minimal memory protection for an untrusted external ingress: an
    oversized body is rejected with 413 as soon as the cap is crossed,
    before parsing and before creating anything. This runs before routing
    and authentication on purpose: the ingress must never read an
    unbounded body, even from unknown or unauthorized callers.
    """
    max_bytes = settings.execution_input_max_bytes
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise domain_error(
                413,
                "execution_input_too_large",
                f"Input exceeds the {max_bytes} byte limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _receive_hook_sync(
    public_id: str,
    authorization: str | None,
    body: bytes,
    idempotency_key: str | None = None,
) -> tuple[int, str, int]:
    """The blocking DB transaction of one Webhook receipt.

    Runs on a worker thread via ``asyncio.to_thread`` (same pattern as the
    scheduler's ``_tick_once``): the session is created and closed inside
    the same thread, so ``FOR UPDATE`` waits, decryption and commits
    never touch the Control event loop.
    """
    # Local import like schedule._tick_once: tests point SessionLocal at the test DB.
    from dlr.control.db import SessionLocal

    session = SessionLocal()
    try:
        execution = webhook_service.receive_webhook(
            session,
            public_id,
            authorization,
            body,
            idempotency_key=idempotency_key,
        )
        policy = execution.webhook_response_snapshot or {}
        return (
            execution.id,
            str(policy.get("response_mode", "accepted")),
            int(str(policy.get("response_timeout_seconds", 30))),
        )
    finally:
        session.close()


def _read_hook_result_sync(execution_id: int) -> tuple[int, dict[str, object]] | None:
    """Read only public, already-redacted result fields in a short transaction.

    No session, row lock or connection survives between polling intervals.
    Retry-wait and failed Attempts are not logical Execution completion.
    """
    with db.SessionLocal() as session:
        row = session.execute(
            select(
                Execution.status,
                Execution.output,
                Execution.error,
                Execution.error_code,
                Execution.last_error_code,
                Execution.output_truncated,
            ).where(Execution.id == execution_id)
        ).one_or_none()
        if row is None:
            return 410, {
                "execution_id": execution_id,
                "status": "unavailable",
                "error": {
                    "code": "execution_result_unavailable",
                    "message": "Execution result is no longer available",
                },
            }
        if row.status not in TERMINAL_STATUSES:
            return None
        body: dict[str, object] = {"execution_id": execution_id, "status": row.status}
        if row.status == "succeeded" and not row.output_truncated:
            return 200, body | {"output": row.output}
        if row.output_truncated:
            return 502, body | {
                "error": {
                    "code": "output_too_large",
                    "message": "Execution output exceeds the stored result limit",
                }
            }
        code = row.error_code or row.last_error_code or f"execution_{row.status}"
        status_code = 409 if row.status == "cancelled" else 422
        if row.status == "expired" or code == "execution_timeout":
            status_code = 504
        return status_code, body | {
            "error": {
                "code": code,
                "message": row.error or f"Execution {row.status}",
            }
        }


async def _wait_hook_result(
    execution_id: int, timeout_seconds: int, request: Request
) -> JSONResponse:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        result = await asyncio.to_thread(_read_hook_result_sync, execution_id)
        if result is not None:
            status_code, body = result
            return JSONResponse(body, status_code=status_code)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return JSONResponse(
                {
                    "execution_id": execution_id,
                    "status": "waiting_timeout",
                    "error": {
                        "code": "webhook_response_timeout",
                        "message": (
                            "Response wait timed out; execution continues. Retry with the same "
                            "Idempotency-Key and body to wait for the same execution."
                        ),
                    },
                },
                status_code=504,
            )
        if await request.is_disconnected():
            # Stop observation only; the committed Execution remains durable.
            return JSONResponse(
                {"execution_id": execution_id, "status": "disconnected"}, status_code=499
            )
        await asyncio.sleep(min(0.25, remaining))


@public_router.post(
    "/api/hooks/{public_id}",
    status_code=202,
    responses={
        200: {"description": "Execution succeeded; output contains the Adapter JSON result"},
        409: {"description": "Ingress conflict or execution cancelled"},
        410: {"description": "Execution result no longer retained"},
        422: {"description": "Invalid input or execution exhausted its retries"},
        502: {"description": "Execution output exceeds the result limit"},
        504: {
            "description": "Response wait timeout or execution timeout/expiry; inspect error.code"
        },
    },
)
async def receive_hook(
    public_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    """Receive durably, then apply the response policy captured at acceptance.

    Accepted mode preserves HTTP 202. Completed mode observes the logical
    Execution, including queued and retry-wait time, without cancelling it
    on timeout or disconnect. Existing ingress authentication and gates apply
    before any result can be observed, including an idempotent replay.
    """
    body = await _read_capped_body(request)
    execution_id, response_mode, timeout_seconds = await asyncio.to_thread(
        _receive_hook_sync, public_id, authorization, body, idempotency_key
    )
    if response_mode == "completed":
        return await _wait_hook_result(execution_id, timeout_seconds, request)
    return JSONResponse({"execution_id": execution_id, "status": "accepted"}, status_code=202)
