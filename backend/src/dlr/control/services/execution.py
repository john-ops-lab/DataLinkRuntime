"""Domain service for the Execution lifecycle (create / query / result).

Owns version pinning, the input size gate, the server-side re-validation of
every big-field contract reported by a Worker, the M3 best-effort progress
appends and the cursor-paged execution history.
"""

import json
import logging
from collections import Counter
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.common.bigfields import truncate_utf8
from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    AdapterVersion,
    Execution,
    ExecutionCredentialBindingSnapshot,
    ExecutionInputArtifactLease,
    Worker,
)
from dlr.control.schemas.execution import (
    ExecutionCreate,
    ExecutionResultReport,
    ExecutionSummary,
)
from dlr.control.services.adapter import domain_error
from dlr.control.services.execution_cancellation import (
    lock_execution_in_admission_order,
    request_cancellation,
)
from dlr.control.services.locale import get_system_locale

logger = logging.getLogger("dlr.control.execution")

# Logical Execution states are distinct from individual Attempt failures.
RABBITMQ_TERMINAL_STATUSES = frozenset({"succeeded", "dead_letter", "cancelled", "expired"})
TERMINAL_STATUSES = RABBITMQ_TERMINAL_STATUSES

# Execution history pagination contract (M3 spec §5).
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 100


class _NoInputOverride:
    """Sentinel distinguishing an omitted request field from JSON null."""


NO_INPUT_OVERRIDE = _NoInputOverride()

# This is intentionally a small in-process metric until the platform metrics
# sink is introduced.  The stable key is useful to tests and operators, while
# the log never includes the legacy value itself.
LEGACY_INPUT_COMPAT_METRICS: Counter[str] = Counter()


def integrity_constraint_name(error: IntegrityError) -> str | None:
    """Return a PostgreSQL constraint name without parsing driver messages."""
    diagnostic = getattr(getattr(error, "orig", None), "diag", None)
    value = getattr(diagnostic, "constraint_name", None)
    return str(value) if value else None


def compact_json_bytes(value: object) -> bytes:
    """Compact JSON serialization as UTF-8 bytes (the big-field unit)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def release_execution_leases(session: Session, execution_id: int) -> None:
    """Release all file Leases for one locked Execution."""
    session.execute(
        delete(ExecutionInputArtifactLease).where(
            ExecutionInputArtifactLease.execution_id == int(execution_id)
        )
    )


def _persist_credential_binding_snapshots(
    session: Session,
    execution: Execution,
    snapshots: Sequence[dict[str, object]],
) -> None:
    """Persist the closed credential reference set beside the JSON snapshot."""
    rows: list[ExecutionCredentialBindingSnapshot] = []
    required_keys = {"binding_id", "credential_id", "env_key", "field"}
    for snapshot in snapshots:
        if set(snapshot) != required_keys:
            raise RuntimeError("invalid credential binding snapshot")
        binding_id = snapshot["binding_id"]
        credential_id = snapshot["credential_id"]
        env_key = snapshot["env_key"]
        field = snapshot["field"]
        if (
            isinstance(binding_id, bool)
            or not isinstance(binding_id, int)
            or binding_id <= 0
            or isinstance(credential_id, bool)
            or not isinstance(credential_id, int)
            or credential_id <= 0
            or not isinstance(env_key, str)
            or not env_key
            or not isinstance(field, str)
            or not field
        ):
            raise RuntimeError("invalid credential binding snapshot")
        rows.append(
            ExecutionCredentialBindingSnapshot(
                execution_id=execution.id,
                binding_id=binding_id,
                credential_id=credential_id,
                env_key=env_key,
                field=field,
            )
        )
    if rows:
        session.add_all(rows)
        session.flush()


def _create_pending_execution_locked(
    session: Session,
    adapter: Adapter,
    *,
    trigger: str,
    runtime_input: object,
    input_source_type: str,
    input_config_revision: int,
    input_snapshot: dict[str, Any],
    target_worker_id: int,
    scheduled_for: Any = None,
    artifact_ids: Sequence[int] = (),
    dispatch_backend: str = "rabbitmq",
    dispatch_generation: int = 1,
    logical_input_bytes: int = 0,
    max_attempts_snapshot: int = 1,
    retry_policy_snapshot: dict[str, object] | None = None,
    resource_profile_snapshot: dict[str, object] | None = None,
    credential_bindings_snapshot: list[dict[str, object]] | None = None,
    schedule_policy_snapshot: dict[str, object] | None = None,
    resource_class: str | None = None,
    version_id_override: int | None = None,
) -> Execution:
    """Create a fully initialized queued Execution under the Adapter lock.

    Trigger-specific callers own authentication, runtime/input validation and
    input resolution.  This narrow helper owns the lifecycle facts every new
    Worker-claimable Execution must freeze in its creation transaction.
    """
    from dlr.control.services.input_config import database_now

    version_id = (
        version_id_override if version_id_override is not None else adapter.latest_version_id
    )
    if version_id is None:  # pragma: no cover - every caller validates readiness
        raise RuntimeError("cannot create an Execution without a saved Adapter version")
    created_at = database_now(session)
    if dispatch_backend != "rabbitmq":
        raise ValueError("unsupported execution backend")
    execution = Execution(
        adapter_id=adapter.id,
        version_id=version_id,
        trigger=trigger,
        status="queued",
        dispatch_backend=dispatch_backend,
        dispatch_generation=dispatch_generation,
        queued_at=created_at,
        attempt_count=0,
        max_attempts_snapshot=max_attempts_snapshot,
        retry_policy_snapshot=retry_policy_snapshot or {},
        resource_profile_snapshot=resource_profile_snapshot or {},
        credential_bindings_snapshot=credential_bindings_snapshot or [],
        schedule_policy_snapshot=schedule_policy_snapshot,
        resource_class=resource_class,
        target_worker_id_snapshot=target_worker_id,
        logical_input_bytes=logical_input_bytes,
        input=runtime_input,
        input_source_type=input_source_type,
        input_config_revision=input_config_revision,
        input_snapshot=input_snapshot,
        timeout_seconds_snapshot=adapter.timeout_seconds,
        recovery_grace_seconds_snapshot=settings.execution_recovery_grace_seconds,
        workspace_cleanup_attempt_timeout_seconds_snapshot=(
            settings.workspace_cleanup_attempt_timeout_seconds
        ),
        workspace_cleanup_total_timeout_seconds_snapshot=(
            settings.workspace_cleanup_total_timeout_seconds
        ),
        workspace_cleanup_status="pending",
        # Freeze the bounded Claim handshake budget in the immutable snapshot.
        claim_deadline_at=created_at + timedelta(seconds=settings.execution_claim_timeout_seconds),
        target_worker_id=target_worker_id,
        scheduled_for=scheduled_for,
        locale=get_system_locale(session),
        created_at=created_at,
    )
    session.add(execution)
    session.flush()
    if artifact_ids:
        session.add_all(
            ExecutionInputArtifactLease(
                execution_id=execution.id,
                artifact_id=artifact_id,
                ordinal=ordinal,
            )
            for ordinal, artifact_id in enumerate(artifact_ids)
        )
        session.flush()
    if dispatch_backend == "rabbitmq" and credential_bindings_snapshot:
        _persist_credential_binding_snapshots(session, execution, credential_bindings_snapshot)
    return execution


def _create_execution_locked(
    session: Session,
    adapter: Adapter | None,
    *,
    trigger: str,
    scheduled_for: Any = None,
    input_override: object = NO_INPUT_OVERRIDE,
    schedule: Any = None,
    idempotency_key: str | None = None,
    idempotency_body: Any = None,
    idempotency_lookup: Any = None,
) -> Execution:
    """Create one Execution while the caller owns the Adapter transaction lock."""
    from dlr.control.services.input_config import resolve_for_execution

    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    if adapter.archived_at is not None:
        raise domain_error(409, "adapter_deleted", "Adapter is deleted")
    if adapter.latest_version_id is None:
        raise domain_error(409, "adapter_has_no_version", "Adapter has no saved Revision yet")
    from dlr.control.services import idempotency as idempotency_service
    from dlr.control.services.reliable_execution import accept_execution

    lookup = idempotency_lookup or idempotency_service.lookup(
        session, adapter.id, trigger, idempotency_body, idempotency_key
    )
    if lookup.record is not None:
        existing = session.get(Execution, lookup.record.execution_id)
        if existing is None:
            raise domain_error(
                409, "idempotency_record_invalid", "Idempotency record is unavailable"
            )
        return existing

    # A Schedule run-now must take the same Schedule lock as the Scheduler,
    # but it never mutates the row or its cursor.
    if schedule is None and adapter.run_mode == "schedule":
        from dlr.control.models import AdapterSchedule

        schedule = session.scalar(
            select(AdapterSchedule)
            .where(AdapterSchedule.adapter_id == adapter.id)
            .with_for_update()
        )

    if input_override is NO_INPUT_OVERRIDE:
        resolved = resolve_for_execution(session, adapter.id)
    else:
        resolved = resolve_for_execution(session, adapter.id, override=input_override)
        LEGACY_INPUT_COMPAT_METRICS["execution_override"] += 1
        logger.info(
            "legacy_input_compat deprecated operation=execution_override adapter_id=%s",
            adapter.id,
        )

    return accept_execution(
        session,
        adapter,
        trigger=trigger,
        runtime_input=resolved.runtime_input,
        input_source_type=resolved.source_type,
        input_config_revision=resolved.revision,
        input_snapshot=resolved.snapshot,
        scheduled_for=scheduled_for,
        artifact_ids=resolved.artifact_ids,
        idempotency_key=idempotency_key,
        idempotency_body=idempotency_body,
        idempotency_lookup=lookup,
        schedule_policy_snapshot=(
            {
                "misfire_policy": schedule.misfire_policy,
                "max_catchup_count": schedule.max_catchup_count,
                "max_catchup_age_seconds": schedule.max_catchup_age_seconds,
            }
            if trigger == "schedule" and schedule is not None
            else None
        ),
    )


def create_execution(
    session: Session,
    adapter_id: int,
    data: ExecutionCreate,
    *,
    idempotency_key: str | None = None,
    idempotency_body: object = None,
) -> Execution:
    """Create one Manual or schedule run-now Execution from saved input."""
    input_is_present = "input" in data.model_fields_set
    rabbit_idempotency_lookup = None
    from dlr.control.services import idempotency as idempotency_service

    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    request_body = idempotency_body
    rabbit_idempotency_lookup = idempotency_service.lookup(
        session,
        adapter_id,
        "manual",
        request_body,
        idempotency_key,
    )
    if rabbit_idempotency_lookup.record is not None:
        existing = session.get(Execution, rabbit_idempotency_lookup.record.execution_id)
        if existing is None:
            raise domain_error(
                409,
                "idempotency_record_invalid",
                "Idempotency record is unavailable",
            )
        session.commit()
        session.refresh(existing)
        return existing
    if input_is_present and not settings.legacy_input_compat_enabled:
        raise domain_error(
            422,
            "execution_input_override_not_supported",
            "Per-run input overrides are disabled; save the Adapter input first",
        )

    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    input_override = data.input if input_is_present else NO_INPUT_OVERRIDE
    try:
        execution = _create_execution_locked(
            session,
            adapter,
            trigger="manual",
            input_override=input_override,
            idempotency_body=idempotency_body,
            idempotency_key=idempotency_key,
            idempotency_lookup=rabbit_idempotency_lookup,
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if integrity_constraint_name(exc) != "uq_executions_active_adapter":
            raise
        raise domain_error(
            409, "adapter_busy", "The Adapter already has an active Execution"
        ) from None
    session.refresh(execution)
    return execution


def get_execution(session: Session, execution_id: int) -> Execution:
    execution = session.get(Execution, execution_id)
    if execution is None:
        raise domain_error(404, "execution_not_found", "Execution not found")
    return execution


def _normalize_output(
    report: ExecutionResultReport,
) -> tuple[object | None, int | None, bool, str | None]:
    """Re-validate the output big-field contract server-side.

    The Worker may already have truncated output (output_truncated=true,
    output=null, output_size and output_preview set). Control re-validates
    the consistency of that truncation claim instead of blindly trusting it:
    a truncated report must carry output=null and a positive output_size;
    contradictory combinations are rejected, never silently rewritten into a
    plausible-looking Execution result. When the Worker reports a complete
    output, Control re-validates the size and truncates if necessary.
    """
    # Worker-reported truncation: re-validate the contract, cap the preview.
    if report.output_truncated:
        if report.output is not None:
            raise domain_error(
                422,
                "output_contract_violation",
                "output_truncated=true requires output to be null",
            )
        if report.output_size is None or report.output_size <= 0:
            raise domain_error(
                422,
                "output_contract_violation",
                "output_truncated=true requires a positive output_size",
            )
        preview_bytes = (report.output_preview or "").encode()
        capped = preview_bytes[: settings.execution_output_preview_max_bytes].decode(
            "utf-8", errors="ignore"
        )
        return None, report.output_size, True, capped
    # Complete output from Worker: re-validate size server-side.
    if report.output is not None:
        raw = compact_json_bytes(report.output)
        if len(raw) <= settings.execution_output_max_bytes:
            return report.output, len(raw), False, None
        # An oversized "complete" output is never stored, even partially.
        preview = raw[: settings.execution_output_preview_max_bytes].decode(
            "utf-8", errors="ignore"
        )
        return None, len(raw), True, preview
    return None, report.output_size, False, None


def _cap_stream(value: str) -> tuple[str, bool]:
    """Enforce the stdout/stderr byte cap even if the Worker did not."""
    capped, truncated = truncate_utf8(value.encode(), settings.execution_stream_max_bytes)
    return capped.decode("utf-8", errors="replace"), truncated


def append_stream(existing: str, already_truncated: bool, chunk: str) -> tuple[str, bool]:
    """Append live output within the same byte bound used by final results."""
    if not chunk:
        return existing, already_truncated
    combined = existing + chunk
    capped, truncated = truncate_utf8(combined.encode(), settings.execution_stream_max_bytes)
    return capped.decode("utf-8", errors="replace"), already_truncated or truncated


def cancel_execution(session: Session, execution_id: int) -> Execution:
    """Request cancellation of one Execution (M3.2).

    Idempotent: queued Executions become ``cancelled`` immediately (they
    can never be claimed again), running Executions get ``cancel_requested``
    set so the owning Worker kills the subprocess on its next progress round
    trip and reports ``cancelled``, and terminal Executions are returned
    unchanged.
    """
    execution = lock_execution_in_admission_order(session, execution_id)
    if execution is None:
        raise domain_error(404, "execution_not_found", "Execution not found")
    request_cancellation(execution)
    if execution.status == "cancelled":
        release_execution_leases(session, execution.id)
        if execution.dispatch_backend == "rabbitmq":
            from dlr.control.services import admission, outbox

            admission.release_admission_once(session, execution)
            outbox.settle_cancelled_outbox(session, execution.id)
    # Terminal states are never rewritten.
    session.commit()
    session.refresh(execution)
    return execution


def list_adapter_executions(
    session: Session,
    adapter_id: int,
    limit: int = DEFAULT_HISTORY_LIMIT,
    before_id: int | None = None,
    trigger: str | None = None,
) -> tuple[list[ExecutionSummary], int | None]:
    """One cursor page of an Adapter's execution history, newest first.

    Uses a ``before_id`` cursor (never offset) and optionally filters by
    trigger before applying the page limit. This preserves cursor semantics
    for the Webhook-only call history while the unfiltered Task history keeps
    manual and schedule runs together. Summaries omit all big fields. The
    second return value is the next-page cursor, or None at the end.
    """
    adapter = session.get(Adapter, adapter_id)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    limit = max(1, min(limit, MAX_HISTORY_LIMIT))
    query = (
        select(
            Execution.id,
            Execution.adapter_id,
            Execution.version_id,
            AdapterVersion.seq.label("version_seq"),
            Execution.worker_id,
            Worker.name.label("worker_name"),
            Execution.trigger,
            Execution.scheduled_for,
            Execution.status,
            Execution.created_at,
            Execution.started_at,
            Execution.ended_at,
            Execution.duration_ms,
        )
        .join(AdapterVersion, AdapterVersion.id == Execution.version_id)
        .outerjoin(Worker, Worker.id == Execution.worker_id)
        .where(Execution.adapter_id == adapter_id)
        .order_by(Execution.id.desc())
        .limit(limit + 1)
    )
    if trigger is not None:
        query = query.where(Execution.trigger == trigger)
    if before_id is not None:
        query = query.where(Execution.id < before_id)
    rows = session.execute(query).mappings().all()
    has_more = len(rows) > limit
    items = [
        ExecutionSummary(
            id=row["id"],
            adapter_id=row["adapter_id"],
            version_id=row["version_id"],
            version_seq=row["version_seq"],
            worker_id=row["worker_id"],
            worker_name=row["worker_name"],
            trigger=row["trigger"],
            scheduled_for=row["scheduled_for"],
            status=row["status"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            duration_ms=row["duration_ms"],
        )
        for row in rows[:limit]
    ]
    next_before_id = items[-1].id if has_more and items else None
    return items, next_before_id
