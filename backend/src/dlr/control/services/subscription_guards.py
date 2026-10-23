"""Subscription ownership guards shared by existing Adapter entry points.

All mutating callers hold the Adapter lock first. These predicates take no
additional locks, so configuration writes cannot invert the subscription lock
order while rendering single-item and catalog snapshots identically.
"""

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from dlr.control.services.adapter import domain_error


def locked_adapter_ids(session: Session, adapter_ids: list[int]) -> set[int]:
    if not adapter_ids:
        return set()
    from dlr.control.models.subscription import EventSubscription

    return set(
        session.scalars(
            select(EventSubscription.adapter_id).where(
                EventSubscription.adapter_id.in_(adapter_ids),
                EventSubscription.deleted_at.is_(None),
                or_(
                    EventSubscription.desired_state != "disabled",
                    EventSubscription.connection_state.in_(
                        ("starting", "listening", "reconnecting", "stopping")
                    ),
                    EventSubscription.delivery_state != "idle",
                    _unfinished_responsibility(),
                ),
            )
        )
    )


def _unfinished_responsibility() -> ColumnElement[bool]:
    from dlr.control.models.subscription import EventReceipt, EventSubscription, SourceCheckpoint

    return or_(
        exists(
            select(1).where(
                EventReceipt.subscription_id == EventSubscription.id,
                EventReceipt.terminal_at.is_(None),
            )
        ),
        exists(
            select(1).where(
                SourceCheckpoint.subscription_id == EventSubscription.id,
                SourceCheckpoint.blocked_receipt_id.is_not(None),
            )
        ),
    )


def runtime_guard(session: Session, adapter_id: int) -> bool:
    return adapter_id in locked_adapter_ids(session, [adapter_id])


def require_subscription_unlocked(session: Session, adapter_id: int) -> None:
    if runtime_guard(session, adapter_id):
        raise domain_error(
            409,
            "subscription_draining",
            "Disable the event subscription and settle its accepted responsibilities first",
        )


def require_no_subscription(session: Session, adapter_id: int) -> None:
    from dlr.control.models.subscription import EventSubscription

    if (
        session.scalar(
            select(EventSubscription.id).where(
                EventSubscription.adapter_id == adapter_id,
                EventSubscription.deleted_at.is_(None),
            )
        )
        is not None
    ):
        raise domain_error(
            409,
            "subscription_binding_conflict",
            "Delete the disabled and drained subscription before changing trigger "
            "or deleting Adapter",
        )
