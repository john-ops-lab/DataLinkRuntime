"""Read-only member projection; a shared Execution is never a member success oracle."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.control.models import (
    EventDelivery,
    EventReceipt,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionExecutionBatch,
    SubscriptionExecutionBatchMember,
)


def delivery_records(
    session: Session, receipt: EventReceipt, *, latest_only: bool = False
) -> list[dict[str, Any]]:
    query = (
        select(EventDelivery, SubscriptionExecutionBatchMember, SubscriptionExecutionBatch)
        .outerjoin(
            SubscriptionExecutionBatchMember,
            SubscriptionExecutionBatchMember.delivery_id == EventDelivery.id,
        )
        .outerjoin(
            SubscriptionExecutionBatch,
            SubscriptionExecutionBatch.execution_id == EventDelivery.batch_execution_id,
        )
        .where(EventDelivery.receipt_id == receipt.id)
        .order_by(EventDelivery.ordinal.desc() if latest_only else EventDelivery.ordinal)
    )
    if latest_only:
        query = query.limit(1)
    rows = []
    for item, member, batch in session.execute(query):
        activation_id = item.activation_override_id or receipt.activation_id
        # A request-local cache only holds immutable Activation/config facts.
        cache = session.info.setdefault("batch_view_modes", {})
        if activation_id not in cache:
            config = session.scalar(
                select(SubscriptionConfigVersion)
                .join(
                    SubscriptionActivation,
                    SubscriptionActivation.config_version_id == SubscriptionConfigVersion.id,
                )
                .where(SubscriptionActivation.id == activation_id)
            )
            cache[activation_id] = (
                "business_batch"
                if config and config.config.get("mode") == "business_batch"
                else "per_event"
            )
        rows.append(
            {
                "id": item.id,
                "ordinal": item.ordinal,
                "execution_id": item.execution_id or item.batch_execution_id,
                "state": item.state,
                "revision_override_id": item.revision_override_id,
                "activation_override_id": item.activation_override_id,
                "effective_activation_id": activation_id,
                "processing_mode": cache[activation_id],
                "batch_execution_id": item.batch_execution_id,
                "batch_member_index": member.member_index if member else None,
                "batch_member_count": batch.member_count if batch else None,
                "batch_member_outcome": item.batch_member_outcome,
                "batch_member_code": item.batch_member_code,
            }
        )
    return rows
