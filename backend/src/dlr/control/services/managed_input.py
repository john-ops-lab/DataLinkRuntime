"""Managed Input settings and capacity usage service (Issue #127 B0).

This module deliberately stops at the B0 public policy/schema boundary.  It
does not create upload sessions, write blobs, bind artifacts, or run GC; those
operations belong to later waves and consume the tables defined here.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.control.models import (
    ManagedInputArtifact,
    ManagedInputCapacity,
    ManagedInputSettings,
    ManagedInputUploadReservation,
)
from dlr.control.schemas.managed_input import (
    DEFAULT_ADAPTER_QUOTA_BYTES,
    DEFAULT_MAX_CUSTOM_RETENTION_SECONDS,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MIN_FREE_SPACE_BYTES,
    DEFAULT_PLATFORM_QUOTA_BYTES,
    DEFAULT_RETENTION_SECONDS,
    DEFAULT_STAGED_TTL_SECONDS,
    ManagedInputAdapterUsage,
    ManagedInputSettingsResponse,
    ManagedInputSettingsUpdate,
    ManagedInputUsage,
)
from dlr.control.services.adapter import domain_error

SINGLETON_ID = 1

# These are the states whose Blob bytes remain charged to the platform.  An
# UPLOADING Artifact is charged through its ACTIVE reservation instead; a
# DELETED Artifact has already released its charge.
CHARGED_ARTIFACT_STATUSES = (
    "STAGED",
    "READY",
    "PENDING_DELETE",
    "DELETING",
    "DELETE_FAILED",
)


def default_settings_values() -> dict[str, int | bool]:
    """Return a fresh copy of the documented database seed values."""
    return {
        "default_retention_seconds": DEFAULT_RETENTION_SECONDS,
        "max_file_bytes": DEFAULT_MAX_FILE_BYTES,
        "platform_quota_bytes": DEFAULT_PLATFORM_QUOTA_BYTES,
        "adapter_quota_bytes": DEFAULT_ADAPTER_QUOTA_BYTES,
        "allow_manual_delete": True,
        "max_custom_retention_seconds": DEFAULT_MAX_CUSTOM_RETENTION_SECONDS,
        "min_free_space_bytes": DEFAULT_MIN_FREE_SPACE_BYTES,
        "staged_ttl_seconds": DEFAULT_STAGED_TTL_SECONDS,
    }


def _insert_default_settings(session: Session) -> ManagedInputSettings:
    """Insert the singleton if a test or an old partial install lacks it."""
    values: dict[str, Any] = {"id": SINGLETON_ID, **default_settings_values()}
    statement = insert(ManagedInputSettings).values(**values)
    statement = statement.on_conflict_do_nothing(index_elements=[ManagedInputSettings.id])
    session.execute(statement)
    setting = session.get(ManagedInputSettings, SINGLETON_ID)
    if setting is None:  # pragma: no cover - protected by the singleton insert
        raise RuntimeError("Managed Input settings singleton could not be initialized")
    return setting


def _insert_default_capacity(session: Session) -> ManagedInputCapacity:
    """Insert the platform capacity singleton if it is absent."""
    statement = insert(ManagedInputCapacity).values(
        id=SINGLETON_ID,
        actual_bytes=0,
        reserved_bytes=0,
    )
    statement = statement.on_conflict_do_nothing(index_elements=[ManagedInputCapacity.id])
    session.execute(statement)
    capacity = session.get(ManagedInputCapacity, SINGLETON_ID)
    if capacity is None:  # pragma: no cover - protected by the singleton insert
        raise RuntimeError("Managed Input capacity singleton could not be initialized")
    return capacity


def get_settings(session: Session, *, for_update: bool = False) -> ManagedInputSettings:
    """Return the policy singleton, preserving the migration seed contract."""
    query = select(ManagedInputSettings).where(ManagedInputSettings.id == SINGLETON_ID)
    if for_update:
        query = query.with_for_update()
    setting = session.scalar(query)
    return setting if setting is not None else _insert_default_settings(session)


def get_capacity(session: Session, *, for_update: bool = False) -> ManagedInputCapacity:
    """Return the serialized platform capacity account."""
    query = select(ManagedInputCapacity).where(ManagedInputCapacity.id == SINGLETON_ID)
    if for_update:
        query = query.with_for_update()
    capacity = session.scalar(query)
    return capacity if capacity is not None else _insert_default_capacity(session)


def _adapter_usage(session: Session) -> dict[int, list[int]]:
    """Aggregate charged and actively reserved bytes by Adapter."""
    usage: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    artifacts = session.execute(
        select(
            ManagedInputArtifact.adapter_id,
            func.coalesce(func.sum(ManagedInputArtifact.size_bytes), 0),
        )
        .where(ManagedInputArtifact.status.in_(CHARGED_ARTIFACT_STATUSES))
        .group_by(ManagedInputArtifact.adapter_id)
    )
    for adapter_id, actual_bytes in artifacts:
        usage[int(adapter_id)][0] = int(actual_bytes or 0)

    reservations = session.execute(
        select(
            ManagedInputUploadReservation.adapter_id,
            func.coalesce(func.sum(ManagedInputUploadReservation.reserved_bytes), 0),
        )
        .where(ManagedInputUploadReservation.status == "ACTIVE")
        .group_by(ManagedInputUploadReservation.adapter_id)
    )
    for adapter_id, reserved_bytes in reservations:
        usage[int(adapter_id)][1] = int(reserved_bytes or 0)
    return usage


def current_usage(
    session: Session, setting: ManagedInputSettings | None = None
) -> ManagedInputUsage:
    """Build non-sensitive platform and per-Adapter usage facts.

    The platform singleton is the serialized charge account used by future
    upload transactions.  Per-Adapter values are derived from the lifecycle
    rows so an administrator can identify which quota is over limit without a
    second mutable per-Adapter counter.
    """
    setting = setting or get_settings(session)
    capacity = get_capacity(session)
    platform_actual = int(capacity.actual_bytes)
    platform_reserved = int(capacity.reserved_bytes)
    by_adapter = _adapter_usage(session)
    adapter_rows = [
        ManagedInputAdapterUsage(
            adapter_id=adapter_id,
            actual_bytes=actual_bytes,
            reserved_bytes=reserved_bytes,
            total_bytes=actual_bytes + reserved_bytes,
            quota_bytes=setting.adapter_quota_bytes,
            over_quota=actual_bytes + reserved_bytes > setting.adapter_quota_bytes,
        )
        for adapter_id, (actual_bytes, reserved_bytes) in sorted(by_adapter.items())
    ]
    return ManagedInputUsage(
        platform_actual_bytes=platform_actual,
        platform_reserved_bytes=platform_reserved,
        platform_total_bytes=platform_actual + platform_reserved,
        adapters=adapter_rows,
    )


def settings_response(
    session: Session, setting: ManagedInputSettings | None = None
) -> ManagedInputSettingsResponse:
    """Build the administrator response without deployment-only values."""
    setting = setting or get_settings(session)
    usage = current_usage(session, setting)
    platform_over_quota = usage.platform_total_bytes > setting.platform_quota_bytes
    adapter_over_quota = [row.adapter_id for row in usage.adapters if row.over_quota]
    return ManagedInputSettingsResponse(
        id=setting.id,
        default_retention_seconds=setting.default_retention_seconds,
        max_file_bytes=setting.max_file_bytes,
        platform_quota_bytes=setting.platform_quota_bytes,
        adapter_quota_bytes=setting.adapter_quota_bytes,
        allow_manual_delete=setting.allow_manual_delete,
        max_custom_retention_seconds=setting.max_custom_retention_seconds,
        min_free_space_bytes=setting.min_free_space_bytes,
        staged_ttl_seconds=setting.staged_ttl_seconds,
        usage=usage,
        over_quota=platform_over_quota or bool(adapter_over_quota),
        platform_over_quota=platform_over_quota,
        adapter_over_quota=adapter_over_quota,
        created_at=setting.created_at or datetime.now(UTC),
        updated_at=setting.updated_at or datetime.now(UTC),
    )


def update_settings(
    session: Session, data: ManagedInputSettingsUpdate
) -> ManagedInputSettingsResponse:
    """Replace policy values while leaving artifacts and charges untouched."""
    setting = get_settings(session, for_update=True)
    for field, value in data.model_dump().items():
        setattr(setting, field, value)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(
            422,
            "managed_input_settings_invalid",
            "Managed Input settings are invalid",
        ) from None
    session.refresh(setting)
    return settings_response(session, setting)
