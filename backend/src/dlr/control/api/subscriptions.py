"""Authenticated event lifecycle and history; ordinary Execution history stays read-only."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control import db
from dlr.control.models.subscription import (
    EventDelivery,
    EventReceipt,
    EventSubscription,
    SubscriptionQuota,
)
from dlr.control.schemas.subscription import (
    EventPage,
    EventResponse,
    QuotaAuditResponse,
    RepairRevision,
    SubscriptionAction,
    SubscriptionClone,
    SubscriptionCreate,
    SubscriptionDisposition,
    SubscriptionPage,
    SubscriptionReplay,
    SubscriptionReset,
    SubscriptionResponse,
    SubscriptionUpdate,
)
from dlr.control.security import Principal, require_business_principal, require_principal
from dlr.control.services import adapter_access
from dlr.control.services import subscription_management as management
from dlr.control.services.adapter import domain_error

router = APIRouter(dependencies=[Depends(require_business_principal)])
DbSession = Annotated[Session, Depends(db.get_session)]
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


def serialize(session: Session, subscription: EventSubscription) -> SubscriptionResponse:
    quota = session.get(SubscriptionQuota, f"sub:{subscription.id}")
    pending = session.scalar(
        select(func.min(EventReceipt.first_received_at)).where(
            EventReceipt.subscription_id == subscription.id, EventReceipt.state == "pending"
        )
    )
    counts = {
        state: count
        for state, count in session.execute(
            select(EventReceipt.state, func.count())
            .where(EventReceipt.subscription_id == subscription.id)
            .group_by(EventReceipt.state)
        ).all()
    }
    if subscription.delivery_state == "repair":
        delivery_state = "repair"
    elif counts.get("failed", 0):
        delivery_state = "blocked"
    elif subscription.desired_state == "disabled" and (
        counts.get("pending", 0) or counts.get("processing", 0)
    ):
        delivery_state = "draining"
    elif counts.get("processing", 0):
        delivery_state = "processing"
    elif counts.get("pending", 0):
        delivery_state = "pending"
    else:
        delivery_state = "idle"
    return SubscriptionResponse(
        id=subscription.id,
        adapter_id=subscription.adapter_id,
        name=subscription.name,
        description=subscription.description,
        source_type=subscription.source_type,
        desired_state=subscription.desired_state,
        connection_state=subscription.connection_state,
        delivery_state=delivery_state,
        config_version=subscription.config_version,
        source_epoch=subscription.source_epoch,
        activation_id=subscription.activation_id,
        repair_revision_id=subscription.repair_revision_id,
        last_error_code=subscription.last_error_code,
        config=management.current_config(session, subscription).config,
        quota={
            key: getattr(quota, key) if quota else 0
            for key in (
                "pending_count",
                "pending_bytes",
                "history_count",
                "history_bytes",
                "dedupe_count",
            )
        },
        oldest_pending_at=pending,
        event_counts=counts,
        available=settings.event_subscriptions_enabled
        and subscription.source_type in management.AVAILABLE_SOURCES,
        created_at=subscription.created_at,
        updated_at=subscription.updated_at,
    )


def commit(session: Session) -> None:
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise domain_error(
            409,
            "subscription_binding_conflict",
            "Subscription binding or source group already exists",
        ) from None


@router.get("/api/event-subscriptions/capabilities")
def capabilities() -> dict[str, Any]:
    return {
        "enabled": settings.event_subscriptions_enabled,
        "available_sources": sorted(management.AVAILABLE_SOURCES)
        if settings.event_subscriptions_enabled
        else [],
    }


@router.get("/api/event-subscriptions", response_model=SubscriptionPage)
def list_subscriptions(
    session: DbSession,
    principal: CurrentPrincipal,
    adapter_id: int | None = None,
    source_type: str | None = None,
    connection_state: str | None = None,
    delivery_state: str | None = None,
    before_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SubscriptionPage:
    query = management.visible_query(principal)
    for column, value in (
        (EventSubscription.adapter_id, adapter_id),
        (EventSubscription.source_type, source_type),
        (EventSubscription.connection_state, connection_state),
        (EventSubscription.delivery_state, delivery_state),
    ):
        if value is not None:
            query = query.where(column == value)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    if before_id is not None:
        query = query.where(EventSubscription.id < before_id)
    rows = list(session.scalars(query.order_by(EventSubscription.id.desc()).limit(limit + 1)))
    return SubscriptionPage(
        items=[serialize(session, item) for item in rows[:limit]],
        total=total,
        next_before_id=rows[limit - 1].id if len(rows) > limit else None,
    )


@router.post("/api/event-subscriptions", response_model=SubscriptionResponse, status_code=201)
def create_subscription(
    payload: SubscriptionCreate, session: DbSession, principal: CurrentPrincipal
) -> SubscriptionResponse:
    try:
        subscription = management.create_subscription(session, payload, principal)
        commit(session)
    except IntegrityError:
        session.rollback()
        raise domain_error(
            409, "subscription_binding_conflict", "Adapter or source group already bound"
        ) from None
    return serialize(session, subscription)


@router.get("/api/event-subscriptions/{subscription_id}", response_model=SubscriptionResponse)
def get_subscription(
    subscription_id: int, session: DbSession, principal: CurrentPrincipal
) -> SubscriptionResponse:
    return serialize(session, management.get_subscription(session, subscription_id, principal))


@router.patch("/api/event-subscriptions/{subscription_id}", response_model=SubscriptionResponse)
def update_subscription(
    subscription_id: int,
    payload: SubscriptionUpdate,
    session: DbSession,
    principal: CurrentPrincipal,
) -> SubscriptionResponse:
    subscription = management.update_subscription(session, subscription_id, payload, principal)
    commit(session)
    return serialize(session, subscription)


@router.post(
    "/api/event-subscriptions/{subscription_id}/enable", response_model=SubscriptionResponse
)
@router.post(
    "/api/event-subscriptions/{subscription_id}/resume", response_model=SubscriptionResponse
)
def enable_subscription(
    subscription_id: int,
    payload: SubscriptionAction,
    session: DbSession,
    principal: CurrentPrincipal,
) -> SubscriptionResponse:
    subscription = management.enable_subscription(
        session, subscription_id, payload.expected_config_version, principal
    )
    commit(session)
    return serialize(session, subscription)


@router.post(
    "/api/event-subscriptions/{subscription_id}/disable", response_model=SubscriptionResponse
)
def disable_subscription(
    subscription_id: int,
    payload: SubscriptionAction,
    session: DbSession,
    principal: CurrentPrincipal,
) -> SubscriptionResponse:
    subscription = management.disable_subscription(
        session, subscription_id, payload.expected_config_version, principal
    )
    commit(session)
    return serialize(session, subscription)


@router.delete("/api/event-subscriptions/{subscription_id}", status_code=204)
def delete_subscription(
    subscription_id: int,
    payload: SubscriptionAction,
    session: DbSession,
    principal: CurrentPrincipal,
) -> Response:
    management.delete_subscription(
        session, subscription_id, payload.expected_config_version, principal
    )
    commit(session)
    return Response(status_code=204)


@router.post("/api/event-subscriptions/{subscription_id}/test-connection")
def test_connection(
    subscription_id: int,
    payload: SubscriptionAction,
    session: DbSession,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    subscription = management.get_subscription(session, subscription_id, principal, "edit")
    management.check_version(subscription, payload.expected_config_version)
    management.require_source(subscription.source_type)
    # Delivered connectors provide a bounded test action; never report an unperformed test.
    raise domain_error(
        409, "source_not_available", "Connection testing is not available for this source"
    )


@router.post(
    "/api/event-subscriptions/{subscription_id}/clone",
    response_model=SubscriptionResponse,
    status_code=201,
)
def clone_subscription(
    subscription_id: int,
    payload: SubscriptionClone,
    session: DbSession,
    principal: CurrentPrincipal,
) -> SubscriptionResponse:
    management.require_feature()
    source = management.get_subscription(session, subscription_id, principal, "read")
    # Acquire existing Adapter locks in stable order before cloning either subscription.
    for adapter_id in sorted({source.adapter_id, payload.target_adapter_id}):
        adapter_access.require_adapter_access(
            session,
            adapter_id,
            principal,
            "edit" if adapter_id == payload.target_adapter_id else "read",
            for_update=True,
        )
    source = management.get_subscription(session, subscription_id, principal, "read", lock=True)
    management.check_version(source, payload.expected_config_version)
    clone = management.clone_for_adapter(session, source.adapter_id, payload.target_adapter_id)
    assert clone is not None
    commit(session)
    return serialize(session, clone)


@router.post(
    "/api/event-subscriptions/{subscription_id}/reset", response_model=SubscriptionResponse
)
def reset_subscription(
    subscription_id: int,
    payload: SubscriptionReset,
    session: DbSession,
    principal: CurrentPrincipal,
) -> SubscriptionResponse:
    subscription = management.reset_subscription(session, subscription_id, payload, principal)
    commit(session)
    return serialize(session, subscription)


@router.post(
    "/api/event-subscriptions/{subscription_id}/repair/enter", response_model=SubscriptionResponse
)
def enter_repair(
    subscription_id: int,
    payload: SubscriptionAction,
    session: DbSession,
    principal: CurrentPrincipal,
) -> SubscriptionResponse:
    subscription = management.enter_repair(
        session, subscription_id, payload.expected_config_version, principal
    )
    commit(session)
    return serialize(session, subscription)


@router.post("/api/event-subscriptions/{subscription_id}/repair/revisions", status_code=201)
def save_repair_revision(
    subscription_id: int, payload: RepairRevision, session: DbSession, principal: CurrentPrincipal
) -> dict[str, int]:
    revision = management.save_repair_revision(session, subscription_id, payload, principal)
    commit(session)
    return {"revision_id": revision.id, "version_seq": revision.seq}


@router.post(
    "/api/event-subscriptions/{subscription_id}/repair/exit", response_model=SubscriptionResponse
)
def exit_repair(
    subscription_id: int,
    payload: SubscriptionAction,
    session: DbSession,
    principal: CurrentPrincipal,
) -> SubscriptionResponse:
    subscription = management.exit_repair(
        session, subscription_id, payload.expected_config_version, principal
    )
    commit(session)
    return serialize(session, subscription)


def _event(session: Session, subscription_id: int, receipt_id: int) -> EventReceipt:
    receipt = session.get(EventReceipt, receipt_id)
    if receipt is None or receipt.subscription_id != subscription_id:
        raise domain_error(404, "event_not_found", "Event not found")
    return receipt


def serialize_event(session: Session, receipt: EventReceipt) -> EventResponse:
    deliveries = session.scalars(
        select(EventDelivery)
        .where(EventDelivery.receipt_id == receipt.id)
        .order_by(EventDelivery.ordinal)
    ).all()
    return EventResponse(
        id=receipt.id,
        source_event_id=receipt.source_event_id,
        source_epoch=receipt.source_epoch,
        state=receipt.state,
        shard=receipt.shard,
        sequence=receipt.sequence,
        envelope=receipt.envelope,
        payload_expired=receipt.payload_expired_at is not None,
        first_received_at=receipt.first_received_at,
        duplicate_count=receipt.duplicate_count,
        deliveries=[
            {
                "id": item.id,
                "ordinal": item.ordinal,
                "execution_id": item.execution_id,
                "state": item.state,
                "revision_override_id": item.revision_override_id,
            }
            for item in deliveries
        ],
    )


@router.get("/api/event-subscriptions/{subscription_id}/events", response_model=EventPage)
def list_events(
    subscription_id: int,
    session: DbSession,
    principal: CurrentPrincipal,
    before_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> EventPage:
    management.get_subscription(session, subscription_id, principal)
    query = select(EventReceipt).where(EventReceipt.subscription_id == subscription_id)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    if before_id is not None:
        query = query.where(EventReceipt.id < before_id)
    rows = list(session.scalars(query.order_by(EventReceipt.id.desc()).limit(limit + 1)))
    return EventPage(
        items=[serialize_event(session, item) for item in rows[:limit]],
        total=total,
        next_before_id=rows[limit - 1].id if len(rows) > limit else None,
    )


@router.get(
    "/api/event-subscriptions/{subscription_id}/events/{receipt_id}", response_model=EventResponse
)
def get_event(
    subscription_id: int, receipt_id: int, session: DbSession, principal: CurrentPrincipal
) -> EventResponse:
    management.get_subscription(session, subscription_id, principal)
    return serialize_event(session, _event(session, subscription_id, receipt_id))


@router.post(
    "/api/event-subscriptions/{subscription_id}/events/{receipt_id}/replay", status_code=201
)
def replay_event(
    subscription_id: int,
    receipt_id: int,
    payload: SubscriptionReplay,
    session: DbSession,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    from dlr.control.services import subscription_delivery

    subscription = management.get_subscription(
        session, subscription_id, principal, "edit", lock=True
    )
    management.check_version(subscription, payload.expected_config_version)
    _event(session, subscription_id, receipt_id)
    delivery = subscription_delivery.replay_event(
        session,
        receipt_id,
        payload.request_id,
        principal.user_id,
        payload.reason,
        revision_id=payload.revision_id,
        use_current_configuration=payload.use_current_configuration,
    )
    commit(session)
    return {"delivery_id": delivery.id, "execution_id": delivery.execution_id}


@router.post("/api/event-subscriptions/{subscription_id}/events/{receipt_id}/skip")
def skip_event(
    subscription_id: int,
    receipt_id: int,
    payload: SubscriptionDisposition,
    session: DbSession,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    from dlr.control.services import subscription_delivery

    subscription = management.get_subscription(
        session, subscription_id, principal, "edit", lock=True
    )
    management.check_version(subscription, payload.expected_config_version)
    _event(session, subscription_id, receipt_id)
    disposition = subscription_delivery.skip_event(
        session, receipt_id, payload.request_id, principal.user_id, payload.reason
    )
    commit(session)
    return {"disposition_id": disposition.id, "outcome": "skipped_without_processing"}


@router.get(
    "/api/event-subscriptions/{subscription_id}/quota-audit", response_model=QuotaAuditResponse
)
def audit_quota(
    subscription_id: int, session: DbSession, principal: CurrentPrincipal
) -> dict[str, Any]:
    from dlr.control.services.subscription_delivery import audit_quota as inspect_quota

    management.get_subscription(session, subscription_id, principal)
    return inspect_quota(session, subscription_id)


@router.post(
    "/api/event-subscriptions/{subscription_id}/quota-repair", response_model=QuotaAuditResponse
)
def repair_quota(
    subscription_id: int,
    payload: SubscriptionDisposition,
    session: DbSession,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    report = management.repair_quota(session, subscription_id, payload, principal)
    commit(session)
    return report
