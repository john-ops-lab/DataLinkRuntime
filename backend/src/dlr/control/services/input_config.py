"""Domain service for the Adapter-level current input object."""

from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import JSON, null, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.input_errors import InputConfigErrorCode
from dlr.control.models import Adapter, AdapterInputConfig, AdapterSchedule
from dlr.control.schemas.input_config import (
    AdapterInputConfigResponse,
    AdapterInputConfigUpsert,
    InputRetention,
    InputRetentionMode,
)
from dlr.control.services import adapter_runtime
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


def _validity(config: AdapterInputConfig) -> tuple[bool, str | None]:
    if config.source_type == "none":
        return True, None
    if config.source_type == "json":
        return True, None
    if config.source_type == "managed_files":
        # Artifact bindings arrive in the following Managed Input waves. A0
        # intentionally represents only the legal empty saved state.
        return False, "managed_files_empty"
    if config.source_type == "remote_files":
        return False, InputConfigErrorCode.SOURCE_NOT_AVAILABLE.value
    return False, InputConfigErrorCode.INVALID.value


def _resolve_config(config: AdapterInputConfig, *, override: object = _UNSET) -> ResolvedInput:
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
        raise domain_error(
            422,
            InputConfigErrorCode.INVALID.value,
            "Managed file input is not ready to run",
            {"reason": "managed_files_empty"},
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
    return _resolve_config(config, override=override)


def validate_saved_config(config: AdapterInputConfig) -> None:
    """Apply the saved-input run gate without changing any state."""
    _resolve_config(config)


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


def input_config_response(config: AdapterInputConfig) -> AdapterInputConfigResponse:
    """Build a public response without operational artifact material."""
    valid_for_run, invalid_reason = _validity(config)
    return AdapterInputConfigResponse(
        adapter_id=config.adapter_id,
        revision=config.revision,
        source_type=config.source_type,  # type: ignore[arg-type]
        json_value=config.json_value,
        retention=_retention_response(config),
        artifacts=[],
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


def _validate_managed_files_payload(data: AdapterInputConfigUpsert) -> None:
    if data.artifact_ids:
        # A0 deliberately does not accept an artifact reference before the
        # Managed Input Store exists. Empty managed_files remains saveable.
        raise domain_error(
            422,
            InputConfigErrorCode.SOURCE_NOT_AVAILABLE.value,
            "Managed file input is not available yet",
        )


def upsert_input_config(
    session: Session, adapter_id: int, data: AdapterInputConfigUpsert
) -> AdapterInputConfig:
    """Apply one optimistic-revision InputConfig update atomically."""
    adapter = _get_task_adapter(session, adapter_id, for_update=True)
    adapter_runtime.require_runtime_unlocked(session, adapter)
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

    if data.source_type == "remote_files":
        raise domain_error(
            422,
            InputConfigErrorCode.SOURCE_NOT_AVAILABLE.value,
            "Remote file input is not available yet",
        )
    if data.source_type == "managed_files":
        _validate_managed_files_payload(data)
    if data.source_type == "json":
        input_size = len(compact_json_bytes(data.json_value))
        if input_size > settings.execution_input_max_bytes:
            raise domain_error(
                413,
                "execution_input_too_large",
                f"Input exceeds the {settings.execution_input_max_bytes} byte limit",
                {"max_bytes": settings.execution_input_max_bytes},
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
    config.revision += 1
    # Keep the old column as a rollback mirror while it still exists.  The
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
