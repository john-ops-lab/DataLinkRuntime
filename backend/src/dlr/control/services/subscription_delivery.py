"""Subscription T1/T2 and bounded custody. Callers own commit/rollback.

Lock order: Adapter -> Subscription -> Lease/Checkpoint -> Receipt/Batch ->
subscription/global Inbox counters -> existing Admission -> Execution/Outbox.
No source RPC, broker ACK, sleep or user code runs in these transactions.
"""

import hashlib
from datetime import timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import case, delete, exists, func, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.selectable import Exists

from dlr.common.config import settings
from dlr.control.models import (
    ActivationCredentialReference,
    Adapter,
    AdapterCredentialBinding,
    DeliveryDisposition,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    Execution,
    SourceCheckpoint,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionLease,
    SubscriptionQuota,
    SyncBatch,
)
from dlr.control.services.adapter import domain_error
from dlr.control.services.event_envelope import validate_envelope
from dlr.control.services.input_config import database_now

DEFAULT_LIMITS = {
    "pending_count": 1000,
    "pending_bytes": 32 * 1024**2,
    "history_count": 10000,
    "history_bytes": 256 * 1024**2,
    "dedupe_count": 500000,
    "rate": 10,
}
GLOBAL_LIMITS = {
    "pending_count": 10000,
    "pending_bytes": 256 * 1024**2,
    "history_count": 100000,
    "history_bytes": 1024**3,
    "dedupe_count": 500000,
    "rate": 50,
}
TERMINAL = {"succeeded", "skipped", "superseded", "abandoned"}
EXECUTION_TERMINAL = {"succeeded", "dead_letter", "cancelled", "expired"}


def lock_subscription(session: Session, subscription_id: int) -> tuple[Adapter, EventSubscription]:
    adapter_id = session.scalar(
        select(EventSubscription.adapter_id).where(EventSubscription.id == subscription_id)
    )
    if adapter_id is None:
        raise domain_error(404, "subscription_not_found", "Subscription not found")
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    subscription = session.get(
        EventSubscription, subscription_id, with_for_update=True, populate_existing=True
    )
    if adapter is None or subscription is None:
        raise domain_error(404, "subscription_not_found", "Subscription not found")
    return adapter, subscription


def refresh_delivery_state(session: Session, sub: EventSubscription) -> None:
    """Persist the same responsibility-derived status that management serializes."""
    if sub.source_type == "kubernetes":
        from dlr.control.services.subscription_sync import sync_status_snapshot

        sub.delivery_state = sync_status_snapshot(session, sub.id).delivery_state
        return
    if sub.delivery_state == "repair":
        return
    counts = {
        state: count
        for state, count in session.execute(
            select(EventReceipt.state, func.count())
            .where(EventReceipt.subscription_id == sub.id)
            .group_by(EventReceipt.state)
        ).all()
    }
    if counts.get("failed", 0):
        sub.delivery_state = "blocked"
    elif sub.desired_state == "disabled" and (
        counts.get("pending", 0) or counts.get("processing", 0)
    ):
        sub.delivery_state = "draining"
    elif counts.get("processing", 0):
        sub.delivery_state = "processing"
    elif counts.get("pending", 0):
        sub.delivery_state = "pending"
    else:
        sub.delivery_state = "idle"


def _lease(
    session: Session, subscription: EventSubscription, owner_id: str, generation: int
) -> SubscriptionLease:
    lease = session.get(
        SubscriptionLease, subscription.id, with_for_update=True, populate_existing=True
    )
    if (
        lease is None
        or lease.owner_id != owner_id
        or lease.generation != generation
        or lease.lease_until <= database_now(session)
        or subscription.desired_state != "enabled"
        or subscription.deleted_at is not None
    ):
        raise domain_error(409, "subscription_owner_stale", "Listener ownership expired")
    return lease


def acquire_lease(
    session: Session, subscription_id: int, owner_id: str, ttl: int = 30
) -> SubscriptionLease:
    if not owner_id or len(owner_id) > 128 or not 1 <= ttl <= 30:
        raise domain_error(422, "subscription_lease_invalid", "Invalid listener lease")
    _, sub = lock_subscription(session, subscription_id)
    if sub.desired_state != "enabled" or sub.deleted_at is not None:
        raise domain_error(409, "subscription_owner_stale", "Subscription is not enabled")
    now = database_now(session)
    lease = session.get(SubscriptionLease, sub.id, with_for_update=True, populate_existing=True)
    if lease is not None and lease.lease_until > now:
        # Reusing an owner name cannot reacquire/revive a cancelled generation.
        raise domain_error(409, "subscription_owner_conflict", "Listener already owned")
    if lease is None:
        lease = SubscriptionLease(
            subscription_id=sub.id,
            owner_id=owner_id,
            generation=1,
            lease_until=now + timedelta(seconds=ttl),
        )
        session.add(lease)
    else:
        lease.owner_id = owner_id
        lease.generation += 1
        lease.lease_until = now + timedelta(seconds=ttl)
    lease.closing_generation = None
    session.flush()
    return lease


def renew_lease(
    session: Session, subscription_id: int, owner_id: str, generation: int, ttl: int = 30
) -> SubscriptionLease:
    if not owner_id or len(owner_id) > 128 or not 1 <= ttl <= 30:
        raise domain_error(422, "subscription_lease_invalid", "Invalid listener lease")
    # Renewal only locks Lease; desired changes are fenced by generation under the same row.
    lease = session.get(
        SubscriptionLease, subscription_id, with_for_update=True, populate_existing=True
    )
    now = database_now(session)
    if (
        lease is None
        or lease.owner_id != owner_id
        or lease.generation != generation
        or lease.lease_until <= now
    ):
        raise domain_error(409, "subscription_owner_stale", "Listener ownership expired")
    lease.lease_until = now + timedelta(seconds=ttl)
    return lease


def _quotas(session: Session, subscription_id: int) -> list[SubscriptionQuota]:
    result = []
    for scope in (f"sub:{subscription_id}", "global"):
        session.execute(insert(SubscriptionQuota).values(scope=scope).on_conflict_do_nothing())
        row = session.get(SubscriptionQuota, scope, with_for_update=True, populate_existing=True)
        assert row is not None
        result.append(row)
    return result


def _limits(session: Session, activation: SubscriptionActivation) -> dict[str, int]:
    config = session.get(SubscriptionConfigVersion, activation.config_version_id)
    assert config is not None
    values = dict(DEFAULT_LIMITS)
    for key, value in config.config.get("limits", {}).items():
        if key not in values or isinstance(value, bool) or not isinstance(value, int):
            raise domain_error(422, "subscription_config_invalid", "Invalid capacity limit")
        if value < 1 or value > values[key]:
            raise domain_error(422, "subscription_config_invalid", "Capacity may only be tightened")
        values[key] = value
    return values


def _reserve(
    session: Session, sub: EventSubscription, activation: SubscriptionActivation, size: int
) -> None:
    if session.scalar(select(exists().where(SyncBatch.ready_kind.is_not(None)))):
        raise domain_error(
            429, "subscription_capacity_exceeded", "Ready control owns the next token"
        )
    rows = _quotas(session, sub.id)
    limits = _limits(session, activation)
    now = database_now(session)
    oldest = session.scalar(
        select(func.min(EventReceipt.first_received_at)).where(
            EventReceipt.subscription_id == sub.id, EventReceipt.state == "pending"
        )
    )
    if oldest is not None and now - oldest >= timedelta(minutes=30):
        raise domain_error(429, "subscription_capacity_exceeded", "Oldest pending exceeded limit")
    for quota, maximum in zip(rows, (limits, GLOBAL_LIMITS), strict=True):
        if quota.blocked_reason is not None:
            below_low_water = all(
                getattr(quota, key)
                + (
                    quota.reserved_control_bytes
                    if key.endswith("bytes")
                    else quota.reserved_control_count
                )
                <= maximum[key] * 0.8
                for key in (
                    "pending_count",
                    "pending_bytes",
                    "history_count",
                    "history_bytes",
                    "dedupe_count",
                )
            )
            if not below_low_water or (oldest is not None and now - oldest >= timedelta(minutes=5)):
                raise domain_error(429, "subscription_capacity_exceeded", "Waiting for low water")
            quota.blocked_reason = None
        for key, delta in (
            ("pending_count", 1),
            ("pending_bytes", size),
            ("history_count", 1),
            ("history_bytes", size),
            ("dedupe_count", 1),
        ):
            reserved = (
                quota.reserved_control_bytes
                if key.endswith("bytes")
                else quota.reserved_control_count
            )
            if getattr(quota, key) + delta + reserved > maximum[key]:
                raise domain_error(429, "subscription_capacity_exceeded", f"Capacity {key} reached")
        if quota.rate_window_at is None or now - quota.rate_window_at >= timedelta(seconds=1):
            quota.rate_count = 0
            quota.rate_window_at = now
        if quota.rate_count >= maximum["rate"]:
            raise domain_error(429, "subscription_capacity_exceeded", "Receipt rate reached")
    for quota in rows:
        quota.pending_count += 1
        quota.pending_bytes += size
        quota.history_count += 1
        quota.history_bytes += size
        quota.dedupe_count += 1
        quota.rate_count += 1


def receive_event(
    session: Session,
    subscription_id: int,
    owner_id: str,
    generation: int,
    envelope: dict[str, Any],
    shard: str,
    position: dict[str, Any],
    assignment_generation: int | None = None,
    batch_id: int | None = None,
) -> tuple[EventReceipt | None, bool]:
    """T1. Source commit is allowed only after the caller commits this entire transaction."""
    if envelope.get("source", {}).get("type") == "kubernetes":
        raise domain_error(
            422,
            "subscription_sync_protocol_required",
            "Kubernetes intake requires the batch protocol",
        )
    content_hash, size = validate_envelope(envelope)
    if envelope["source"]["type"] == "kafka" and position.get("offset") != str(
        int(envelope["source"]["position"]["offset"]) + 1
    ):
        raise domain_error(
            422, "event_position_invalid", "Kafka checkpoint requires safe next offset"
        )
    _, sub = lock_subscription(session, subscription_id)
    _lease(session, sub, owner_id, generation)
    checkpoint = session.scalar(
        select(SourceCheckpoint)
        .where(
            SourceCheckpoint.subscription_id == sub.id,
            SourceCheckpoint.source_epoch == sub.source_epoch,
            SourceCheckpoint.shard == shard,
        )
        .with_for_update()
    )
    existing = session.scalar(
        select(EventReceipt)
        .where(
            EventReceipt.subscription_id == sub.id,
            EventReceipt.source_epoch == sub.source_epoch,
            EventReceipt.source_event_id == envelope["event"]["id"],
        )
        .with_for_update()
    )
    if (
        checkpoint is not None
        and assignment_generation is not None
        and (assignment_generation != checkpoint.assignment_generation)
    ):
        raise domain_error(409, "subscription_owner_stale", "Partition assignment expired")
    if existing is not None:
        if existing.content_hash != content_hash:
            # The caller records/commits a bounded blocked diagnosis separately on rejection.
            raise domain_error(
                409, "event_identity_conflict", "Same source identity changed content"
            )
        existing.duplicate_count += 1
        existing.last_seen_at = database_now(session)
        return existing, True
    if checkpoint is not None and sub.source_type == "kafka":
        safe_offset = checkpoint.position.get("offset")
        if safe_offset is not None and int(envelope["source"]["position"]["offset"]) < int(
            safe_offset
        ):
            # Retained checkpoint outlives the 30-day source-identity window.
            return None, True
    if (
        checkpoint is not None
        and checkpoint.blocked_unaccepted is not None
        and checkpoint.blocked_unaccepted["code"] != "subscription_capacity_exceeded"
    ):
        raise domain_error(
            409, "source_shard_blocked", "Unaccepted source fault requires disposition"
        )
    if not settings.event_subscriptions_enabled:
        raise domain_error(409, "source_not_available", "Event subscriptions are disabled")
    if sub.activation_id is None:
        raise domain_error(409, "subscription_activation_missing", "Activation is unavailable")
    activation = session.get(SubscriptionActivation, sub.activation_id)
    if activation is None or activation.subscription_id != sub.id:
        raise domain_error(409, "subscription_activation_missing", "Activation is unavailable")
    config = session.get(SubscriptionConfigVersion, activation.config_version_id)
    assert config is not None
    if (
        envelope["source"]["type"] != sub.source_type
        or str(envelope["subscription"]["id"]) != str(sub.id)
        or envelope["subscription"]["source_epoch"] != sub.source_epoch
        or envelope["subscription"]["config_version"] != config.version
    ):
        raise domain_error(409, "subscription_config_conflict", "Event activation metadata differs")
    if checkpoint is None:
        shard_count = (
            session.scalar(
                select(func.count())
                .select_from(SourceCheckpoint)
                .where(
                    SourceCheckpoint.subscription_id == sub.id,
                    SourceCheckpoint.source_epoch == sub.source_epoch,
                )
            )
            or 0
        )
        if shard_count >= (16 if sub.source_type == "kafka" else 1):
            raise domain_error(429, "subscription_capacity_exceeded", "Source shard budget reached")
        checkpoint = SourceCheckpoint(
            subscription_id=sub.id,
            source_epoch=sub.source_epoch,
            shard=shard,
            position={},
            confirmed_position={},
            next_sequence=1,
            assignment_generation=assignment_generation or 1,
        )
        session.add(checkpoint)
        session.flush()
    if (
        assignment_generation is not None
        and assignment_generation != checkpoint.assignment_generation
    ):
        raise domain_error(409, "subscription_owner_stale", "Partition assignment expired")
    _reserve(session, sub, activation, size)
    now = database_now(session)
    receipt = EventReceipt(
        subscription_id=sub.id,
        activation_id=activation.id,
        source_epoch=sub.source_epoch,
        source_event_id=envelope["event"]["id"],
        content_hash=content_hash,
        envelope=envelope,
        payload_bytes=size,
        shard=shard,
        sequence=checkpoint.next_sequence,
        batch_id=batch_id,
        state="pending",
        first_received_at=now,
        last_seen_at=now,
    )
    session.add(receipt)
    session.flush()
    session.add(EventDelivery(receipt_id=receipt.id, ordinal=1, state="pending"))
    checkpoint.next_sequence += 1
    checkpoint.position = position
    if (
        checkpoint.blocked_unaccepted is not None
        and checkpoint.blocked_unaccepted["code"] == "subscription_capacity_exceeded"
    ):
        checkpoint.blocked_unaccepted = None
    session.flush()
    refresh_delivery_state(session, sub)
    return receipt, False


def _delivery_context(
    session: Session, delivery_id: int
) -> tuple[Adapter, EventSubscription, SourceCheckpoint, EventReceipt, EventDelivery]:
    info = session.execute(
        select(EventReceipt.subscription_id, EventReceipt.id)
        .join(EventDelivery, EventDelivery.receipt_id == EventReceipt.id)
        .where(EventDelivery.id == delivery_id)
    ).first()
    if info is None:
        raise domain_error(404, "event_delivery_not_found", "Delivery not found")
    adapter, sub = lock_subscription(session, info[0])
    receipt_info = session.get(EventReceipt, info[1])
    assert receipt_info is not None
    checkpoint = session.scalar(
        select(SourceCheckpoint)
        .where(
            SourceCheckpoint.subscription_id == sub.id,
            SourceCheckpoint.source_epoch == receipt_info.source_epoch,
            SourceCheckpoint.shard == receipt_info.shard,
        )
        .with_for_update()
    )
    if receipt_info.batch_id is not None:
        session.get(SyncBatch, receipt_info.batch_id, with_for_update=True, populate_existing=True)
    receipt = session.get(EventReceipt, info[1], with_for_update=True, populate_existing=True)
    delivery = session.get(EventDelivery, delivery_id, with_for_update=True, populate_existing=True)
    assert receipt is not None and delivery is not None and checkpoint is not None
    return adapter, sub, checkpoint, receipt, delivery


def dispatch_delivery(session: Session, delivery_id: int) -> Execution | None:
    """T2 commits Admission, immutable Execution, Outbox, delivery and quota together."""
    from dlr.control.services.reliable_execution import accept_execution

    adapter, sub, checkpoint, receipt, delivery = _delivery_context(session, delivery_id)
    if delivery.execution_id is not None:
        return session.get(Execution, delivery.execution_id)
    if delivery.state != "pending":
        return None
    if sub.delivery_state == "repair" and (
        delivery.revision_override_id is None
        or delivery.revision_override_id != sub.repair_revision_id
        or checkpoint.blocked_receipt_id != receipt.id
    ):
        return None
    if delivery.retry_after is not None and delivery.retry_after > database_now(session):
        return None
    from dlr.control.services import subscription_sync as sync

    if sub.source_type == "kubernetes" and not sync.delivery_eligible(
        session, sub, checkpoint, receipt
    ):
        return None
    closing_control = receipt.source_phase in {"sync.completed", "sync.failed"}
    earlier = session.scalar(
        select(EventReceipt.id)
        .where(
            EventReceipt.subscription_id == sub.id,
            EventReceipt.source_epoch == receipt.source_epoch,
            EventReceipt.shard == receipt.shard,
            EventReceipt.sequence < receipt.sequence,
            EventReceipt.state.not_in(TERMINAL),
        )
        .limit(1)
    )
    if earlier is not None and not closing_control:
        return None
    if receipt.envelope is None:
        raise domain_error(409, "event_payload_expired", "Event payload expired")
    activation = session.get(
        SubscriptionActivation, delivery.activation_override_id or receipt.activation_id
    )
    assert activation is not None
    refs = session.scalars(
        select(ActivationCredentialReference).where(
            ActivationCredentialReference.activation_id == activation.id
        )
    ).all()
    quotas = _quotas(session, sub.id)
    try:
        with session.begin_nested():
            execution = accept_execution(
                session,
                adapter,
                trigger="subscription",
                runtime_input=receipt.envelope,
                input_source_type="json",
                input_config_revision=1,
                input_snapshot={"source_type": "json", "revision": None},
                version_id=delivery.revision_override_id or activation.revision_id,
                worker_id_override=activation.worker_id,
                subscription_activation_id=activation.id,
                credential_bindings_override=[
                    {
                        "binding_id": ref.binding_id,
                        "credential_id": ref.credential_id,
                        "env_key": ref.env_key,
                        "field": ref.field,
                    }
                    for ref in refs
                ],
            )
            sync.freeze_completion(session, receipt, execution.id)
            delivery.execution_id = execution.id
            delivery.state = "processing"
            receipt.state = "processing"
            checkpoint.blocked_receipt_id = receipt.id
            for quota in quotas:
                quota.pending_count -= 1
                quota.pending_bytes -= receipt.payload_bytes
            session.flush()
    except HTTPException as error:
        if error.status_code not in {429, 503}:
            raise
        delivery.retry_after = database_now(session) + timedelta(seconds=5)
        return None
    refresh_delivery_state(session, sub)
    return execution


def observe_terminal(session: Session, delivery_id: int) -> None:
    _, sub, checkpoint, receipt, delivery = _delivery_context(session, delivery_id)
    if delivery.execution_id is None or delivery.state != "processing":
        return
    execution = session.get(Execution, delivery.execution_id)
    if execution is None or execution.status not in EXECUTION_TERMINAL:
        return
    now = database_now(session)
    delivery.completed_at = now
    if execution.status == "succeeded":
        delivery.state = receipt.state = "succeeded"
        receipt.terminal_at = now
        checkpoint.blocked_receipt_id = None
    else:
        delivery.state = receipt.state = "failed"
        checkpoint.blocked_receipt_id = receipt.id
    if sub.source_type == "kubernetes":
        from dlr.control.services import subscription_sync as sync

        sync._recompute_blocker(session, sub, checkpoint)
        if receipt.batch_id is not None:
            sync.maintain_batch(session, receipt.batch_id)
    refresh_delivery_state(session, sub)


def has_unfinished(
    session: Session, subscription_id: int, exclude_receipt_id: int | None = None
) -> bool:
    from dlr.control.services.subscription_sync import unfinished_sync_predicate

    if session.scalar(select(unfinished_sync_predicate(subscription_id))):
        return True
    return bool(
        session.scalar(
            select(
                exists().where(
                    EventReceipt.subscription_id == subscription_id,
                    EventReceipt.state.not_in(TERMINAL),
                    EventReceipt.id != exclude_receipt_id
                    if exclude_receipt_id is not None
                    else true(),
                )
            )
        )
    )


def execution_protected() -> Exists:
    """Correlated retention guard, including payload and dedupe protection windows."""
    return exists(
        select(EventDelivery.id)
        .join(EventReceipt)
        .where(
            EventDelivery.execution_id == Execution.id,
            (EventReceipt.state.not_in(TERMINAL))
            | (EventReceipt.envelope.is_not(None))
            | (EventReceipt.terminal_at > func.now() - timedelta(days=30)),
        )
    ).correlate(Execution)


def _disposition(
    session: Session,
    sub: EventSubscription,
    receipt: EventReceipt,
    request_id: str,
    action: str,
    actor_id: int | None,
    reason: str,
) -> DeliveryDisposition | None:
    if not request_id or len(request_id) > 128 or not reason.strip() or len(reason) > 4096:
        raise domain_error(422, "event_disposition_invalid", "Request ID and reason required")
    existing = session.scalar(
        select(DeliveryDisposition).where(
            DeliveryDisposition.subscription_id == sub.id,
            DeliveryDisposition.request_id == request_id,
        )
    )
    if existing is not None and (
        existing.action != action
        or existing.receipt_id != receipt.id
        or existing.reason != reason
        or existing.actor_id != actor_id
    ):
        raise domain_error(409, "event_disposition_conflict", "Request ID already used differently")
    return existing


def current_replay_context(session: Session, sub: EventSubscription) -> tuple[dict[str, Any], str]:
    """Read-only reference preview. Call under the Adapter lock when accepting replay."""
    from dlr.control.services.event_envelope import canonical
    from dlr.control.services.reliable_execution import resolve_queue_target_worker

    adapter = session.get(Adapter, sub.adapter_id)
    assert adapter is not None
    if adapter.latest_version_id is None:
        raise domain_error(409, "subscription_activation_missing", "Current target unavailable")
    worker = resolve_queue_target_worker(session, adapter)
    config = session.scalar(
        select(SubscriptionConfigVersion).where(
            SubscriptionConfigVersion.subscription_id == sub.id,
            SubscriptionConfigVersion.version == sub.config_version,
        )
    )
    assert config is not None
    context = {
        "config_version": config.version,
        "source_epoch": config.source_epoch,
        "revision_id": adapter.latest_version_id,
        "worker_id": worker.id,
        "source_credential_id": config.source_credential_id,
        "credential_refs": [
            {"credential_id": row.credential_id, "env_key": row.env_key, "field": row.field}
            for row in session.scalars(
                select(AdapterCredentialBinding)
                .where(AdapterCredentialBinding.adapter_id == adapter.id)
                .order_by(AdapterCredentialBinding.env_key)
            )
        ],
    }
    # This is an optimistic concurrency fingerprint, not an authorization token.
    token = hashlib.sha256(canonical({"subscription_id": sub.id, **context})).hexdigest()
    return context, token


def replay_event(
    session: Session,
    receipt_id: int,
    request_id: str,
    actor_id: int | None,
    reason: str,
    revision_id: int | None = None,
    use_current_configuration: bool = False,
    expected_preview_token: str | None = None,
    expected_config_version: int | None = None,
) -> EventDelivery:
    info = session.get(EventReceipt, receipt_id)
    if info is None:
        raise domain_error(404, "event_not_found", "Event not found")
    _, sub = lock_subscription(session, info.subscription_id)
    original_id = session.scalar(
        select(EventDelivery.id)
        .where(EventDelivery.receipt_id == receipt_id)
        .order_by(EventDelivery.ordinal.desc())
        .limit(1)
    )
    assert original_id is not None
    _, _, checkpoint, receipt, previous = _delivery_context(session, original_id)
    existing = _disposition(session, sub, receipt, request_id, "replay", actor_id, reason)
    if existing is not None:
        delivery = session.get(EventDelivery, existing.delivery_id)
        assert delivery is not None
        if (
            delivery.revision_override_id != revision_id
            or bool(delivery.activation_override_id) != use_current_configuration
            or existing.details.get("preview_token") != expected_preview_token
            or (
                "expected_config_version" in existing.details
                and existing.details["expected_config_version"] != expected_config_version
            )
        ):
            raise domain_error(409, "event_disposition_conflict", "Replay version differs")
        return delivery
    if expected_config_version is not None and sub.config_version != expected_config_version:
        raise domain_error(
            409, "subscription_config_conflict", "Subscription configuration changed"
        )
    if receipt.envelope is None:
        raise domain_error(409, "event_payload_expired", "Event payload expired")
    from dlr.control.services.subscription_sync import disposition_guard

    disposition_guard(session, receipt)
    if previous.state in {"pending", "processing"}:
        raise domain_error(409, "event_replay_in_progress", "Event still has an active delivery")
    if checkpoint.blocked_receipt_id != receipt.id and (
        sub.desired_state != "disabled" or has_unfinished(session, sub.id)
    ):
        raise domain_error(409, "subscription_draining", "Out of band replay requires drained stop")
    if revision_id is not None and (
        sub.delivery_state != "repair"
        or sub.repair_revision_id != revision_id
        or checkpoint.blocked_receipt_id != receipt.id
    ):
        raise domain_error(409, "subscription_repair_required", "Explicit repair Revision required")
    activation = session.get(SubscriptionActivation, receipt.activation_id)
    assert activation is not None
    override_id = None
    if use_current_configuration:
        if (
            revision_id is not None
            or sub.desired_state != "disabled"
            or has_unfinished(session, sub.id, exclude_receipt_id=receipt.id)
        ):
            raise domain_error(409, "subscription_draining", "Current replay requires drained stop")
        context, preview_token = current_replay_context(session, sub)
        if expected_preview_token is not None and expected_preview_token != preview_token:
            raise domain_error(
                409, "subscription_config_conflict", "Current replay configuration changed"
            )
        current_config = session.scalar(
            select(SubscriptionConfigVersion).where(
                SubscriptionConfigVersion.subscription_id == sub.id,
                SubscriptionConfigVersion.version == sub.config_version,
            )
        )
        assert current_config is not None
        activation = SubscriptionActivation(
            subscription_id=sub.id,
            config_version_id=current_config.id,
            revision_id=context["revision_id"],
            worker_id=context["worker_id"],
        )
        session.add(activation)
        session.flush()
        override_id = activation.id
        for binding in session.scalars(
            select(AdapterCredentialBinding).where(
                AdapterCredentialBinding.adapter_id == sub.adapter_id
            )
        ):
            session.add(
                ActivationCredentialReference(
                    activation_id=activation.id,
                    binding_id=binding.id,
                    credential_id=binding.credential_id,
                    env_key=binding.env_key,
                    field=binding.field,
                )
            )
    # Every replay also consumes retained history budget for its execution/audit chain.
    rows = _quotas(session, sub.id)
    limits = _limits(session, activation)
    for quota, maximum in zip(rows, (limits, GLOBAL_LIMITS), strict=True):
        if (
            quota.pending_count + 1 + quota.reserved_control_count > maximum["pending_count"]
            or quota.pending_bytes + receipt.payload_bytes + quota.reserved_control_bytes
            > maximum["pending_bytes"]
            or quota.history_count + 1 + quota.reserved_control_count > maximum["history_count"]
            or quota.history_bytes + receipt.payload_bytes + quota.reserved_control_bytes
            > maximum["history_bytes"]
        ):
            raise domain_error(429, "subscription_capacity_exceeded", "Replay Inbox full")
    for quota in rows:
        quota.pending_count += 1
        quota.pending_bytes += receipt.payload_bytes
        quota.history_count += 1
        quota.history_bytes += receipt.payload_bytes
    delivery = EventDelivery(
        receipt_id=receipt.id,
        ordinal=previous.ordinal + 1,
        state="pending",
        revision_override_id=revision_id,
        activation_override_id=override_id,
    )
    session.add(delivery)
    receipt.state = "pending"
    receipt.terminal_at = None
    session.flush()
    session.add(
        DeliveryDisposition(
            subscription_id=sub.id,
            request_id=request_id,
            action="replay",
            actor_id=actor_id,
            reason=reason,
            receipt_id=receipt.id,
            delivery_id=delivery.id,
            details={
                "previous_delivery_id": previous.id,
                "preview_token": expected_preview_token,
                "expected_config_version": expected_config_version,
            },
        )
    )
    refresh_delivery_state(session, sub)
    return delivery


def skip_event(
    session: Session, receipt_id: int, request_id: str, actor_id: int | None, reason: str
) -> DeliveryDisposition:
    info = session.get(EventReceipt, receipt_id)
    if info is None:
        raise domain_error(404, "event_not_found", "Event not found")
    _, sub = lock_subscription(session, info.subscription_id)
    original_id = session.scalar(
        select(EventDelivery.id)
        .where(EventDelivery.receipt_id == receipt_id)
        .order_by(EventDelivery.ordinal.desc())
        .limit(1)
    )
    assert original_id is not None
    _, _, checkpoint, receipt, delivery = _delivery_context(session, original_id)
    existing = _disposition(session, sub, receipt, request_id, "skip", actor_id, reason)
    if existing is not None:
        return existing
    from dlr.control.services.subscription_sync import disposition_guard

    disposition_guard(session, receipt, skip=True)
    if delivery.state == "processing":
        raise domain_error(409, "event_execution_active", "Execution must reach terminal first")
    if receipt.state in TERMINAL:
        raise domain_error(409, "event_already_resolved", "Event already resolved")
    batch = (
        session.get(SyncBatch, receipt.batch_id, with_for_update=True) if receipt.batch_id else None
    )
    if delivery.state == "pending":
        for quota in _quotas(session, sub.id):
            quota.pending_count -= 1
            quota.pending_bytes -= receipt.payload_bytes
    delivery.state = receipt.state = "skipped"
    delivery.completed_at = receipt.terminal_at = database_now(session)
    if checkpoint.blocked_receipt_id == receipt.id:
        checkpoint.blocked_receipt_id = None
    if sub.source_type == "kubernetes":
        from dlr.control.services.subscription_sync import after_skip

        after_skip(session, sub, receipt, checkpoint)
    elif batch is not None:
        batch.state, batch.failure_code = "failed", "member_skipped"
    disposition = DeliveryDisposition(
        subscription_id=sub.id,
        request_id=request_id,
        action="skip",
        actor_id=actor_id,
        reason=reason,
        receipt_id=receipt.id,
        delivery_id=delivery.id,
        details={},
    )
    session.add(disposition)
    refresh_delivery_state(session, sub)
    return disposition


def cleanup_history(session: Session, subscription_id: int) -> dict[str, int]:
    _, sub = lock_subscription(session, subscription_id)
    checkpoints = session.scalars(
        select(SourceCheckpoint)
        .where(
            SourceCheckpoint.subscription_id == sub.id,
            SourceCheckpoint.blocked_receipt_id.is_not(None),
        )
        .order_by(SourceCheckpoint.shard)
        .with_for_update()
    ).all()
    protected = [checkpoint.blocked_receipt_id for checkpoint in checkpoints]
    now = database_now(session)
    member = aliased(EventReceipt)
    active_batch = exists(
        select(SyncBatch.id).where(
            SyncBatch.id == EventReceipt.batch_id,
            (
                (SyncBatch.activation_id.is_(None) & SyncBatch.state.in_(["reading", "processing"]))
                | (
                    SyncBatch.activation_id.is_not(None)
                    & (
                        SyncBatch.finished_at.is_(None)
                        | SyncBatch.ready_kind.is_not(None)
                        | (SyncBatch.reserved_control_count > 0)
                    )
                )
            ),
        )
    ).correlate(EventReceipt)
    unresolved_member = exists(
        select(member.id).where(
            member.batch_id == EventReceipt.batch_id, member.state.not_in(TERMINAL)
        )
    ).correlate(EventReceipt)
    receipts = session.scalars(
        select(EventReceipt)
        .where(
            EventReceipt.subscription_id == sub.id,
            EventReceipt.state.in_(TERMINAL),
            EventReceipt.terminal_at <= now - timedelta(days=7),
            EventReceipt.envelope.is_not(None)
            | (EventReceipt.terminal_at <= now - timedelta(days=30)),
            EventReceipt.id.not_in(protected),
            ~active_batch,
            ~unresolved_member,
        )
        .order_by(EventReceipt.id)
        .limit(100)
        .with_for_update()
    ).all()
    eligible = receipts
    standalone = session.scalars(
        select(DeliveryDisposition)
        .where(
            DeliveryDisposition.subscription_id == sub.id,
            DeliveryDisposition.receipt_id.is_(None),
            DeliveryDisposition.created_at <= now - timedelta(days=30),
        )
        .order_by(DeliveryDisposition.id)
        .limit(100)
        .with_for_update()
    ).all()
    deleting_receipts = [
        receipt.id
        for receipt in receipts
        if receipt.terminal_at is not None and receipt.terminal_at <= now - timedelta(days=30)
    ]
    deleted_old = sub.deleted_at is not None and sub.deleted_at <= now - timedelta(days=30)
    receipt_ref = exists(
        select(EventReceipt.id).where(
            EventReceipt.activation_id == SubscriptionActivation.id,
            EventReceipt.id.not_in(deleting_receipts),
        )
    ).correlate(SubscriptionActivation)
    override_ref = exists(
        select(EventDelivery.id)
        .join(EventReceipt)
        .where(
            EventDelivery.activation_override_id == SubscriptionActivation.id,
            EventReceipt.id.not_in(deleting_receipts),
        )
    ).correlate(SubscriptionActivation)
    orphan_activations = session.scalars(
        select(SubscriptionActivation)
        .where(
            SubscriptionActivation.subscription_id == sub.id,
            SubscriptionActivation.created_at <= now - timedelta(days=30),
            ~receipt_ref,
            ~override_ref,
            ~exists(
                select(SyncBatch.id).where(SyncBatch.activation_id == SubscriptionActivation.id)
            ).correlate(SubscriptionActivation),
            SubscriptionActivation.id != sub.activation_id
            if not deleted_old and sub.activation_id is not None
            else true(),
        )
        .order_by(SubscriptionActivation.id)
        .limit(100)
        .with_for_update()
    ).all()
    deleting_activations = [activation.id for activation in orphan_activations]
    activation_ref = exists(
        select(SubscriptionActivation.id).where(
            SubscriptionActivation.config_version_id == SubscriptionConfigVersion.id,
            SubscriptionActivation.id.not_in(deleting_activations),
        )
    ).correlate(SubscriptionConfigVersion)
    orphan_configs = session.scalars(
        select(SubscriptionConfigVersion)
        .where(
            SubscriptionConfigVersion.subscription_id == sub.id,
            SubscriptionConfigVersion.created_at <= now - timedelta(days=30),
            ~activation_ref,
            SubscriptionConfigVersion.version != sub.config_version if not deleted_old else true(),
        )
        .order_by(SubscriptionConfigVersion.id)
        .limit(100)
        .with_for_update()
    ).all()
    quotas = _quotas(session, sub.id)
    for disposition in standalone:
        session.delete(disposition)
        for quota in quotas:
            quota.dedupe_count -= 1
    expired = deleted = 0
    for receipt in eligible:
        if receipt.envelope is not None:
            receipt.envelope = None
            receipt.payload_expired_at = now
            for quota in quotas:
                copies = (
                    session.scalar(
                        select(func.count())
                        .select_from(EventDelivery)
                        .where(EventDelivery.receipt_id == receipt.id)
                    )
                    or 0
                )
                quota.history_count -= copies
                quota.history_bytes -= receipt.payload_bytes * copies
            expired += 1
        if receipt.terminal_at is not None and receipt.terminal_at <= now - timedelta(days=30):
            audits = session.execute(
                select(
                    func.count(), func.coalesce(func.sum(DeliveryDisposition.audit_bytes), 0)
                ).where(
                    DeliveryDisposition.receipt_id == receipt.id,
                    DeliveryDisposition.audit_bytes > 0,
                )
            ).one()
            for quota in quotas:
                quota.history_count -= audits[0]
                quota.history_bytes -= audits[1]
                quota.dedupe_count -= audits[0]
            delivery_ids = select(EventDelivery.id).where(EventDelivery.receipt_id == receipt.id)
            session.execute(
                delete(DeliveryDisposition).where(DeliveryDisposition.delivery_id.in_(delivery_ids))
            )
            session.execute(delete(EventDelivery).where(EventDelivery.receipt_id == receipt.id))
            session.delete(receipt)
            for quota in quotas:
                quota.dedupe_count -= 1
            deleted += 1
    session.flush()
    old_batches = session.scalars(
        select(SyncBatch)
        .where(
            SyncBatch.subscription_id == sub.id,
            SyncBatch.activation_id.is_not(None),
            SyncBatch.finished_at <= now - timedelta(days=30),
            SyncBatch.ready_kind.is_(None),
            SyncBatch.reserved_control_count == 0,
            SyncBatch.reserved_control_bytes == 0,
            ~exists(select(EventReceipt.id).where(EventReceipt.batch_id == SyncBatch.id)).correlate(
                SyncBatch
            ),
            ~exists(
                select(DeliveryDisposition.id).where(DeliveryDisposition.batch_id == SyncBatch.id)
            ).correlate(SyncBatch),
            # Preserve the current recovery intent until explicitly superseded by a
            # new batch/epoch. Historical completed batches retain independent freeze.
            (
                (SyncBatch.pending_gap.is_(None))
                | (SyncBatch.source_epoch != sub.source_epoch)
                | (
                    SyncBatch.id
                    < select(func.max(SyncBatch.id))
                    .where(SyncBatch.subscription_id == sub.id)
                    .scalar_subquery()
                )
            ),
        )
        .order_by(SyncBatch.id)
        .limit(100)
        .with_for_update()
    ).all()
    for old_batch in old_batches:
        session.delete(old_batch)
    session.flush()
    for activation in orphan_activations:
        session.execute(
            delete(ActivationCredentialReference).where(
                ActivationCredentialReference.activation_id == activation.id
            )
        )
        if sub.activation_id == activation.id:
            sub.activation_id = None
        session.delete(activation)
    session.flush()
    for config in orphan_configs:
        session.delete(config)
    session.flush()
    return {"payloads_expired": expired, "metadata_deleted": deleted}


def purge_deleted_adapter_history(session: Session, adapter_id: int) -> None:
    """Called only by guarded permanent Adapter deletion; never an online TTL shortcut."""
    subs = session.scalars(
        select(EventSubscription)
        .where(EventSubscription.adapter_id == adapter_id)
        .order_by(EventSubscription.id)
        .with_for_update()
    ).all()
    for sub in subs:
        if (
            sub.deleted_at is None
            or sub.desired_state != "disabled"
            or has_unfinished(session, sub.id)
        ):
            raise domain_error(
                409, "subscription_draining", "Delete the drained subscription first"
            )
        receipt_ids = select(EventReceipt.id).where(EventReceipt.subscription_id == sub.id)
        activation_ids = select(SubscriptionActivation.id).where(
            SubscriptionActivation.subscription_id == sub.id
        )
        sub_quota, global_quota = _quotas(session, sub.id)
        for key in (
            "pending_count",
            "pending_bytes",
            "history_count",
            "history_bytes",
            "dedupe_count",
            "reserved_control_count",
            "reserved_control_bytes",
        ):
            setattr(global_quota, key, getattr(global_quota, key) - getattr(sub_quota, key))
        session.execute(
            delete(DeliveryDisposition).where(DeliveryDisposition.subscription_id == sub.id)
        )
        session.execute(delete(EventDelivery).where(EventDelivery.receipt_id.in_(receipt_ids)))
        session.execute(delete(EventReceipt).where(EventReceipt.subscription_id == sub.id))
        session.execute(delete(SyncBatch).where(SyncBatch.subscription_id == sub.id))
        session.execute(delete(SourceCheckpoint).where(SourceCheckpoint.subscription_id == sub.id))
        session.execute(
            delete(SubscriptionLease).where(SubscriptionLease.subscription_id == sub.id)
        )
        session.execute(
            delete(ActivationCredentialReference).where(
                ActivationCredentialReference.activation_id.in_(activation_ids)
            )
        )
        session.execute(
            delete(SubscriptionActivation).where(SubscriptionActivation.subscription_id == sub.id)
        )
        session.execute(
            delete(SubscriptionConfigVersion).where(
                SubscriptionConfigVersion.subscription_id == sub.id
            )
        )
        session.execute(delete(SubscriptionQuota).where(SubscriptionQuota.scope == f"sub:{sub.id}"))
        session.delete(sub)


def record_rejection(
    session: Session,
    subscription_id: int,
    owner_id: str,
    generation: int,
    shard: str,
    code: str,
    source_event_id: str,
    position: dict[str, Any],
    size: int = 0,
    content_hash: str | None = None,
) -> None:
    """Durable bounded diagnosis in a fresh transaction after rejected T1 rolls back.

    This is not a Receipt and never authorizes source ACK. One summary per bounded shard.
    """
    from dlr.control.services.event_envelope import canonical

    _, sub = lock_subscription(session, subscription_id)
    _lease(session, sub, owner_id, generation)
    summary = {
        "code": code,
        "source_event_id": source_event_id,
        "position": position,
        "size": max(0, size),
        "content_hash": content_hash,
        "duplicate_count": 0,
    }
    if (
        len(shard) > 512
        or len(source_event_id) > 128
        or len(code) > 64
        or len(canonical(summary)) > 2048
    ):
        raise domain_error(422, "event_diagnosis_invalid", "Fault summary exceeds bounded shape")
    checkpoint = session.scalar(
        select(SourceCheckpoint)
        .where(
            SourceCheckpoint.subscription_id == sub.id,
            SourceCheckpoint.source_epoch == sub.source_epoch,
            SourceCheckpoint.shard == shard,
        )
        .with_for_update()
    )
    if checkpoint is None:
        count = (
            session.scalar(
                select(func.count())
                .select_from(SourceCheckpoint)
                .where(
                    SourceCheckpoint.subscription_id == sub.id,
                    SourceCheckpoint.source_epoch == sub.source_epoch,
                )
            )
            or 0
        )
        if count >= (16 if sub.source_type == "kafka" else 1):
            raise domain_error(429, "subscription_capacity_exceeded", "Source shard budget reached")
        checkpoint = SourceCheckpoint(
            subscription_id=sub.id,
            source_epoch=sub.source_epoch,
            shard=shard,
            position={},
            confirmed_position={},
            next_sequence=1,
            assignment_generation=1,
        )
        session.add(checkpoint)
    old = checkpoint.blocked_unaccepted
    if old is not None and old["source_event_id"] == source_event_id and old["code"] == code:
        summary["duplicate_count"] = min(int(old.get("duplicate_count", 0)) + 1, 2**63 - 1)
    if (
        old is not None
        and old["code"] != "subscription_capacity_exceeded"
        and (old["source_event_id"] != source_event_id or old["code"] != code)
    ):
        raise domain_error(409, "source_shard_blocked", "First unaccepted fault remains unresolved")
    if old is not None and old["source_event_id"] == source_event_id and old["code"] == code:
        summary = {**old, "duplicate_count": summary["duplicate_count"]}
    checkpoint.blocked_unaccepted = summary
    sub.last_error_code = code
    sub.connection_state = "blocked"
    if code == "subscription_capacity_exceeded":
        for quota in _quotas(session, sub.id):
            quota.blocked_reason = code


def skip_unaccepted(
    session: Session,
    subscription_id: int,
    shard: str,
    request_id: str,
    actor_id: int | None,
    reason: str,
    source_event_id: str,
    next_position: dict[str, Any],
) -> DeliveryDisposition:
    """Explicit source-position disposition; caller confirms source only after DB commit."""
    from dlr.control.services.event_envelope import canonical

    _, sub = lock_subscription(session, subscription_id)
    checkpoint = session.scalar(
        select(SourceCheckpoint)
        .where(
            SourceCheckpoint.subscription_id == sub.id,
            SourceCheckpoint.source_epoch == sub.source_epoch,
            SourceCheckpoint.shard == shard,
        )
        .with_for_update()
    )
    old = session.scalar(
        select(DeliveryDisposition).where(
            DeliveryDisposition.subscription_id == sub.id,
            DeliveryDisposition.request_id == request_id,
        )
    )
    details = {
        "source_event_id": source_event_id,
        "shard": shard,
        "source_epoch": sub.source_epoch,
        "next_position": next_position,
    }
    if old is not None:
        if (
            old.action != "skip_unaccepted"
            or old.details != details
            or old.reason != reason
            or old.actor_id != actor_id
        ):
            raise domain_error(409, "event_disposition_conflict", "Disposition request differs")
        return old
    if not request_id or len(request_id) > 128 or not reason.strip() or len(reason) > 4096:
        raise domain_error(422, "event_disposition_invalid", "Request ID and reason required")
    if (
        checkpoint is None
        or checkpoint.blocked_unaccepted is None
        or checkpoint.blocked_unaccepted["source_event_id"] != source_event_id
    ):
        raise domain_error(409, "event_diagnosis_invalid", "No matching unaccepted source fault")
    if sub.source_type != "kafka":
        raise domain_error(409, "source_history_gap", "Kubernetes fault requires a new full sync")
    if len(canonical(next_position)) > 1024:
        raise domain_error(422, "event_diagnosis_invalid", "Position exceeds bounded shape")
    raw_offset = checkpoint.blocked_unaccepted["position"].get("offset")
    supplied_offset = next_position.get("offset")
    if raw_offset is None or supplied_offset is None:
        raise domain_error(422, "event_diagnosis_invalid", "Kafka decimal offset is required")
    try:
        expected_offset = max(int(raw_offset) + 1, int(checkpoint.position.get("offset", "0")))
        if str(expected_offset) != supplied_offset:
            raise ValueError("unsafe skip offset")
    except (ValueError, TypeError):
        raise domain_error(
            422, "event_diagnosis_invalid", "Skip must use the exact safe next offset"
        ) from None

    reserve_disposition_metadata(session, sub.id)
    checkpoint.position = next_position
    checkpoint.blocked_unaccepted = None
    disposition = DeliveryDisposition(
        subscription_id=sub.id,
        request_id=request_id,
        action="skip_unaccepted",
        actor_id=actor_id,
        reason=reason,
        details=details,
    )
    session.add(disposition)
    return disposition


QUOTA_FACT_KEYS = (
    "pending_count",
    "pending_bytes",
    "history_count",
    "history_bytes",
    "dedupe_count",
    "reserved_control_count",
    "reserved_control_bytes",
)


def _current_limits(session: Session, sub: EventSubscription) -> dict[str, int]:
    config = session.scalar(
        select(SubscriptionConfigVersion).where(
            SubscriptionConfigVersion.subscription_id == sub.id,
            SubscriptionConfigVersion.version == sub.config_version,
        )
    )
    values = dict(DEFAULT_LIMITS)
    if config is not None:
        for key, value in config.config.get("limits", {}).items():
            if key not in values or isinstance(value, bool) or not isinstance(value, int):
                raise domain_error(422, "subscription_config_invalid", "Invalid capacity limit")
            if not 1 <= value <= values[key]:
                raise domain_error(
                    422, "subscription_config_invalid", "Capacity may only be tightened"
                )
            values[key] = value
    return values


def reserve_disposition_metadata(session: Session, subscription_id: int) -> None:
    """Bound audit rows without a source Receipt (reset, quota repair, poison skip)."""
    _, sub = lock_subscription(session, subscription_id)
    rows = _quotas(session, sub.id)
    for quota, maximum in zip(rows, (_current_limits(session, sub), GLOBAL_LIMITS), strict=True):
        if quota.dedupe_count + quota.reserved_control_count + 1 > maximum["dedupe_count"]:
            raise domain_error(
                429, "subscription_capacity_exceeded", "Disposition metadata budget full"
            )
    for quota in rows:
        quota.dedupe_count += 1


def audit_quota(session: Session, subscription_id: int, *, repair: bool = False) -> dict[str, Any]:
    """One SQL snapshot compares the local ledger with authoritative responsibilities.

    Explicit repair changes counters only. It may preserve pending/processing rows:
    corrupted counters can otherwise make draining impossible. The listener must
    actually be closed; merely timing out a lease is insufficient.
    """
    local = global_quota = None
    if repair:
        _, sub = lock_subscription(session, subscription_id)
        lease = session.get(SubscriptionLease, sub.id, with_for_update=True)
        if (
            sub.desired_state != "disabled"
            or sub.connection_state != "stopped"
            or (lease is not None and lease.closing_generation is not None)
        ):
            raise domain_error(
                409, "subscription_listener_not_closed", "Stop and close listener first"
            )
        local, global_quota = _quotas(session, sub.id)
    pending = EventDelivery.state == "pending"
    retained = EventReceipt.envelope.is_not(None)
    stats = (
        select(
            func.coalesce(func.sum(case((pending, 1), else_=0)), 0).label("pending_count"),
            func.coalesce(func.sum(case((pending, EventReceipt.payload_bytes), else_=0)), 0).label(
                "pending_bytes"
            ),
            func.coalesce(func.sum(case((retained, 1), else_=0)), 0).label("history_count"),
            func.coalesce(func.sum(case((retained, EventReceipt.payload_bytes), else_=0)), 0).label(
                "history_bytes"
            ),
        )
        .select_from(EventDelivery)
        .join(EventReceipt)
        .where(EventReceipt.subscription_id == subscription_id)
        .subquery()
    )
    receipt_count = (
        select(func.count())
        .select_from(EventReceipt)
        .where(EventReceipt.subscription_id == subscription_id)
        .scalar_subquery()
    )
    disposition_count = (
        select(func.count())
        .select_from(DeliveryDisposition)
        .where(
            DeliveryDisposition.subscription_id == subscription_id,
            DeliveryDisposition.receipt_id.is_(None),
        )
        .scalar_subquery()
    )
    audit_count = (
        select(func.count())
        .select_from(DeliveryDisposition)
        .where(
            DeliveryDisposition.subscription_id == subscription_id,
            DeliveryDisposition.audit_bytes > 0,
        )
        .scalar_subquery()
    )
    audit_bytes = (
        select(func.coalesce(func.sum(DeliveryDisposition.audit_bytes), 0))
        .where(
            DeliveryDisposition.subscription_id == subscription_id,
            DeliveryDisposition.audit_bytes > 0,
        )
        .scalar_subquery()
    )
    reserve_count = (
        select(func.coalesce(func.sum(SyncBatch.reserved_control_count), 0))
        .where(SyncBatch.subscription_id == subscription_id)
        .scalar_subquery()
    )
    reserve_bytes = (
        select(func.coalesce(func.sum(SyncBatch.reserved_control_bytes), 0))
        .where(SyncBatch.subscription_id == subscription_id)
        .scalar_subquery()
    )
    expected_columns = [
        stats.c.pending_count.label("expected_pending_count"),
        stats.c.pending_bytes.label("expected_pending_bytes"),
        (stats.c.history_count + audit_count).label("expected_history_count"),
        (stats.c.history_bytes + audit_bytes).label("expected_history_bytes"),
        (receipt_count + disposition_count + audit_count).label("expected_dedupe_count"),
        reserve_count.label("expected_reserved_control_count"),
        reserve_bytes.label("expected_reserved_control_bytes"),
    ]
    actual_columns = [
        func.coalesce(getattr(SubscriptionQuota, key), 0).label(f"actual_{key}")
        for key in QUOTA_FACT_KEYS
    ]
    row = session.execute(
        select(*expected_columns, *actual_columns).select_from(
            stats.outerjoin(SubscriptionQuota, SubscriptionQuota.scope == f"sub:{subscription_id}")
        )
    ).one()
    expected = {key: int(row._mapping[f"expected_{key}"]) for key in QUOTA_FACT_KEYS}
    actual = {key: int(row._mapping[f"actual_{key}"]) for key in QUOTA_FACT_KEYS}
    differences = {
        key: {"actual": actual[key], "expected": expected[key]}
        for key in QUOTA_FACT_KEYS
        if actual[key] != expected[key]
    }
    if repair:
        assert local is not None and global_quota is not None
        for key in QUOTA_FACT_KEYS:
            setattr(local, key, expected[key])
        session.flush()
        # Rebuild shared aggregates, do not blindly add a delta to an independently
        # corrupt global row. Every other quota writer waits on the held global lock.
        sums = session.execute(
            select(
                *[
                    func.coalesce(func.sum(getattr(SubscriptionQuota, key)), 0)
                    for key in QUOTA_FACT_KEYS
                ]
            ).where(SubscriptionQuota.scope.like("sub:%"))
        ).one()
        for key, value in zip(QUOTA_FACT_KEYS, sums, strict=True):
            setattr(global_quota, key, int(value))
    return {"actual": actual, "expected": expected, "differences": differences, "repaired": repair}


def audit_global_quota(session: Session, *, repair: bool = False) -> dict[str, Any]:
    """Internal/admin-only shared aggregate audit; never returned through an Adapter view."""
    global_quota = None
    if repair:
        session.execute(insert(SubscriptionQuota).values(scope="global").on_conflict_do_nothing())
        global_quota = session.get(
            SubscriptionQuota, "global", with_for_update=True, populate_existing=True
        )
    scopes = aliased(SubscriptionQuota)
    totals = (
        select(
            *[
                func.coalesce(func.sum(getattr(scopes, key)), 0).label(key)
                for key in QUOTA_FACT_KEYS
            ]
        )
        .where(scopes.scope.like("sub:%"))
        .subquery()
    )
    row = session.execute(
        select(
            *[getattr(totals.c, key).label(f"expected_{key}") for key in QUOTA_FACT_KEYS],
            *[
                func.coalesce(getattr(SubscriptionQuota, key), 0).label(f"actual_{key}")
                for key in QUOTA_FACT_KEYS
            ],
        ).select_from(totals.outerjoin(SubscriptionQuota, SubscriptionQuota.scope == "global"))
    ).one()
    expected = {key: int(row._mapping[f"expected_{key}"]) for key in QUOTA_FACT_KEYS}
    actual = {key: int(row._mapping[f"actual_{key}"]) for key in QUOTA_FACT_KEYS}
    if repair:
        assert global_quota is not None
        for key in QUOTA_FACT_KEYS:
            setattr(global_quota, key, expected[key])
    return {
        "actual": actual,
        "expected": expected,
        "differences": {
            key: {"actual": actual[key], "expected": expected[key]}
            for key in QUOTA_FACT_KEYS
            if actual[key] != expected[key]
        },
        "repaired": repair,
    }
