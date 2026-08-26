"""Domain service for the Adapter-level current input object."""

from typing import cast

from sqlalchemy import JSON, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.control.input_errors import InputConfigErrorCode
from dlr.control.models import Adapter, AdapterInputConfig
from dlr.control.schemas.input_config import (
    AdapterInputConfigResponse,
    AdapterInputConfigUpsert,
    InputRetention,
    InputRetentionMode,
)
from dlr.control.services import adapter_runtime
from dlr.control.services.adapter import domain_error


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
            "input_config_not_initialized",
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
    config = session.scalar(
        select(AdapterInputConfig)
        .where(AdapterInputConfig.adapter_id == adapter_id)
        .with_for_update()
    )
    if config is None:
        raise domain_error(
            409,
            "input_config_not_initialized",
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

    config.source_type = data.source_type
    if data.source_type == "json":
        # A Python None must be persisted as JSON null, not SQL NULL; the
        # latter means "no JSON field" and is reserved for non-JSON sources.
        config.json_value = JSON.NULL if data.json_value is None else data.json_value
    else:
        config.json_value = None
    if data.source_type == "managed_files":
        assert data.retention is not None
        config.retention_mode = data.retention.mode
        config.retention_seconds = data.retention.seconds
    else:
        config.retention_mode = "system_default"
        config.retention_seconds = None
    config.revision += 1
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
