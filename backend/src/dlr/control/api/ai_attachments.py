"""Edit-authorized, page-scoped ephemeral attachments, independent of Managed Input."""

import asyncio
import contextlib
import threading
import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control import db
from dlr.control.ai.attachment_documents import DocumentConversionError
from dlr.control.ai.attachment_store import (
    AiAttachmentStore,
    get_attachment_store,
    principal_owner,
)
from dlr.control.schemas.ai import (
    AiAttachmentCapabilitiesResponse,
    AiAttachmentPrepareRequest,
    AiPreparedAttachment,
)
from dlr.control.security import Principal, require_business_principal, require_principal
from dlr.control.services import adapter_access
from dlr.control.services import ai as ai_service

router = APIRouter(dependencies=[Depends(require_business_principal)])
DbSession = Annotated[Session, Depends(db.get_session)]
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]
Store = Annotated[AiAttachmentStore, Depends(get_attachment_store)]


@router.get(
    "/api/adapters/{adapter_id}/ai/attachment-capabilities",
    response_model=AiAttachmentCapabilitiesResponse,
)
def capabilities(
    adapter_id: int,
    principal: CurrentPrincipal,
    session: DbSession,
) -> AiAttachmentCapabilitiesResponse:
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    return ai_service.attachment_capabilities(session)


@router.post("/api/adapters/{adapter_id}/ai/attachments", response_model=AiPreparedAttachment)
async def prepare(
    adapter_id: int,
    payload: AiAttachmentPrepareRequest,
    request: Request,
    principal: CurrentPrincipal,
    session: DbSession,
    store: Store,
) -> AiPreparedAttachment:
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    cancel = threading.Event()
    operation = asyncio.create_task(
        asyncio.to_thread(
            store.prepare,
            principal_owner(principal),
            adapter_id,
            payload.scope_id,
            payload.attachment,
            deadline=time.monotonic() + settings.ai_attachment_prepare_timeout_seconds,
            cancel_event=cancel,
        )
    )
    try:
        while True:
            done, _ = await asyncio.wait({operation}, timeout=0.1)
            if done:
                return operation.result()
            if await request.is_disconnected():
                cancel.set()
                return await operation
    except asyncio.CancelledError:
        cancel.set()
        # The worker owns cleanup; wait for its cancellation acknowledgement.
        with contextlib.suppress(DocumentConversionError):
            await asyncio.shield(operation)
        raise


@router.delete("/api/adapters/{adapter_id}/ai/attachments/scopes/{scope_id}", status_code=204)
def release_scope(
    adapter_id: int,
    scope_id: UUID,
    principal: CurrentPrincipal,
    session: DbSession,
    store: Store,
) -> None:
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    store.release(principal_owner(principal), adapter_id, scope_id)


@router.get(
    "/api/adapters/{adapter_id}/ai/attachments/{attachment_ref}",
    response_model=AiPreparedAttachment,
)
def get_prepared(
    adapter_id: int,
    attachment_ref: str,
    scope_id: UUID,
    principal: CurrentPrincipal,
    session: DbSession,
    store: Store,
) -> AiPreparedAttachment:
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    return store.get(principal_owner(principal), adapter_id, scope_id, attachment_ref)


@router.delete("/api/adapters/{adapter_id}/ai/attachments/{attachment_ref}", status_code=204)
def release_attachment(
    adapter_id: int,
    attachment_ref: str,
    scope_id: UUID,
    principal: CurrentPrincipal,
    session: DbSession,
    store: Store,
) -> None:
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    store.release(principal_owner(principal), adapter_id, scope_id, attachment_ref)
