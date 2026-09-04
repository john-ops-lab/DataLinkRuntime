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

- Worker effectively offline / an active Execution (transient
  gate failures): the planned point stays due; nothing is written and
  nothing is queued. On recovery the latest planned point up to now is
  created at most once, never replayed per period.
- Control downtime: no ticks run; on recovery the latest planned point in
  the missed window is created at most once.
- Structural gate failures (deleted, wrong Adapter type, missing Revision/Worker
  pointers, capability mismatch, oversized input) consume the
  point: the cursor advances and the point is skipped without queueing.
- Explicit disable / cron edit / enable re-base the cursor to the
  next future planned point, so the missed window is skipped entirely.
"""

import asyncio
import enum
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    AdapterInputConfig,
    AdapterSchedule,
    Execution,
    ScheduleDispatchOutcome,
    Worker,
)
from dlr.control.schemas.schedule import ScheduleUpsert
from dlr.control.services import (
    adapter_runtime,
    worker_availability,
)
from dlr.control.services import (
    input_config as input_config_service,
)
from dlr.control.services.adapter import (
    _require_not_archived,
    domain_error,
)
from dlr.control.services.execution import (
    LEGACY_INPUT_COMPAT_METRICS,
    _create_execution_locked,
    integrity_constraint_name,
)
from dlr.control.services.reliable_execution import resolve_queue_target_worker

logger = logging.getLogger("dlr.control.schedule")

# M5.2 v1: exactly 5 whitespace-separated fields
# (minute hour day-of-month month day-of-week). Seconds, years, keywords and
# macros are rejected so the contract stays fixed.
CRON_FIELD_COUNT = 5


class ScheduleTickResult(enum.Enum):
    """Outcome of processing one due Schedule row inside its transaction."""

    CREATED = "created"  # Execution created; cursor advanced; commit
    CONSUMED = "consumed"  # point consumed without an Execution; cursor advanced; commit
    BLOCKED = "blocked"  # capacity is temporary; metadata commits, cursor stays due
    HELD = "held"  # transient block; nothing written; roll back, the point stays due


@dataclass(frozen=True)
class _DuePointWindow:
    """A bounded, ascending page of due points.

    Five-field cron has a one-minute minimum period and the configured
    catch-up age is at most seven days.  The page is therefore large enough to
    hold every point in the age window.  If a cursor is older than that, one
    page of expired points is committed and the next tick continues from the
    following point.  This keeps every transaction bounded without dropping an
    arbitrarily old point on a direct cursor jump.
    """

    points: tuple[datetime, ...]
    next_point: datetime | None
    truncated: bool


@dataclass(frozen=True)
class _DuePointGroup:
    """One exact outcome group with a bounded concrete tail."""

    points: tuple[datetime, ...]
    first: datetime | None
    last: datetime | None
    count: int


# Five-field cron cannot produce points more frequently than once per minute.
# Add two boundary slots for inclusive endpoints and timezone transitions.
SCHEDULE_AUDIT_PAGE_SIZE = 604_800 // 60 + 2
MAX_SCHEDULE_OUTCOMES_PER_VALIDATION = 1_000


class ScheduleOutcomeValidationError(ValueError):
    """A persisted Schedule outcome cannot be reconstructed from its snapshot."""


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


def _due_points(
    cron: str,
    tz_name: str,
    since: datetime,
    now: datetime,
    *,
    limit: int | None = None,
) -> _DuePointWindow:
    """Return one bounded ascending page of due points.

    ``limit`` is retained as a private compatibility argument for callers
    from the first B1 implementation, but a caller cannot lower the hard
    audit bound.  A page that reaches the bound reports the next concrete
    point when it is still due, allowing the caller to continue without
    enumerating an unbounded outage in one transaction.
    """
    page_limit = SCHEDULE_AUDIT_PAGE_SIZE if limit is None else max(limit, SCHEDULE_AUDIT_PAGE_SIZE)
    # Five-field cron has a minimum one-minute period, so the hard bound above
    # is sufficient for every configured catch-up-age window.  Keep the
    # argument bounded even if a future internal caller passes a bad value.
    page_limit = min(page_limit, SCHEDULE_AUDIT_PAGE_SIZE)
    since_utc = since.astimezone(UTC)
    now_utc = now.astimezone(UTC)
    if since_utc > now_utc:
        return _DuePointWindow((), None, False)

    # ``next_run_at`` is a durable planned cursor.  Include it explicitly so
    # a cursor that is exactly due is never lost to croniter's strict
    # ``get_next`` semantics (and keep old test/deployment rows fail-safe if a
    # legacy cursor is not perfectly aligned to a cron minute).
    points: list[datetime] = [since_utc]
    iterator = croniter(
        cron,
        (since_utc - timedelta(microseconds=1)).astimezone(_local_tz(tz_name)),
    )
    next_point: datetime | None = None
    while len(points) < page_limit:
        candidate = cast(datetime, iterator.get_next(datetime)).astimezone(UTC)
        if candidate > now_utc:
            break
        if candidate > points[-1]:
            points.append(candidate)
    if len(points) == page_limit:
        candidate = cast(datetime, iterator.get_next(datetime)).astimezone(UTC)
        if candidate <= now_utc:
            next_point = candidate
    return _DuePointWindow(tuple(points), next_point, next_point is not None)


def _record_outcome(
    session: Session,
    schedule: AdapterSchedule,
    *,
    first: datetime,
    last: datetime,
    count: int,
    outcome: str,
    reason: str | None = None,
    execution_id: int | None = None,
) -> None:
    """Persist one bounded, reconstructible Schedule outcome range."""
    if count < 1:
        return
    record = ScheduleDispatchOutcome(
        schedule_id=schedule.id,
        first_scheduled_for=first,
        last_scheduled_for=last,
        occurrence_count=count,
        outcome=outcome,
        reason=reason,
        cron_snapshot=schedule.cron,
        timezone_snapshot=schedule.timezone,
        execution_id=execution_id,
    )
    validate_schedule_outcome(record)
    session.add(record)


def _record_point_outcomes(
    session: Session,
    schedule: AdapterSchedule,
    outcomes: Sequence[tuple[datetime, str, str | None, int | None]],
) -> None:
    """Aggregate adjacent point outcomes without losing their exact range."""
    if not outcomes:
        return
    start, outcome, reason, execution_id = outcomes[0]
    last = start
    count = 1

    def flush() -> None:
        _record_outcome(
            session,
            schedule,
            first=start,
            last=last,
            count=count,
            outcome=outcome,
            reason=reason,
            execution_id=execution_id,
        )

    for point, point_outcome, point_reason, point_execution_id in outcomes[1:]:
        # An Execution id makes the result a one-point enqueued fact.  Other
        # adjacent points with the same outcome/reason can be represented by
        # one reconstructible range, keeping a long page bounded in rows.
        can_extend = (
            execution_id is None
            and point_execution_id is None
            and point_outcome == outcome
            and point_reason == reason
        )
        if can_extend:
            last = point
            count += 1
            continue
        flush()
        start, outcome, reason, execution_id = (
            point,
            point_outcome,
            point_reason,
            point_execution_id,
        )
        last = point
        count = 1
    flush()


def rebuild_schedule_outcome_points(
    outcome: ScheduleDispatchOutcome,
) -> tuple[datetime, ...]:
    """Rebuild one outcome's exact bounded point sequence from its snapshots."""
    first = outcome.first_scheduled_for
    last = outcome.last_scheduled_for
    count = outcome.occurrence_count
    if first.tzinfo is None or last.tzinfo is None:
        raise ScheduleOutcomeValidationError("Schedule outcome timestamps must be timezone-aware")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 1
        or count > SCHEDULE_AUDIT_PAGE_SIZE
    ):
        raise ScheduleOutcomeValidationError("Schedule outcome count exceeds the bounded page")
    try:
        cron = validate_cron(outcome.cron_snapshot)
        timezone = validate_timezone(outcome.timezone_snapshot)
    except ValueError as exc:
        raise ScheduleOutcomeValidationError("Schedule outcome snapshot is invalid") from exc
    first_utc = first.astimezone(UTC)
    last_utc = last.astimezone(UTC)
    if last_utc < first_utc:
        raise ScheduleOutcomeValidationError("Schedule outcome range is reversed")

    # The durable cursor is included as the first audit point even for an old
    # legacy row that was not minute-aligned.  Subsequent points are rebuilt
    # with the frozen five-field cron/timezone snapshot.  The loop is bounded
    # by the same hard page cap as scheduler processing.
    points: list[datetime] = [first_utc]
    iterator = croniter(
        cron,
        (first_utc - timedelta(microseconds=1)).astimezone(_local_tz(timezone)),
    )
    attempts = 0
    max_attempts = count * 2 + 2
    while len(points) < count and attempts < max_attempts:
        attempts += 1
        candidate = cast(datetime, iterator.get_next(datetime)).astimezone(UTC)
        if candidate > points[-1]:
            points.append(candidate)
    if len(points) != count:
        raise ScheduleOutcomeValidationError(
            "Schedule outcome cron sequence is not reconstructible"
        )
    if points[-1] != last_utc:
        raise ScheduleOutcomeValidationError("Schedule outcome last point does not match snapshot")
    return tuple(points)


def validate_schedule_outcome(outcome: ScheduleDispatchOutcome) -> None:
    """Fail closed if one persisted outcome's count/first/last is inconsistent."""
    rebuild_schedule_outcome_points(outcome)


def validate_schedule_outcomes(outcomes: Sequence[ScheduleDispatchOutcome]) -> None:
    """Validate a bounded ordered set and reject overlapping audit ranges."""
    if len(outcomes) > MAX_SCHEDULE_OUTCOMES_PER_VALIDATION:
        raise ScheduleOutcomeValidationError("Schedule outcome validation batch is too large")
    previous: tuple[int, datetime] | None = None
    for outcome in sorted(outcomes, key=lambda row: (row.schedule_id, row.first_scheduled_for)):
        points = rebuild_schedule_outcome_points(outcome)
        first = points[0]
        if previous is not None and outcome.schedule_id == previous[0] and first <= previous[1]:
            raise ScheduleOutcomeValidationError("Schedule outcome ranges overlap")
        previous = (outcome.schedule_id, points[-1])


def _set_schedule_blocked(
    schedule: AdapterSchedule,
    *,
    reason: str,
    detail: dict[str, object] | None,
    now: datetime,
) -> None:
    schedule.last_blocked_reason = reason
    schedule.last_blocked_detail = detail
    schedule.last_blocked_at = now


def _rabbit_outstanding(session: Session, adapter_id: int) -> bool:
    """Whether a RabbitMQ Adapter has any logical work not yet terminal."""
    return (
        session.scalar(
            select(Execution.id).where(
                Execution.adapter_id == adapter_id,
                Execution.dispatch_backend == "rabbitmq",
                Execution.status.in_(("queued", "running", "retry_wait")),
            )
        )
        is not None
    )


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
    if adapter.adapter_type != "task":
        raise domain_error(409, "adapter_type_mismatch", "Only task Adapters support Schedule")
    if adapter.run_mode != "schedule":
        raise domain_error(
            409,
            "schedule_mode_required",
            "Switch the Task to schedule mode before configuring Schedule",
        )
    _require_not_archived(adapter)
    try:
        cron = validate_cron(data.cron)
    except ValueError as exc:
        raise domain_error(
            422,
            "schedule_invalid_cron",
            str(exc),
            {"reason": str(exc)},
        ) from None
    try:
        tz_name = validate_timezone(data.timezone)
    except ValueError as exc:
        raise domain_error(
            422,
            "schedule_invalid_timezone",
            str(exc),
            {"reason": str(exc)},
        ) from None
    if "input" in data.model_fields_set and not settings.legacy_input_compat_enabled:
        raise domain_error(
            422,
            "execution_input_override_not_supported",
            "Schedule input overrides are disabled; save the Adapter input first",
        )
    if data.enabled and adapter.latest_version_id is None:
        raise domain_error(
            409,
            "adapter_has_no_version",
            "Save a Revision before enabling Schedule",
        )
    if data.enabled and adapter.runtime_worker_id is None:
        raise domain_error(
            409,
            "runtime_worker_required",
            "Select a runtime Worker before enabling Schedule",
        )
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
            "input_config_not_initialized",
            "Adapter input configuration is not initialized",
        )
    legacy_input_present = "input" in data.model_fields_set
    legacy_input_changed = legacy_input_present and (
        config.source_type != "json" or config.json_value != data.input
    )
    effective_misfire_policy: str
    if schedule is None:
        effective_misfire_policy = data.misfire_policy or "coalesce_latest"
        effective_max_catchup_count = data.max_catchup_count or 100
        effective_max_catchup_age_seconds = data.max_catchup_age_seconds or 86_400
    else:
        effective_misfire_policy = data.misfire_policy or schedule.misfire_policy
        effective_max_catchup_count = data.max_catchup_count or schedule.max_catchup_count
        effective_max_catchup_age_seconds = (
            data.max_catchup_age_seconds or schedule.max_catchup_age_seconds
        )
    if adapter_runtime.adapter_runtime_locked(session, adapter):
        disable_only = (
            schedule is not None
            and schedule.enabled
            and not data.enabled
            and cron == schedule.cron
            and tz_name == schedule.timezone
            and not legacy_input_changed
            and effective_misfire_policy == schedule.misfire_policy
            and effective_max_catchup_count == schedule.max_catchup_count
            and effective_max_catchup_age_seconds == schedule.max_catchup_age_seconds
        )
        if not disable_only:
            adapter_runtime.require_runtime_unlocked(session, adapter)
    if legacy_input_present and settings.legacy_input_compat_enabled:
        if legacy_input_changed:
            input_config_service.apply_legacy_schedule_input_locked(
                session,
                config,
                schedule,
                data.input,
            )
        LEGACY_INPUT_COMPAT_METRICS["schedule_input"] += 1
        logger.info(
            "legacy_input_compat deprecated operation=schedule_input adapter_id=%s",
            adapter_id,
        )
    if data.enabled:
        input_config_service.validate_saved_config(config, session=session)
    now = worker_availability.current_time(session)
    next_run_at = next_run_after(cron, tz_name, now) if data.enabled else None
    if schedule is None:
        schedule = AdapterSchedule(adapter_id=adapter_id)
        session.add(schedule)
    schedule.cron = cron
    schedule.timezone = tz_name
    schedule.input = input_config_service._legacy_schedule_value(config)
    schedule.enabled = data.enabled
    schedule.next_run_at = next_run_at
    schedule.misfire_policy = effective_misfire_policy
    schedule.max_catchup_count = effective_max_catchup_count
    schedule.max_catchup_age_seconds = effective_max_catchup_age_seconds
    schedule.last_blocked_reason = None
    schedule.last_blocked_detail = None
    schedule.last_blocked_at = None
    schedule.last_processed_due_at = None
    session.commit()
    session.refresh(schedule)
    return schedule


# --- Scheduler tick ---------------------------------------------------------------


def _structural_gate_failure(session: Session, adapter: Adapter, schedule: AdapterSchedule) -> bool:
    """Explicit lifecycle/configuration states that consume the due point.

    These reflect deliberate administrator state or broken configuration; a due point is skipped by
    advancing the cursor, never queued. Disable / edit additionally
    re-base the cursor, so their closed windows are never caught up.
    """
    if (
        adapter.archived_at is not None
        or adapter.adapter_type != "task"
        or adapter.run_mode != "schedule"
    ):
        return True
    if adapter.latest_version_id is None or adapter.runtime_worker_id is None:
        return True
    worker = session.get(Worker, adapter.runtime_worker_id)
    if worker is None or adapter.language not in worker.capabilities:
        return True
    if settings.rabbitmq_execution_enabled:
        try:
            resolve_queue_target_worker(session, adapter)
        except HTTPException:
            return True
    return False


def _transient_gate_failure(session: Session, adapter: Adapter, *, now: datetime) -> bool:
    """Temporary conditions that keep the planned point due (no queueing).

    A stale Worker or a still-running Execution recovers on its
    own; the point must stay due so recovery creates exactly the latest
    planned point up to now, immediately.
    """
    if settings.rabbitmq_execution_enabled:
        # RabbitMQ ingress intentionally accepts an effective-offline fixed
        # Worker; the durable queue is the short-term buffer.  Only the
        # legacy HTTP Claim path waits for online health here.
        return False
    worker = session.get(Worker, cast(int, adapter.runtime_worker_id))
    if worker is None or not worker_availability.is_effectively_online(worker, now=now):
        return True
    return adapter_runtime.active_execution(session, adapter.id) is not None


def _process_structural_due_schedule(
    session: Session,
    schedule: AdapterSchedule,
    *,
    since: datetime,
    now: datetime,
) -> ScheduleTickResult:
    """Consume one bounded page when a Schedule cannot ever be dispatched.

    Structural failures are not temporary capacity pressure: every point in
    the page is explicitly skipped and the cursor advances only to the next
    concrete point represented by the page.  This gives long invalid/backlog
    ranges the same reconstructible outcome semantics as RabbitMQ policies.
    """
    window = _due_points(schedule.cron, schedule.timezone, since, now)
    if not window.points:
        return ScheduleTickResult.HELD
    _record_outcome(
        session,
        schedule,
        first=window.points[0],
        last=window.points[-1],
        count=len(window.points),
        outcome="skipped",
        reason="runtime_worker_invalid",
    )
    schedule.next_run_at = window.next_point or next_run_after(
        schedule.cron, schedule.timezone, now
    )
    schedule.last_processed_due_at = window.points[-1]
    _set_schedule_blocked(schedule, reason="runtime_worker_invalid", detail=None, now=now)
    return ScheduleTickResult.CONSUMED


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
        return _process_structural_due_schedule(session, schedule, since=since, now=now)
    if _transient_gate_failure(session, adapter, now=now):
        return ScheduleTickResult.HELD
    if not settings.rabbitmq_execution_enabled:
        return _process_legacy_due_schedule(session, adapter, schedule, since=since, now=now)
    return _process_rabbitmq_due_schedule(session, adapter, schedule, since=since, now=now)


def _process_legacy_due_schedule(
    session: Session,
    adapter: Adapter,
    schedule: AdapterSchedule,
    *,
    since: datetime,
    now: datetime,
) -> ScheduleTickResult:
    """Keep the pre-B1 single-run Scheduler behavior unchanged."""
    due_point = latest_due_point(schedule.cron, schedule.timezone, since, now)
    try:
        with session.begin_nested():
            _create_execution_locked(
                session,
                adapter,
                trigger="schedule",
                scheduled_for=due_point,
                schedule=schedule,
            )
    except HTTPException as exc:
        detail: dict[str, object] = exc.detail if isinstance(exc.detail, dict) else {}
        code = detail.get("code")
        if code in {
            "input_invalid",
            "input_source_not_available",
            "execution_input_too_large",
        }:
            schedule.next_run_at = next_run_after(schedule.cron, schedule.timezone, now)
            schedule.last_blocked_reason = "input_invalid"
            params = detail.get("params")
            blocked_detail = dict(params) if isinstance(params, dict) else {}
            if code != "input_invalid":
                blocked_detail = {"code": str(code), **blocked_detail}
            schedule.last_blocked_detail = blocked_detail or None
            schedule.last_blocked_at = now
            schedule.last_processed_due_at = due_point
            return ScheduleTickResult.CONSUMED
        raise
    except IntegrityError as exc:
        if integrity_constraint_name(exc) not in {
            "uq_executions_active_adapter",
            "uq_executions_schedule_point",
        }:
            raise
        # Lost a race (duplicate planned point or an active Execution): the
        # cursor advance still commits, so the point is never retried.
        logger.info(
            "schedule execution race lost for adapter %s at %s",
            adapter.id,
            due_point.isoformat(),
        )
        schedule.next_run_at = next_run_after(schedule.cron, schedule.timezone, now)
        schedule.last_processed_due_at = due_point
        schedule.last_blocked_reason = None
        schedule.last_blocked_detail = None
        schedule.last_blocked_at = None
        return ScheduleTickResult.CONSUMED
    schedule.next_run_at = next_run_after(schedule.cron, schedule.timezone, now)
    schedule.last_processed_due_at = due_point
    schedule.last_blocked_reason = None
    schedule.last_blocked_detail = None
    schedule.last_blocked_at = None
    return ScheduleTickResult.CREATED


def _schedule_http_error(exc: HTTPException) -> tuple[str, dict[str, object]]:
    detail: dict[str, object] = exc.detail if isinstance(exc.detail, dict) else {}
    code = str(detail.get("code", "schedule_dispatch_failed"))
    params = detail.get("params")
    return code, dict(params) if isinstance(params, dict) else {}


def _process_rabbitmq_due_schedule(
    session: Session,
    adapter: Adapter,
    schedule: AdapterSchedule,
    *,
    since: datetime,
    now: datetime,
) -> ScheduleTickResult:
    """Apply the explicit B1 policy to a bounded due-point window."""
    window = _due_points(schedule.cron, schedule.timezone, since, now)
    if not window.points:
        return ScheduleTickResult.HELD
    cutoff = now - timedelta(seconds=int(schedule.max_catchup_age_seconds))
    expired_points = tuple(point for point in window.points if point < cutoff)
    eligible_points = tuple(point for point in window.points if point >= cutoff)
    expired = _DuePointGroup(
        expired_points,
        expired_points[0] if expired_points else None,
        expired_points[-1] if expired_points else None,
        len(expired_points),
    )
    eligible = _DuePointGroup(
        eligible_points,
        eligible_points[0] if eligible_points else None,
        eligible_points[-1] if eligible_points else None,
        len(eligible_points),
    )
    if not eligible.points:
        if expired.count > 0:
            _record_outcome(
                session,
                schedule,
                first=cast(datetime, expired.first),
                last=cast(datetime, expired.last),
                count=expired.count,
                outcome="expired",
                reason="catchup_age",
            )
        schedule.next_run_at = window.next_point or next_run_after(
            schedule.cron, schedule.timezone, now
        )
        schedule.last_processed_due_at = window.points[-1]
        schedule.last_blocked_reason = None
        schedule.last_blocked_detail = None
        schedule.last_blocked_at = None
        return ScheduleTickResult.CONSUMED

    # The configured seven-day age window fits in one hard page for a
    # five-field cron.  If a future change violates that invariant, consume
    # only the expired prefix and leave the first eligible point due rather
    # than silently advancing across an unaudited eligible range.
    if window.truncated:
        if expired.count == 0:
            return ScheduleTickResult.HELD
        _record_outcome(
            session,
            schedule,
            first=cast(datetime, expired.first),
            last=cast(datetime, expired.last),
            count=expired.count,
            outcome="expired",
            reason="catchup_age",
        )
        schedule.next_run_at = cast(datetime, eligible.first)
        schedule.last_processed_due_at = cast(datetime, expired.last)
        return ScheduleTickResult.CONSUMED

    if schedule.misfire_policy == "coalesce_latest":
        return _process_coalesce_latest(
            session, adapter, schedule, eligible=eligible, expired=expired, now=now
        )
    if schedule.misfire_policy == "skip_while_busy":
        return _process_skip_while_busy(
            session, adapter, schedule, eligible=eligible, expired=expired, now=now
        )
    return _process_queue_every_occurrence(
        session, adapter, schedule, eligible=eligible, expired=expired, now=now, since=since
    )


def _process_coalesce_latest(
    session: Session,
    adapter: Adapter,
    schedule: AdapterSchedule,
    *,
    eligible: _DuePointGroup,
    expired: _DuePointGroup,
    now: datetime,
) -> ScheduleTickResult:
    """Queue the newest eligible point and audit all earlier points."""
    latest = cast(datetime, eligible.last)
    try:
        with session.begin_nested():
            execution = _create_execution_locked(
                session,
                adapter,
                trigger="schedule",
                scheduled_for=latest,
                schedule=schedule,
            )
    except HTTPException as exc:
        code, params = _schedule_http_error(exc)
        if code in {"adapter_queue_full", "runtime_capacity_full", "outbox_backlog_full"}:
            _set_schedule_blocked(schedule, reason=code, detail=params or None, now=now)
            return ScheduleTickResult.BLOCKED
        if code in {"input_invalid", "input_source_not_available", "execution_input_too_large"}:
            if expired.count > 0:
                _record_outcome(
                    session,
                    schedule,
                    first=cast(datetime, expired.first),
                    last=cast(datetime, expired.last),
                    count=expired.count,
                    outcome="expired",
                    reason="catchup_age",
                )
            if eligible.count > 1:
                _record_outcome(
                    session,
                    schedule,
                    first=cast(datetime, eligible.first),
                    last=eligible.points[-2],
                    count=eligible.count - 1,
                    outcome="coalesced",
                    reason="coalesce_latest",
                )
            _record_outcome(
                session,
                schedule,
                first=latest,
                last=latest,
                count=1,
                outcome="skipped",
                reason="input_invalid",
            )
            schedule.next_run_at = next_run_after(schedule.cron, schedule.timezone, now)
            schedule.last_processed_due_at = latest
            _set_schedule_blocked(schedule, reason="input_invalid", detail=params or None, now=now)
            return ScheduleTickResult.CONSUMED
        raise
    except IntegrityError as exc:
        if integrity_constraint_name(exc) not in {
            "uq_executions_schedule_point",
            "uq_executions_active_adapter",
        }:
            raise
        execution = None
    if expired.count > 0:
        _record_outcome(
            session,
            schedule,
            first=cast(datetime, expired.first),
            last=cast(datetime, expired.last),
            count=expired.count,
            outcome="expired",
            reason="catchup_age",
        )
    if eligible.count > 1:
        _record_outcome(
            session,
            schedule,
            first=cast(datetime, eligible.first),
            last=eligible.points[-2],
            count=eligible.count - 1,
            outcome="coalesced",
            reason="coalesce_latest",
        )
    _record_outcome(
        session,
        schedule,
        first=latest,
        last=latest,
        count=1,
        outcome="enqueued",
        reason="accepted" if execution is not None else "duplicate",
        execution_id=execution.id if execution is not None else None,
    )
    schedule.next_run_at = next_run_after(schedule.cron, schedule.timezone, now)
    schedule.last_processed_due_at = latest
    schedule.last_blocked_reason = None
    schedule.last_blocked_detail = None
    schedule.last_blocked_at = None
    return ScheduleTickResult.CREATED


def _process_skip_while_busy(
    session: Session,
    adapter: Adapter,
    schedule: AdapterSchedule,
    *,
    eligible: _DuePointGroup,
    expired: _DuePointGroup,
    now: datetime,
) -> ScheduleTickResult:
    """Consume eligible points while RabbitMQ work keeps the Adapter busy."""
    if _rabbit_outstanding(session, adapter.id):
        if expired.count > 0:
            _record_outcome(
                session,
                schedule,
                first=cast(datetime, expired.first),
                last=cast(datetime, expired.last),
                count=expired.count,
                outcome="expired",
                reason="catchup_age",
            )
        _record_outcome(
            session,
            schedule,
            first=cast(datetime, eligible.first),
            last=cast(datetime, eligible.last),
            count=eligible.count,
            outcome="skipped",
            reason="adapter_busy",
        )
        schedule.next_run_at = next_run_after(schedule.cron, schedule.timezone, now)
        schedule.last_processed_due_at = cast(datetime, eligible.last)
        _set_schedule_blocked(schedule, reason="adapter_busy", detail=None, now=now)
        return ScheduleTickResult.CONSUMED
    if expired.count > 0:
        _record_outcome(
            session,
            schedule,
            first=cast(datetime, expired.first),
            last=cast(datetime, expired.last),
            count=expired.count,
            outcome="expired",
            reason="catchup_age",
        )
    point_outcomes: list[tuple[datetime, str, str | None, int | None]] = []
    input_invalid_seen = False
    for index, point in enumerate(eligible.points):
        try:
            with session.begin_nested():
                execution = _create_execution_locked(
                    session,
                    adapter,
                    trigger="schedule",
                    scheduled_for=point,
                    schedule=schedule,
                )
        except HTTPException as exc:
            code, params = _schedule_http_error(exc)
            if code in {"adapter_queue_full", "runtime_capacity_full", "outbox_backlog_full"}:
                point_outcomes.extend(
                    (tail, "skipped", "admission_full", None) for tail in eligible.points[index:]
                )
                _record_point_outcomes(session, schedule, point_outcomes)
                schedule.next_run_at = next_run_after(schedule.cron, schedule.timezone, now)
                schedule.last_processed_due_at = cast(datetime, eligible.last)
                _set_schedule_blocked(schedule, reason=code, detail=params or None, now=now)
                return ScheduleTickResult.CONSUMED
            if code in {"input_invalid", "input_source_not_available", "execution_input_too_large"}:
                point_outcomes.append((point, "skipped", "input_invalid", None))
                input_invalid_seen = True
                continue
            raise
        except IntegrityError as exc:
            constraint = integrity_constraint_name(exc)
            if constraint not in {"uq_executions_schedule_point", "uq_executions_active_adapter"}:
                raise
            # A schedule-point duplicate is only a duplicate audit fact when
            # the existing point is terminal.  If another RabbitMQ row is
            # still outstanding, this point and its tail were rejected by the
            # same Adapter-busy condition and must not be mislabeled.
            if constraint == "uq_executions_active_adapter" or _rabbit_outstanding(
                session, adapter.id
            ):
                point_outcomes.extend(
                    (tail, "skipped", "adapter_busy", None) for tail in eligible.points[index:]
                )
                _record_point_outcomes(session, schedule, point_outcomes)
                schedule.next_run_at = next_run_after(schedule.cron, schedule.timezone, now)
                schedule.last_processed_due_at = cast(datetime, eligible.last)
                _set_schedule_blocked(schedule, reason="adapter_busy", detail=None, now=now)
                return ScheduleTickResult.CONSUMED
            point_outcomes.append((point, "skipped", "duplicate", None))
            continue

        point_outcomes.append((point, "enqueued", "accepted", execution.id))
        # The first accepted point makes the Adapter busy.  Consume the rest
        # in ascending order as adapter_busy instead of trying the newest
        # point first or falsely attributing earlier points to busy pressure.
        point_outcomes.extend(
            (tail, "skipped", "adapter_busy", None) for tail in eligible.points[index + 1 :]
        )
        _record_point_outcomes(session, schedule, point_outcomes)
        schedule.next_run_at = next_run_after(schedule.cron, schedule.timezone, now)
        schedule.last_processed_due_at = cast(datetime, eligible.last)
        schedule.last_blocked_reason = None
        schedule.last_blocked_detail = None
        schedule.last_blocked_at = None
        return ScheduleTickResult.CREATED

    _record_point_outcomes(session, schedule, point_outcomes)
    schedule.next_run_at = next_run_after(schedule.cron, schedule.timezone, now)
    schedule.last_processed_due_at = cast(datetime, eligible.last)
    if input_invalid_seen:
        _set_schedule_blocked(schedule, reason="input_invalid", detail=None, now=now)
    else:
        schedule.last_blocked_reason = None
        schedule.last_blocked_detail = None
        schedule.last_blocked_at = None
    return ScheduleTickResult.CONSUMED


def _process_queue_every_occurrence(
    session: Session,
    adapter: Adapter,
    schedule: AdapterSchedule,
    *,
    eligible: _DuePointGroup,
    expired: _DuePointGroup,
    now: datetime,
    since: datetime,
) -> ScheduleTickResult:
    """Queue points in order and stop at the first unaccepted point."""
    if expired.count > 0:
        _record_outcome(
            session,
            schedule,
            first=cast(datetime, expired.first),
            last=cast(datetime, expired.last),
            count=expired.count,
            outcome="expired",
            reason="catchup_age",
        )
    max_count = int(schedule.max_catchup_count)
    queue_points = eligible.points
    last_processed_point: datetime | None = expired.last
    if eligible.count > max_count:
        skipped_count = eligible.count - max_count
        _record_outcome(
            session,
            schedule,
            first=cast(datetime, eligible.first),
            last=eligible.points[-max_count - 1],
            count=skipped_count,
            outcome="expired",
            reason="catchup_limit",
        )
        queue_points = eligible.points[-max_count:]
        last_processed_point = eligible.points[-max_count - 1]
    created = 0
    first_unaccepted: datetime | None = None
    for point in queue_points:
        try:
            with session.begin_nested():
                execution = _create_execution_locked(
                    session,
                    adapter,
                    trigger="schedule",
                    scheduled_for=point,
                    schedule=schedule,
                )
        except HTTPException as exc:
            code, params = _schedule_http_error(exc)
            if code in {"adapter_queue_full", "runtime_capacity_full", "outbox_backlog_full"}:
                first_unaccepted = point
                _set_schedule_blocked(schedule, reason=code, detail=params or None, now=now)
                break
            if code in {
                "input_invalid",
                "input_source_not_available",
                "execution_input_too_large",
            }:
                _record_outcome(
                    session,
                    schedule,
                    first=point,
                    last=point,
                    count=1,
                    outcome="skipped",
                    reason="input_invalid",
                )
                last_processed_point = point
                continue
            raise
        except IntegrityError as exc:
            if integrity_constraint_name(exc) not in {
                "uq_executions_schedule_point",
                "uq_executions_active_adapter",
            }:
                raise
            _record_outcome(
                session,
                schedule,
                first=point,
                last=point,
                count=1,
                outcome="skipped",
                reason="duplicate",
            )
            last_processed_point = point
            continue
        _record_outcome(
            session,
            schedule,
            first=point,
            last=point,
            count=1,
            outcome="enqueued",
            reason="accepted",
            execution_id=execution.id,
        )
        created += 1
        last_processed_point = point
    if first_unaccepted is not None:
        schedule.next_run_at = first_unaccepted
        if last_processed_point is not None:
            schedule.last_processed_due_at = last_processed_point
        return ScheduleTickResult.CREATED if created else ScheduleTickResult.BLOCKED
    schedule.next_run_at = next_run_after(schedule.cron, schedule.timezone, now)
    schedule.last_processed_due_at = eligible.last or expired.last or since
    schedule.last_blocked_reason = None
    schedule.last_blocked_detail = None
    schedule.last_blocked_at = None
    return ScheduleTickResult.CREATED if created else ScheduleTickResult.CONSUMED


def _process_due_row(schedule_id: int, adapter_id: int, *, now: datetime) -> int:
    """Process one due row in its own short transaction.

    Locks are taken in the platform order (Adapter first, Schedule second,
    matching Start / Stop / PUT Schedule) with SKIP LOCKED, and the due
    condition is re-checked inside the final locks. Returns the number of
    Schedule Executions created by this transaction.
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
            return 0
        schedule = session.scalar(
            select(AdapterSchedule)
            .where(AdapterSchedule.id == schedule_id)
            .with_for_update(skip_locked=True)
        )
        if schedule is None:
            return 0
        # Re-check inside the final locks: another scheduler may already
        # have processed the row, or a concurrent PUT may have disabled or
        # re-based it to the future.
        if not schedule.enabled or schedule.next_run_at is None or schedule.next_run_at > now:
            return 0
        before_created = int(
            session.scalar(
                select(func.count(Execution.id)).where(
                    Execution.adapter_id == adapter.id,
                    Execution.trigger == "schedule",
                )
            )
            or 0
        )
        outcome = process_due_schedule(session, adapter, schedule, now=now)
        if outcome is ScheduleTickResult.HELD:
            # Transient failure: roll back so the cursor (and the point)
            # stays due; recovery creates the latest point only.
            session.rollback()
            return 0
        after_created = int(
            session.scalar(
                select(func.count(Execution.id)).where(
                    Execution.adapter_id == adapter.id,
                    Execution.trigger == "schedule",
                )
            )
            or 0
        )
        created = after_created - before_created
        if created < 0:  # pragma: no cover - the Adapter lock prevents this
            raise RuntimeError("Schedule Execution count moved backwards")
        session.commit()
        return created
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
            created += _process_due_row(schedule_id, adapter_id, now=now)
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
