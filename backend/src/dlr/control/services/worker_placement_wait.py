"""A frozen placement cycle, separate from the execution Retry Policy."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import or_, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import Execution, ExecutionAttempt
from dlr.control.models.worker_capacity import ExecutionPlacementAction
from dlr.control.services.background_control import reserve_page
from dlr.control.services.control_ha_capacity import assert_local_control, recheck_local_control

if TYPE_CHECKING:
    from dlr.control.security import Principal

MAX_HELD_PLACEMENTS = 10
MAX_PLACEMENT_MISSES = 6


def start_cycle(execution: Execution, *, now: datetime, timeout_seconds: int | None) -> None:
    execution.placement_wait_timeout_seconds = timeout_seconds
    execution.placement_deadline_at = (
        now + timedelta(seconds=timeout_seconds) if timeout_seconds is not None else None
    )
    execution.placement_hold_count = 0
    execution.placement_misses = 0
    execution.placement_after_worker_id = None
    execution.placement_checked_at = None
    execution.next_placement_at = now
    execution.placement_reason = "capacity_unknown"


def expired(execution: Execution, *, now: datetime) -> bool:
    deadline = execution.placement_deadline_at
    return deadline is not None and deadline <= now


def can_place(execution: Execution, *, now: datetime, capacity_changed: bool = False) -> bool:
    return (
        not expired(execution, now=now)
        and execution.placement_hold_count < MAX_HELD_PLACEMENTS
        and (
            capacity_changed
            or execution.next_placement_at is None
            or execution.next_placement_at <= now
        )
    )


def defer(
    execution: Execution, *, now: datetime, reason: str, page_remaining: bool = False
) -> None:
    execution.placement_reason = reason
    execution.placement_checked_at = now
    delay = 0 if page_remaining else min(30, 2**execution.placement_misses)
    if not page_remaining:
        execution.placement_misses = min(MAX_PLACEMENT_MISSES, execution.placement_misses + 1)
    execution.next_placement_at = now + timedelta(seconds=delay)


def record_hold(execution: Execution) -> None:
    execution.placement_hold_count += 1
    execution.placement_misses = 0
    execution.placement_reason = None
    execution.next_placement_at = None


def reopen_after_revocation(execution: Execution, *, now: datetime) -> None:
    if execution.placement_hold_count >= MAX_HELD_PLACEMENTS:
        execution.placement_reason = "placement_limit_reached"
        execution.next_placement_at = None
    else:
        execution.next_placement_at = now
        execution.placement_reason = "capacity_unknown"


def audit_action(
    session: Session, execution: Execution, *, principal: Principal, action: str, now: datetime
) -> None:
    session.add(
        ExecutionPlacementAction(
            execution_id=execution.id,
            dispatch_generation=execution.dispatch_generation,
            action=action,
            actor_kind=principal.kind,
            actor_id=principal.user_id,
            created_at=now,
        )
    )


def retry_placement(
    session: Session, execution_id: int, expected_generation: int, principal: Principal
) -> Execution:
    """Start an explicitly authorized cycle on the same business responsibility."""
    from dlr.control.services import outbox, worker_placement_cutover
    from dlr.control.services.adapter import domain_error
    from dlr.control.services.adapter_access import require_adapter_access
    from dlr.control.services.execution_cancellation import (
        lock_execution_in_admission_order,
        lock_execution_tail,
    )
    from dlr.control.services.worker_availability import current_time

    execution = lock_execution_in_admission_order(session, execution_id)
    if execution is None:
        raise domain_error(404, "execution_not_found", "Execution not found")
    require_adapter_access(session, execution.adapter_id, principal, "edit")
    previous = session.get(ExecutionPlacementAction, (execution.id, expected_generation, "retry"))
    if previous is not None and (previous.actor_kind, previous.actor_id) == (
        principal.kind,
        principal.user_id,
    ):
        return execution
    now = current_time(session)
    if (
        worker_placement_cutover.mode(session, hold=True) != "reserved"
        or execution.dispatch_generation != expected_generation
        or not execution.requires_worker_reservation
        or execution.status != "queued"
        or execution.cancel_requested
        or execution.target_worker_id is not None
        or execution.placement_reason != "placement_limit_reached"
        or expired(execution, now=now)
    ):
        raise domain_error(409, "placement_retry_unavailable", "Placement retry is unavailable")
    tail = lock_execution_tail(session, execution)
    row = next((r for r in tail.outbox_rows if r.dispatch_generation == expected_generation), None)
    if (
        row is None
        or row.status != "awaiting_target"
        or any(item.status in {"claimed", "running"} for item in tail.attempts)
    ):
        raise domain_error(409, "placement_retry_unavailable", "Placement retry is unavailable")
    now = current_time(session)
    if expired(execution, now=now):
        raise domain_error(409, "placement_retry_unavailable", "Placement retry is unavailable")
    audit_action(session, execution, principal=principal, action="retry", now=now)
    outbox.supersede(row, "worker_placement_retry_requested")
    execution.dispatch_generation += 1
    execution.queued_at = now
    start_cycle(execution, now=now, timeout_seconds=execution.placement_wait_timeout_seconds)
    outbox.create_dispatch_outbox(session, execution, available_at=now)
    session.commit()
    return execution


def expire_queued(session: Session, execution_id: int) -> bool:
    """Settle the unstarted placement cycle without consuming an Attempt."""
    from dlr.control.services import admission, outbox, worker_reservation
    from dlr.control.services.attempt import ACTIVE_ATTEMPT_STATUSES, _create_holds_locked
    from dlr.control.services.execution import release_execution_leases
    from dlr.control.services.execution_cancellation import (
        lock_execution_in_admission_order,
        lock_execution_tail,
    )
    from dlr.control.services.incident_disposition import converge_terminal_dispositions_locked
    from dlr.control.services.subscription_business_batch import finalize_on_terminal
    from dlr.control.services.worker_availability import current_time

    execution = lock_execution_in_admission_order(session, execution_id)
    if (
        execution is None
        or not execution.requires_worker_reservation
        or execution.status != "queued"
    ):
        return False
    now = current_time(session)
    if not expired(execution, now=now):
        return False
    tail = lock_execution_tail(session, execution)
    if any(attempt.status in ACTIVE_ATTEMPT_STATUSES for attempt in tail.attempts):
        return False
    if tail.reservation is not None and tail.reservation.status not in {"held", "released"}:
        return False
    execution.status = "expired"
    execution.ended_at = now
    execution.next_placement_at = None
    execution.placement_reason = "placement_deadline_expired"
    execution.error_code = execution.last_error_code = "placement_deadline_expired"
    finalize_on_terminal(session, execution, not_started=execution.attempt_count == 0)
    _create_holds_locked(session, execution, now=now)
    if all(
        attempt.worker_identity_id is None or attempt.cleanup_settled_at is not None
        for attempt in tail.attempts
    ):
        release_execution_leases(session, execution.id)
    admission.release_admission_once(session, execution, now=now)
    worker_reservation.release_held(tail.reservation, now=now, capacity=tail.capacity)
    outbox.settle_pending_outbox(
        session,
        execution.id,
        disposition="execution_expired",
        now=now,
        locked_rows=tail.outbox_rows,
    )
    converge_terminal_dispositions_locked(session, execution, tail.incidents, now=now)
    return True


def _retry_execution(session: Session, execution_id: int) -> bool:
    from dlr.control.services import outbox
    from dlr.control.services.execution_cancellation import (
        lock_execution_in_admission_order,
        lock_execution_tail,
    )
    from dlr.control.services.worker_availability import current_time
    from dlr.control.services.worker_placement_cutover import mode

    execution = lock_execution_in_admission_order(session, execution_id)
    if (
        execution is None
        or not execution.requires_worker_reservation
        or execution.status != "retry_wait"
        or execution.cancel_requested
        or mode(session, hold=True) != "reserved"
    ):
        return False
    now = current_time(session)
    if execution.next_attempt_at is None or execution.next_attempt_at > now:
        return False
    tail = lock_execution_tail(session, execution)
    last = tail.attempts[-1] if tail.attempts else None
    if last is None or last.attempt_no != execution.attempt_count:
        return False
    if last.status not in {"failed", "timed_out", "worker_lost"}:
        return False
    if last.recovery_not_before is not None and now < last.recovery_not_before:
        return False
    if last.worker_identity_id is not None and last.cleanup_settled_at is None:
        # A Pool can fence an old Slot after grace while the separate resource
        # charge stays on the old Worker until physical reconciliation.
        reservation = tail.reservation
        if (
            execution.route_pool_id is None
            or last.status != "worker_lost"
            or last.recovery_not_before is None
            or reservation is None
            or reservation.status != "reclaiming"
            or reservation.attempt_id != last.id
            or tail.slot.active_attempt_id != last.id
            or tail.slot.fencing_token != last.fencing_token
        ):
            return False
        tail.slot.active_attempt_id = None
        tail.slot.lease_expires_at = None
    elif tail.slot.active_attempt_id is not None:
        return False
    for row in tail.outbox_rows:
        if row.dispatch_generation == execution.dispatch_generation and row.status != "superseded":
            outbox.supersede(row, "worker_placement_retry")
    execution.dispatch_generation += 1
    execution.target_worker_id = None
    execution.status = "queued"
    execution.queued_at = now
    execution.next_attempt_at = None
    start_cycle(execution, now=now, timeout_seconds=execution.placement_wait_timeout_seconds)
    outbox.create_dispatch_outbox(session, execution, available_at=now)
    return True


def retry_due(session: Session, *, limit: int = 100) -> int:
    """Start a fresh placement cycle only after the frozen execution retry."""
    from dlr.control.services.worker_availability import current_time
    from dlr.control.services.worker_placement_cutover import mode

    if not settings.worker_remote_v4_enabled or mode(session) != "reserved":
        session.rollback()
        return 0
    now = current_time(session)
    ids = reserve_page(
        session,
        "reserved_retry_due",
        select(Execution.id)
        .join(
            ExecutionAttempt,
            (ExecutionAttempt.execution_id == Execution.id)
            & (ExecutionAttempt.attempt_no == Execution.attempt_count),
        )
        .where(
            Execution.requires_worker_reservation,
            Execution.status == "retry_wait",
            Execution.next_attempt_at <= now,
            or_(
                ExecutionAttempt.recovery_not_before.is_(None),
                ExecutionAttempt.recovery_not_before <= now,
            ),
            or_(
                ExecutionAttempt.worker_identity_id.is_(None),
                ExecutionAttempt.cleanup_settled_at.is_not(None),
                (
                    Execution.route_pool_id.is_not(None)
                    & (ExecutionAttempt.status == "worker_lost")
                    & ExecutionAttempt.recovery_not_before.is_not(None)
                ),
            ),
        ),
        Execution.id,
        limit=max(1, min(limit, 100)),
    )
    moved = 0
    for execution_id in ids:
        try:
            session.execute(text("SET LOCAL lock_timeout = '1s'"))
            assert_local_control(session)
            if _retry_execution(session, execution_id):
                recheck_local_control(session)
                session.commit()
                moved += 1
            else:
                session.rollback()
        except OperationalError as error:
            session.rollback()
            if getattr(error.orig, "sqlstate", None) != "55P03":
                raise
    return moved
