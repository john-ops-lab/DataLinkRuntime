"""Batch 1 reliable ingress preparation.

This module creates the PostgreSQL Execution, Admission and Outbox facts in
one transaction when the explicit RabbitMQ ingress gate is enabled.  It does
not consume RabbitMQ messages or create Attempts; those are intentionally
left to the later v3 Worker batch.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import Adapter, AdapterCredentialBinding, Execution, Worker
from dlr.control.services import admission, idempotency, outbox
from dlr.control.services.adapter import domain_error
from dlr.control.services.execution import (
    _create_pending_execution_locked,
)

RETRY_POLICY_KEYS = frozenset(
    {
        "max_attempts",
        "initial_backoff_seconds",
        "multiplier",
        "max_backoff_seconds",
        "jitter_ratio",
        "retryable_error_classes",
    }
)
RETRYABLE_ERROR_CLASSES = ("platform_transient", "worker_lost")
RETRYABLE_ERROR_CLASS_SET = frozenset(RETRYABLE_ERROR_CLASSES)
DEFAULT_RETRY_POLICY: dict[str, object] = {
    "max_attempts": 3,
    "initial_backoff_seconds": 5.0,
    "multiplier": 2.0,
    "max_backoff_seconds": 300.0,
    "jitter_ratio": 0.2,
    "retryable_error_classes": ["platform_transient", "worker_lost"],
}
RESOURCE_PROFILE_CLASS = "standard"
RESOURCE_PROFILE_SCHEMA_VERSION = 1


def validate_retry_policy(policy: Mapping[str, object]) -> dict[str, object]:
    """Validate and return the closed Retry Policy snapshot shape.

    Batch 1 only freezes this data.  Attempt transitions and retry dispatch
    remain disabled until their later Batch.
    """
    if set(policy) != RETRY_POLICY_KEYS:
        raise ValueError("retry policy must contain the closed required fields")
    max_attempts = policy["max_attempts"]
    initial_backoff = policy["initial_backoff_seconds"]
    multiplier = policy["multiplier"]
    max_backoff = policy["max_backoff_seconds"]
    jitter_ratio = policy["jitter_ratio"]
    classes = policy["retryable_error_classes"]
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= 100
    ):
        raise ValueError("retry policy max_attempts must be between 1 and 100")
    numeric = (initial_backoff, multiplier, max_backoff, jitter_ratio)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in numeric):
        raise ValueError("retry policy numeric fields must be finite numbers")
    initial = float(cast(int | float, initial_backoff))
    factor = float(cast(int | float, multiplier))
    maximum = float(cast(int | float, max_backoff))
    jitter = float(cast(int | float, jitter_ratio))
    if not 0 < initial <= 300:
        raise ValueError("retry policy initial_backoff_seconds must be between 0 and 300")
    if not 1 <= factor <= 10:
        raise ValueError("retry policy multiplier must be between 1 and 10")
    if not 0 < maximum <= 3_600:
        raise ValueError("retry policy max_backoff_seconds must be between 0 and 3600")
    if initial > maximum:
        raise ValueError("retry policy initial backoff must not exceed max backoff")
    if not 0 <= jitter <= 0.2:
        raise ValueError("retry policy jitter_ratio must be between 0 and 0.2")
    if (
        not isinstance(classes, list)
        or not classes
        or any(not isinstance(item, str) for item in classes)
        or len(set(classes)) != len(classes)
        or not set(classes).issubset(RETRYABLE_ERROR_CLASS_SET)
    ):
        raise ValueError("retry policy error classes are not conservative and closed")
    return {
        "max_attempts": max_attempts,
        "initial_backoff_seconds": initial,
        "multiplier": factor,
        "max_backoff_seconds": maximum,
        "jitter_ratio": jitter,
        "retryable_error_classes": list(classes),
    }


def default_retry_policy() -> dict[str, object]:
    """Build one immutable policy from the currently validated deployment config."""
    return validate_retry_policy(
        {
            "max_attempts": settings.execution_retry_max_attempts,
            "initial_backoff_seconds": settings.execution_retry_initial_backoff_seconds,
            "multiplier": settings.execution_retry_multiplier,
            "max_backoff_seconds": settings.execution_retry_max_backoff_seconds,
            "jitter_ratio": settings.execution_retry_jitter_ratio,
            "retryable_error_classes": list(RETRYABLE_ERROR_CLASSES),
        }
    )


def default_resource_profile(adapter_timeout_seconds: int | None = None) -> dict[str, object]:
    """Return the closed, immutable ``standard`` profile for one Execution."""

    timeout_seconds = (
        settings.execution_timeout_seconds
        if adapter_timeout_seconds is None
        else adapter_timeout_seconds
    )
    return {
        "schema_version": RESOURCE_PROFILE_SCHEMA_VERSION,
        "resource_class": RESOURCE_PROFILE_CLASS,
        "backend": settings.sandbox_backend,
        "cpu_cores": settings.sandbox_cpu_cores,
        "memory_bytes": settings.sandbox_memory_bytes,
        "pids": settings.sandbox_pids,
        "tmp_bytes": settings.sandbox_tmp_bytes,
        "nofile": settings.sandbox_nofile,
        "execution_timeout_seconds": timeout_seconds,
        "claim_timeout_seconds": settings.execution_claim_timeout_seconds,
        "recovery_grace_seconds": settings.execution_recovery_grace_seconds,
        "workspace_cleanup_attempt_timeout_seconds": (
            settings.workspace_cleanup_attempt_timeout_seconds
        ),
        "workspace_cleanup_total_timeout_seconds": settings.workspace_cleanup_total_timeout_seconds,
        "stream_max_bytes": settings.execution_stream_max_bytes,
        "output_max_bytes": settings.execution_output_max_bytes,
        "output_preview_max_bytes": settings.execution_output_preview_max_bytes,
    }


def resolve_queue_target_worker(session: Session, adapter: Adapter) -> Worker:
    """Validate a fixed target without requiring it to be effectively online."""

    if adapter.runtime_worker_id is None:
        raise domain_error(
            409,
            "runtime_worker_invalid",
            "A fixed runtime Worker is required for reliable dispatch",
        )
    worker = session.get(Worker, adapter.runtime_worker_id)
    if worker is None:
        raise domain_error(
            409,
            "runtime_worker_invalid",
            "The configured runtime Worker does not exist",
        )
    if adapter.language not in worker.capabilities:
        raise domain_error(
            409,
            "runtime_worker_invalid",
            "The configured runtime Worker does not support the Adapter language",
            {"language": adapter.language},
        )
    # RabbitMQ dispatch is a v3 capability.  Registration is still allowed
    # for diagnosis, but a v1/v2 fixed Worker cannot receive a RabbitMQ row.
    if int(worker.protocol_version or 1) < 3:
        raise domain_error(
            409,
            "runtime_worker_invalid",
            "The configured runtime Worker does not support RabbitMQ dispatch",
        )
    return worker


def _db_now(session: Session) -> datetime:
    from dlr.control.services.input_config import database_now

    return database_now(session)


def _credential_bindings_snapshot(session: Session, adapter_id: int) -> list[dict[str, object]]:
    """Freeze binding references while the owning Adapter row is locked."""
    bindings = session.scalars(
        select(AdapterCredentialBinding)
        .where(AdapterCredentialBinding.adapter_id == adapter_id)
        .order_by(AdapterCredentialBinding.env_key, AdapterCredentialBinding.id)
        .with_for_update()
    ).all()
    return [
        {
            "binding_id": binding.id,
            "credential_id": binding.credential_id,
            "env_key": binding.env_key,
            "field": binding.field,
        }
        for binding in bindings
    ]


def accept_execution(
    session: Session,
    adapter: Adapter,
    *,
    trigger: str,
    runtime_input: object,
    input_source_type: str,
    input_config_revision: int,
    input_snapshot: dict[str, Any],
    artifact_ids: tuple[int, ...] = (),
    scheduled_for: datetime | None = None,
    idempotency_key: str | None = None,
    idempotency_body: Any = None,
    idempotency_lookup: idempotency.IdempotencyLookup | None = None,
    schedule_policy_snapshot: dict[str, object] | None = None,
    canary: bool = False,
    version_id: int | None = None,
) -> Execution:
    """Atomically accept one gated RabbitMQ Execution or its idempotent hit."""

    if not settings.rabbitmq_execution_enabled and not (
        canary and settings.rabbitmq_execution_canary_enabled
    ):
        raise RuntimeError("RabbitMQ reliable ingress is disabled")
    from dlr.control.services import rabbitmq

    if not rabbitmq.ingress_configuration_ready(session, allow_disabled=canary):
        raise domain_error(
            503,
            "rabbitmq_not_ready",
            "Reliable RabbitMQ runtime is not verified",
            {"retry_after": 1},
        )
    if trigger == "schedule" and schedule_policy_snapshot is None:
        raise domain_error(
            409,
            "schedule_snapshot_unavailable",
            "Schedule policy snapshot is unavailable",
        )
    try:
        retry_policy = default_retry_policy()
    except ValueError:
        raise domain_error(
            503,
            "runtime_configuration_invalid",
            "Reliable runtime Retry Policy configuration is invalid",
        ) from None
    locked_adapter = session.get(Adapter, adapter.id, with_for_update=True)
    if locked_adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    adapter = locked_adapter
    if adapter.archived_at is not None:
        raise domain_error(409, "adapter_deleted", "Adapter is deleted")
    if adapter.latest_version_id is None:
        raise domain_error(409, "adapter_has_no_version", "Adapter has no saved Revision yet")

    lookup = idempotency_lookup or idempotency.lookup(
        session, adapter.id, trigger, idempotency_body, idempotency_key
    )
    if lookup.record is not None:
        existing = session.get(Execution, lookup.record.execution_id)
        if existing is None:
            # The FK/retention contract should make this impossible.  Refuse
            # to create a second responsibility instead of hiding corruption.
            raise domain_error(
                409, "idempotency_record_invalid", "Idempotency record is unavailable"
            )
        return existing

    worker = resolve_queue_target_worker(session, adapter)
    if not rabbitmq.ingress_configuration_ready(
        session, worker_id=worker.id, allow_disabled=canary
    ):
        raise domain_error(
            503,
            "rabbitmq_not_ready",
            "Reliable RabbitMQ topology is not verified for the target Worker",
            {"retry_after": 1},
        )
    credential_bindings_snapshot = _credential_bindings_snapshot(session, adapter.id)
    try:
        logical_bytes = admission.logical_input_bytes(
            input_source_type,
            runtime_input,
            input_snapshot,
        )
    except ValueError:
        raise domain_error(
            422,
            "input_invalid",
            "Request input is outside the canonical JSON number domain",
        ) from None
    admission.reserve_admission(session, adapter.id, logical_bytes)
    if input_source_type == "managed_files":
        from dlr.control.services.attempt import ensure_managed_file_hold_capacity

        ensure_managed_file_hold_capacity(session)
    now = _db_now(session)
    execution = _create_pending_execution_locked(
        session,
        adapter,
        trigger=trigger,
        runtime_input=runtime_input,
        input_source_type=input_source_type,
        input_config_revision=input_config_revision,
        input_snapshot=input_snapshot,
        target_worker_id=worker.id,
        scheduled_for=scheduled_for,
        artifact_ids=artifact_ids,
        dispatch_backend="rabbitmq",
        dispatch_generation=1,
        logical_input_bytes=logical_bytes,
        max_attempts_snapshot=cast(int, retry_policy["max_attempts"]),
        retry_policy_snapshot=retry_policy,
        resource_profile_snapshot=default_resource_profile(adapter.timeout_seconds),
        credential_bindings_snapshot=credential_bindings_snapshot,
        schedule_policy_snapshot=schedule_policy_snapshot,
        resource_class=RESOURCE_PROFILE_CLASS,
        version_id_override=version_id,
    )
    # Keep the DB clock as the accepted-at source for all generated facts.
    execution.queued_at = now
    if lookup.record is not None:  # pragma: no cover - handled above
        raise AssertionError("duplicate idempotency record")
    if idempotency_key is not None:
        record = idempotency.create_record(
            session,
            adapter_id=adapter.id,
            execution_id=execution.id,
            key_digest=lookup.key_hash,
            request_digest=lookup.payload_hash,
            now=now,
        )
        execution.idempotency_record_id = record.id
    outbox.create_dispatch_outbox(session, execution, available_at=now)
    # The row is flushed before this check, so the protection line is charged
    # with its exact serialized payload bytes rather than the configured
    # maximum.  The transaction rolls back the provisional facts if the
    # complete row would exceed the line.
    outbox.require_outbox_capacity(session, additional_count=0, additional_bytes=0)
    return execution
