"""Domain service for the Execution lifecycle (create / query / result).

Owns version pinning, the input size gate and the server-side
re-validation of every big-field contract reported by a Worker.
"""

import json

from sqlalchemy import func
from sqlalchemy.orm import Session

from dlr.common.bigfields import truncate_utf8
from dlr.common.config import settings
from dlr.control.models import Adapter, AdapterVersion, Execution
from dlr.control.schemas.execution import ExecutionCreate, ExecutionResultReport
from dlr.control.services.adapter import domain_error

# Statuses after which an Execution never changes again.
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timeout", "cancelled"})


def compact_json_bytes(value: object) -> bytes:
    """Compact JSON serialization as UTF-8 bytes (the big-field unit)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def create_execution(session: Session, adapter_id: int, data: ExecutionCreate) -> Execution:
    """Create a Manual Execution pinned to one immutable version."""
    adapter = session.get(Adapter, adapter_id)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
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
    output=null, output_size and output_preview set). In that case Control
    must trust the truncation flag and not re-interpret the preview as a
    complete value. When the Worker reports a complete output, Control
    re-validates the size and truncates if necessary.
    """
    # Worker-reported truncation: trust the flag, cap the preview defensively.
    if report.output_truncated:
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
