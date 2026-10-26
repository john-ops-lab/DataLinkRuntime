"""Read-only member projection; a shared Execution is never a member success oracle."""

from typing import Any

from sqlalchemy import func, select
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
        select(
            EventDelivery,
            SubscriptionExecutionBatchMember,
            SubscriptionExecutionBatch,
            SubscriptionConfigVersion.config["mode"].as_string(),
        )
        .outerjoin(
            SubscriptionExecutionBatchMember,
            SubscriptionExecutionBatchMember.delivery_id == EventDelivery.id,
        )
        .outerjoin(
            SubscriptionExecutionBatch,
            SubscriptionExecutionBatch.execution_id == EventDelivery.batch_execution_id,
        )
        .outerjoin(
            SubscriptionActivation,
            SubscriptionActivation.id
            == func.coalesce(EventDelivery.activation_override_id, receipt.activation_id),
        )
        .outerjoin(
            SubscriptionConfigVersion,
            SubscriptionConfigVersion.id == SubscriptionActivation.config_version_id,
        )
        .where(EventDelivery.receipt_id == receipt.id)
        .order_by(EventDelivery.ordinal.desc() if latest_only else EventDelivery.ordinal)
    )
    if latest_only:
        query = query.limit(1)
    rows = []
    for item, member, batch, mode in session.execute(query):
        activation_id = item.activation_override_id or receipt.activation_id
        rows.append(
            {
                "id": item.id,
                "ordinal": item.ordinal,
                "execution_id": item.execution_id or item.batch_execution_id,
                "state": item.state,
                "revision_override_id": item.revision_override_id,
                "activation_override_id": item.activation_override_id,
                "effective_activation_id": activation_id,
                "processing_mode": "business_batch" if mode == "business_batch" else "per_event",
                "batch_execution_id": item.batch_execution_id,
                "batch_member_index": member.member_index if member else None,
                "batch_member_count": batch.member_count if batch else None,
                "batch_member_outcome": item.batch_member_outcome,
                "batch_member_code": item.batch_member_code,
            }
        )
    return rows
