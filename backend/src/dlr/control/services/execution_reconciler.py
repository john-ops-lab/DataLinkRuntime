"""Control-side convergence for stale Worker Executions.

The reconciler owns the point at which a pending or running Execution becomes
terminal after its Worker lease expires.  It locks a bounded batch with
``SKIP LOCKED`` and commits the business terminal state, cleanup state, timing
facts and input-Lease release as one transaction.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from dlr.common.config import settings
from dlr.control import db
from dlr.control.models import Execution, Worker
from dlr.control.services import worker_availability
from dlr.control.services.execution import TERMINAL_STATUSES, release_execution_leases

logger = logging.getLogger("dlr.control.execution_reconciler")

# Keep a single tick bounded.  A later tick continues with the next batch, so
# one slow or large deployment cannot hold the row locks indefinitely.
STALE_RECONCILER_BATCH_SIZE = 100

# These aliases are replaceable in tests without mutating the process-wide
# asyncio module.  The existing schedule polling cadence is the deployment's
# control-plane polling floor; C3 intentionally adds no second interval knob.
_asyncio_to_thread = asyncio.to_thread
_asyncio_sleep = asyncio.sleep


@dataclass(frozen=True)
class StaleExecutionReport:
    """Counts from one bounded stale-reconciliation transaction."""

    scanned: int = 0
    reconciled: int = 0
    pending_failed: int = 0
    running_timeout: int = 0
    running_worker_lost: int = 0
    skipped: int = 0

    @property
    def running_reconciled(self) -> int:
        """Number of running rows converged in this batch."""
        return self.running_timeout + self.running_worker_lost


def _as_utc(value: datetime) -> datetime:
    """Normalize database/test timestamps before Python-side duration math."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _interval(seconds: object) -> Any:
    """Build a PostgreSQL interval while retaining per-row snapshots."""
    # SQLAlchemy's generic ``func`` does not accept PostgreSQL's named
    # arguments; pass the seven positional make_interval fields instead.
    return func.make_interval(0, 0, 0, 0, 0, 0, seconds)


def _stale_query(now: datetime | None, *, batch_size: int) -> Select[tuple[Execution]]:
    """Select only due active rows and lock one bounded batch.

    New C0 rows carry explicit deadlines.  The null-deadline branches retain
    safe governance for historical rows created before those snapshots were
    introduced, using their creation/start time and the current defensive
    platform fallback.  The running branch still honors each row's frozen
    recovery grace value.
    """
    # Candidate selection only needs a monotonic cutoff.  Production's
    # clock_timestamp() decision clock is deliberately sampled after
    # SKIP LOCKED returns, as close as possible to health and ended_at writes.
    comparison_now: Any = now if now is not None else func.now()
    pending_fallback_deadline = Execution.created_at + _interval(
        settings.execution_claim_timeout_seconds
    )
    pending_stale = and_(
        Execution.status == "pending",
        or_(
            and_(
                Execution.claim_deadline_at.is_not(None),
                Execution.claim_deadline_at <= comparison_now,
            ),
            and_(
                Execution.claim_deadline_at.is_(None),
                pending_fallback_deadline <= comparison_now,
            ),
        ),
    )

    timeout_seconds = func.coalesce(
        Execution.timeout_seconds_snapshot,
        settings.execution_timeout_seconds,
    )
    grace_seconds = func.coalesce(
        Execution.recovery_grace_seconds_snapshot,
        settings.execution_recovery_grace_seconds,
    )
    fallback_execution_deadline = Execution.started_at + _interval(timeout_seconds)
    execution_deadline = func.coalesce(
        Execution.execution_deadline_at,
        fallback_execution_deadline,
    )
    running_stale = and_(
        Execution.status == "running",
        execution_deadline + _interval(grace_seconds) <= comparison_now,
    )
    return (
        select(Execution)
        .where(
            Execution.dispatch_backend == "legacy",
            or_(pending_stale, running_stale),
        )
        .order_by(Execution.created_at.asc(), Execution.id.asc())
        .with_for_update(skip_locked=True)
        .limit(batch_size)
    )


def _set_terminal_timing(execution: Execution, *, ended_at: datetime) -> None:
    """Set terminal timing from one authoritative decision timestamp."""
    execution.ended_at = ended_at
    if execution.started_at is None:
        execution.duration_ms = None
        return
    elapsed_ms = int((_as_utc(ended_at) - _as_utc(execution.started_at)).total_seconds() * 1000)
    execution.duration_ms = max(0, elapsed_ms)


def _reconcile_pending(execution: Execution, session: Session, *, now: datetime) -> None:
    """Converge an unclaimed stale row without inventing Worker cleanup."""
    execution.status = "failed"
    execution.error = "Execution was not claimed before the claim deadline"
    execution.error_code = "worker_unavailable"
    execution.workspace_cleanup_status = "completed"
    execution.workspace_cleanup_error_code = None
    _set_terminal_timing(execution, ended_at=now)
    release_execution_leases(session, execution.id)


def _reconcile_running(execution: Execution, session: Session, *, now: datetime) -> str:
    """Converge an overdue running row based on effective Worker health."""
    worker = session.get(Worker, execution.worker_id) if execution.worker_id is not None else None
    worker_is_healthy = worker is not None and worker_availability.is_effectively_online(
        worker, now=now
    )
    if worker_is_healthy:
        execution.status = "timeout"
        execution.error = "Execution exceeded its deadline"
        execution.error_code = None
        outcome = "timeout"
    else:
        execution.status = "failed"
        execution.error = "The owning Worker was unavailable after the execution deadline"
        execution.error_code = "worker_lost"
        outcome = "worker_lost"
    execution.workspace_cleanup_status = "deferred"
    execution.workspace_cleanup_error_code = "workspace_cleanup_unknown"
    _set_terminal_timing(execution, ended_at=now)
    release_execution_leases(session, execution.id)
    return outcome


def reconcile_stale_executions(
    session: Session,
    *,
    now: datetime | None = None,
    batch_size: int = STALE_RECONCILER_BATCH_SIZE,
) -> StaleExecutionReport:
    """Atomically converge one bounded batch of stale active Executions.

    ``now`` is intended for a frozen-clock test.  Production callers omit it;
    the stale predicate uses the database clock for candidate selection, then
    a fresh PostgreSQL ``clock_timestamp()`` is sampled after SKIP LOCKED
    returns and reused for every Worker health check and terminal row.
    A commit failure rolls the whole batch back, including Lease deletion.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    injected_now = _as_utc(now) if now is not None else None
    executions = list(
        session.scalars(
            _stale_query(
                injected_now,
                batch_size=min(batch_size, STALE_RECONCILER_BATCH_SIZE),
            )
        ).all()
    )
    if not executions:
        # The SELECT opens a transaction even when no row is due.  Release it
        # so a long-running Control loop does not retain a snapshot/connection.
        session.rollback()
        return StaleExecutionReport()

    # Keep the explicit test clock deterministic, but sample production time
    # only after the SKIP LOCKED candidate query has acquired row locks.
    effective_now = (
        injected_now
        if injected_now is not None
        else _as_utc(worker_availability.current_time(session))
    )

    pending_failed = 0
    running_timeout = 0
    running_worker_lost = 0
    skipped = 0
    for execution in executions:
        if execution.status in TERMINAL_STATUSES or execution.ended_at is not None:
            skipped += 1
            continue
        if execution.status == "pending":
            _reconcile_pending(execution, session, now=effective_now)
            pending_failed += 1
        elif execution.status == "running":
            outcome = _reconcile_running(execution, session, now=effective_now)
            if outcome == "timeout":
                running_timeout += 1
            else:
                running_worker_lost += 1
        else:  # pragma: no cover - status is protected by the database check
            skipped += 1

    reconciled = pending_failed + running_timeout + running_worker_lost
    try:
        if reconciled:
            session.commit()
        else:
            session.rollback()
    except Exception:
        session.rollback()
        raise
    return StaleExecutionReport(
        scanned=len(executions),
        reconciled=reconciled,
        pending_failed=pending_failed,
        running_timeout=running_timeout,
        running_worker_lost=running_worker_lost,
        skipped=skipped,
    )


def _reconcile_tick() -> StaleExecutionReport:
    """Run one background tick in a short-lived database session."""
    with db.SessionLocal() as session:
        return reconcile_stale_executions(session)


async def stale_execution_reconciler_loop() -> None:
    """Continuously reconcile stale executions without cancellation leaks."""
    logger.info(
        "stale execution reconciler loop started interval=%.2f batch_size=%s",
        settings.schedule_poll_seconds,
        STALE_RECONCILER_BATCH_SIZE,
    )
    while True:
        try:
            await _asyncio_to_thread(_reconcile_tick)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next tick must retry governance
            logger.warning(
                "stale execution reconciliation cycle failed; retrying next interval",
                exc_info=True,
            )
        await _asyncio_sleep(settings.schedule_poll_seconds)


__all__ = [
    "reconcile_stale_executions",
    "stale_execution_reconciler_loop",
]
