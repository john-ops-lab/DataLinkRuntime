"""Managed Input policy and B1 staged-upload endpoints."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
from collections.abc import Callable
from typing import Annotated, BinaryIO

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.common.managed_input import MANAGED_INPUT_FILE_EXTENSIONS
from dlr.control import db
from dlr.control.models import ManagedInputArtifact, ManagedInputArtifactStatus
from dlr.control.schemas.managed_input import (
    ManagedInputArtifactResponse,
    ManagedInputCapabilityResponse,
    ManagedInputSettingsResponse,
    ManagedInputSettingsUpdate,
)
from dlr.control.security import (
    Principal,
    require_admin_principal,
    require_business_principal,
    require_principal,
    require_upload_principal,
)
from dlr.control.services import adapter_access, managed_input_gc, managed_input_upload
from dlr.control.services import input_config as input_config_service
from dlr.control.services import managed_input as managed_input_service
from dlr.control.services.artifact_store import ArtifactStoreError, LocalFileArtifactStore
from dlr.control.services.managed_input_upload import UploadSessionState
from dlr.control.services.multipart import MultipartParseError, MultipartReader

logger = logging.getLogger("dlr.control.managed_input")

router = APIRouter(dependencies=[Depends(require_admin_principal)])
adapter_router = APIRouter(dependencies=[Depends(require_business_principal)])
# Capability is intentionally readable by ordinary business users.  It is a
# small set of safe release/form facts, not an alias for the admin settings resource.
capability_router = APIRouter(dependencies=[Depends(require_business_principal)])
upload_router = APIRouter()

DbSession = Annotated[Session, Depends(db.get_session)]
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]
CurrentUploadPrincipal = Annotated[Principal, Depends(require_upload_principal)]


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
    principal: CurrentPrincipal,
    session: DbSession,
) -> ManagedInputSettingsResponse:
    """Replace the database policy without changing stored artifacts."""
    return managed_input_service.update_settings(
        session,
        payload,
        actor_kind=principal.kind,
        actor_id=principal.user_id,
    )


@router.post(
    "/api/system/managed-input-artifacts/{artifact_id}/retry-delete",
    status_code=204,
)
def retry_managed_input_artifact_delete(artifact_id: int, session: DbSession) -> Response:
    """Release one DELETE_FAILED Artifact for an explicit administrator retry."""
    managed_input_gc.retry_failed_artifact_deletion(session, artifact_id)
    return Response(status_code=204)


@router.post(
    "/api/system/managed-input-deletion-jobs/{job_id}/retry-delete",
    status_code=204,
)
def retry_managed_input_deletion_job(job_id: int, session: DbSession) -> Response:
    """Release one thresholded detached job for an administrator retry."""
    managed_input_gc.retry_failed_deletion_job(session, job_id)
    return Response(status_code=204)


@capability_router.get(
    "/api/system/managed-input-capability",
    response_model=ManagedInputCapabilityResponse,
)
def get_managed_input_capability(session: DbSession) -> ManagedInputCapabilityResponse:
    """Expose only the user-facing Managed Input release facts.

    ``ready`` is intentionally tied to the deployment flag at this boundary.
    The retention limits are safe business-form constraints; storage paths,
    quota usage, credentials and administrator-only details remain excluded.
    """

    enabled = bool(settings.managed_files_enabled)
    policy = managed_input_service.get_settings(session)
    return ManagedInputCapabilityResponse(
        managed_files_enabled=enabled,
        ready=enabled,
        default_retention_seconds=int(policy.default_retention_seconds),
        max_custom_retention_seconds=int(policy.max_custom_retention_seconds),
        allow_manual_delete=bool(policy.allow_manual_delete),
        allowed_extensions=list(MANAGED_INPUT_FILE_EXTENSIONS),
    )


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


def _run_in_session[T](operation: Callable[[Session], T]) -> T:
    """Run one blocking DB operation in a short-lived worker-thread Session."""
    with db.SessionLocal() as short_session:
        return operation(short_session)


async def _run_db[T](operation: Callable[[Session], T]) -> T:
    """Keep synchronous SQLAlchemy work off the event loop and release it promptly."""
    return await asyncio.to_thread(_run_in_session, operation)


def _new_store_or_error() -> LocalFileArtifactStore:
    """Map local-store startup failures to the stable upload boundary."""
    try:
        return LocalFileArtifactStore(settings.artifact_store_root)
    except (ArtifactStoreError, OSError):
        raise _safe_upload_error("artifact_store_unavailable", 503) from None


def _delete_upload_paths(store: LocalFileArtifactStore, storage_key: str) -> None:
    """Best-effort filesystem cleanup used only after DB compensation fails."""
    store.delete_part(storage_key)
    store.delete(storage_key)


async def _compensate_upload(
    state: UploadSessionState | None,
    store: LocalFileArtifactStore | None,
    *,
    error_code: str,
) -> None:
    """Best-effort compensation using short DB work and worker-thread I/O."""
    if state is None:
        return
    try:
        await _run_db(
            lambda short_session: managed_input_upload.abort_upload(
                short_session,
                state.adapter_id,
                state.upload_session_id,
                error_code=error_code,
                store=store,
            )
        )
    except Exception:
        if store is not None:
            try:
                await asyncio.to_thread(_delete_upload_paths, store, state.storage_key)
            except (ArtifactStoreError, OSError):
                logger.warning("managed input upload compensation deferred")


UPLOAD_RENEWAL_INTERVAL_SECONDS = 30.0


def _renewal_delay_seconds(_state: UploadSessionState) -> float:
    """Return the fixed event-loop interval used by the monotonic waiter."""
    return UPLOAD_RENEWAL_INTERVAL_SECONDS


async def _renew_upload_writer(
    state_box: list[UploadSessionState],
    store: LocalFileArtifactStore,
    stop: asyncio.Event,
) -> None:
    """Keep one active writer lease alive independently of incoming chunks."""
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=_renewal_delay_seconds(state_box[0]))
            return
        except TimeoutError:
            pass
        current = state_box[0]
        state_box[0] = await _run_db(
            functools.partial(
                managed_input_upload.renew_upload_reservation,
                adapter_id=current.adapter_id,
                upload_session_id=current.upload_session_id,
                store=store,
            )
        )


async def _stop_upload_renewal(
    task: asyncio.Task[None] | None,
    stop: asyncio.Event | None,
    *,
    suppress_errors: bool,
) -> None:
    if task is None or stop is None:
        return
    stop.set()
    try:
        await task
    except Exception:
        if not suppress_errors:
            raise


async def _stream_upload(
    request: Request,
    adapter_id: int,
    principal: Principal,
) -> ManagedInputArtifactResponse:
    """Parse and persist one multipart file without buffering its contents."""
    await _run_db(
        lambda short_session: adapter_access.require_adapter_access(
            short_session, adapter_id, principal, "edit"
        )
    )
    managed_input_upload.require_feature_enabled()
    reader: MultipartReader
    state: UploadSessionState | None = None
    store: LocalFileArtifactStore | None = None
    finalized = False
    file_seen = False
    total_bytes = 0
    digest = hashlib.sha256()
    renewal_state: list[UploadSessionState] | None = None
    renewal_stop: asyncio.Event | None = None
    renewal_task: asyncio.Task[None] | None = None
    try:
        reader = MultipartReader(request.stream(), request.headers.get("content-type"))
        # The low-watermark snapshot is captured at reservation creation and
        # refreshed only by bounded reservation growth, never per chunk.
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
            filename = managed_input_upload.validate_original_filename(part.filename)
            content_type = part.content_type
            # The feature flag and metadata checks happen before construction;
            # a disabled or rejected upload must not create a store root.
            store = await asyncio.to_thread(_new_store_or_error)
            state = await _run_db(
                functools.partial(
                    managed_input_upload.begin_upload,
                    adapter_id=adapter_id,
                    original_filename=filename,
                    content_type=content_type,
                    created_by_user_id=principal.user_id,
                    actor_kind=principal.kind,
                    store=store,
                )
            )
            renewal_state = [state]
            renewal_stop = asyncio.Event()
            renewal_task = asyncio.create_task(
                _renew_upload_writer(renewal_state, store, renewal_stop)
            )
            part_context = store.put_part(state.storage_key)
            handle: BinaryIO | None = None
            try:
                handle = await asyncio.to_thread(part_context.__enter__)
                async for chunk in reader.iter_part_body():
                    if not chunk:
                        continue
                    if renewal_task.done():
                        await renewal_task
                    if renewal_state is not None:
                        state = renewal_state[0]
                    total_bytes += len(chunk)
                    if total_bytes > state.reserved_bytes:
                        state = await _run_db(
                            functools.partial(
                                managed_input_upload.expand_upload_reservation,
                                adapter_id=adapter_id,
                                upload_session_id=state.upload_session_id,
                                requested_total_bytes=total_bytes,
                                store=store,
                                growth_bytes=managed_input_upload.RESERVATION_GROWTH_BYTES,
                            )
                        )
                        renewal_state[0] = state
                    await asyncio.to_thread(
                        managed_input_upload.check_stream_low_watermark_bytes,
                        store,
                        state.min_free_space_bytes,
                        len(chunk),
                    )
                    await asyncio.to_thread(handle.write, chunk)
                    digest.update(chunk)
            finally:
                if handle is not None:
                    await asyncio.to_thread(part_context.__exit__, None, None, None)

        await reader.ensure_complete()
        if not file_seen or state is None or store is None:
            raise _safe_upload_error("input_invalid", 422)
        await _stop_upload_renewal(renewal_task, renewal_stop, suppress_errors=False)
        if renewal_state is not None:
            state = renewal_state[0]
        upload_state = state
        upload_store = store
        await asyncio.to_thread(upload_store.commit, upload_state.storage_key)
        artifact = await _run_db(
            functools.partial(
                managed_input_upload.consume_upload_reservation,
                adapter_id=adapter_id,
                upload_session_id=upload_state.upload_session_id,
                actual_size_bytes=total_bytes,
                sha256=digest.hexdigest(),
                store=upload_store,
            )
        )
        finalized = True
        return managed_input_upload.artifact_response(artifact)
    except BaseException as exc:
        await _stop_upload_renewal(renewal_task, renewal_stop, suppress_errors=True)
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
            await _compensate_upload(state, store, error_code=error_code)
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


@upload_router.post(
    "/api/adapters/{adapter_id}/input-artifacts",
    response_model=ManagedInputArtifactResponse,
    status_code=201,
)
async def upload_input_artifact(
    adapter_id: int,
    request: Request,
    principal: CurrentUploadPrincipal,
) -> ManagedInputArtifactResponse:
    """Stream one file part into an opaque local object."""
    return await _stream_upload(request, adapter_id, principal)


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
    expected_revision: int | None = Query(default=None, ge=1),
) -> Response:
    """Delete STAGED bytes or revision-safely unbind one current READY Artifact."""
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    status = session.scalar(
        select(ManagedInputArtifact.status).where(
            ManagedInputArtifact.id == int(artifact_id),
            ManagedInputArtifact.adapter_id == int(adapter_id),
        )
    )
    if status == ManagedInputArtifactStatus.READY:
        if expected_revision is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "input_invalid",
                    "message": "expected_revision is required for a READY Input Artifact",
                    "params": {"reason": "expected_revision_required"},
                },
            )
        input_config_service.remove_ready_artifact(
            session,
            adapter_id,
            artifact_id,
            expected_revision=expected_revision,
        )
        return Response(status_code=204)
    if status == ManagedInputArtifactStatus.PENDING_DELETE:
        return Response(status_code=204)
    managed_input_upload.delete_staged(
        session,
        adapter_id,
        artifact_id,
        actor_kind=principal.kind,
        actor_id=principal.user_id,
    )
    return Response(status_code=204)
