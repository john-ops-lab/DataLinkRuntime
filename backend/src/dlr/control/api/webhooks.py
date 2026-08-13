"""Adapter Webhook endpoints of the Control Node (M5.3).

Two routers with different authentication models:

- ``router`` (admin token): the singleton Webhook configuration API
  (GET/PUT /api/adapters/{adapter_id}/webhook).
- ``public_router`` (no admin token): the external ingress
  POST /api/hooks/{public_id}, authenticated only by its own Bearer token
  (the referenced token Credential).

The ingress reads the request body with a hard byte cap so an oversized
external request can never occupy unbounded memory; the service layer
re-validates the same limit.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control import db
from dlr.control.schemas.webhook import WebhookResponse, WebhookUpsert
from dlr.control.security import AuthorizationHeader, require_admin_token
from dlr.control.services import webhook as webhook_service
from dlr.control.services.adapter import domain_error

router = APIRouter(dependencies=[Depends(require_admin_token)])
public_router = APIRouter()

DbSession = Annotated[Session, Depends(db.get_session)]


@router.get("/api/adapters/{adapter_id}/webhook", response_model=WebhookResponse)
def get_webhook(adapter_id: int, session: DbSession) -> WebhookResponse:
    """Return the Adapter's Webhook; 404 ``webhook_not_configured`` if absent.

    Never returns Credential plaintext or ciphertext.
    """
    return webhook_service.get_webhook(session, adapter_id)


@router.put("/api/adapters/{adapter_id}/webhook", response_model=WebhookResponse)
def put_webhook(adapter_id: int, payload: WebhookUpsert, session: DbSession) -> WebhookResponse:
    """Create or update the Adapter's Webhook.

    The Credential must exist and be of type ``token``. The ``public_id``
    is server-generated on first creation and stays stable afterwards.
    """
    return webhook_service.upsert_webhook(session, adapter_id, payload)


async def _read_capped_body(request: Request) -> bytes:
    """Read the request body with a hard cap at the Execution input limit.

    The minimal memory protection for an untrusted external ingress: an
    oversized body is rejected with 413 as soon as the cap is crossed,
    before parsing and before creating anything.
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


@public_router.post("/api/hooks/{public_id}", status_code=202)
async def receive_hook(
    public_id: str,
    request: Request,
    session: DbSession,
    authorization: AuthorizationHeader = None,
) -> dict[str, object]:
    """External Webhook ingress: validate, create a pending Execution, 202.

    Control never waits for the Execution; the response only carries the
    created execution id. Unknown public_id -> 404; disabled -> 409
    ``webhook_disabled``; wrong/missing token -> 401; gate failures ->
    the stable 409/413/400 family.
    """
    body = await _read_capped_body(request)
    execution = webhook_service.receive_webhook(session, public_id, authorization, body)
    return {"execution_id": execution.id, "status": "accepted"}
