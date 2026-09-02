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

Concurrency contract: the async route only streams the body; the blocking
database transaction runs via ``asyncio.to_thread`` on its own session
(created and closed inside the same worker thread), so lock waits and
commits never block the Control event loop. No async ORM is introduced.
"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control import db
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
) -> int:
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
        return execution.id
    finally:
        session.close()


@public_router.post("/api/hooks/{public_id}", status_code=202)
async def receive_hook(
    public_id: str,
    request: Request,
    authorization: AuthorizationHeader = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    """External Webhook ingress: validate, create a pending Execution, 202.

    Control never waits for the Execution; the response only carries the
    created execution id. Real precedence: the raw body stream cap runs
    first (413 even for unknown/unauthorized callers); then unknown
    public_id/disabled -> 404; wrong/missing token -> 401; gate failures ->
    the stable 409 family; body contract ->
    400 (invalid/non-standard JSON) and 413 (compact JSON over the cap).
    """
    body = await _read_capped_body(request)
    execution_id = await asyncio.to_thread(
        _receive_hook_sync, public_id, authorization, body, idempotency_key
    )
    return {"execution_id": execution_id, "status": "accepted"}
