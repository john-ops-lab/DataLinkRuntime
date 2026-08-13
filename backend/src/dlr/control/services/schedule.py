"""Domain service for the Adapter Schedule Trigger (M5.2).

Owns the singleton Schedule configuration API, the cron/timezone validation,
the timezone-aware planned-point arithmetic and the lightweight scheduler
tick. PostgreSQL is the only scheduling state source: ``next_run_at`` is the
single cursor, locked rows plus ``FOR UPDATE SKIP LOCKED`` serialize
concurrent schedulers, and the partial unique index on
``(adapter_id, scheduled_for)`` is the final duplicate-creation defense.

Catch-up contract (single latest run):

- Control downtime / Worker offline / production busy: the cursor is always
  advanced to the next future point; on recovery the latest planned point in
  the missed window is created at most once, never replayed per period.
- Explicit Stop / disable / cron edit: the cursor is re-based to the next
  future point, so the missed window is skipped entirely.
"""

import asyncio
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
from dlr.control.services.adapter import _active_production_execution, domain_error
from dlr.control.services.execution import compact_json_bytes

logger = logging.getLogger("dlr.control.schedule")

# M5.2 v1: exactly 5 whitespace-separated fields
# (minute hour day-of-month month day-of-week). Seconds, years, keywords and
# macros are rejected so the contract stays fixed.
CRON_FIELD_COUNT = 5


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


def upsert_schedule(
    session: Session, adapter_id: int, data: ScheduleUpsert
) -> AdapterSchedule:
    """Create or fully replace the Adapter's Schedule (PUT semantics).

    Validation failures persist nothing. The cursor is always re-based to
    the next future planned point (or NULL when disabled), so edits,
    disables and enables never replay historical points.
    """
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
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
        select(AdapterSchedule)
        .where(AdapterSchedule.adapter_id == adapter_id)
        .with_for_update()
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
        select(AdapterSchedule)
        .where(AdapterSchedule.adapter_id == adapter_id)
        .with_for_update()
    )
    if schedule is None or not schedule.enabled:
        return
    schedule.next_run_at = next_run_after(schedule.cron, schedule.timezone, now)


# --- Scheduler tick ---------------------------------------------------------------


def _gate_allows_execution(
    session: Session, adapter: Adapter | None, schedule: AdapterSchedule, *, now: datetime
) -> bool:
    """The unified production gate evaluated at every due point."""
    if adapter is None or adapter.archived_at is not None:
        return False
    if adapter.production_state != "running":
        return False
    if adapter.production_version_id is None or adapter.production_worker_id is None:
        return False
    worker = session.get(Worker, adapter.production_worker_id)
    if worker is None:
        return False
    if not worker_availability.is_effectively_online(worker, now=now):
        return False
    if adapter.language not in worker.capabilities:
        return False
    if _active_production_execution(session, adapter.id) is not None:
        return False
    # Defensive re-validation: an oversized stored input never executes.
    return len(compact_json_bytes(schedule.input)) <= settings.execution_input_max_bytes


def process_due_schedule(session: Session, schedule: AdapterSchedule, *, now: datetime) -> bool:
    """Handle one due Schedule: advance the cursor, maybe create an Execution.

    The cursor always moves to the next future planned point in the same
    transaction, which implements the single-latest-catch-up rule: missed
    windows yield at most one Execution (the latest planned point) once the
    conditions recover, and never replay per period. Gate failures skip the
    point without queueing.

    Returns True when a Schedule Execution was created.
    """
    since = schedule.next_run_at
    if since is None:
        return False
    due_point = latest_due_point(schedule.cron, schedule.timezone, since, now)
    schedule.next_run_at = next_run_after(schedule.cron, schedule.timezone, now)
    created = False
    try:
        with session.begin_nested():
            # Serialize with Start/Stop/PATCH on the same Adapter row so the
            # gate cannot be committed stale by a concurrent lifecycle change.
            adapter = session.get(Adapter, schedule.adapter_id, with_for_update=True)
            if _gate_allows_execution(session, adapter, schedule, now=now):
                assert adapter is not None  # guaranteed by the gate
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
                created = True
    except IntegrityError:
        # Lost a race (duplicate planned point or an active Production
        # Execution created concurrently): the savepoint rolled back, the
        # cursor advance below still commits, so the point is never retried.
        logger.info(
            "schedule execution race lost for adapter %s at %s",
            schedule.adapter_id,
            due_point.isoformat(),
        )
        created = False
    # The cursor advance commits in every outcome (created / gate skipped /
    # race lost), so a missed window is never replayed period by period.
    session.commit()
    return created


def scheduler_tick(session: Session, *, now: datetime | None = None) -> int:
    """One scheduler poll: process every due enabled Schedule once.

    ``FOR UPDATE SKIP LOCKED`` makes concurrent scheduler loops partition
    due Schedules instead of fighting over them; the scheduled_for unique
    index remains the final duplicate defense. Returns the number of
    Schedule Executions created this tick.
    """
    if now is None:
        now = worker_availability.current_time(session)
    due_schedules = session.scalars(
        select(AdapterSchedule)
        .where(
            AdapterSchedule.enabled.is_(True),
            AdapterSchedule.next_run_at.is_not(None),
            AdapterSchedule.next_run_at <= now,
        )
        .order_by(AdapterSchedule.next_run_at, AdapterSchedule.id)
        .with_for_update(skip_locked=True)
    ).all()
    created = 0
    for schedule in due_schedules:
        try:
            if process_due_schedule(session, schedule, now=now):
                created += 1
        except Exception:
            # One broken Schedule must never stall the whole tick; release
            # its state and continue with the remaining due rows.
            logger.exception("scheduler tick failed for schedule %s", schedule.id)
            session.rollback()
    return created


async def scheduler_loop() -> None:
    """The Control background polling loop (PostgreSQL-only state source).

    Runs until cancelled; each tick opens its own short-lived session. No
    Redis/Celery/APScheduler — a sleep-and-poll task is the whole scheduler.
    """
    logger.info(
        "schedule loop started (poll every %.2fs)", settings.schedule_poll_seconds
    )
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
