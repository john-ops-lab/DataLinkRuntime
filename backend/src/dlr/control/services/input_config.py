"""Domain service for the Adapter-level current input object."""

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import JSON, delete, func, null, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.input_errors import InputConfigErrorCode, ManagedInputErrorCode
from dlr.control.models import (
    Adapter,
    AdapterInputArtifactBinding,
    AdapterInputConfig,
    AdapterSchedule,
    Execution,
    ManagedInputArtifact,
    ManagedInputArtifactStatus,
    ManagedInputSettings,
)
from dlr.control.schemas.input_config import (
    AdapterInputArtifactSummary,
    AdapterInputConfigResponse,
    AdapterInputConfigUpsert,
    InputRetention,
    InputRetentionMode,
)
from dlr.control.services import adapter_runtime
from dlr.control.services import managed_input as managed_input_service
from dlr.control.services.adapter import domain_error
from dlr.control.services.execution import compact_json_bytes

_UNSET = object()


@dataclass(frozen=True)
class ResolvedInput:
    """The immutable input facts used while creating one Execution."""

    runtime_input: object
    source_type: str
    revision: int
    snapshot: dict[str, Any]


def _retention_response(config: AdapterInputConfig) -> InputRetention:
    return InputRetention(
        mode=cast(InputRetentionMode, config.retention_mode),
        seconds=config.retention_seconds,
    )


def database_now(session: Session) -> datetime:
    """Read the authoritative current time from the PostgreSQL server."""
    value = session.scalar(select(func.clock_timestamp()))
    if value is None:  # pragma: no cover - PostgreSQL always returns a value
        raise RuntimeError("Database clock did not return a timestamp")
    timestamp = cast(datetime, value)
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _status_value(status: object) -> str:
    value = getattr(status, "value", status)
    return str(value)


def _normalized_filename(filename: str) -> str:
    """Normalize only the display basename used for current-set conflicts."""
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    return unicodedata.normalize("NFC", basename).casefold()


def _artifact_run_reason(artifact: ManagedInputArtifact, *, now: datetime) -> str | None:
    if _status_value(artifact.status) != ManagedInputArtifactStatus.READY.value:
        return "artifact_not_ready"
    if artifact.expires_at is not None and _as_utc(artifact.expires_at) <= now:
        return "artifact_expired"
    # A READY row without the checksum produced by the upload boundary is
    # corrupt metadata. It is never handed to an Execution as a runnable
    # input, while lifecycle governance may still quarantine it later.
    if (
        artifact.sha256 is None
        or len(artifact.sha256) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in artifact.sha256)
    ):
        return "artifact_corrupt"
    return None


def _validity(
    config: AdapterInputConfig,
    artifacts: Sequence[ManagedInputArtifact] | None = None,
    *,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    if config.source_type == "none":
        return True, None
    if config.source_type == "json":
        return True, None
    if config.source_type == "managed_files":
        current = list(artifacts or [])
        if not current:
            return False, "managed_files_empty"
        if not settings.managed_files_enabled:
            return False, InputConfigErrorCode.SOURCE_NOT_AVAILABLE.value
        current_now = now or datetime.now(UTC)
        for artifact in current:
            reason = _artifact_run_reason(artifact, now=current_now)
            if reason is not None:
                return False, reason
        return True, None
    if config.source_type == "remote_files":
        return False, InputConfigErrorCode.SOURCE_NOT_AVAILABLE.value
    return False, InputConfigErrorCode.INVALID.value


def _resolve_config(
    config: AdapterInputConfig,
    *,
    override: object = _UNSET,
    artifacts: Sequence[ManagedInputArtifact] | None = None,
    now: datetime | None = None,
) -> ResolvedInput:
    """Resolve one locked config without consulting the legacy Schedule column."""
    if override is not _UNSET:
        if len(compact_json_bytes(override)) > settings.execution_input_max_bytes:
            raise domain_error(
                413,
                "execution_input_too_large",
                f"Input exceeds the {settings.execution_input_max_bytes} byte limit",
                {"max_bytes": settings.execution_input_max_bytes},
            )
        return ResolvedInput(
            runtime_input=override,
            source_type="json",
            revision=config.revision,
            snapshot={"source_type": "json", "revision": config.revision},
        )
    if config.source_type == "remote_files":
        raise domain_error(
            422,
            InputConfigErrorCode.SOURCE_NOT_AVAILABLE.value,
            "Remote file input is not available yet",
        )
    if config.source_type == "managed_files":
        current = list(artifacts or [])
        valid_for_run, invalid_reason = _validity(config, current, now=now)
        if not valid_for_run:
            raise domain_error(
                422,
                InputConfigErrorCode.INVALID.value,
                "Managed file input is not ready to run",
                {"reason": invalid_reason or InputConfigErrorCode.INVALID.value},
            )
        snapshot_artifacts = [
            {
                "ordinal": ordinal,
                "original_filename": artifact.original_filename,
                "content_type": artifact.content_type,
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
            }
            for ordinal, artifact in enumerate(current)
        ]
        return ResolvedInput(
            runtime_input=None,
            source_type="managed_files",
            revision=config.revision,
            snapshot={
                "source_type": "managed_files",
                "revision": config.revision,
                "artifacts": snapshot_artifacts,
            },
        )
    if config.source_type not in {"none", "json"}:
        raise domain_error(
            422,
            InputConfigErrorCode.INVALID.value,
            "Input configuration is invalid",
        )

    source_type = config.source_type
    runtime_input: object = config.json_value if source_type == "json" else None
    if (
        source_type == "json"
        and len(compact_json_bytes(runtime_input)) > settings.execution_input_max_bytes
    ):
        raise domain_error(
            413,
            "execution_input_too_large",
            f"Input exceeds the {settings.execution_input_max_bytes} byte limit",
            {"max_bytes": settings.execution_input_max_bytes},
        )
    return ResolvedInput(
        runtime_input=runtime_input,
        source_type=source_type,
        revision=config.revision,
        snapshot={"source_type": source_type, "revision": config.revision},
    )


def resolve_for_execution(
    session: Session, adapter_id: int, *, override: object = _UNSET
) -> ResolvedInput:
    """Resolve the saved Adapter input under its row lock.

    Manual, run-now and Scheduler callers all use this entry point.  The
    optional override exists only for the explicitly enabled legacy window;
    it never writes the current AdapterInputConfig.
    """
    # Execution callers already hold this lock; taking it again keeps direct
    # resolver callers on the same global order and makes the invariant
    # explicit: Adapter -> InputConfig -> Binding -> Artifact.
    _get_task_adapter(session, adapter_id, for_update=True)
    config = session.scalar(
        select(AdapterInputConfig)
        .where(AdapterInputConfig.adapter_id == adapter_id)
        .with_for_update()
    )
    if config is None:
        raise domain_error(
            409,
            InputConfigErrorCode.NOT_INITIALIZED.value,
            "Adapter input configuration is not initialized",
        )
    if override is not _UNSET:
        return _resolve_config(config, override=override)
    bindings = _lock_current_bindings_and_artifacts(session, adapter_id)
    return _resolve_config(
        config,
        artifacts=[artifact for _, artifact in bindings],
        now=database_now(session),
    )


def validate_saved_config(config: AdapterInputConfig, *, session: Session | None = None) -> None:
    """Apply the saved-input run gate without changing any state."""
    if session is None or config.source_type != "managed_files":
        _resolve_config(config)
        return
    bindings = _lock_current_bindings_and_artifacts(session, config.adapter_id)
    _resolve_config(
        config,
        artifacts=[artifact for _, artifact in bindings],
        now=database_now(session),
    )


def _set_json_config(config: AdapterInputConfig, value: object) -> None:
    """Set a legacy-mirrored JSON value on a locked config and advance once."""
    config.source_type = "json"
    config.json_value = JSON.NULL if value is None else value
    config.retention_mode = "system_default"
    config.retention_seconds = None
    config.revision += 1


def _legacy_schedule_value(config: AdapterInputConfig) -> object:
    """Return the compatibility mirror value for the old Schedule column."""
    return config.json_value if config.source_type == "json" else None


def _artifact_summary(
    artifact: ManagedInputArtifact, *, ordinal: int
) -> AdapterInputArtifactSummary:
    return AdapterInputArtifactSummary(
        id=artifact.id,
        ordinal=ordinal,
        original_filename=artifact.original_filename,
        content_type=artifact.content_type,
        size_bytes=artifact.size_bytes,
        sha256=artifact.sha256,
        status=_status_value(artifact.status),
        retention_mode=cast(InputRetentionMode, artifact.retention_mode),
        expires_at=artifact.expires_at,
    )


def _current_bindings(
    session: Session, adapter_id: int
) -> list[tuple[AdapterInputArtifactBinding, ManagedInputArtifact]]:
    rows = session.execute(
        select(AdapterInputArtifactBinding, ManagedInputArtifact)
        .join(
            ManagedInputArtifact,
            (ManagedInputArtifact.id == AdapterInputArtifactBinding.artifact_id)
            & (ManagedInputArtifact.adapter_id == AdapterInputArtifactBinding.adapter_id),
        )
        .where(AdapterInputArtifactBinding.adapter_id == adapter_id)
        .order_by(AdapterInputArtifactBinding.ordinal)
    ).all()
    return [(binding, artifact) for binding, artifact in rows]


def input_config_response(
    config: AdapterInputConfig, *, session: Session | None = None
) -> AdapterInputConfigResponse:
    """Build a public response without operational artifact material."""
    current = _current_bindings(session, config.adapter_id) if session is not None else []
    current_artifacts = [artifact for _, artifact in current]
    now = (
        database_now(session)
        if session is not None and config.source_type == "managed_files"
        else None
    )
    valid_for_run, invalid_reason = _validity(config, current_artifacts, now=now)
    return AdapterInputConfigResponse(
        adapter_id=config.adapter_id,
        revision=config.revision,
        source_type=config.source_type,  # type: ignore[arg-type]
        json_value=config.json_value,
        retention=_retention_response(config),
        artifacts=[
            _artifact_summary(artifact, ordinal=binding.ordinal) for binding, artifact in current
        ],
        valid_for_run=valid_for_run,
        invalid_reason=invalid_reason,
    )


def _get_task_adapter(session: Session, adapter_id: int, *, for_update: bool) -> Adapter:
    query = select(Adapter).where(Adapter.id == adapter_id)
    if for_update:
        query = query.with_for_update()
    adapter = session.scalar(query)
    if adapter is None or adapter.archived_at is not None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    if adapter.adapter_type != "task":
        raise domain_error(
            409,
            "adapter_type_mismatch",
            "Only task Adapters have an input configuration",
        )
    return adapter


def get_input_config(session: Session, adapter_id: int) -> AdapterInputConfig:
    """Return the current Task input configuration."""
    _get_task_adapter(session, adapter_id, for_update=False)
    config = session.get(AdapterInputConfig, adapter_id)
    if config is None:
        # The expand migration creates this row for every historical Task;
        # absence indicates an incomplete deployment rather than a new input
        # choice, so do not silently invent a second source of truth on GET.
        raise domain_error(
            409,
            InputConfigErrorCode.NOT_INITIALIZED.value,
            "Adapter input configuration is not initialized",
        )
    return config


def _lock_binding_rows(session: Session, adapter_id: int) -> list[AdapterInputArtifactBinding]:
    """Lock current bindings in deterministic Artifact-id order."""
    return list(
        session.scalars(
            select(AdapterInputArtifactBinding)
            .where(AdapterInputArtifactBinding.adapter_id == adapter_id)
            .order_by(AdapterInputArtifactBinding.artifact_id)
            .with_for_update()
        ).all()
    )


def _lock_artifacts(
    session: Session, adapter_id: int, artifact_ids: Sequence[int]
) -> list[ManagedInputArtifact]:
    """Lock only same-Adapter Artifacts, always by ascending id."""
    if not artifact_ids:
        return []
    return list(
        session.scalars(
            select(ManagedInputArtifact)
            .where(
                ManagedInputArtifact.adapter_id == adapter_id,
                ManagedInputArtifact.id.in_(artifact_ids),
            )
            .order_by(ManagedInputArtifact.id)
            .with_for_update()
        ).all()
    )


def _lock_current_bindings_and_artifacts(
    session: Session, adapter_id: int
) -> list[tuple[AdapterInputArtifactBinding, ManagedInputArtifact]]:
    """Lock Binding rows then their Artifacts in the platform lock order."""
    bindings = _lock_binding_rows(session, adapter_id)
    artifacts = _lock_artifacts(session, adapter_id, [row.artifact_id for row in bindings])
    artifacts_by_id = {artifact.id: artifact for artifact in artifacts}
    if len(artifacts_by_id) != len(bindings):
        raise domain_error(
            409,
            InputConfigErrorCode.INVALID.value,
            "Current input binding is invalid",
            {"reason": ManagedInputErrorCode.ARTIFACT_NOT_FOUND.value},
        )
    return [
        (binding, artifacts_by_id[binding.artifact_id])
        for binding in sorted(bindings, key=lambda row: row.ordinal)
    ]


def _require_user_runtime_unlocked(
    session: Session, adapter: Adapter, schedule: AdapterSchedule | None
) -> None:
    """Check runtime state after the Adapter/Schedule/Config locks are held."""
    if (schedule is not None and schedule.enabled) or adapter_runtime.active_execution(
        session, adapter.id
    ) is not None:
        raise domain_error(
            409,
            "adapter_runtime_locked",
            "Stop the Adapter and wait for its active Execution to finish before changing "
            "runtime configuration",
        )


def _validate_retention(data: AdapterInputConfigUpsert, *, setting: ManagedInputSettings) -> None:
    """Validate the request against the locked database policy singleton."""
    assert data.retention is not None
    mode = data.retention.mode
    if mode == "custom":
        max_seconds = setting.max_custom_retention_seconds
        if data.retention.seconds is None or data.retention.seconds > max_seconds:
            raise domain_error(
                422,
                InputConfigErrorCode.INVALID.value,
                "Custom retention exceeds the Managed Input policy",
                {"reason": "retention_out_of_range", "max_seconds": max_seconds},
            )
    elif mode == "manual_delete" and not setting.allow_manual_delete:
        raise domain_error(
            422,
            InputConfigErrorCode.INVALID.value,
            "Manual deletion retention is disabled by policy",
            {"reason": "manual_delete_not_allowed"},
        )


def _retention_expiry(
    data: AdapterInputConfigUpsert, *, setting: ManagedInputSettings, now: datetime
) -> datetime | None:
    assert data.retention is not None
    if data.retention.mode == "manual_delete":
        return None
    if data.retention.mode == "custom":
        assert data.retention.seconds is not None
        seconds = data.retention.seconds
    else:
        seconds = setting.default_retention_seconds
    return now + timedelta(seconds=seconds)


def _validate_managed_files_payload(
    data: AdapterInputConfigUpsert,
    artifacts: Sequence[ManagedInputArtifact],
    *,
    setting: ManagedInputSettings,
    now: datetime,
) -> None:
    artifact_ids = data.artifact_ids or []
    if len(artifact_ids) > 8:
        raise domain_error(
            422,
            InputConfigErrorCode.INVALID.value,
            "At most eight managed input files may be bound",
            {"reason": "managed_files_limit", "max_files": 8},
        )
    if artifact_ids and not settings.managed_files_enabled:
        raise domain_error(
            422,
            InputConfigErrorCode.SOURCE_NOT_AVAILABLE.value,
            "Managed file input is not available yet",
        )
    _validate_retention(data, setting=setting)
    if not artifact_ids:
        return

    names: set[str] = set()
    for artifact in artifacts:
        status = _status_value(artifact.status)
        if status not in {
            ManagedInputArtifactStatus.STAGED.value,
            ManagedInputArtifactStatus.READY.value,
        }:
            raise domain_error(
                422,
                ManagedInputErrorCode.ARTIFACT_NOT_READY.value,
                "Input Artifact is not ready to bind",
                {"reason": "artifact_not_ready"},
            )
        if artifact.expires_at is not None and _as_utc(artifact.expires_at) <= now:
            raise domain_error(
                422,
                InputConfigErrorCode.INVALID.value,
                "Input Artifact has expired",
                {"reason": "artifact_expired"},
            )
        normalized = _normalized_filename(artifact.original_filename)
        if normalized in names:
            raise domain_error(
                422,
                InputConfigErrorCode.INVALID.value,
                "Managed input file names must be unique",
                {"reason": "artifact_name_conflict"},
            )
        names.add(normalized)


def upsert_input_config(
    session: Session, adapter_id: int, data: AdapterInputConfigUpsert
) -> AdapterInputConfig:
    """Apply one optimistic-revision InputConfig update atomically."""
    adapter = _get_task_adapter(session, adapter_id, for_update=True)
    schedule = session.scalar(
        select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter_id).with_for_update()
    )
    config = session.scalar(
        select(AdapterInputConfig)
        .where(AdapterInputConfig.adapter_id == adapter_id)
        .with_for_update()
    )
    if config is None:
        raise domain_error(
            409,
            InputConfigErrorCode.NOT_INITIALIZED.value,
            "Adapter input configuration is not initialized",
        )
    if config.revision != data.expected_revision:
        raise domain_error(
            409,
            InputConfigErrorCode.REVISION_CONFLICT.value,
            "Adapter input configuration revision is stale",
            {"expected_revision": data.expected_revision, "current_revision": config.revision},
        )
    _require_user_runtime_unlocked(session, adapter, schedule)

    if data.source_type == "remote_files":
        raise domain_error(
            422,
            InputConfigErrorCode.SOURCE_NOT_AVAILABLE.value,
            "Remote file input is not available yet",
        )
    if data.source_type == "json":
        input_size = len(compact_json_bytes(data.json_value))
        if input_size > settings.execution_input_max_bytes:
            raise domain_error(
                413,
                "execution_input_too_large",
                f"Input exceeds the {settings.execution_input_max_bytes} byte limit",
                {"max_bytes": settings.execution_input_max_bytes},
            )

    current_bindings = _lock_binding_rows(session, adapter_id)
    current_ids = [row.artifact_id for row in current_bindings]
    selected_ids = list(data.artifact_ids or []) if data.source_type == "managed_files" else []
    all_ids = sorted(set(current_ids).union(selected_ids))
    all_artifacts = _lock_artifacts(session, adapter_id, all_ids)
    artifacts_by_id = {artifact.id: artifact for artifact in all_artifacts}
    if len(artifacts_by_id) != len(all_ids):
        raise domain_error(
            404,
            ManagedInputErrorCode.ARTIFACT_NOT_FOUND.value,
            "Input Artifact not found",
        )
    selected_artifacts = [artifacts_by_id[artifact_id] for artifact_id in selected_ids]
    setting = None
    now = None
    if data.source_type == "managed_files":
        setting = managed_input_service.get_settings(session, for_update=True)
        now = database_now(session)
        _validate_managed_files_payload(data, selected_artifacts, setting=setting, now=now)

    new_revision = config.revision + 1
    selected_id_set = set(selected_ids)
    for artifact_id in current_ids:
        if artifact_id not in selected_id_set:
            artifact = artifacts_by_id[artifact_id]
            if _status_value(artifact.status) in {
                ManagedInputArtifactStatus.STAGED.value,
                ManagedInputArtifactStatus.READY.value,
            }:
                artifact.status = ManagedInputArtifactStatus.PENDING_DELETE

    if data.source_type == "managed_files":
        assert setting is not None and now is not None and data.retention is not None
        expiry = _retention_expiry(data, setting=setting, now=now)
        for artifact in selected_artifacts:
            artifact.status = ManagedInputArtifactStatus.READY
            artifact.retention_mode = data.retention.mode
            artifact.expires_at = expiry

    session.execute(
        delete(AdapterInputArtifactBinding).where(
            AdapterInputArtifactBinding.adapter_id == adapter_id
        )
    )
    if data.source_type == "managed_files":
        for ordinal, artifact_id in enumerate(selected_ids):
            session.add(
                AdapterInputArtifactBinding(
                    adapter_id=adapter_id,
                    artifact_id=artifact_id,
                    input_config_revision=new_revision,
                    ordinal=ordinal,
                )
            )

    config.source_type = data.source_type
    if data.source_type == "json":
        # A Python None must be persisted as JSON null, not SQL NULL; the
        # latter means "no JSON field" and is reserved for non-JSON sources.
        config.json_value = JSON.NULL if data.json_value is None else data.json_value
    else:
        config.json_value = null()
    if data.source_type == "managed_files":
        assert data.retention is not None
        config.retention_mode = data.retention.mode
        config.retention_seconds = data.retention.seconds
    else:
        config.retention_mode = "system_default"
        config.retention_seconds = None
    config.revision = new_revision
    # Keep the old column as a rollback mirror while it still exists. The
    # Scheduler never reads this value; both writes are in this transaction.
    if schedule is not None:
        schedule.input = _legacy_schedule_value(config)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(
            422,
            InputConfigErrorCode.INVALID.value,
            "Input configuration is invalid",
        ) from None
    session.refresh(config)
    return config


def reconcile_current_bindings(
    session: Session, adapter_id: int, *, now: datetime | None = None
) -> AdapterInputConfig:
    """System-only lifecycle transition for expired or corrupt current files.

    This is deliberately only the metadata transition. It never deletes a
    Blob and it never rewrites an active Execution; the later GC/Lease wave
    owns those operations.
    """
    _get_task_adapter(session, adapter_id, for_update=True)
    session.scalar(
        select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter_id).with_for_update()
    )
    config = session.scalar(
        select(AdapterInputConfig)
        .where(AdapterInputConfig.adapter_id == adapter_id)
        .with_for_update()
    )
    if config is None:
        raise domain_error(
            409,
            InputConfigErrorCode.NOT_INITIALIZED.value,
            "Adapter input configuration is not initialized",
        )
    if config.source_type != "managed_files":
        session.commit()
        session.refresh(config)
        return config

    binding_rows = _lock_binding_rows(session, adapter_id)
    artifact_ids = [row.artifact_id for row in binding_rows]
    artifacts = _lock_artifacts(session, adapter_id, artifact_ids)
    artifacts_by_id = {artifact.id: artifact for artifact in artifacts}
    if len(artifacts_by_id) != len(binding_rows):
        raise domain_error(
            409,
            InputConfigErrorCode.INVALID.value,
            "Current input binding is invalid",
            {"reason": ManagedInputErrorCode.ARTIFACT_NOT_FOUND.value},
        )
    # Keep active Execution rows locked after Artifact rows. The Execution
    # snapshot is immutable and there is no Lease table in this B2 baseline;
    # leaving the active row and its snapshot untouched is the protection.
    session.scalars(
        select(Execution)
        .where(
            Execution.adapter_id == adapter_id,
            Execution.status.in_(("pending", "running")),
        )
        .order_by(Execution.id)
        .with_for_update()
    ).all()
    current_now = now or database_now(session)
    expired_ids = [
        artifact.id
        for artifact in artifacts
        if _artifact_run_reason(artifact, now=current_now) is not None
    ]
    if not expired_ids:
        session.commit()
        session.refresh(config)
        return config

    new_revision = config.revision + 1
    session.execute(
        delete(AdapterInputArtifactBinding).where(
            AdapterInputArtifactBinding.adapter_id == adapter_id,
            AdapterInputArtifactBinding.artifact_id.in_(expired_ids),
        )
    )
    for binding in binding_rows:
        if binding.artifact_id not in expired_ids:
            binding.input_config_revision = new_revision
    for artifact_id in expired_ids:
        artifact = artifacts_by_id[artifact_id]
        if _status_value(artifact.status) not in {
            ManagedInputArtifactStatus.DELETED.value,
            ManagedInputArtifactStatus.DELETING.value,
        }:
            artifact.status = ManagedInputArtifactStatus.PENDING_DELETE
    config.revision = new_revision
    session.commit()
    session.refresh(config)
    return config


def expire_current_bindings(
    session: Session, adapter_id: int, *, now: datetime | None = None
) -> AdapterInputConfig:
    """Compatibility name for the system lifecycle transition hook."""
    return reconcile_current_bindings(session, adapter_id, now=now)
