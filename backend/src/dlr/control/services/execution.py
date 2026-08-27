"""Domain service for the Execution lifecycle (create / query / result).

Owns version pinning, the input size gate, the server-side re-validation of
every big-field contract reported by a Worker, the M3 best-effort progress
appends and the cursor-paged execution history.
"""

import json
import logging
from collections import Counter
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.common.bigfields import truncate_utf8
from dlr.common.config import settings, validate_deployment_configuration
from dlr.control.models import (
    Adapter,
    AdapterVersion,
    Execution,
    ExecutionInputArtifactLease,
    Worker,
)
from dlr.control.schemas.execution import (
    ExecutionCreate,
    ExecutionResultReport,
    ExecutionSummary,
    ProgressReport,
)
from dlr.control.services import adapter_runtime, worker_availability
from dlr.control.services.adapter import domain_error, resolve_runtime_worker
from dlr.control.services.execution_cancellation import lock_execution, request_cancellation
from dlr.control.services.locale import get_system_locale
from dlr.control.services.worker_protocol import require_claim_token

logger = logging.getLogger("dlr.control.execution")

# Statuses after which an Execution never changes again.
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timeout", "cancelled"})

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


def _create_execution_locked(
    session: Session,
    adapter: Adapter | None,
    *,
    trigger: str,
    scheduled_for: Any = None,
    input_override: object = NO_INPUT_OVERRIDE,
    schedule: Any = None,
) -> Execution:
    """Create one Execution while the caller owns the Adapter transaction lock."""
    from dlr.control.services.input_config import resolve_for_execution

    validate_deployment_configuration(settings)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    if adapter.archived_at is not None:
        raise domain_error(409, "adapter_deleted", "Adapter is deleted")
    if adapter.latest_version_id is None:
        raise domain_error(409, "adapter_has_no_version", "Adapter has no saved Revision yet")
    if adapter_runtime.active_execution(session, adapter.id) is not None:
        raise domain_error(409, "adapter_busy", "The Adapter already has an active Execution")

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

    worker = resolve_runtime_worker(
        session,
        adapter,
        now=worker_availability.current_time(session),
    )
    from dlr.control.services.input_config import database_now

    created_at = database_now(session)
    execution = Execution(
        adapter_id=adapter.id,
        version_id=adapter.latest_version_id,
        trigger=trigger,
        status="pending",
        input=resolved.runtime_input,
        input_source_type=resolved.source_type,
        input_config_revision=resolved.revision,
        input_snapshot=resolved.snapshot,
        timeout_seconds_snapshot=adapter.timeout_seconds,
        recovery_grace_seconds_snapshot=settings.execution_recovery_grace_seconds,
        workspace_cleanup_attempt_timeout_seconds_snapshot=(
            settings.workspace_cleanup_attempt_timeout_seconds
        ),
        workspace_cleanup_total_timeout_seconds_snapshot=settings.workspace_cleanup_total_timeout_seconds,
        claim_deadline_at=created_at + timedelta(seconds=settings.execution_claim_timeout_seconds),
        workspace_cleanup_status="completed",
        target_worker_id=worker.id,
        scheduled_for=scheduled_for,
        locale=get_system_locale(session),
        created_at=created_at,
    )
    session.add(execution)
    session.flush()
    if resolved.artifact_ids:
        session.add_all(
            ExecutionInputArtifactLease(
                execution_id=execution.id,
                artifact_id=artifact_id,
                ordinal=ordinal,
            )
            for ordinal, artifact_id in enumerate(resolved.artifact_ids)
        )
        session.flush()
    return execution


def create_execution(session: Session, adapter_id: int, data: ExecutionCreate) -> Execution:
    """Create one Manual or schedule run-now Execution from saved input."""
    input_is_present = "input" in data.model_fields_set
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
        )
        session.commit()
    except IntegrityError:
        session.rollback()
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


def apply_result(
    session: Session,
    worker_id: int,
    execution_id: int,
    report: ExecutionResultReport,
    *,
    claim_token: str | None = None,
) -> Execution:
    """Persist a terminal result reported by the owning Worker.

    Idempotent: re-reporting an already terminal Execution succeeds without
    changing it, supporting retries after a lost response. The Execution row
    is locked for the duration of the transaction so two concurrent result
    reports from the same Worker cannot race to write different terminal
    states; ownership is checked before the terminal-idempotent shortcut so
    a non-owning Worker always gets 409, even after the Execution finished.
    """
    execution = (
        session.query(Execution)
        .filter(Execution.id == execution_id)
        .with_for_update()
        .one_or_none()
    )
    if execution is None:
        raise domain_error(404, "execution_not_found", "Execution not found")
    # Ownership first: a non-owning Worker must always see 409, even when the
    # Execution has already reached a terminal state.
    if execution.worker_id != worker_id:
        raise domain_error(409, "execution_not_owned", "Execution is not assigned to this worker")
    if execution.claim_token_hash is not None:
        require_claim_token(claim_token, execution.claim_token_hash)
    if execution.status != "running":
        # Already terminal: idempotent return. The row lock above guarantees a
        # concurrent second report from the same Worker observes the same
        # terminal state and cannot overwrite it.
        return execution

    output, output_size, output_truncated, output_preview = _normalize_output(report)
    # The Worker may already have truncated streams; the Control-side cap is
    # an additional safety net, but the persisted truncated flag must be true
    # if either side actually truncated.
    stdout, stdout_capped = _cap_stream(report.stdout)
    stderr, stderr_capped = _cap_stream(report.stderr)
    stdout_truncated = report.stdout_truncated or stdout_capped
    stderr_truncated = report.stderr_truncated or stderr_capped

    execution.status = report.status
    execution.output = output
    execution.output_size = output_size
    execution.output_truncated = output_truncated
    execution.output_preview = output_preview
    execution.stdout = stdout
    execution.stdout_truncated = stdout_truncated
    execution.stderr = stderr
    execution.stderr_truncated = stderr_truncated
    execution.error = report.error
    execution.error_code = report.error_code
    if execution.claim_token_hash is None:
        execution.workspace_cleanup_status = "deferred"
        execution.workspace_cleanup_error_code = "workspace_cleanup_legacy_unverified"
    elif report.workspace_cleanup_status is not None:
        execution.workspace_cleanup_status = report.workspace_cleanup_status
        execution.workspace_cleanup_error_code = report.workspace_cleanup_error_code
        if report.workspace_cleanup_status == "completed":
            execution.workspace_cleanup_error_code = None
    release_execution_leases(session, execution.id)
    # Both timestamps come from the database clock, so duration_ms never
    # mixes client and server clocks. The numeric expression is assignment-
    # cast to the BIGINT column by PostgreSQL.
    now = func.now()
    execution.ended_at = now
    execution.duration_ms = func.extract("epoch", now - Execution.started_at) * 1000
    session.commit()
    session.refresh(execution)
    return execution


def _append_stream(existing: str, already_truncated: bool, chunk: str) -> tuple[str, bool]:
    """Append one progress chunk while honoring the 1 MiB stream cap.

    Once a stream was truncated (by progress growth or by the final result)
    it stays truncated; the stored text keeps the earliest content plus the
    newest content so both the startup lines and the most recent lines stay
    visible. Progress appends never touch output/error/status/timing.
    """
    if not chunk:
        return existing, already_truncated
    combined = existing + chunk
    if len(combined.encode()) <= settings.execution_stream_max_bytes:
        return combined, already_truncated
    capped, _ = truncate_utf8(combined.encode(), settings.execution_stream_max_bytes)
    return capped.decode("utf-8", errors="replace"), True


def apply_progress(
    session: Session,
    worker_id: int,
    execution_id: int,
    report: ProgressReport,
    *,
    claim_token: str | None = None,
) -> bool:
    """Append best-effort stdout/stderr chunks from the owning Worker.

    Progress never changes Execution status and never touches output, error,
    ended_at or duration_ms. After the Execution reached a terminal state the
    chunks are dropped, so the tail of the progress stream can never
    overwrite the M2 final result; ownership is checked first, so a
    non-owning Worker still gets 409 even for terminal Executions.

    Returns the current ``cancel_requested`` flag: the progress round trip
    doubles as the cancel channel (M3.2), and empty uploads are accepted as
    pure cancel polls (no commit happens when nothing changed).
    """
    execution = (
        session.query(Execution)
        .filter(Execution.id == execution_id)
        .with_for_update()
        .one_or_none()
    )
    if execution is None:
        raise domain_error(404, "execution_not_found", "Execution not found")
    if execution.worker_id != worker_id:
        raise domain_error(409, "execution_not_owned", "Execution is not assigned to this worker")
    if execution.claim_token_hash is not None:
        require_claim_token(claim_token, execution.claim_token_hash)
    if execution.status != "running":
        # Terminal: drop the chunks, still answer the cancel flag.
        return execution.cancel_requested
    if report.stdout_chunk or report.stderr_chunk:
        stdout, stdout_truncated = _append_stream(
            execution.stdout, execution.stdout_truncated, report.stdout_chunk
        )
        stderr, stderr_truncated = _append_stream(
            execution.stderr, execution.stderr_truncated, report.stderr_chunk
        )
        execution.stdout = stdout
        execution.stdout_truncated = stdout_truncated
        execution.stderr = stderr
        execution.stderr_truncated = stderr_truncated
        session.commit()
    return execution.cancel_requested


def cancel_execution(session: Session, execution_id: int) -> Execution:
    """Request cancellation of one Execution (M3.2).

    Idempotent: pending Executions become ``cancelled`` immediately (they
    can never be claimed again), running Executions get ``cancel_requested``
    set so the owning Worker kills the subprocess on its next progress round
    trip and reports ``cancelled``, and terminal Executions are returned
    unchanged.
    """
    execution = lock_execution(session, execution_id)
    if execution is None:
        raise domain_error(404, "execution_not_found", "Execution not found")
    request_cancellation(execution)
    if execution.status == "cancelled":
        release_execution_leases(session, execution.id)
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
