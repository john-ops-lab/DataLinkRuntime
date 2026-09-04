"""Shared row-locking primitives for Execution cancellation.

Callers own the surrounding transaction. Locking before reading the status is
load-bearing: it serializes cancellation with Worker claim, which also locks
the Execution row before changing ``pending`` to ``running``.
"""

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from dlr.control.models import Execution

ACTIVE_EXECUTION_STATUSES = ("pending", "running")
RABBITMQ_CANCELLABLE_STATUSES = ("queued", "retry_wait")
RABBITMQ_NONTERMINAL_STATUSES = ("queued", "running", "retry_wait")


def lock_nonterminal_executions(session: Session, adapter_id: int) -> list[Execution]:
    """Lock every non-terminal Execution for one Adapter in id order.

    The legacy partial unique index only covers ``pending``/``running``.  A
    RabbitMQ Adapter may have several ``queued`` or ``retry_wait`` rows, so a
    single-row active lookup is not a safe deletion barrier.
    """
    return list(
        session.scalars(
            select(Execution)
            .where(
                Execution.adapter_id == adapter_id,
                or_(
                    and_(
                        Execution.dispatch_backend == "legacy",
                        Execution.status.in_(ACTIVE_EXECUTION_STATUSES),
                    ),
                    and_(
                        Execution.dispatch_backend == "rabbitmq",
                        Execution.status.in_(RABBITMQ_NONTERMINAL_STATUSES),
                    ),
                ),
            )
            .order_by(Execution.id.asc())
            .with_for_update()
        ).all()
    )


def lock_execution(session: Session, execution_id: int) -> Execution | None:
    """Lock one Execution so its cancellation decision uses fresh state."""
    return session.scalar(select(Execution).where(Execution.id == execution_id).with_for_update())


def lock_execution_in_admission_order(session: Session, execution_id: int) -> Execution | None:
    """Lock RabbitMQ cancellation scope before its Execution row.

    The initial lookup is deliberately non-locking: the immutable Adapter
    reference only identifies which Adapter row to lock next.  Once that
    Adapter and both Admission counters are held, the authoritative Execution
    row is locked.  This matches ingress, targeted reconciliation and
    stop/delete, preventing an Execution-first / counter-first cycle.
    """
    identity = session.execute(
        select(Execution.adapter_id, Execution.dispatch_backend).where(Execution.id == execution_id)
    ).one_or_none()
    if identity is None:
        return None
    adapter_id, dispatch_backend = identity
    if dispatch_backend == "rabbitmq":
        from dlr.control.services import admission

        if admission.lock_admission_scope(session, int(adapter_id)) is None:
            return None
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
    if execution.status == "pending" or (
        execution.dispatch_backend == "rabbitmq"
        and execution.status in RABBITMQ_CANCELLABLE_STATUSES
    ):
        execution.status = "cancelled"
        execution.ended_at = func.now()
        execution.last_error_code = (
            "cancelled" if execution.dispatch_backend == "rabbitmq" else None
        )
    elif execution.status == "running":
        execution.cancel_requested = True
