"""B1 Managed Input upload, reservation, and staged-artifact services.

The service owns the database half of the upload protocol.  HTTP parsing and
the private filesystem implementation remain separate so that every state
transition can be exercised without trusting request metadata or exposing a
storage path.
"""

from __future__ import annotations

import logging
import re
import secrets
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.common.managed_input import MANAGED_INPUT_FILE_EXTENSION_SET
from dlr.control.input_errors import ManagedInputErrorCode
from dlr.control.models import (
    Adapter,
    ArtifactDeletionJob,
    ManagedInputArtifact,
    ManagedInputArtifactStatus,
    ManagedInputCapacity,
    ManagedInputReservationStatus,
    ManagedInputSettings,
    ManagedInputUploadReservation,
)
from dlr.control.schemas.managed_input import (
    ManagedInputArtifactResponse,
)
from dlr.control.services import managed_input as policy_service
from dlr.control.services.adapter import domain_error
from dlr.control.services.artifact_store import (
    ArtifactAuditResult,
    ArtifactStoreError,
    LocalFileArtifactStore,
)
from dlr.control.services.managed_input_audit import record_audit_event

logger = logging.getLogger("dlr.control.managed_input_upload")

ALLOWED_FILE_EXTENSIONS = MANAGED_INPUT_FILE_EXTENSION_SET
INITIAL_RESERVATION_BYTES = 64 * 1024
RESERVATION_GROWTH_BYTES = 1024 * 1024
MAX_FILENAME_BYTES = 512
MAX_CONTENT_TYPE_BYTES = 256
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class UploadSessionState:
    """Internal state needed by one stream; never serialize the key."""

    adapter_id: int
    artifact_id: int
    reservation_id: int
    upload_session_id: str
    storage_key: str
    reserved_bytes: int
    expires_at: datetime | None = None
    # Captured at a reservation create/renew/growth boundary.  Stream chunks
    # use this snapshot between bounded growth commits instead of querying the
    # policy database for every chunk.
    min_free_space_bytes: int = 0


def utcnow() -> datetime:
    """Return the one clock representation used by reservation transitions."""
    return datetime.now(UTC)


def _delete_claim_is_live(artifact: ManagedInputArtifact) -> bool:
    """Return whether another deletion worker still owns this Artifact."""
    lease_until = artifact.delete_lease_until
    if lease_until is None:
        return False
    if lease_until.tzinfo is None:
        lease_until = lease_until.replace(tzinfo=UTC)
    return lease_until > utcnow()


def feature_enabled() -> bool:
    """Return the deployment flag without creating a store or a DB row."""
    return bool(settings.managed_files_enabled)


def require_feature_enabled() -> None:
    if not feature_enabled():
        raise domain_error(
            422,
            ManagedInputErrorCode.FEATURE_NOT_AVAILABLE.value,
            "Managed file input is not available",
        )


def validate_original_filename(filename: str) -> str:
    """Validate display metadata while never treating it as a filesystem path."""
    if not isinstance(filename, str) or not filename or "\x00" in filename:
        raise domain_error(
            422,
            ManagedInputErrorCode.INVALID.value,
            "Input file metadata is invalid",
            {"reason": "filename"},
        )
    try:
        filename_bytes = filename.encode("utf-8")
    except UnicodeEncodeError:
        raise domain_error(
            422,
            ManagedInputErrorCode.INVALID.value,
            "Input file metadata is invalid",
            {"reason": "filename"},
        ) from None
    if len(filename_bytes) > MAX_FILENAME_BYTES or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in filename
    ):
        raise domain_error(
            422,
            ManagedInputErrorCode.INVALID.value,
            "Input file metadata is invalid",
            {"reason": "filename"},
        )

    # Multipart filenames may contain path-looking metadata.  It is retained
    # for display, but only the final component determines the fixed suffix.
    display_name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    extension = "." + display_name.rsplit(".", 1)[-1].casefold() if "." in display_name else ""
    if not display_name or display_name in {".", ".."} or extension not in ALLOWED_FILE_EXTENSIONS:
        raise domain_error(
            422,
            ManagedInputErrorCode.FILE_TYPE_NOT_ALLOWED.value,
            "This file type is not allowed",
        )
    return filename


def normalize_content_type(content_type: str | None) -> str:
    """Keep MIME as bounded display metadata; suffix and bytes are authoritative."""
    if not content_type or "\x00" in content_type:
        return "application/octet-stream"
    try:
        if len(content_type.encode("utf-8")) > MAX_CONTENT_TYPE_BYTES or any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in content_type
        ):
            return "application/octet-stream"
    except UnicodeEncodeError:
        return "application/octet-stream"
    return content_type


def _lock_adapter(session: Session, adapter_id: int) -> Adapter:
    adapter = session.scalar(select(Adapter).where(Adapter.id == adapter_id).with_for_update())
    if adapter is None or adapter.archived_at is not None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    return adapter


def _lock_capacity(session: Session) -> ManagedInputCapacity:
    return policy_service.get_capacity(session, for_update=True)


def _lock_reservation(
    session: Session, adapter_id: int, upload_session_id: str
) -> ManagedInputUploadReservation:
    reservation = session.scalar(
        select(ManagedInputUploadReservation)
        .where(
            ManagedInputUploadReservation.adapter_id == adapter_id,
            ManagedInputUploadReservation.upload_session_id == upload_session_id,
        )
        .with_for_update()
    )
    if reservation is None:
        raise domain_error(
            409,
            ManagedInputErrorCode.SESSION_EXPIRED.value,
            "Upload session is no longer active",
        )
    return reservation


def _lock_reservation_by_id(
    session: Session, reservation_id: int
) -> ManagedInputUploadReservation | None:
    """Lock an expiry candidate without requiring its Adapter to be active."""
    return session.scalar(
        select(ManagedInputUploadReservation)
        .where(ManagedInputUploadReservation.id == reservation_id)
        .with_for_update()
    )


def _lock_artifact(
    session: Session, adapter_id: int, artifact_id: int
) -> ManagedInputArtifact | None:
    artifact = session.scalar(
        select(ManagedInputArtifact)
        .where(
            ManagedInputArtifact.id == artifact_id,
        )
        .with_for_update()
    )
    if artifact is None or artifact.adapter_id != adapter_id:
        return None
    return artifact


def _artifact_for_session(
    session: Session, adapter_id: int, upload_session_id: str, *, for_update: bool = True
) -> ManagedInputArtifact | None:
    query = select(ManagedInputArtifact).where(
        ManagedInputArtifact.adapter_id == adapter_id,
        ManagedInputArtifact.upload_session_id == upload_session_id,
    )
    if for_update:
        query = query.with_for_update()
    return session.scalar(query)


def _adapter_usage(session: Session, adapter_id: int) -> tuple[int, int]:
    actual = session.scalar(
        select(func.coalesce(func.sum(ManagedInputArtifact.size_bytes), 0)).where(
            ManagedInputArtifact.adapter_id == adapter_id,
            ManagedInputArtifact.status.in_(policy_service.CHARGED_ARTIFACT_STATUSES),
        )
    )
    reserved = session.scalar(
        select(func.coalesce(func.sum(ManagedInputUploadReservation.reserved_bytes), 0)).where(
            ManagedInputUploadReservation.adapter_id == adapter_id,
            ManagedInputUploadReservation.status == ManagedInputReservationStatus.ACTIVE,
        )
    )
    return int(actual or 0), int(reserved or 0)


def _free_bytes(store: LocalFileArtifactStore) -> int:
    return int(shutil.disk_usage(store.root).free)


def check_stream_low_watermark(
    store: LocalFileArtifactStore, setting: ManagedInputSettings, additional_bytes: int
) -> None:
    """Check free space immediately before one stream write."""
    check_stream_low_watermark_bytes(store, int(setting.min_free_space_bytes), additional_bytes)


def check_stream_low_watermark_bytes(
    store: LocalFileArtifactStore, min_free_space_bytes: int, additional_bytes: int
) -> None:
    """Check a reservation-boundary watermark without holding a DB Session."""
    if additional_bytes <= 0:
        return
    if _free_bytes(store) - additional_bytes < min_free_space_bytes:
        raise domain_error(
            409,
            ManagedInputErrorCode.LOW_WATERMARK.value,
            "Artifact storage is below its configured free-space watermark",
        )


def _check_capacity(
    session: Session,
    adapter_id: int,
    setting: ManagedInputSettings,
    capacity: ManagedInputCapacity,
    additional_bytes: int,
    store: LocalFileArtifactStore,
) -> None:
    if additional_bytes <= 0:
        return
    adapter_actual, adapter_reserved = _adapter_usage(session, adapter_id)
    if adapter_actual + adapter_reserved + additional_bytes > int(setting.adapter_quota_bytes):
        raise domain_error(
            409,
            ManagedInputErrorCode.ADAPTER_QUOTA_EXCEEDED.value,
            "Adapter input quota exceeded",
        )
    if int(capacity.actual_bytes) + int(capacity.reserved_bytes) + additional_bytes > int(
        setting.platform_quota_bytes
    ):
        raise domain_error(
            409,
            ManagedInputErrorCode.PLATFORM_QUOTA_EXCEEDED.value,
            "Platform input quota exceeded",
        )
    check_stream_low_watermark(store, setting, additional_bytes)


def _refresh_expiry(setting: ManagedInputSettings, now: datetime) -> datetime:
    return now + timedelta(seconds=int(setting.staged_ttl_seconds))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _release_reserved(capacity: ManagedInputCapacity, reserved_bytes: int) -> None:
    if reserved_bytes < 0 or int(capacity.reserved_bytes) < reserved_bytes:
        raise RuntimeError("Managed Input reservation accounting is inconsistent")
    capacity.reserved_bytes -= reserved_bytes


def _mark_upload_deleted(
    artifact: ManagedInputArtifact | None, now: datetime, error_code: str
) -> None:
    if artifact is not None and artifact.status == ManagedInputArtifactStatus.UPLOADING:
        artifact.status = ManagedInputArtifactStatus.DELETED
        artifact.deleted_at = now
        artifact.last_error_code = error_code


def _state(
    artifact: ManagedInputArtifact,
    reservation: ManagedInputUploadReservation,
    setting: ManagedInputSettings | None = None,
) -> UploadSessionState:
    # Policy values are intentionally captured only at reservation transition
    # boundaries.  The streaming loop carries this value across its bounded
    # growth interval and never opens a policy Session per transport chunk.
    min_free_space_bytes = int(setting.min_free_space_bytes) if setting is not None else 0
    return UploadSessionState(
        adapter_id=int(artifact.adapter_id),
        artifact_id=int(artifact.id),
        reservation_id=int(reservation.id),
        upload_session_id=reservation.upload_session_id,
        storage_key=artifact.storage_key,
        reserved_bytes=int(reservation.reserved_bytes),
        expires_at=reservation.expires_at,
        min_free_space_bytes=min_free_space_bytes,
    )


def begin_upload(
    session: Session,
    adapter_id: int,
    *,
    original_filename: str,
    content_type: str | None,
    created_by_user_id: int | None = None,
    actor_kind: str | None = None,
    store: LocalFileArtifactStore | None = None,
) -> UploadSessionState:
    """Create one UPLOADING Artifact and its ACTIVE reservation atomically."""
    require_feature_enabled()
    original_filename = validate_original_filename(original_filename)
    normalized_type = normalize_content_type(content_type)
    store = store or LocalFileArtifactStore()

    # Every writer takes Adapter then platform capacity.  This is also the
    # order used by deletion/expiry paths and prevents a quota race deadlock.
    adapter = _lock_adapter(session, adapter_id)
    capacity = _lock_capacity(session)
    setting = policy_service.get_settings(session)
    initial_bytes = min(INITIAL_RESERVATION_BYTES, int(setting.max_file_bytes))
    _check_capacity(session, adapter.id, setting, capacity, initial_bytes, store)
    now = utcnow()
    expires_at = _refresh_expiry(setting, now)
    upload_session_id = secrets.token_urlsafe(32)
    reservation = ManagedInputUploadReservation(
        adapter_id=adapter.id,
        upload_session_id=upload_session_id,
        reserved_bytes=initial_bytes,
        status=ManagedInputReservationStatus.ACTIVE,
        expires_at=expires_at,
    )
    session.add(reservation)
    session.flush()
    artifact = ManagedInputArtifact(
        adapter_id=adapter.id,
        created_by_user_id=created_by_user_id,
        upload_session_id=upload_session_id,
        upload_reservation_id=reservation.id,
        original_filename=original_filename,
        storage_key=store.new_storage_key(),
        content_type=normalized_type,
        size_bytes=0,
        status=ManagedInputArtifactStatus.UPLOADING,
        expires_at=expires_at,
    )
    session.add(artifact)
    capacity.reserved_bytes += initial_bytes
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    logger.info(
        "managed input upload started adapter=%s artifact=%s reservation=%s",
        adapter.id,
        artifact.id,
        reservation.id,
    )
    record_audit_event(
        "upload",
        "started",
        actor_kind=actor_kind or ("user" if created_by_user_id is not None else "system"),
        actor_id=created_by_user_id,
        adapter_id=int(adapter.id),
        artifact_id=int(artifact.id),
    )
    return _state(artifact, reservation, setting)


def _terminal_upload_error() -> NoReturn:
    raise domain_error(
        409,
        ManagedInputErrorCode.SESSION_EXPIRED.value,
        "Upload session is no longer active",
    )


def _expire_locked_upload(
    capacity: ManagedInputCapacity,
    reservation: ManagedInputUploadReservation,
    artifact: ManagedInputArtifact | None,
    now: datetime,
) -> None:
    if reservation.status == ManagedInputReservationStatus.ACTIVE:
        _release_reserved(capacity, int(reservation.reserved_bytes))
        reservation.status = ManagedInputReservationStatus.EXPIRED
        _mark_upload_deleted(artifact, now, ManagedInputErrorCode.SESSION_EXPIRED.value)


def renew_upload_reservation(
    session: Session,
    adapter_id: int,
    upload_session_id: str,
    *,
    store: LocalFileArtifactStore | None = None,
) -> UploadSessionState:
    """Conditionally renew an ACTIVE writer lease under the common lock order."""
    _lock_adapter(session, adapter_id)
    capacity = _lock_capacity(session)
    reservation = _lock_reservation(session, adapter_id, upload_session_id)
    artifact = _artifact_for_session(session, adapter_id, upload_session_id)
    now = utcnow()
    if reservation.status != ManagedInputReservationStatus.ACTIVE:
        _terminal_upload_error()
    if _as_utc(reservation.expires_at) <= now:
        key = artifact.storage_key if artifact is not None else None
        _expire_locked_upload(capacity, reservation, artifact, now)
        session.commit()
        if key is not None and store is not None:
            _cleanup_paths(store, key)
        _terminal_upload_error()
    setting = policy_service.get_settings(session)
    reservation.expires_at = _refresh_expiry(setting, now)
    if artifact is not None and artifact.status == ManagedInputArtifactStatus.UPLOADING:
        artifact.expires_at = reservation.expires_at
    session.commit()
    if artifact is None:
        raise domain_error(
            409,
            ManagedInputErrorCode.ARTIFACT_NOT_READY.value,
            "Input Artifact is not uploading",
        )
    return _state(artifact, reservation, setting)


def expand_upload_reservation(
    session: Session,
    adapter_id: int,
    upload_session_id: str,
    requested_total_bytes: int,
    *,
    store: LocalFileArtifactStore | None = None,
    growth_bytes: int | None = None,
) -> UploadSessionState:
    """Atomically extend a reservation before a writer stores new bytes.

    ``growth_bytes`` lets a streaming caller grow in bounded batches.  The
    returned state refreshes the low-watermark snapshot at that boundary;
    callers must not turn this into a per-transport-chunk policy query.
    """
    if requested_total_bytes < 0:
        raise domain_error(
            422,
            ManagedInputErrorCode.INVALID.value,
            "Input size is invalid",
            {"reason": "size"},
        )
    _lock_adapter(session, adapter_id)
    capacity = _lock_capacity(session)
    reservation = _lock_reservation(session, adapter_id, upload_session_id)
    artifact = _artifact_for_session(session, adapter_id, upload_session_id)
    now = utcnow()
    if reservation.status != ManagedInputReservationStatus.ACTIVE:
        _terminal_upload_error()
    if _as_utc(reservation.expires_at) <= now:
        key = artifact.storage_key if artifact is not None else None
        _expire_locked_upload(capacity, reservation, artifact, now)
        session.commit()
        if key is not None and store is not None:
            _cleanup_paths(store, key)
        _terminal_upload_error()
    if artifact is None or artifact.status != ManagedInputArtifactStatus.UPLOADING:
        raise domain_error(
            409,
            ManagedInputErrorCode.ARTIFACT_NOT_READY.value,
            "Input Artifact is not uploading",
        )
    setting = policy_service.get_settings(session)
    if requested_total_bytes > int(setting.max_file_bytes):
        raise domain_error(
            413,
            ManagedInputErrorCode.FILE_TOO_LARGE.value,
            "Input file is too large",
        )
    if growth_bytes is not None and growth_bytes > 0:
        requested_total_bytes = min(
            int(setting.max_file_bytes),
            max(requested_total_bytes, int(reservation.reserved_bytes) + growth_bytes),
        )
    delta = requested_total_bytes - int(reservation.reserved_bytes)
    if delta > 0:
        store = store or LocalFileArtifactStore()
        _check_capacity(session, adapter_id, setting, capacity, delta, store)
        reservation.reserved_bytes = requested_total_bytes
        capacity.reserved_bytes += delta
    reservation.expires_at = _refresh_expiry(setting, now)
    artifact.expires_at = reservation.expires_at
    session.commit()
    return _state(artifact, reservation, setting)


def consume_upload_reservation(
    session: Session,
    adapter_id: int,
    upload_session_id: str,
    *,
    actual_size_bytes: int,
    sha256: str,
    store: LocalFileArtifactStore | None = None,
) -> ManagedInputArtifact:
    """Commit STAGED metadata and capacity charge in one DB transaction."""
    if actual_size_bytes < 0:
        raise domain_error(
            422,
            ManagedInputErrorCode.INVALID.value,
            "Input size is invalid",
            {"reason": "size"},
        )
    if SHA256_PATTERN.fullmatch(sha256) is None:
        raise domain_error(
            422,
            ManagedInputErrorCode.CHECKSUM_INVALID.value,
            "Input Artifact checksum is invalid",
        )
    _lock_adapter(session, adapter_id)
    capacity = _lock_capacity(session)
    reservation = _lock_reservation(session, adapter_id, upload_session_id)
    artifact = _artifact_for_session(session, adapter_id, upload_session_id)
    now = utcnow()
    if reservation.status != ManagedInputReservationStatus.ACTIVE:
        _terminal_upload_error()
    if _as_utc(reservation.expires_at) <= now:
        key = artifact.storage_key if artifact is not None else None
        _expire_locked_upload(capacity, reservation, artifact, now)
        session.commit()
        if key is not None and store is not None:
            _cleanup_paths(store, key)
        _terminal_upload_error()
    if artifact is None or artifact.status != ManagedInputArtifactStatus.UPLOADING:
        raise domain_error(
            409,
            ManagedInputErrorCode.ARTIFACT_NOT_READY.value,
            "Input Artifact is not uploading",
        )
    if store is not None:
        published = store.stat(artifact.storage_key)
        if published is None or published.size_bytes != actual_size_bytes:
            raise domain_error(
                503,
                ManagedInputErrorCode.UPLOAD_FAILED.value,
                "Input upload could not be finalized",
            )
    setting = policy_service.get_settings(session)
    if actual_size_bytes > int(setting.max_file_bytes):
        raise domain_error(
            413,
            ManagedInputErrorCode.FILE_TOO_LARGE.value,
            "Input file is too large",
        )

    # A direct service caller may not have called expand for the final chunk.
    # Grow under the same capacity lock instead of silently overcharging.
    delta = actual_size_bytes - int(reservation.reserved_bytes)
    if delta > 0:
        store = store or LocalFileArtifactStore()
        _check_capacity(session, adapter_id, setting, capacity, delta, store)
        reservation.reserved_bytes = actual_size_bytes
        capacity.reserved_bytes += delta
    _release_reserved(capacity, int(reservation.reserved_bytes))
    capacity.actual_bytes += actual_size_bytes
    reservation.status = ManagedInputReservationStatus.CONSUMED
    reservation.consumed_at = now
    artifact.size_bytes = actual_size_bytes
    artifact.sha256 = sha256
    artifact.status = ManagedInputArtifactStatus.STAGED
    artifact.expires_at = _refresh_expiry(setting, now)
    session.commit()
    logger.info(
        "managed input upload staged adapter=%s artifact=%s reservation=%s size=%s",
        adapter_id,
        artifact.id,
        reservation.id,
        actual_size_bytes,
    )
    record_audit_event(
        "upload",
        "staged",
        adapter_id=adapter_id,
        artifact_id=int(artifact.id),
    )
    return artifact


def abort_upload(
    session: Session,
    adapter_id: int,
    upload_session_id: str,
    *,
    error_code: str = ManagedInputErrorCode.UPLOAD_FAILED.value,
    store: LocalFileArtifactStore | None = None,
) -> None:
    """Cancel an active writer exactly once; repeated compensation is safe."""
    _lock_adapter(session, adapter_id)
    capacity = _lock_capacity(session)
    reservation = _lock_reservation(session, adapter_id, upload_session_id)
    artifact = _artifact_for_session(session, adapter_id, upload_session_id)
    key = artifact.storage_key if artifact is not None else None
    now = utcnow()
    if reservation.status == ManagedInputReservationStatus.ACTIVE:
        _release_reserved(capacity, int(reservation.reserved_bytes))
        reservation.status = ManagedInputReservationStatus.CANCELLED
        _mark_upload_deleted(artifact, now, error_code)
    elif artifact is not None and artifact.status == ManagedInputArtifactStatus.UPLOADING:
        _mark_upload_deleted(artifact, now, error_code)
    session.commit()
    if key is not None and store is not None:
        _cleanup_paths(store, key)
    logger.info(
        "managed input upload aborted adapter=%s artifact=%s code=%s",
        adapter_id,
        artifact.id if artifact is not None else None,
        error_code,
    )
    record_audit_event(
        "upload",
        "failed",
        adapter_id=adapter_id,
        artifact_id=int(artifact.id) if artifact is not None else None,
        code=error_code,
    )


def _cleanup_paths(store: LocalFileArtifactStore, storage_key: str) -> None:
    """Best-effort idempotent compensation for a failed writer."""
    try:
        store.delete_part(storage_key)
        store.delete(storage_key)
    except (ArtifactStoreError, OSError):
        logger.warning("managed input upload compensation deferred")


def expire_upload_reservations(
    session: Session,
    *,
    store: LocalFileArtifactStore | None = None,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    """Expire stale ACTIVE writers using conditional, idempotent transitions."""
    if limit <= 0:
        return 0
    now = now or utcnow()
    candidates = list(
        session.execute(
            select(
                ManagedInputUploadReservation.id,
                ManagedInputUploadReservation.adapter_id,
                ManagedInputUploadReservation.upload_session_id,
            )
            .where(
                ManagedInputUploadReservation.status == ManagedInputReservationStatus.ACTIVE,
                ManagedInputUploadReservation.expires_at <= now,
            )
            .order_by(ManagedInputUploadReservation.id)
            .limit(limit)
        ).all()
    )
    store = store or LocalFileArtifactStore()
    count = 0
    for reservation_id, adapter_id, upload_session_id in candidates:
        try:
            capacity = _lock_capacity(session)
            reservation = _lock_reservation_by_id(session, int(reservation_id))
            if (
                reservation is None
                or reservation.adapter_id != adapter_id
                or reservation.upload_session_id != upload_session_id
                or reservation.status != ManagedInputReservationStatus.ACTIVE
                or _as_utc(reservation.expires_at) > now
            ):
                session.rollback()
                continue
            artifact = _artifact_for_session(session, int(adapter_id), upload_session_id)
            key = artifact.storage_key if artifact is not None else None
            _expire_locked_upload(capacity, reservation, artifact, now)
            session.commit()
        except HTTPException:
            session.rollback()
            continue
        if key is not None:
            _cleanup_paths(store, key)
        logger.info(
            "managed input upload expired adapter=%s artifact=%s reservation=%s",
            adapter_id,
            artifact.id if artifact is not None else None,
            reservation_id,
        )
        record_audit_event(
            "upload_ttl",
            "expired",
            adapter_id=int(adapter_id),
            artifact_id=int(artifact.id) if artifact is not None else None,
            code=ManagedInputErrorCode.SESSION_EXPIRED.value,
        )
        count += 1
    return count


def list_staged(session: Session, adapter_id: int) -> list[ManagedInputArtifactResponse]:
    """Return only the current Adapter's STAGED metadata."""
    rows = session.scalars(
        select(ManagedInputArtifact)
        .where(
            ManagedInputArtifact.adapter_id == adapter_id,
            ManagedInputArtifact.status == ManagedInputArtifactStatus.STAGED,
        )
        .order_by(ManagedInputArtifact.created_at, ManagedInputArtifact.id)
    ).all()
    return [artifact_response(row) for row in rows]


def artifact_response(artifact: ManagedInputArtifact) -> ManagedInputArtifactResponse:
    """Project one STAGED row without an operational storage reference."""
    if artifact.status != ManagedInputArtifactStatus.STAGED or artifact.sha256 is None:
        raise domain_error(
            409,
            ManagedInputErrorCode.ARTIFACT_NOT_READY.value,
            "Input Artifact is not staged",
        )
    return ManagedInputArtifactResponse(
        id=artifact.id,
        original_filename=artifact.original_filename,
        content_type=artifact.content_type,
        size_bytes=artifact.size_bytes,
        sha256=artifact.sha256,
        status="STAGED",
        created_at=artifact.created_at,
        expires_at=artifact.expires_at,
    )


def delete_staged(
    session: Session,
    adapter_id: int,
    artifact_id: int,
    *,
    actor_kind: str | None = None,
    actor_id: int | None = None,
    store: LocalFileArtifactStore | None = None,
) -> bool:
    """Delete a staged blob and release actual bytes exactly once."""
    _lock_adapter(session, adapter_id)
    artifact = _lock_artifact(session, adapter_id, artifact_id)
    if artifact is None:
        session.commit()
        return False
    if artifact.status == ManagedInputArtifactStatus.DELETED:
        session.commit()
        return False
    if artifact.status == ManagedInputArtifactStatus.DELETING and _delete_claim_is_live(artifact):
        raise domain_error(
            409,
            ManagedInputErrorCode.ARTIFACT_DELETE_IN_PROGRESS.value,
            "Input Artifact deletion is already in progress",
        )
    if (
        artifact.status == ManagedInputArtifactStatus.DELETE_FAILED
        and int(artifact.delete_attempts) >= settings.artifact_delete_alert_threshold
    ):
        raise domain_error(
            409,
            ManagedInputErrorCode.ARTIFACT_RETRY_NOT_ALLOWED.value,
            "Input Artifact requires an administrator retry",
        )
    if artifact.status not in {
        ManagedInputArtifactStatus.STAGED,
        ManagedInputArtifactStatus.DELETE_FAILED,
        ManagedInputArtifactStatus.DELETING,
    }:
        raise domain_error(
            409,
            ManagedInputErrorCode.ARTIFACT_NOT_READY.value,
            "Input Artifact is not staged",
        )
    store = store or LocalFileArtifactStore()
    # Keep the synchronous HTTP contract for explicit user deletion while
    # using the same short database claim and idempotent finalization as GC.
    # ``force`` only bypasses a previous GC backoff because this is an explicit
    # user retry; it never takes over a live DELETING lease.
    from dlr.control.services import managed_input_gc

    session.commit()
    claim = managed_input_gc.claim_artifact_deletion(
        session,
        artifact_id,
        adapter_id=adapter_id,
        force=True,
    )
    if claim is None:
        current = session.get(ManagedInputArtifact, artifact_id)
        if current is None or current.adapter_id != adapter_id:
            session.commit()
            return False
        if current.status == ManagedInputArtifactStatus.DELETED:
            session.commit()
            return False
        if current.status == ManagedInputArtifactStatus.DELETING and _delete_claim_is_live(current):
            raise domain_error(
                409,
                ManagedInputErrorCode.ARTIFACT_DELETE_IN_PROGRESS.value,
                "Input Artifact deletion is already in progress",
            )
        if (
            current.status == ManagedInputArtifactStatus.DELETE_FAILED
            and int(current.delete_attempts) >= settings.artifact_delete_alert_threshold
        ):
            raise domain_error(
                409,
                ManagedInputErrorCode.ARTIFACT_RETRY_NOT_ALLOWED.value,
                "Input Artifact requires an administrator retry",
            )
        raise domain_error(
            409,
            ManagedInputErrorCode.ARTIFACT_NOT_READY.value,
            "Input Artifact is not staged",
        )
    try:
        store.delete(claim.storage_key)
    except (ArtifactStoreError, OSError):
        managed_input_gc.finalize_artifact_deletion(
            session,
            claim,
            succeeded=False,
            error_code=ManagedInputErrorCode.ARTIFACT_DELETE_FAILED.value,
        )
        record_audit_event(
            "delete",
            "failed",
            actor_kind=actor_kind,
            actor_id=actor_id,
            adapter_id=adapter_id,
            artifact_id=artifact_id,
            code=ManagedInputErrorCode.ARTIFACT_DELETE_FAILED.value,
        )
        raise domain_error(
            503,
            ManagedInputErrorCode.ARTIFACT_DELETE_FAILED.value,
            "Input Artifact could not be deleted",
        ) from None
    managed_input_gc.finalize_artifact_deletion(session, claim, succeeded=True)
    record_audit_event(
        "delete",
        "success",
        actor_kind=actor_kind,
        actor_id=actor_id,
        adapter_id=adapter_id,
        artifact_id=artifact_id,
    )
    logger.info("managed input artifact deleted adapter=%s artifact=%s", adapter_id, artifact_id)
    return True


def audit_unowned_objects(
    session: Session,
    *,
    store: LocalFileArtifactStore | None = None,
    older_than: datetime | None = None,
) -> ArtifactAuditResult:
    """Audit random keys absent from live Artifact and deletion-job metadata."""
    store = store or LocalFileArtifactStore()
    artifact_keys = set(
        session.scalars(
            select(ManagedInputArtifact.storage_key).where(
                ManagedInputArtifact.status != ManagedInputArtifactStatus.DELETED
            )
        ).all()
    )
    deletion_keys = set(session.scalars(select(ArtifactDeletionJob.storage_key)).all())
    return store.audit_orphans(artifact_keys | deletion_keys, older_than=older_than)


__all__ = [
    "ALLOWED_FILE_EXTENSIONS",
    "INITIAL_RESERVATION_BYTES",
    "RESERVATION_GROWTH_BYTES",
    "UploadSessionState",
    "abort_upload",
    "artifact_response",
    "audit_unowned_objects",
    "begin_upload",
    "check_stream_low_watermark",
    "check_stream_low_watermark_bytes",
    "consume_upload_reservation",
    "delete_staged",
    "expand_upload_reservation",
    "expire_upload_reservations",
    "feature_enabled",
    "list_staged",
    "normalize_content_type",
    "renew_upload_reservation",
    "require_feature_enabled",
    "utcnow",
    "validate_original_filename",
]
