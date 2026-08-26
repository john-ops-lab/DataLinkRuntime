"""Managed Input policy and B1 staged-upload endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control import db
from dlr.control.schemas.managed_input import (
    ManagedInputArtifactResponse,
    ManagedInputSettingsResponse,
    ManagedInputSettingsUpdate,
    ManagedInputUploadSessionResponse,
)
from dlr.control.security import (
    Principal,
    require_admin_principal,
    require_business_principal,
    require_principal,
)
from dlr.control.services import adapter_access, managed_input_upload
from dlr.control.services import managed_input as managed_input_service
from dlr.control.services.artifact_store import ArtifactStoreError, LocalFileArtifactStore
from dlr.control.services.managed_input_upload import UploadSessionState
from dlr.control.services.multipart import MultipartParseError, MultipartReader

logger = logging.getLogger("dlr.control.managed_input")

router = APIRouter(dependencies=[Depends(require_admin_principal)])
adapter_router = APIRouter(dependencies=[Depends(require_business_principal)])

DbSession = Annotated[Session, Depends(db.get_session)]
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


@router.get(
    "/api/system/managed-input-settings",
    response_model=ManagedInputSettingsResponse,
)
def get_managed_input_settings(session: DbSession) -> ManagedInputSettingsResponse:
    """Return policy, capacity usage, and observable quota state."""
    return managed_input_service.settings_response(session)


@router.put(
    "/api/system/managed-input-settings",
    response_model=ManagedInputSettingsResponse,
)
def put_managed_input_settings(
    payload: ManagedInputSettingsUpdate,
    session: DbSession,
) -> ManagedInputSettingsResponse:
    """Replace the database policy without changing stored artifacts."""
    return managed_input_service.update_settings(session, payload)


def _safe_upload_error(code: str, status_code: int) -> HTTPException:
    messages = {
        "input_upload_interrupted": "Input upload was interrupted",
        "input_upload_failed": "Input upload failed",
        "artifact_store_unavailable": "Artifact storage is unavailable",
    }
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": messages.get(code, "Input upload failed")},
    )


def _compensate_upload(
    session: Session,
    state: UploadSessionState | None,
    store: LocalFileArtifactStore | None,
    *,
    error_code: str,
) -> None:
    """Best-effort DB and filesystem compensation without sensitive logging."""
    if state is None:
        return
    try:
        session.rollback()
        managed_input_upload.abort_upload(
            session,
            state.adapter_id,
            state.upload_session_id,
            error_code=error_code,
            store=store,
        )
    except Exception:
        session.rollback()
        if store is not None:
            try:
                store.delete_part(state.storage_key)
                store.delete(state.storage_key)
            except (ArtifactStoreError, OSError):
                logger.warning("managed input upload compensation deferred")


async def _stream_upload(
    request: Request,
    session: DbSession,
    adapter_id: int,
    principal: CurrentPrincipal,
) -> ManagedInputArtifactResponse:
    """Parse and persist one multipart file without buffering its contents."""
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    managed_input_upload.require_feature_enabled()
    reader: MultipartReader
    state: UploadSessionState | None = None
    store: LocalFileArtifactStore | None = None
    finalized = False
    file_seen = False
    total_bytes = 0
    digest = hashlib.sha256()
    try:
        reader = MultipartReader(request.stream(), request.headers.get("content-type"))
        while True:
            part = await reader.next_part()
            if part is None:
                break
            if part.filename is None:
                async for _ in reader.iter_part_body():
                    pass
                continue
            if file_seen:
                async for _ in reader.iter_part_body():
                    pass
                raise _safe_upload_error("input_invalid", 422)
            file_seen = True
            managed_input_upload.validate_original_filename(part.filename)
            # The feature flag and metadata checks happen before construction;
            # a disabled or rejected upload must not create a store root.
            store = LocalFileArtifactStore(settings.artifact_store_root)
            state = managed_input_upload.begin_upload(
                session,
                adapter_id,
                original_filename=part.filename,
                content_type=part.content_type,
                created_by_user_id=principal.user_id,
                store=store,
            )
            with store.put_part(state.storage_key) as handle:
                async for chunk in reader.iter_part_body():
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    state = managed_input_upload.expand_upload_reservation(
                        session,
                        adapter_id,
                        state.upload_session_id,
                        total_bytes,
                        store=store,
                    )
                    policy = managed_input_service.get_settings(session)
                    managed_input_upload.check_stream_low_watermark(store, policy, len(chunk))
                    handle.write(chunk)
                    digest.update(chunk)

        await reader.ensure_complete()
        if not file_seen or state is None or store is None:
            raise _safe_upload_error("input_invalid", 422)
        store.commit(state.storage_key)
        artifact = managed_input_upload.consume_upload_reservation(
            session,
            adapter_id,
            state.upload_session_id,
            actual_size_bytes=total_bytes,
            sha256=digest.hexdigest(),
            store=store,
        )
        finalized = True
        return managed_input_upload.artifact_response(artifact)
    except BaseException as exc:
        error_code = "input_upload_failed"
        if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
            candidate = exc.detail.get("code")
            if isinstance(candidate, str):
                error_code = candidate
        elif isinstance(exc, MultipartParseError):
            error_code = "input_upload_interrupted"
        elif isinstance(exc, (ArtifactStoreError, OSError)):
            error_code = "artifact_store_unavailable"
        if not finalized:
            _compensate_upload(session, state, store, error_code=error_code)
        if isinstance(exc, HTTPException):
            raise
        if isinstance(exc, asyncio.CancelledError):
            raise
        if isinstance(exc, MultipartParseError):
            raise _safe_upload_error("input_upload_interrupted", 422) from None
        if isinstance(exc, (ArtifactStoreError, OSError)):
            raise _safe_upload_error("artifact_store_unavailable", 503) from None
        logger.warning("managed input upload failed adapter=%s", adapter_id)
        raise _safe_upload_error("input_upload_failed", 503) from None


@adapter_router.post(
    "/api/adapters/{adapter_id}/input-artifacts",
    response_model=ManagedInputArtifactResponse,
    status_code=201,
)
async def upload_input_artifact(
    adapter_id: int,
    request: Request,
    principal: CurrentPrincipal,
    session: DbSession,
) -> ManagedInputArtifactResponse:
    """Stream one file part into an opaque local object."""
    return await _stream_upload(request, session, adapter_id, principal)


@adapter_router.get(
    "/api/adapters/{adapter_id}/input-artifacts/sessions/{upload_session_id}",
    response_model=ManagedInputUploadSessionResponse,
)
def recover_input_upload(
    adapter_id: int,
    upload_session_id: str,
    principal: CurrentPrincipal,
    session: DbSession,
) -> ManagedInputUploadSessionResponse:
    """Return safe progress metadata for an active upload session."""
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    managed_input_upload.require_feature_enabled()
    store = LocalFileArtifactStore(settings.artifact_store_root)
    state = managed_input_upload.recover_upload_session(
        session, adapter_id, upload_session_id, store=store
    )
    return managed_input_upload.upload_session_response(state, store)


@adapter_router.post(
    "/api/adapters/{adapter_id}/input-artifacts/sessions/{upload_session_id}/renew",
    response_model=ManagedInputUploadSessionResponse,
)
def renew_input_upload(
    adapter_id: int,
    upload_session_id: str,
    principal: CurrentPrincipal,
    session: DbSession,
) -> ManagedInputUploadSessionResponse:
    """Renew the same active reservation used by an upload writer."""
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    managed_input_upload.require_feature_enabled()
    store = LocalFileArtifactStore(settings.artifact_store_root)
    state = managed_input_upload.renew_upload_reservation(
        session, adapter_id, upload_session_id, store=store
    )
    return managed_input_upload.upload_session_response(state, store)


@adapter_router.get(
    "/api/adapters/{adapter_id}/input-artifacts",
    response_model=list[ManagedInputArtifactResponse],
)
def list_input_artifacts(
    adapter_id: int,
    principal: CurrentPrincipal,
    session: DbSession,
    status: str = Query(default="staged"),
) -> list[ManagedInputArtifactResponse]:
    """List only safe STAGED metadata owned by this Adapter."""
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    if status.casefold() != "staged":
        raise HTTPException(
            status_code=422,
            detail={"code": "input_invalid", "message": "Only staged artifacts are listable"},
        )
    return managed_input_upload.list_staged(session, adapter_id)


@adapter_router.delete(
    "/api/adapters/{adapter_id}/input-artifacts/{artifact_id}",
    status_code=204,
)
def delete_input_artifact(
    adapter_id: int,
    artifact_id: int,
    principal: CurrentPrincipal,
    session: DbSession,
) -> Response:
    """Delete one same-Adapter staged artifact idempotently."""
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    managed_input_upload.delete_staged(session, adapter_id, artifact_id)
    return Response(status_code=204)
