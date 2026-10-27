"""Whole-manifest garbage collection for prepaid shared execution copies."""

from datetime import timedelta

from sqlalchemy import delete, exists, func, select
from sqlalchemy.orm import Session, aliased, defer

from dlr.control.models import (
    DeliveryDisposition,
    EventDelivery,
    EventReceipt,
    Execution,
    SubscriptionExecutionBatch,
    SubscriptionExecutionBatchMember,
)
from dlr.control.services.adapter import domain_error
from dlr.control.services.input_config import database_now


def _cleanup(
    session: Session, subscription_id: int, limit: int, *, permanent: bool
) -> dict[str, int]:
    from dlr.control.services import subscription_delivery as delivery_service

    adapter, sub = delivery_service.lock_subscription(session, subscription_id)
    empty = {"batches": 0, "members": 0, "bytes_released": 0}
    if not session.scalar(
        select(exists().where(SubscriptionExecutionBatch.subscription_id == subscription_id))
    ):
        return empty
    if permanent and not (
        adapter.archived_at is not None
        and sub.deleted_at is not None
        and sub.desired_state == "disabled"
        and sub.connection_state == "stopped"
    ):
        raise domain_error(
            409, "batch_is_protected", "Permanent cleanup requires the archived and drained source"
        )
    now = database_now(session)
    cutoff = now - timedelta(days=30)
    member_budget = max(1, min(limit, 100))
    protected = exists(
        select(SubscriptionExecutionBatchMember.delivery_id)
        .join(EventReceipt, EventReceipt.id == SubscriptionExecutionBatchMember.receipt_id)
        .where(
            SubscriptionExecutionBatchMember.execution_id
            == SubscriptionExecutionBatch.execution_id,
            (EventReceipt.state.not_in(delivery_service.TERMINAL))
            | EventReceipt.terminal_at.is_(None)
            | ((EventReceipt.terminal_at > cutoff) if not permanent else False),
        )
    ).correlate(SubscriptionExecutionBatch)
    member = aliased(SubscriptionExecutionBatchMember)
    current = aliased(EventDelivery)
    older = aliased(EventDelivery)
    older_delivery = (
        exists(
            select(older.id).where(
                older.receipt_id == current.receipt_id, older.ordinal < current.ordinal
            )
        )
        .correlate(current)
    )
    has_older_delivery = (
        exists(
            select(member.delivery_id)
            .join(current, current.id == member.delivery_id)
            .where(
                member.execution_id == SubscriptionExecutionBatch.execution_id, older_delivery
            )
        )
        .correlate(SubscriptionExecutionBatch)
    )
    candidates = session.scalars(
        select(SubscriptionExecutionBatch)
        .join(Execution, Execution.id == SubscriptionExecutionBatch.execution_id)
        .where(
            SubscriptionExecutionBatch.subscription_id == sub.id,
            Execution.status.in_(delivery_service.EXECUTION_TERMINAL),
            SubscriptionExecutionBatch.result.is_not(None),
            ~protected,
            ~has_older_delivery,
        )
        .order_by(SubscriptionExecutionBatch.created_at, SubscriptionExecutionBatch.execution_id)
        .limit(member_budget)
        .with_for_update(of=SubscriptionExecutionBatch)
    ).all()
    result = empty
    quotas = delivery_service._quotas(session, sub.id)
    for batch in candidates:
        if result["members"] + batch.member_count > member_budget:
            if not result["members"] and batch.member_count > member_budget:
                raise domain_error(
                    409, "batch_cleanup_budget_too_small", "Cleanup budget cannot fit a batch"
                )
            break
        rows = session.execute(
            select(SubscriptionExecutionBatchMember, EventDelivery, EventReceipt)
            .options(defer(EventReceipt.source_raw_body), defer(EventReceipt.envelope))
            .join(EventDelivery, EventDelivery.id == SubscriptionExecutionBatchMember.delivery_id)
            .join(EventReceipt, EventReceipt.id == SubscriptionExecutionBatchMember.receipt_id)
            .where(SubscriptionExecutionBatchMember.execution_id == batch.execution_id)
            .order_by(SubscriptionExecutionBatchMember.member_index)
            .with_for_update(of=(EventDelivery, EventReceipt))
        ).all()
        if len(rows) != batch.member_count or any(
            receipt.state not in delivery_service.TERMINAL
            or receipt.terminal_at is None
            or (not permanent and receipt.terminal_at > cutoff)
            for _, _, receipt in rows
        ):
            continue
        delivery_ids = [item.id for _, item, _ in rows]
        released_count = sum(
            1
            for _, item, receipt in rows
            if receipt.retained_payload_bytes > 0 or item.retained_execution_bytes > 0
        )
        released_bytes = sum(
            item.retained_execution_bytes + item.retained_metadata_bytes + item.retained_batch_bytes
            for _, item, _ in rows
        )
        audits = session.execute(
            select(func.count(), func.coalesce(func.sum(DeliveryDisposition.audit_bytes), 0)).where(
                DeliveryDisposition.delivery_id.in_(delivery_ids),
                DeliveryDisposition.audit_bytes > 0,
            )
        ).one()
        execution = session.get(Execution, batch.execution_id, with_for_update=True)
        assert execution is not None
        session.execute(
            delete(DeliveryDisposition).where(DeliveryDisposition.delivery_id.in_(delivery_ids))
        )
        session.execute(
            delete(SubscriptionExecutionBatchMember).where(
                SubscriptionExecutionBatchMember.execution_id == batch.execution_id
            )
        )
        session.execute(delete(EventDelivery).where(EventDelivery.id.in_(delivery_ids)))
        # The delete guard checks the immutable manifest while Receipts still
        # exist. Only then may the real shared input/result copies be cleared.
        session.delete(batch)
        session.flush()
        execution.input = None
        execution.output = None
        execution.output_preview = None
        for quota in quotas:
            quota.history_count -= released_count + audits[0]
            quota.history_bytes -= released_bytes + audits[1]
            quota.dedupe_count -= audits[0]
        result["batches"] += 1
        result["members"] += len(rows)
        result["bytes_released"] += released_bytes + audits[1]
    session.flush()
    return result


def cleanup_batches(session: Session, subscription_id: int, limit: int = 100) -> dict[str, int]:
    return _cleanup(session, subscription_id, limit, permanent=False)


def purge_batches(session: Session, subscription_id: int, limit: int = 100) -> dict[str, int]:
    return _cleanup(session, subscription_id, limit, permanent=True)
