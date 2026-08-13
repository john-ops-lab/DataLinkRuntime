"""Domain service for the Adapter Schedule Trigger (M5.2).

Owns the singleton Schedule configuration API, the cron/timezone validation,
the timezone-aware planned-point arithmetic and the lightweight scheduler
tick. PostgreSQL is the only scheduling state source: ``next_run_at`` is the
single cursor, and the partial unique index on ``(adapter_id,
scheduled_for)`` plus the unified active-unique index are the final
duplicate-creation defenses.

Lock order: every path that locks both rows (Start / Stop / PUT Schedule /
scheduler tick) takes the Adapter row first and the Schedule row second, so
concurrent operations can never deadlock. The tick processes each due row in
its own short transaction with SKIP LOCKED and re-checks the due condition
inside the final locks.

Catch-up contract (single latest run, never queued):

- Worker effectively offline / an active production Execution (transient
  gate failures): the planned point stays due; nothing is written and
  nothing is queued. On recovery the latest planned point up to now is
  created at most once, never replayed per period.
- Control downtime: no ticks run; on recovery the latest planned point in
  the missed window is created at most once.
- Structural gate failures (archived, production entry not running, missing
  version/Worker pointers, capability mismatch, oversized input) consume the
  point: the cursor advances and the point is skipped without queueing.
- Explicit Stop / disable / cron edit / enable re-base the cursor to the
  next future planned point, so the missed window is skipped entirely.
"""

import asyncio
import enum
import logging
from datetime import UTC, datetime
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import Adapter, AdapterSchedule, Execution, Worker
from dlr.control.schemas.schedule import ScheduleUpsert
from dlr.control.services import worker_availability
from dlr.control.services.adapter import (
    _active_production_execution,
    _require_not_archived,
    domain_error,
)
from dlr.control.services.execution import compact_json_bytes

logger = logging.getLogger("dlr.control.schedule")

# M5.2 v1: exactly 5 whitespace-separated fields
# (minute hour day-of-month month day-of-week). Seconds, years, keywords and
# macros are rejected so the contract stays fixed.
CRON_FIELD_COUNT = 5


class ScheduleTickResult(enum.Enum):
    """Outcome of processing one due Schedule row inside its transaction."""

    CREATED = "created"  # Execution created; cursor advanced; commit
    CONSUMED = "consumed"  # point consumed without an Execution; cursor advanced; commit
    HELD = "held"  # transient block; nothing written; roll back, the point stays due


def validate_cron(expression: str) -> str:
    """Validate a 5-field cron expression and return its normalized form.

    Raises ValueError for anything that is not exactly five
    whitespace-separated fields or that croniter rejects.
    """
    fields = expression.split()
    if len(fields) != CRON_FIELD_COUNT:
        raise ValueError("cron must have exactly 5 fields")
    normalized = " ".join(fields)
    if not croniter.is_valid(normalized):
        raise ValueError("cron is not a valid 5-field expression")
    return normalized


def validate_timezone(name: str) -> str:
    """Validate an IANA timezone name (e.g. Asia/Shanghai, UTC)."""
    try:
        ZoneInfo(name)
    except (ValueError, KeyError, ZoneInfoNotFoundError):
        raise ValueError("timezone is not a valid IANA timezone") from None
    return name


def _local_tz(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def next_run_after(cron: str, tz_name: str, now: datetime) -> datetime:
    """The next planned point strictly after ``now``, as UTC.

    The cron is evaluated in the configured business timezone; the result
    is persisted timezone-aware in UTC. DST handling follows croniter: a
    skipped wall time fires at the transition boundary, an ambiguous wall
    time fires on both occurrences — fixed behavior, never server-local.
    """
    local_now = now.astimezone(_local_tz(tz_name))
    local_next = cast(datetime, croniter(cron, local_now).get_next(datetime))
    return local_next.astimezone(UTC)


def latest_due_point(cron: str, tz_name: str, since: datetime, now: datetime) -> datetime:
    """The latest planned point in ``[since, now]``, as UTC.

    ``since`` is the stored cursor and always a planned point itself; when
    nothing newer falls before ``now`` (e.g. the tick ran exactly on the
    planned minute), the cursor point is the due point.
    """
    local_now = now.astimezone(_local_tz(tz_name))
    local_prev = cast(datetime, croniter(cron, local_now).get_prev(datetime))
    prev_utc = local_prev.astimezone(UTC)
    since_utc = since.astimezone(UTC)
    return prev_utc if prev_utc >= since_utc else since_utc


# --- Schedule configuration API -------------------------------------------------


def get_schedule(session: Session, adapter_id: int) -> AdapterSchedule:
    """Return the Adapter's Schedule or 404 ``schedule_not_configured``."""
    adapter = session.get(Adapter, adapter_id)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    schedule = session.scalar(
        select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter_id)
    )
    if schedule is None:
        raise domain_error(404, "schedule_not_configured", "Schedule is not configured")
    return schedule


def upsert_schedule(session: Session, adapter_id: int, data: ScheduleUpsert) -> AdapterSchedule:
    """Create or fully replace the Adapter's Schedule (PUT semantics).

    Archived Adapters are rejected with 409 ``adapter_archived`` (read-only
    contract; GET keeps viewing). Validation failures persist nothing. The
    cursor is always re-based to the next future planned point (or NULL when
    disabled), so edits, disables and enables never replay historical
    points.
    """
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    # Archived Adapters stay read-only (M3.2 contract); GET keeps viewing.
    _require_not_archived(adapter)
    try:
        cron = validate_cron(data.cron)
    except ValueError as exc:
        raise domain_error(422, "schedule_invalid_cron", str(exc)) from None
    try:
        tz_name = validate_timezone(data.timezone)
    except ValueError as exc:
        raise domain_error(422, "schedule_invalid_timezone", str(exc)) from None
    # Same big-field contract as Execution input: reject before persisting.
    if len(compact_json_bytes(data.input)) > settings.execution_input_max_bytes:
        raise domain_error(
            413,
            "execution_input_too_large",
            f"Input exceeds the {settings.execution_input_max_bytes} byte limit",
        )
    now = worker_availability.current_time(session)
    next_run_at = next_run_after(cron, tz_name, now) if data.enabled else None
    schedule = session.scalar(
        select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter_id).with_for_update()
    )
    if schedule is None:
        schedule = AdapterSchedule(adapter_id=adapter_id)
        session.add(schedule)
    schedule.cron = cron
    schedule.timezone = tz_name
    schedule.input = data.input
    schedule.enabled = data.enabled
    schedule.next_run_at = next_run_at
    session.commit()
    session.refresh(schedule)
    return schedule


def rebase_enabled_schedule(session: Session, adapter_id: int, now: datetime) -> None:
    """Re-base the Adapter's enabled Schedule cursor to the next future point.

    Called inside the Start transaction (M5.2): after a Stop → Start cycle
    the Schedule begins from the next future planned point and never catches
    up the points missed while the production entry was closed.
    """
    schedule = session.scalar(
        select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter_id).with_for_update()
    )
    if schedule is None or not schedule.enabled:
        return
    schedule.next_run_at = next_run_after(schedule.cron, schedule.timezone, now)


# --- Scheduler tick ---------------------------------------------------------------


def _structural_gate_failure(session: Session, adapter: Adapter, schedule: AdapterSchedule) -> bool:
    """Explicit lifecycle/configuration states that consume the due point.

    These reflect deliberate administrator state (archived, production entry
    closed) or broken configuration; a due point under them is skipped by
    advancing the cursor, never queued. Stop / disable / edit additionally
    re-base the cursor, so their closed windows are never caught up.
    """
    if adapter.archived_at is not None:
        return True
    if adapter.production_state != "running":
        return True
    if adapter.production_version_id is None or adapter.production_worker_id is None:
        return True
    worker = session.get(Worker, adapter.production_worker_id)
    if worker is None or adapter.language not in worker.capabilities:
        return True
    # Defensive re-validation: an oversized stored input never executes.
    return len(compact_json_bytes(schedule.input)) > settings.execution_input_max_bytes


def _transient_gate_failure(session: Session, adapter: Adapter, *, now: datetime) -> bool:
    """Temporary conditions that keep the planned point due (no queueing).

    A stale Worker or a still-running production Execution recovers on its
    own; the point must stay due so recovery creates exactly the latest
    planned point up to now, immediately.
    """
    worker = session.get(Worker, cast(int, adapter.production_worker_id))
    if worker is None or not worker_availability.is_effectively_online(worker, now=now):
        return True
    return _active_production_execution(session, adapter.id) is not None


def process_due_schedule(
    session: Session, adapter: Adapter, schedule: AdapterSchedule, *, now: datetime
) -> ScheduleTickResult:
    """Handle one due Schedule inside the caller's transaction.

    The caller must already hold the Adapter row lock and the Schedule row
    lock (platform order: Adapter first) and decides commit/rollback from
    the result: CREATED / CONSUMED advance the cursor and must be committed
    (a missed window is never replayed period by period); HELD wrote
    nothing and must be rolled back, so the point stays due and the next
    poll retries.
    """
    since = schedule.next_run_at
    if since is None:
        return ScheduleTickResult.HELD
    if _structural_gate_failure(session, adapter, schedule):
        schedule.next_run_at = next_run_after(schedule.cron, schedule.timezone, now)
        return ScheduleTickResult.CONSUMED
    if _transient_gate_failure(session, adapter, now=now):
        return ScheduleTickResult.HELD
    due_point = latest_due_point(schedule.cron, schedule.timezone, since, now)
    schedule.next_run_at = next_run_after(schedule.cron, schedule.timezone, now)
    try:
        with session.begin_nested():
            session.add(
                Execution(
                    adapter_id=adapter.id,
                    version_id=adapter.production_version_id,
                    trigger="schedule",
                    status="pending",
                    target_worker_id=adapter.production_worker_id,
                    input=schedule.input,
                    scheduled_for=due_point,
                )
            )
            session.flush()
    except IntegrityError:
        # Lost a race (duplicate planned point or an active Production
        # Execution created concurrently): the savepoint rolled back, the
        # cursor advance still commits, so the point is never retried.
        logger.info(
            "schedule execution race lost for adapter %s at %s",
            adapter.id,
            due_point.isoformat(),
        )
        return ScheduleTickResult.CONSUMED
    return ScheduleTickResult.CREATED


def _process_due_row(schedule_id: int, adapter_id: int, *, now: datetime) -> bool:
    """Process one due row in its own short transaction.

    Locks are taken in the platform order (Adapter first, Schedule second,
    matching Start / Stop / PUT Schedule) with SKIP LOCKED, and the due
    condition is re-checked inside the final locks. Returns True when a
    Schedule Execution was created.
    """
    # Local import like _tick_once: tests point SessionLocal at the test DB.
    from dlr.control.db import SessionLocal

    session = SessionLocal()
    try:
        adapter = session.scalar(
            select(Adapter).where(Adapter.id == adapter_id).with_for_update(skip_locked=True)
        )
        if adapter is None:
            # Locked by a concurrent tick / lifecycle change (or deleted):
            # skip without blocking; the next poll retries if still due.
            return False
        schedule = session.scalar(
            select(AdapterSchedule)
            .where(AdapterSchedule.id == schedule_id)
            .with_for_update(skip_locked=True)
        )
        if schedule is None:
            return False
        # Re-check inside the final locks: another scheduler may already
        # have processed the row, or a concurrent PUT may have disabled or
        # re-based it to the future.
        if not schedule.enabled or schedule.next_run_at is None or schedule.next_run_at > now:
            return False
        outcome = process_due_schedule(session, adapter, schedule, now=now)
        if outcome is ScheduleTickResult.HELD:
            # Transient failure: roll back so the cursor (and the point)
            # stays due; recovery creates the latest point only.
            session.rollback()
            return False
        session.commit()
        return outcome is ScheduleTickResult.CREATED
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def scheduler_tick(session: Session, *, now: datetime | None = None) -> int:
    """One scheduler poll: process every due enabled Schedule once.

    Discovery is a plain read; every due row is then processed in its own
    transaction (see _process_due_row), so concurrent schedulers partition
    due rows via SKIP LOCKED without releasing each other's locks mid-tick
    and without processing a row whose due condition changed meanwhile.
    Returns the number of Schedule Executions created this tick.
    """
    if now is None:
        now = worker_availability.current_time(session)
    candidates = session.execute(
        select(AdapterSchedule.id, AdapterSchedule.adapter_id)
        .where(
            AdapterSchedule.enabled.is_(True),
            AdapterSchedule.next_run_at.is_not(None),
            AdapterSchedule.next_run_at <= now,
        )
        .order_by(AdapterSchedule.next_run_at, AdapterSchedule.id)
    ).all()
    created = 0
    for schedule_id, adapter_id in candidates:
        try:
            if _process_due_row(schedule_id, adapter_id, now=now):
                created += 1
        except Exception:
            # One broken Schedule must never stall the whole tick.
            logger.exception("scheduler tick failed for schedule %s", schedule_id)
    return created


async def scheduler_loop() -> None:
    """The Control background polling loop (PostgreSQL-only state source).

    Runs until cancelled; each tick opens its own short-lived session. No
    Redis/Celery/APScheduler — a sleep-and-poll task is the whole scheduler.
    """
    logger.info("schedule loop started (poll every %.2fs)", settings.schedule_poll_seconds)
    while True:
        await asyncio.sleep(settings.schedule_poll_seconds)
        try:
            await asyncio.to_thread(_tick_once)
        except Exception:
            logger.exception("scheduler tick crashed")


def _tick_once() -> None:
    """One blocking tick on its own session; run via ``asyncio.to_thread``."""
    from dlr.control.db import SessionLocal

    session = SessionLocal()
    try:
        scheduler_tick(session)
    finally:
        session.close()
