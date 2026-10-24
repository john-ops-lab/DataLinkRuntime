"""Subscription lifecycle. Callers commit; every mutation starts with the Adapter lock."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Select, exists, false, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import InstrumentedAttribute, Session

from dlr.common.config import settings
from dlr.common.jcs import canonicalize
from dlr.control.models import (
    Adapter,
    AdapterCredentialBinding,
    AdapterPermission,
    AdapterVersion,
    Credential,
    Execution,
)
from dlr.control.models.subscription import (
    ActivationCredentialReference,
    DeliveryDisposition,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    SourceCheckpoint,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionLease,
    SubscriptionQuota,
)
from dlr.control.schemas.subscription import (
    RepairRevision,
    SourceConfig,
    SubscriptionCreate,
    SubscriptionDisposition,
    SubscriptionReset,
    SubscriptionUpdate,
)
from dlr.control.security import Principal
from dlr.control.services import adapter_access
from dlr.control.services.adapter import domain_error
from dlr.control.services.input_config import database_now
from dlr.control.services.subscription_registry import configuration_identity, source_group_key

logger = logging.getLogger("dlr.control.subscriptions")
# Public candidates follow their source batch gates. Enablement alone does not
# establish product acceptance; implemented connectors also serve diagnostics.
AVAILABLE_SOURCES: frozenset[str] = frozenset({"kafka", "kubernetes"})
NONTERMINAL = ("queued", "running", "retry_wait")
MAX_ENABLED_SUBSCRIPTIONS = 16
_delivery_cursor = 0
_delivery_upper = 0
_subscription_cursor = 0
_subscription_upper = 0
_quota_cursor = 0
_quota_upper = 0
_batch_cursor = 0
_batch_upper = 0


def bounded_page(
    session: Session,
    query: Select[tuple[int]],
    identity: InstrumentedAttribute[int],
    cursor: int,
    upper: int,
) -> tuple[list[int], int, int]:
    """A finite sweep must revisit old work despite a continuously growing tail."""
    if not upper:
        upper = session.scalar(query.with_only_columns(func.max(identity)).order_by(None)) or 0
    if not upper:
        return [], 0, 0
    rows = list(
        session.scalars(
            query.where(identity > cursor, identity <= upper).order_by(identity).limit(100)
        )
    )
    if not rows or rows[-1] >= upper:
        return rows, 0, 0
    return rows, rows[-1], upper


def _flush(session: Session) -> None:
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise domain_error(
            409, "subscription_binding_conflict", "Adapter or source group is already bound"
        ) from None


def require_feature() -> None:
    if not settings.event_subscriptions_enabled:
        raise domain_error(409, "source_not_available", "Event subscriptions are disabled")


def require_source(source_type: str) -> None:
    require_feature()
    if source_type not in AVAILABLE_SOURCES:
        raise domain_error(409, "source_not_available", "This event source is not yet available")


def get_subscription(
    session: Session,
    subscription_id: int,
    principal: Principal,
    action: adapter_access.AccessAction = "read",
    *,
    lock: bool = False,
) -> EventSubscription:
    identity = session.execute(
        select(EventSubscription.adapter_id).where(
            EventSubscription.id == subscription_id, EventSubscription.deleted_at.is_(None)
        )
    ).scalar_one_or_none()
    if identity is None:
        raise domain_error(404, "subscription_not_found", "Event subscription not found")
    try:
        adapter_access.require_adapter_access(session, identity, principal, action, for_update=lock)
    except HTTPException as error:
        if error.status_code == 404:
            raise domain_error(
                404, "subscription_not_found", "Event subscription not found"
            ) from None
        raise
    query = select(EventSubscription).where(
        EventSubscription.id == subscription_id, EventSubscription.deleted_at.is_(None)
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    subscription = session.scalar(query)
    if subscription is None:
        raise domain_error(404, "subscription_not_found", "Event subscription not found")
    return subscription


def check_version(subscription: EventSubscription, expected: int | None) -> None:
    if expected != subscription.config_version:
        raise domain_error(
            409, "subscription_config_conflict", "Subscription configuration changed"
        )


def current_config(session: Session, subscription: EventSubscription) -> SubscriptionConfigVersion:
    config = session.scalar(
        select(SubscriptionConfigVersion).where(
            SubscriptionConfigVersion.subscription_id == subscription.id,
            SubscriptionConfigVersion.version == subscription.config_version,
        )
    )
    if config is None:
        raise domain_error(
            409, "subscription_config_invalid", "Subscription configuration is unavailable"
        )
    return config


def _credential_access(
    session: Session, adapter_id: int, credential_id: int | None, principal: Principal
) -> None:
    if credential_id is not None:
        adapter_access.require_adapter_access(session, adapter_id, principal, "owner")
        if session.get(Credential, credential_id) is None:
            raise domain_error(404, "credential_not_found", "Credential not found")


def _configuration(config: SourceConfig) -> dict[str, Any]:
    from dlr.control.services.subscription_delivery import DEFAULT_LIMITS

    value = config.model_dump(mode="json")
    for key, amount in value.get("limits", {}).items():
        if (
            key not in DEFAULT_LIMITS
            or isinstance(amount, bool)
            or not 0 < amount <= DEFAULT_LIMITS[key]
        ):
            raise domain_error(
                422, "subscription_config_invalid", "Limits may only tighten known defaults"
            )
    ca = value.get("ca_pem") or ""
    if "PRIVATE KEY" in ca:
        raise domain_error(
            422, "subscription_config_invalid", "CA configuration cannot contain a private key"
        )
    if value["source_type"] == "kafka" and not value["group_id"]:
        value["group_id"] = "dlr-subscription-" + uuid.uuid4().hex
    return value


def _validate_adapter(adapter: Adapter) -> None:
    if adapter.adapter_type != "task" or adapter.run_mode != "manual":
        raise domain_error(
            409, "subscription_binding_conflict", "Subscriptions require a Task/manual Adapter"
        )


def _source_identity(value: dict[str, Any]) -> dict[str, Any] | None:
    if value["source_type"] != "kubernetes":
        return None
    from dlr.control.services.subscription_kubernetes_identity import (
        KubernetesIdentityError,
        kubernetes_scope_identity,
    )

    try:
        return dict(kubernetes_scope_identity(value))
    except KubernetesIdentityError as error:
        raise domain_error(
            422, error.code, "Kubernetes source identity could not be verified"
        ) from None
    except ValueError:
        raise domain_error(
            422, "subscription_config_invalid", "Kubernetes source configuration is invalid"
        ) from None


def require_verified_identity(config: SubscriptionConfigVersion) -> None:
    if config.config["source_type"] != "kubernetes":
        return
    if config.source_identity is None:
        raise domain_error(
            409, "source_epoch_required", "Save a verified configuration in an explicit new epoch"
        )
    if config.source_identity != _source_identity(config.config):
        raise domain_error(
            409, "source_identity_changed", "Effective source trust changed; create a new epoch"
        )


def _new_config(
    session: Session,
    subscription: EventSubscription,
    value: dict[str, Any],
    *,
    source_identity: dict[str, Any] | None = None,
) -> None:
    session.add(
        SubscriptionConfigVersion(
            subscription_id=subscription.id,
            version=subscription.config_version,
            source_epoch=subscription.source_epoch,
            source_credential_id=value.get("credential_id"),
            config=value,
            source_identity=source_identity
            if source_identity is not None
            else _source_identity(value),
        )
    )
    subscription.source_type = value["source_type"]
    subscription.updated_at = database_now(session)
    subscription.source_group_key = source_group_key(value)


def create_subscription(
    session: Session, payload: SubscriptionCreate, principal: Principal
) -> EventSubscription:
    require_feature()
    adapter = adapter_access.require_adapter_access(
        session, payload.adapter_id, principal, "edit", for_update=True
    ).adapter
    _validate_adapter(adapter)
    if (
        session.scalar(
            select(EventSubscription.id).where(
                EventSubscription.adapter_id == adapter.id, EventSubscription.deleted_at.is_(None)
            )
        )
        is not None
    ):
        raise domain_error(
            409, "subscription_binding_conflict", "Adapter already has a subscription"
        )
    value = _configuration(payload.config)
    _credential_access(session, adapter.id, value.get("credential_id"), principal)
    subscription = EventSubscription(
        adapter_id=adapter.id,
        source_type=value["source_type"],
        name=payload.name,
        description=payload.description,
        desired_state="disabled",
        connection_state="stopped",
        delivery_state="idle",
        config_version=1,
        source_epoch=uuid.uuid4().hex,
    )
    session.add(subscription)
    _flush(session)
    _new_config(session, subscription, value)
    _flush(session)
    return subscription


def _require_drained(session: Session, subscription: EventSubscription) -> None:
    from dlr.control.services.subscription_delivery import has_unfinished

    if (
        subscription.desired_state != "disabled"
        or subscription.connection_state != "stopped"
        or subscription.delivery_state == "repair"
        or has_unfinished(session, subscription.id)
    ):
        raise domain_error(
            409, "subscription_draining", "Disable and settle all accepted responsibilities first"
        )


def _identity(
    value: dict[str, Any], source_identity: dict[str, Any] | None = None
) -> dict[str, Any]:
    return configuration_identity(value, source_identity)


def update_subscription(
    session: Session, subscription_id: int, payload: SubscriptionUpdate, principal: Principal
) -> EventSubscription:
    subscription = get_subscription(session, subscription_id, principal, "edit", lock=True)
    if payload.config is not None:
        check_version(subscription, payload.expected_config_version)
        _require_drained(session, subscription)
        value = _configuration(payload.config)
        previous_config = current_config(session, subscription)
        previous = previous_config.config
        identity = _source_identity(value)
        if (
            previous.get("source_type") == "kafka"
            and value.get("group_id") != previous.get("group_id")
            and payload.config.source_type == "kafka"
            and payload.config.group_id is None
        ):
            value["group_id"] = previous.get("group_id")
        _credential_access(session, subscription.adapter_id, value.get("credential_id"), principal)
        if _identity(previous, previous_config.source_identity) != _identity(value, identity):
            if not payload.new_epoch:
                raise domain_error(
                    409,
                    "source_epoch_required",
                    "Source or scope changes require an explicit new epoch",
                )
            subscription.source_epoch = uuid.uuid4().hex
        elif payload.new_epoch:
            subscription.source_epoch = uuid.uuid4().hex
        subscription.config_version += 1
        _new_config(session, subscription, value, source_identity=identity)
        subscription.activation_id = None
    elif payload.expected_config_version is not None:
        check_version(subscription, payload.expected_config_version)
    if payload.name is not None:
        subscription.name = payload.name
    if payload.description is not None:
        subscription.description = payload.description
    subscription.updated_at = database_now(session)
    _flush(session)
    return subscription


def _nonterminal(session: Session, subscription: EventSubscription) -> bool:
    return bool(
        session.scalar(
            select(
                exists().where(
                    EventDelivery.receipt_id == EventReceipt.id,
                    EventReceipt.subscription_id == subscription.id,
                    EventDelivery.execution_id == Execution.id,
                    Execution.status.in_(NONTERMINAL),
                )
            )
        )
    )


def _fence(session: Session, subscription: EventSubscription) -> None:
    lease = session.get(SubscriptionLease, subscription.id, with_for_update=True)
    if lease is not None:
        if lease.owner_id and lease.closing_generation is None:
            lease.closing_generation = lease.generation
        lease.generation += 1
        lease.lease_until = database_now(session)


def _activate(session: Session, subscription: EventSubscription) -> SubscriptionActivation:
    from dlr.control.services.reliable_execution import resolve_queue_target_worker

    adapter = session.get(Adapter, subscription.adapter_id)
    assert adapter is not None
    _validate_adapter(adapter)
    if adapter.latest_version_id is None:
        raise domain_error(409, "adapter_has_no_version", "Save an Adapter Revision first")
    worker = resolve_queue_target_worker(session, adapter)
    activation = SubscriptionActivation(
        subscription_id=subscription.id,
        config_version_id=current_config(session, subscription).id,
        revision_id=adapter.latest_version_id,
        worker_id=worker.id,
    )
    session.add(activation)
    _flush(session)
    for binding in session.scalars(
        select(AdapterCredentialBinding)
        .where(AdapterCredentialBinding.adapter_id == adapter.id)
        .order_by(AdapterCredentialBinding.id)
        .with_for_update()
    ).all():
        session.add(
            ActivationCredentialReference(
                activation_id=activation.id,
                binding_id=binding.id,
                credential_id=binding.credential_id,
                env_key=binding.env_key,
                field=binding.field,
            )
        )
    subscription.activation_id = activation.id
    return activation


def enable_subscription(
    session: Session, subscription_id: int, expected: int, principal: Principal
) -> EventSubscription:
    subscription = get_subscription(session, subscription_id, principal, "edit", lock=True)
    check_version(subscription, expected)
    require_source(subscription.source_type)
    require_verified_identity(current_config(session, subscription))
    if subscription.desired_state == "enabled":
        if subscription.connection_state == "blocked":
            _fence(session, subscription)
            subscription.connection_state = "starting"
            subscription.last_error_code = None
        return subscription
    _require_drained(session, subscription)
    if (
        session.scalar(
            select(Execution.id)
            .where(
                Execution.adapter_id == subscription.adapter_id,
                Execution.status.in_(NONTERMINAL),
            )
            .limit(1)
        )
        is not None
    ):
        raise domain_error(409, "subscription_draining", "Wait for ordinary executions to settle")
    _credential_access(
        session,
        subscription.adapter_id,
        current_config(session, subscription).source_credential_id,
        principal,
    )
    _fence(session, subscription)
    _activate(session, subscription)
    # Serialize the enabled count with the global Inbox scope; only read other subscriptions.
    session.execute(insert(SubscriptionQuota).values(scope="global").on_conflict_do_nothing())
    session.get(SubscriptionQuota, "global", with_for_update=True)
    enabled_count = (
        session.scalar(
            select(func.count())
            .select_from(EventSubscription)
            .where(
                EventSubscription.deleted_at.is_(None), EventSubscription.desired_state == "enabled"
            )
        )
        or 0
    )
    if enabled_count >= MAX_ENABLED_SUBSCRIPTIONS:
        raise domain_error(
            429, "subscription_capacity_exceeded", "Enabled subscription limit reached"
        )
    subscription.desired_state = "enabled"
    subscription.connection_state = "starting"
    subscription.delivery_state = "idle"
    subscription.last_error_code = None
    subscription.updated_at = database_now(session)
    _flush(session)
    return subscription


def disable_subscription(
    session: Session, subscription_id: int, expected: int, principal: Principal
) -> EventSubscription:
    from dlr.control.services.subscription_delivery import refresh_delivery_state

    subscription = get_subscription(session, subscription_id, principal, "edit", lock=True)
    check_version(subscription, expected)
    if subscription.desired_state != "disabled":
        _fence(session, subscription)
    subscription.desired_state = "disabled"
    # Native owners acknowledge stopping only after their clients actually close.
    lease = session.get(SubscriptionLease, subscription.id)
    subscription.connection_state = "stopping" if lease and lease.owner_id else "stopped"
    refresh_delivery_state(session, subscription)
    subscription.updated_at = database_now(session)
    return subscription


def delete_subscription(
    session: Session, subscription_id: int, expected: int, principal: Principal
) -> None:
    subscription = get_subscription(session, subscription_id, principal, "edit", lock=True)
    check_version(subscription, expected)
    _require_drained(session, subscription)
    subscription.deleted_at = database_now(session)
    subscription.updated_at = subscription.deleted_at


def listener_closed(
    session: Session, subscription_id: int, owner_id: str, stopped_generation: int
) -> None:
    """Acknowledge actual native close; expiry alone must not pretend resource reclamation."""
    from dlr.control.services.subscription_delivery import lock_subscription

    _, subscription = lock_subscription(session, subscription_id)
    lease = session.get(SubscriptionLease, subscription.id, with_for_update=True)
    if (
        lease is None
        or lease.owner_id != owner_id
        or lease.closing_generation != stopped_generation
    ):
        raise domain_error(
            409, "subscription_owner_stale", "Listener close acknowledgement is stale"
        )
    lease.owner_id = ""
    lease.closing_generation = None
    lease.lease_until = database_now(session)
    if subscription.desired_state == "disabled":
        subscription.connection_state = "stopped"


def clone_for_adapter(
    session: Session, source_adapter_id: int, target_adapter_id: int
) -> EventSubscription | None:
    source = session.scalar(
        select(EventSubscription).where(
            EventSubscription.adapter_id == source_adapter_id,
            EventSubscription.deleted_at.is_(None),
        )
    )
    if source is None:
        return None
    target = session.get(Adapter, target_adapter_id, with_for_update=True)
    if target is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    _validate_adapter(target)
    value = dict(current_config(session, source).config)
    if source.source_type == "kafka":
        value["group_id"] = "dlr-subscription-" + uuid.uuid4().hex
    elif source.source_type == "rocketmq":
        value["group_id"] = "dlr-copy-" + uuid.uuid4().hex
    elif source.source_type == "rabbitmq":
        value["queue"] = value["queue"][:200] + "-dlr-copy-" + uuid.uuid4().hex
    clone = EventSubscription(
        adapter_id=target.id,
        source_type=source.source_type,
        name=source.name,
        description=source.description,
        source_epoch=uuid.uuid4().hex,
        config_version=1,
        desired_state="disabled",
        connection_state="stopped",
        delivery_state="idle",
    )
    session.add(clone)
    _flush(session)
    _new_config(session, clone, value)
    _flush(session)
    return clone


def reset_subscription(
    session: Session, subscription_id: int, payload: SubscriptionReset, principal: Principal
) -> EventSubscription:
    subscription = get_subscription(session, subscription_id, principal, "edit", lock=True)
    digest = hashlib.sha256(canonicalize(payload.model_dump(mode="json"))).hexdigest()
    prior = session.scalar(
        select(DeliveryDisposition).where(
            DeliveryDisposition.subscription_id == subscription.id,
            DeliveryDisposition.request_id == payload.request_id,
        )
    )
    if prior is not None:
        if prior.action != "reset" or prior.details.get("request_hash") != digest:
            raise domain_error(
                409, "subscription_config_conflict", "Disposition request ID already used"
            )
        return subscription
    check_version(subscription, payload.expected_config_version)
    _require_drained(session, subscription)
    value = (
        _configuration(payload.config)
        if payload.config
        else dict(current_config(session, subscription).config)
    )
    _credential_access(session, subscription.adapter_id, value.get("credential_id"), principal)
    old_epoch = subscription.source_epoch
    subscription.source_epoch = uuid.uuid4().hex
    subscription.config_version += 1
    subscription.activation_id = None
    _fence(session, subscription)
    _new_config(session, subscription, value)
    from dlr.control.services.subscription_delivery import reserve_disposition_metadata

    reserve_disposition_metadata(session, subscription.id)
    session.add(
        DeliveryDisposition(
            subscription_id=subscription.id,
            request_id=payload.request_id,
            action="reset",
            actor_id=principal.user_id,
            reason=payload.reason,
            details={
                "old_epoch": old_epoch,
                "new_epoch": subscription.source_epoch,
                "request_hash": digest,
            },
        )
    )
    _flush(session)
    return subscription


def repair_quota(
    session: Session, subscription_id: int, payload: SubscriptionDisposition, principal: Principal
) -> dict[str, Any]:
    from dlr.control.services.subscription_delivery import audit_quota, reserve_disposition_metadata

    subscription = get_subscription(session, subscription_id, principal, "edit", lock=True)
    digest = hashlib.sha256(canonicalize(payload.model_dump(mode="json"))).hexdigest()
    prior = session.scalar(
        select(DeliveryDisposition).where(
            DeliveryDisposition.subscription_id == subscription.id,
            DeliveryDisposition.request_id == payload.request_id,
        )
    )
    if prior is not None:
        if prior.action != "quota_repair" or prior.details.get("request_hash") != digest:
            raise domain_error(
                409, "subscription_config_conflict", "Disposition request ID already used"
            )
        prior_report = prior.details.get("report")
        if not isinstance(prior_report, dict):
            raise domain_error(
                409, "subscription_config_conflict", "Quota audit receipt is invalid"
            )
        return prior_report
    check_version(subscription, payload.expected_config_version)
    if subscription.desired_state != "disabled" or subscription.connection_state != "stopped":
        raise domain_error(409, "subscription_draining", "Stop the listener before quota repair")
    report = audit_quota(session, subscription.id, repair=True)
    reserve_disposition_metadata(session, subscription.id)
    session.add(
        DeliveryDisposition(
            subscription_id=subscription.id,
            request_id=payload.request_id,
            action="quota_repair",
            actor_id=principal.user_id,
            reason=payload.reason,
            details={"request_hash": digest, "report": report},
        )
    )
    _flush(session)
    return report


def enter_repair(
    session: Session, subscription_id: int, expected: int, principal: Principal
) -> EventSubscription:
    subscription = get_subscription(session, subscription_id, principal, "edit", lock=True)
    check_version(subscription, expected)
    if subscription.desired_state != "disabled" or subscription.connection_state != "stopped":
        raise domain_error(409, "subscription_draining", "Stop the listener before entering repair")
    if subscription.delivery_state == "repair":
        return subscription
    subscription.delivery_state = "repair"
    subscription.repair_revision_id = None
    subscription.updated_at = database_now(session)
    return subscription


def save_repair_revision(
    session: Session, subscription_id: int, payload: RepairRevision, principal: Principal
) -> AdapterVersion:
    subscription = get_subscription(session, subscription_id, principal, "edit", lock=True)
    check_version(subscription, payload.expected_config_version)
    if subscription.delivery_state != "repair" or _nonterminal(session, subscription):
        raise domain_error(
            409, "subscription_draining", "Wait for existing Executions to settle in repair"
        )
    if subscription.activation_id is None:
        raise domain_error(
            409, "subscription_config_invalid", "Repair requires a previous activation"
        )
    activation = session.get(SubscriptionActivation, subscription.activation_id)
    assert activation is not None
    original = session.get(AdapterVersion, activation.revision_id)
    assert original is not None
    seq = (
        session.scalar(
            select(func.max(AdapterVersion.seq)).where(
                AdapterVersion.adapter_id == subscription.adapter_id
            )
        )
        or 0
    )
    revision = AdapterVersion(
        adapter_id=subscription.adapter_id,
        seq=seq + 1,
        code=payload.code,
        requirements=payload.requirements,
        runtime_config=dict(original.runtime_config),
    )
    session.add(revision)
    _flush(session)
    subscription.repair_revision_id = revision.id
    subscription.updated_at = database_now(session)
    return revision


def exit_repair(
    session: Session, subscription_id: int, expected: int, principal: Principal
) -> EventSubscription:
    from dlr.control.services.subscription_delivery import refresh_delivery_state

    subscription = get_subscription(session, subscription_id, principal, "edit", lock=True)
    check_version(subscription, expected)
    if subscription.delivery_state != "repair" or _nonterminal(session, subscription):
        raise domain_error(409, "subscription_draining", "Repair execution has not settled")
    if (
        session.scalar(
            select(SourceCheckpoint.id)
            .where(
                SourceCheckpoint.subscription_id == subscription.id,
                SourceCheckpoint.blocked_receipt_id.is_not(None),
            )
            .limit(1)
        )
        is not None
    ):
        raise domain_error(
            409, "subscription_draining", "Settle the failed event before exiting repair"
        )
    subscription.delivery_state = "idle"
    refresh_delivery_state(session, subscription)
    subscription.repair_revision_id = None
    subscription.updated_at = database_now(session)
    return subscription


def visible_query(principal: Principal) -> Any:
    query = (
        select(EventSubscription)
        .join(Adapter, Adapter.id == EventSubscription.adapter_id)
        .where(EventSubscription.deleted_at.is_(None), Adapter.archived_at.is_(None))
    )
    if principal.kind != "superadmin" and principal.role != "admin":
        shared = select(AdapterPermission.adapter_id).where(
            AdapterPermission.user_id == principal.user_id
        )
        query = (
            query.where(or_(Adapter.owner_user_id == principal.user_id, Adapter.id.in_(shared)))
            if principal.user_id is not None
            else query.where(false())
        )
    return query


def maintenance_tick() -> None:
    """Bounded, one-responsibility transactions; feature-off still preserves draining."""
    from dlr.control import db
    from dlr.control.services import subscription_delivery as delivery
    from dlr.control.services import subscription_sync as sync

    global _delivery_cursor, _delivery_upper, _subscription_cursor, _subscription_upper
    global _batch_cursor, _batch_upper

    with db.SessionLocal() as session:
        deliveries, _delivery_cursor, _delivery_upper = bounded_page(
            session,
            select(EventDelivery.id).where(EventDelivery.state.in_(("pending", "processing"))),
            EventDelivery.id,
            _delivery_cursor,
            _delivery_upper,
        )
        subscriptions, _subscription_cursor, _subscription_upper = bounded_page(
            session,
            select(EventSubscription.id),
            EventSubscription.id,
            _subscription_cursor,
            _subscription_upper,
        )
        states: dict[int, str] = {
            delivery_id: state
            for delivery_id, state in session.execute(
                select(EventDelivery.id, EventDelivery.state).where(
                    EventDelivery.id.in_(deliveries)
                )
            ).all()
        }
    for delivery_id in deliveries:
        if states.get(delivery_id) != "processing":
            continue
        with db.SessionLocal() as session:
            try:
                item = session.get(EventDelivery, delivery_id)
                if item is not None and item.state == "processing":
                    delivery.observe_terminal(session, delivery_id)
                session.commit()
            except Exception:
                session.rollback()
                logger.warning("subscription delivery tick failed", exc_info=False)
    with db.SessionLocal() as session:
        batches, _batch_cursor, _batch_upper = sync.select_batch_maintenance_candidates(
            session, _batch_cursor, _batch_upper, 100
        )
    for batch_id in batches:
        with db.SessionLocal() as session:
            try:
                sync.maintain_batch(session, batch_id)
                session.commit()
            except Exception:
                session.rollback()
                logger.warning("subscription batch tick failed", exc_info=False)
    # Fair, persisted ready controls have a dedicated scan, independent of Sub GC.
    with db.SessionLocal() as session:
        tickets = sync.select_ready_control_candidates(session, limit=100)
    for ticket in tickets:
        with db.SessionLocal() as session:
            try:
                sync.materialize_ready_control(session, ticket)
                session.commit()
            except Exception:
                session.rollback()
                logger.warning("subscription control tick failed", exc_info=False)
    for delivery_id in deliveries:
        if states.get(delivery_id) != "pending":
            continue
        with db.SessionLocal() as session:
            try:
                item = session.get(EventDelivery, delivery_id)
                if item is not None and item.state == "pending":
                    delivery.dispatch_delivery(session, delivery_id)
                session.commit()
            except Exception:
                session.rollback()
                logger.warning("subscription delivery tick failed", exc_info=False)
    for subscription_id in subscriptions:
        with db.SessionLocal() as session:
            try:
                _, subscription = delivery.lock_subscription(session, subscription_id)
                delivery.cleanup_history(session, subscription_id)
                if subscription.deleted_at is None:
                    delivery.refresh_delivery_state(session, subscription)
                session.commit()
            except Exception:
                session.rollback()
                logger.warning("subscription history tick failed", exc_info=False)


async def subscription_maintenance_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(maintenance_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("subscription maintenance unavailable", exc_info=False)
        await asyncio.sleep(settings.event_subscription_tick_seconds)


def quota_audit_tick() -> None:
    """Read-only bounded page; one ledger-only global comparison per low-frequency cycle."""
    from dlr.control import db
    from dlr.control.services.subscription_delivery import audit_global_quota, audit_quota

    global _quota_cursor, _quota_upper
    with db.SessionLocal() as session:
        ids, _quota_cursor, _quota_upper = bounded_page(
            session,
            select(EventSubscription.id),
            EventSubscription.id,
            _quota_cursor,
            _quota_upper,
        )
    for subscription_id in ids:
        with db.SessionLocal() as session:
            report = audit_quota(session, subscription_id)
            if report["differences"]:
                logger.warning("subscription quota drift subscription_id=%s", subscription_id)
    with db.SessionLocal() as session:
        if audit_global_quota(session)["differences"]:
            logger.warning("subscription global quota ledger drift")


async def subscription_quota_audit_loop() -> None:
    while True:
        # Unlike delivery maintenance, audits are not a per-second global workload.
        await asyncio.sleep(settings.event_subscription_quota_audit_seconds)
        try:
            await asyncio.to_thread(quota_audit_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("subscription quota audit unavailable", exc_info=False)
