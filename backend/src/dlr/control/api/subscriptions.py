"""Authenticated event lifecycle and history; ordinary Execution history stays read-only."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from dlr.common.config import settings
from dlr.control import db
from dlr.control.models.subscription import (
    ActivationCredentialReference,
    DeliveryDisposition,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    SourceCheckpoint,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionQuota,
)
from dlr.control.schemas.subscription import (
    ActivationReference,
    ActivationResponse,
    CheckpointPage,
    CheckpointResponse,
    EventPage,
    EventResponse,
    QuotaAuditResponse,
    RepairRevision,
    SourceConnectionResponse,
    SubscriptionAction,
    SubscriptionClone,
    SubscriptionCreate,
    SubscriptionDisposition,
    SubscriptionPage,
    SubscriptionReplay,
    SubscriptionReset,
    SubscriptionResponse,
    SubscriptionUpdate,
    UnacceptedSkip,
)
from dlr.control.security import (
    Principal,
    require_admin_principal,
    require_business_principal,
    require_principal,
)
from dlr.control.services import adapter_access
from dlr.control.services import subscription_management as management
from dlr.control.services.adapter import domain_error
from dlr.control.services.subscription_source import SourceFailure

router = APIRouter(dependencies=[Depends(require_business_principal)])
DbSession = Annotated[Session, Depends(db.get_session)]
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


def serialize(
    session: Session, subscription: EventSubscription, principal: Principal
) -> SubscriptionResponse:
    from dlr.control.services.subscription_delivery import DEFAULT_LIMITS, GLOBAL_LIMITS

    current = management.current_config(session, subscription)
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
    counts["accepted"] = sum(counts.values())
    counts["replayed"] = (
        session.scalar(
            select(func.count())
            .select_from(EventDelivery)
            .join(EventReceipt, EventReceipt.id == EventDelivery.receipt_id)
            .where(EventReceipt.subscription_id == subscription.id, EventDelivery.ordinal > 1)
        )
        or 0
    )
    counts["coalesced"] = counts.get("superseded", 0)
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
        config=current.config,
        limits={**DEFAULT_LIMITS, **current.config.get("limits", {})},
        global_limits=dict(GLOBAL_LIMITS),
        quota={
            key: getattr(quota, key) if quota else 0
            for key in (
                "pending_count",
                "pending_bytes",
                "history_count",
                "history_bytes",
                "dedupe_count",
                "reserved_control_count",
                "reserved_control_bytes",
                "rate_count",
            )
        },
        oldest_pending_at=pending,
        event_counts=counts,
        available=settings.event_subscriptions_enabled
        and subscription.source_type in management.AVAILABLE_SOURCES,
        access_level=adapter_access.require_adapter_access(
            session, subscription.adapter_id, principal
        ).level,
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


@router.get(
    "/api/event-subscriptions/runtime-status", dependencies=[Depends(require_admin_principal)]
)
def runtime_status() -> dict[str, int]:
    """Process-wide counters for administrators; no source identities or native RPCs."""
    from dlr.control.services.subscription_runtime import get_runtime

    runtime = get_runtime()
    return {
        "active_count": runtime.active_count,
        "application_buffer_used_bytes": runtime.buffer.used_bytes,
        "application_buffer_peak_bytes": runtime.buffer.peak_bytes,
        "application_buffer_limit_bytes": runtime.buffer.limit_bytes,
    }


def _safe_position(value: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "topic",
        "partition",
        "offset",
        "cluster_id",
        "topic_id",
        "reset_pending",
        "resource_version",
        "batch_id",
        "scope_fingerprint",
    }
    return {
        key: item
        for key, item in value.items()
        if key in keys and (isinstance(item, bool) or isinstance(item, str) and len(item) <= 4096)
    }


@router.get("/api/event-subscriptions/{subscription_id}/checkpoints", response_model=CheckpointPage)
def list_checkpoints(
    subscription_id: int,
    session: DbSession,
    principal: CurrentPrincipal,
    before_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> CheckpointPage:
    sub = management.get_subscription(session, subscription_id, principal)
    query = select(SourceCheckpoint).where(
        SourceCheckpoint.subscription_id == sub.id,
        SourceCheckpoint.source_epoch == sub.source_epoch,
    )
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    if before_id is not None:
        query = query.where(SourceCheckpoint.id < before_id)
    rows = list(session.scalars(query.order_by(SourceCheckpoint.id.desc()).limit(limit + 1)))
    items = []
    for row in rows[:limit]:
        fault = row.blocked_unaccepted
        summary = None
        if fault is not None:
            summary = {
                key: fault.get(key)
                for key in ("code", "source_event_id", "size", "content_hash", "duplicate_count")
            }
            summary["position"] = _safe_position(fault.get("position", {}))
        items.append(
            CheckpointResponse(
                id=row.id,
                shard=row.shard,
                source_epoch=row.source_epoch,
                position=_safe_position(row.position),
                confirmed_position=_safe_position(row.confirmed_position),
                assignment_generation=row.assignment_generation,
                blocked_receipt_id=row.blocked_receipt_id,
                blocked_unaccepted=summary,
                source_lag=None,
            )
        )
    return CheckpointPage(
        items=items, total=total, next_before_id=rows[limit - 1].id if len(rows) > limit else None
    )


@router.get(
    "/api/event-subscriptions/{subscription_id}/activations/{activation_id}",
    response_model=ActivationResponse,
)
def get_activation(
    subscription_id: int, activation_id: int, session: DbSession, principal: CurrentPrincipal
) -> ActivationResponse:
    management.get_subscription(session, subscription_id, principal)
    activation = session.get(SubscriptionActivation, activation_id)
    if activation is None or activation.subscription_id != subscription_id:
        raise domain_error(404, "subscription_activation_not_found", "Activation not found")
    return _serialize_activation(session, activation)


def _serialize_activation(
    session: Session, activation: SubscriptionActivation
) -> ActivationResponse:
    config = session.get(SubscriptionConfigVersion, activation.config_version_id)
    assert config is not None
    return ActivationResponse(
        id=activation.id,
        config_version=config.version,
        source_epoch=config.source_epoch,
        revision_id=activation.revision_id,
        worker_id=activation.worker_id,
        source_credential_id=config.source_credential_id,
        credential_refs=[
            ActivationReference(
                credential_id=row.credential_id, env_key=row.env_key, field=row.field
            )
            for row in session.scalars(
                select(ActivationCredentialReference)
                .where(ActivationCredentialReference.activation_id == activation.id)
                .order_by(ActivationCredentialReference.env_key)
            )
        ],
    )


@router.post("/api/event-subscriptions/{subscription_id}/skip-unaccepted")
def skip_unaccepted(
    subscription_id: int, payload: UnacceptedSkip, session: DbSession, principal: CurrentPrincipal
) -> dict[str, Any]:
    from dlr.control.services import subscription_delivery as delivery

    sub = management.get_subscription(session, subscription_id, principal, "edit", lock=True)
    management.check_version(sub, payload.expected_config_version)
    old = session.scalar(
        select(DeliveryDisposition).where(
            DeliveryDisposition.subscription_id == sub.id,
            DeliveryDisposition.request_id == payload.request_id,
        )
    )
    if old is not None:
        if (
            old.action != "skip_unaccepted"
            or old.actor_id != principal.user_id
            or old.reason != payload.reason
            or old.details.get("shard") != payload.shard
            or old.details.get("source_event_id") != payload.source_event_id
            or old.details.get("source_epoch") != sub.source_epoch
        ):
            raise domain_error(409, "event_disposition_conflict", "Disposition request differs")
        return {"disposition_id": old.id, "outcome": "skipped_without_processing"}
    checkpoint = session.scalar(
        select(SourceCheckpoint)
        .where(
            SourceCheckpoint.subscription_id == sub.id,
            SourceCheckpoint.source_epoch == sub.source_epoch,
            SourceCheckpoint.shard == payload.shard,
        )
        .with_for_update()
    )
    fault = checkpoint.blocked_unaccepted if checkpoint is not None else None
    if (
        checkpoint is None
        or fault is None
        or fault.get("source_event_id") != payload.source_event_id
    ):
        raise domain_error(409, "event_diagnosis_invalid", "No matching unaccepted source fault")
    if sub.source_type != "kafka":
        raise domain_error(409, "source_history_gap", "Kubernetes fault requires a new full sync")
    try:
        offset = max(
            int(fault["position"]["offset"]) + 1, int(checkpoint.position.get("offset", "0"))
        )
    except (ValueError, TypeError, KeyError):
        raise domain_error(409, "event_diagnosis_invalid", "Source offset is unavailable") from None
    disposition = delivery.skip_unaccepted(
        session,
        sub.id,
        payload.shard,
        payload.request_id,
        principal.user_id,
        payload.reason,
        payload.source_event_id,
        {**checkpoint.position, "offset": str(offset)},
    )
    commit(session)
    return {"disposition_id": disposition.id, "outcome": "skipped_without_processing"}


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
        items=[serialize(session, item, principal) for item in rows[:limit]],
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
    return serialize(session, subscription, principal)


@router.get("/api/event-subscriptions/{subscription_id}", response_model=SubscriptionResponse)
def get_subscription(
    subscription_id: int, session: DbSession, principal: CurrentPrincipal
) -> SubscriptionResponse:
    return serialize(
        session, management.get_subscription(session, subscription_id, principal), principal
    )


@router.patch("/api/event-subscriptions/{subscription_id}", response_model=SubscriptionResponse)
def update_subscription(
    subscription_id: int,
    payload: SubscriptionUpdate,
    session: DbSession,
    principal: CurrentPrincipal,
) -> SubscriptionResponse:
    subscription = management.update_subscription(session, subscription_id, payload, principal)
    commit(session)
    return serialize(session, subscription, principal)


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
    return serialize(session, subscription, principal)


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
    return serialize(session, subscription, principal)


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


def _prepare_connection_test(
    subscription_id: int,
    payload: SubscriptionAction,
    session: Session,
    principal: Principal,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Only short SQL/decryption work uses the ordinary synchronous API pool."""
    from dlr.control.services.subscription_runtime import credential_values

    try:
        subscription = management.get_subscription(session, subscription_id, principal, "edit")
        management.check_version(subscription, payload.expected_config_version)
        management.require_feature()
        config = management.current_config(session, subscription)
        return dict(config.config), credential_values(session, config.source_credential_id)
    finally:
        session.rollback()


@router.post(
    "/api/event-subscriptions/{subscription_id}/test-connection",
    response_model=SourceConnectionResponse,
    response_model_exclude_none=True,
)
async def test_connection(
    subscription_id: int,
    payload: SubscriptionAction,
    session: DbSession,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    from dlr.control.services.subscription_runtime import get_runtime

    credentials: dict[str, str] = {}
    try:
        source_config, credentials = await run_in_threadpool(
            _prepare_connection_test, subscription_id, payload, session, principal
        )
        runtime = get_runtime()
        if source_config["source_type"] not in runtime.registry:
            raise domain_error(
                409, "source_not_available", "This event source is not yet implemented"
            )
        # Source RPCs use only the dedicated native budget, after the DB worker returned.
        return await runtime.test_connection(subscription_id, source_config, credentials)
    except SourceFailure as error:
        status = (
            429
            if error.disposition == "capacity"
            else 503
            if error.disposition == "backoff"
            else 409
        )
        raise domain_error(status, error.code, "Source connection could not be verified") from None
    finally:
        credentials.clear()


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
    return serialize(session, clone, principal)


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
    return serialize(session, subscription, principal)


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
    return serialize(session, subscription, principal)


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
    return serialize(session, subscription, principal)


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
        activation_id=receipt.activation_id,
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
                "activation_override_id": item.activation_override_id,
                "effective_activation_id": item.activation_override_id or receipt.activation_id,
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


@router.get("/api/event-subscriptions/{subscription_id}/events/{receipt_id}/replay-context")
def replay_context(
    subscription_id: int, receipt_id: int, session: DbSession, principal: CurrentPrincipal
) -> dict[str, Any]:
    from dlr.control.services.subscription_delivery import current_replay_context

    sub = management.get_subscription(session, subscription_id, principal)
    receipt = _event(session, subscription_id, receipt_id)
    activation = session.get(SubscriptionActivation, receipt.activation_id)
    assert activation is not None
    current, token = current_replay_context(session, sub)
    return {
        "original": _serialize_activation(session, activation),
        "current": current,
        "preview_token": token,
    }


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
        expected_preview_token=payload.expected_preview_token,
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
