"""Shared row-locking primitives for Execution cancellation.

Callers own the surrounding transaction. Admission-order locking serializes
cancellation with Claim and Attempt transitions.
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dlr.control.models import (
    AdapterExecutionSlot,
    Execution,
    ExecutionAttempt,
    ExecutionInfrastructureIncident,
    ExecutionOutbox,
)

ACTIVE_EXECUTION_STATUSES = ("running",)
RABBITMQ_CANCELLABLE_STATUSES = ("queued", "retry_wait")
RABBITMQ_NONTERMINAL_STATUSES = ("queued", "running", "retry_wait")
CANCELLATION_ERROR_CODE = "execution_cancelled"


@dataclass(frozen=True)
class ExecutionLockTail:
    """Rows after the Execution prefix, held in the canonical order."""

    attempts: tuple[ExecutionAttempt, ...]
    slot: AdapterExecutionSlot
    incidents: tuple[ExecutionInfrastructureIncident, ...]
    outbox_rows: tuple[ExecutionOutbox, ...]


def lock_nonterminal_executions(session: Session, adapter_id: int) -> list[Execution]:
    """Lock every non-terminal Execution for one Adapter in id order.

    An Adapter may have several ``queued`` or ``retry_wait`` rows, so a
    single-row active lookup is not a safe deletion barrier.
    """
    return list(
        session.scalars(
            select(Execution)
            .where(
                Execution.adapter_id == adapter_id,
                Execution.status.in_(RABBITMQ_NONTERMINAL_STATUSES),
            )
            .order_by(Execution.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )


def lock_execution(session: Session, execution_id: int) -> Execution | None:
    """Lock one Execution so its cancellation decision uses fresh state."""
    return session.scalar(
        select(Execution)
        .where(Execution.id == execution_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def lock_execution_attempts(session: Session, execution_id: int) -> tuple[ExecutionAttempt, ...]:
    """Lock all of one Execution's Attempts before the Adapter slot."""

    return tuple(
        session.scalars(
            select(ExecutionAttempt)
            .where(ExecutionAttempt.execution_id == execution_id)
            .order_by(ExecutionAttempt.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )


def lock_incidents_and_outbox(
    session: Session, execution_id: int
) -> tuple[tuple[ExecutionInfrastructureIncident, ...], tuple[ExecutionOutbox, ...]]:
    """Lock the durable incident/outbox suffix after Attempt and Slot locks."""

    incidents = tuple(
        session.scalars(
            select(ExecutionInfrastructureIncident)
            .where(ExecutionInfrastructureIncident.execution_id == execution_id)
            .order_by(ExecutionInfrastructureIncident.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    outbox_rows = tuple(
        session.scalars(
            select(ExecutionOutbox)
            .where(ExecutionOutbox.execution_id == execution_id)
            .order_by(ExecutionOutbox.dispatch_generation.asc(), ExecutionOutbox.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    return incidents, outbox_rows


def lock_execution_tail(session: Session, execution: Execution) -> ExecutionLockTail:
    """Lock Attempt→Slot→Incident→Outbox after the caller holds Execution."""

    attempts = lock_execution_attempts(session, execution.id)
    slot = session.scalar(
        select(AdapterExecutionSlot)
        .where(
            AdapterExecutionSlot.adapter_id == execution.adapter_id,
            AdapterExecutionSlot.slot_no == 0,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if slot is None:
        slot = AdapterExecutionSlot(adapter_id=execution.adapter_id, slot_no=0)
        session.add(slot)
        session.flush()
    incidents, outbox_rows = lock_incidents_and_outbox(session, execution.id)
    return ExecutionLockTail(attempts, slot, incidents, outbox_rows)


def lock_execution_in_admission_order(
    session: Session, execution_id: int, *, guard_reactivation: bool = False
) -> Execution | None:
    """Lock RabbitMQ cancellation scope before its Execution row.

    The initial lookup is deliberately non-locking: the immutable Adapter
    reference only identifies which Adapter row to lock next.  Once that
    Adapter and both Admission counters are held, the authoritative Execution
    row is locked.  This matches ingress, targeted reconciliation and
    stop/delete, preventing an Execution-first / counter-first cycle.
    """
    identity = session.execute(
        select(
            Execution.adapter_id,
            Execution.dispatch_backend,
            Execution.version_id,
            Execution.target_worker_id_snapshot,
            Execution.target_worker_id,
            Execution.worker_id,
        ).where(Execution.id == execution_id)
    ).one_or_none()
    if identity is None:
        return None
    (
        adapter_id,
        dispatch_backend,
        version_id,
        target_worker_id_snapshot,
        target_worker_id,
        actual_worker_id,
    ) = identity
    if dispatch_backend == "rabbitmq":
        from dlr.control.services import admission

        if admission.lock_admission_scope(session, int(adapter_id)) is None:
            return None
    if guard_reactivation:
        worker_id = target_worker_id_snapshot or target_worker_id or actual_worker_id
        if worker_id is None:
            from dlr.control.services.adapter import domain_error

            raise domain_error(
                409,
                "cache_reference_identity_invalid",
                "Execution cache responsibility is unavailable",
            )
        from dlr.control.services import cache_governance

        cache_governance.ensure_reference_allowed(
            session,
            worker_id=int(worker_id),
            adapter_id=int(adapter_id),
            version_id=int(version_id),
        )
    return lock_execution(session, execution_id)


def lock_active_execution(session: Session, adapter_id: int) -> Execution | None:
    """Lock the Adapter's active Execution, if one exists."""
    return session.scalar(
        select(Execution)
        .where(
            Execution.adapter_id == adapter_id,
            Execution.status.in_(ACTIVE_EXECUTION_STATUSES),
        )
        .with_for_update()
        .limit(1)
    )


def request_cancellation(execution: Execution) -> None:
    """Apply the cancellation transition to an already locked Execution."""
    if execution.status in RABBITMQ_CANCELLABLE_STATUSES:
        execution.status = "cancelled"
        execution.ended_at = func.now()
        execution.error_code = CANCELLATION_ERROR_CODE
        execution.last_error_code = CANCELLATION_ERROR_CODE
    elif execution.status == "running":
        execution.cancel_requested = True
