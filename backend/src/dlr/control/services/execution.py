"""Domain service for the Execution lifecycle (create / query / result).

Owns version pinning, the input size gate, the server-side re-validation of
every big-field contract reported by a Worker, the M3 best-effort progress
appends and the cursor-paged execution history.
"""

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dlr.common.bigfields import truncate_utf8
from dlr.common.config import settings
from dlr.control.models import Adapter, AdapterVersion, Execution, Worker
from dlr.control.schemas.execution import (
    ExecutionCreate,
    ExecutionResultReport,
    ExecutionSummary,
    ProgressReport,
)
from dlr.control.services.adapter import domain_error
from dlr.control.services.execution_cancellation import lock_execution, request_cancellation

# Statuses after which an Execution never changes again.
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timeout", "cancelled"})

# Execution history pagination contract (M3 spec §5).
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 100


def compact_json_bytes(value: object) -> bytes:
    """Compact JSON serialization as UTF-8 bytes (the big-field unit)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def _resolve_test_target(session: Session, adapter: Adapter) -> int | None:
    """Scheduling target for a test run (M3.2).

    A configured production Worker is always the target. Without one, the
    only online Worker is adopted (and written back as the production
    Worker); with multiple online Workers the caller must pick one first.
    With no online Worker at all the target stays None so the historical
    any-Worker claiming keeps working.
    """
    if adapter.production_worker_id is not None:
        worker = session.get(Worker, adapter.production_worker_id)
        if worker is not None:
            if worker.status != "online":
                raise domain_error(409, "worker_offline", "The production Worker is offline")
            if adapter.language not in worker.capabilities:
                raise domain_error(
                    409,
                    "worker_capability_missing",
                    f"The production Worker does not support {adapter.language}",
                )
            return worker.id
    online = list(
        session.scalars(
            select(Worker).where(
                Worker.status == "online",
                Worker.capabilities.contains([adapter.language]),
            )
        ).all()
    )
    if len(online) == 1:
        adapter.production_worker_id = online[0].id
        return online[0].id
    if len(online) > 1:
        raise domain_error(
            409,
            "production_worker_required",
            "Multiple online Workers exist; configure a production Worker first",
        )
    any_online = session.scalar(select(Worker.id).where(Worker.status == "online").limit(1))
    if any_online is not None:
        raise domain_error(
            409,
            "worker_capability_missing",
            f"No online Worker supports {adapter.language}",
        )
    return None


def create_execution(session: Session, adapter_id: int, data: ExecutionCreate) -> Execution:
    """Create a Manual (test) Execution pinned to one immutable version."""
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    if adapter.archived_at is not None:
        raise domain_error(409, "adapter_archived", "Adapter is archived")
    # Oversized input is rejected before anything is persisted; it is never
    # truncated and executed.
    if len(compact_json_bytes(data.input)) > settings.execution_input_max_bytes:
        raise domain_error(
            413,
            "execution_input_too_large",
            f"Input exceeds the {settings.execution_input_max_bytes} byte limit",
        )
    if data.version_id is not None:
        version = session.get(AdapterVersion, data.version_id)
        if version is None or version.adapter_id != adapter_id:
            raise domain_error(404, "version_not_found", "Version not found")
        version_id = version.id
    else:
        if adapter.latest_version_id is None:
            raise domain_error(409, "adapter_has_no_version", "Adapter has no saved version yet")
        version_id = adapter.latest_version_id
    execution = Execution(
        adapter_id=adapter_id,
        version_id=version_id,
        trigger="manual",
        status="pending",
        input=data.input,
        target_worker_id=_resolve_test_target(session, adapter),
    )
    session.add(execution)
    session.commit()
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
    session: Session, worker_id: int, execution_id: int, report: ExecutionResultReport
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
    session: Session, worker_id: int, execution_id: int, report: ProgressReport
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
    # Terminal states are never rewritten.
    session.commit()
    session.refresh(execution)
    return execution


def list_adapter_executions(
    session: Session,
    adapter_id: int,
    limit: int = DEFAULT_HISTORY_LIMIT,
    before_id: int | None = None,
) -> tuple[list[ExecutionSummary], int | None]:
    """One cursor page of an Adapter's execution history, newest first.

    Uses a ``before_id`` cursor (never offset) and returns lightweight
    summaries without the input/output/stdout/stderr big fields. The second
    return value is the cursor for the next page, or None when the history
    ends here.
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
