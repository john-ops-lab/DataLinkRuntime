"""Shared row-locking primitives for Execution cancellation.

Callers own the surrounding transaction. Locking before reading the status is
load-bearing: it serializes cancellation with Worker claim, which also locks
the Execution row before changing ``pending`` to ``running``.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dlr.control.models import Execution

ACTIVE_EXECUTION_STATUSES = ("pending", "running")


def lock_execution(session: Session, execution_id: int) -> Execution | None:
    """Lock one Execution so its cancellation decision uses fresh state."""
    return session.scalar(select(Execution).where(Execution.id == execution_id).with_for_update())


def lock_active_production_execution(session: Session, adapter_id: int) -> Execution | None:
    """Lock the Adapter's active production-class Execution, if one exists.

    M5.1: covers production/schedule/webhook triggers.
    """
    return session.scalar(
        select(Execution)
        .where(
            Execution.adapter_id == adapter_id,
            Execution.trigger.in_(("production", "schedule", "webhook")),
            Execution.status.in_(ACTIVE_EXECUTION_STATUSES),
        )
        .with_for_update()
        .limit(1)
    )


def request_cancellation(execution: Execution) -> None:
    """Apply the cancellation transition to an already locked Execution."""
    if execution.status == "pending":
        execution.status = "cancelled"
        execution.ended_at = func.now()
    elif execution.status == "running":
        execution.cancel_requested = True
