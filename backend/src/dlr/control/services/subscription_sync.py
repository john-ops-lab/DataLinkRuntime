"""Kubernetes custody and batch qualification; callers own commit/rollback.

Only source ingestion requires a live lease. Maintenance of prepaid obligations
works after fencing or feature disable and never performs source I/O.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import Exists

from dlr.common.config import settings
from dlr.control.models import (
    DeliveryDisposition,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    Execution,
    SourceCheckpoint,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionLease,
    SyncBatch,
)
from dlr.control.services import subscription_accounting as accounting
from dlr.control.services.adapter import domain_error
from dlr.control.services.event_envelope import canonical, event_id, validate_envelope
from dlr.control.services.input_config import database_now
from dlr.control.services.subscription_source import SourceRunContext

KUBERNETES_SHARD = "kubernetes"

CONTROL_ENVELOPE_BYTES = accounting.CONTROL_ENVELOPE_BYTES
CONTROL_BYTES = accounting.CONTROL_RESERVATION_BYTES
CONTROL_COUNT = 3
MAX_BATCH_ITEMS = 5000
MAX_BATCH_BYTES = 128 * 1024**2
CONTROL_KINDS = ("sync.started", "sync.completed", "sync.failed")
SYNC_TERMINAL = {"succeeded", "skipped", "superseded", "abandoned"}


@dataclass(frozen=True)
class SyncRef:
    batch_id: int
    subscription_id: int
    source_epoch: str
    activation_id: int
    scope_fingerprint: str
    read_complete: bool = False
    item_count: int = 0
    resource_version: str | None = None


@dataclass(frozen=True)
class SyncSourcePlan:
    action: Literal["new_list", "watch", "hold", "blocked"]
    batch: SyncRef | None = None
    safe_rv: str | None = None
    code: str | None = None
    safe_sequence: int = 0

    @property
    def batch_id(self) -> int | None:
        return self.batch.batch_id if self.batch else None


@dataclass(frozen=True)
class SyncAcceptResult:
    receipt_id: int
    duplicate: bool
    safe_sequence: int


@dataclass(frozen=True)
class SyncStep:
    changed: bool = False
    closed: bool = False
    deferred_code: str | None = None


@dataclass(frozen=True)
class NativeStartDecision:
    action: Literal["allow", "hold", "blocked"]
    code: str | None = None


@dataclass(frozen=True)
class ControlTicket:
    batch_id: int
    kind: str
    ready_since: datetime
    stable_id: int


@dataclass(frozen=True)
class RemainingReserve:
    count: int
    bytes: int


@dataclass(frozen=True)
class CompletionAbandonmentResult:
    disposition_id: int
    batch_id: int
    receipt_id: int
    latest_delivery_id: int
    execution_id: int | None
    request_id: str
    actor_id: int | None
    reason: str
    closed_at: datetime
    cancelled_pending_id: int | None = None
    outcome: str = "abandoned"
    business_result: str = "unconfirmed"


@dataclass(frozen=True)
class SyncControlView:
    kind: str
    receipt_id: int
    state: str | None
    latest_delivery_id: int | None
    latest_delivery_ordinal: int | None
    execution_id: int | None
    effective_activation_id: int | None
    revision_override_id: int | None


@dataclass(frozen=True)
class SyncBatchView:
    id: int
    subscription_id: int
    source_epoch: str | None
    activation_id: int | None
    config_version: int | None
    scope_fingerprint: str
    state: str
    read_complete: bool
    completion_committed: bool
    completion_abandoned: bool
    closed: bool
    drain_open: bool
    control_pending: tuple[str, ...]
    remaining_reserve: RemainingReserve
    pending_gap: dict[str, Any] | None
    failure_code: str | None
    created_at: datetime
    finished_at: datetime | None
    resource_version: str | None
    item_count: int
    page_count: int
    total_bytes: int
    controls: tuple[SyncControlView, ...]
    abandonment: CompletionAbandonmentResult | None


@dataclass(frozen=True)
class SyncStatus:
    delivery_state: str
    unfinished: bool
    latest_batch: SyncBatchView | None


def unfinished_sync_predicate(subscription_id_expr: Any) -> Exists:
    # Pre-connector skeletons have no activation and preserve their old meaning.
    # Real connector batches keep explicit protocol/reserve/intent facts.
    return exists().where(
        SyncBatch.subscription_id == subscription_id_expr,
        or_(
            and_(
                SyncBatch.activation_id.is_(None),
                SyncBatch.state.in_(["reading", "processing"]),
            ),
            and_(
                SyncBatch.activation_id.is_not(None),
                or_(
                    SyncBatch.finished_at.is_(None),
                    SyncBatch.ready_kind.is_not(None),
                    SyncBatch.reserved_control_count > 0,
                    SyncBatch.reserved_control_bytes > 0,
                ),
            ),
        ),
    )


def _closed(batch: SyncBatch) -> bool:
    if batch.activation_id is None:
        return batch.state not in {"reading", "processing"}
    return (
        batch.finished_at is not None
        and batch.ready_kind is None
        and batch.reserved_control_count == 0
        and batch.reserved_control_bytes == 0
    )


def _ref(batch: SyncBatch) -> SyncRef:
    if batch.activation_id is None or batch.source_epoch is None:
        raise domain_error(409, "subscription_sync_identity_missing", "Batch identity unavailable")
    return SyncRef(
        batch.id,
        batch.subscription_id,
        batch.source_epoch,
        batch.activation_id,
        batch.scope_fingerprint,
        batch.read_complete,
        batch.item_count,
        batch.resource_version,
    )


def _latest_batch(session: Session, sub_id: int) -> SyncBatch | None:
    batch = session.scalar(
        select(SyncBatch)
        .where(SyncBatch.subscription_id == sub_id, SyncBatch.finished_at.is_(None))
        .order_by(SyncBatch.id.desc())
        .limit(1)
    )
    if batch is None:
        batch = session.scalar(
            select(SyncBatch)
            .where(SyncBatch.subscription_id == sub_id)
            .order_by(SyncBatch.id.desc())
            .limit(1)
        )
    return batch


def _latest_delivery(session: Session, receipt_id: int) -> EventDelivery | None:
    return session.scalar(
        select(EventDelivery)
        .where(EventDelivery.receipt_id == receipt_id)
        .order_by(EventDelivery.ordinal.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )


def _abandon_result(disposition: DeliveryDisposition) -> CompletionAbandonmentResult:
    data = disposition.details
    assert disposition.batch_id is not None
    assert disposition.receipt_id is not None and disposition.delivery_id is not None
    return CompletionAbandonmentResult(
        disposition.id,
        disposition.batch_id,
        disposition.receipt_id,
        disposition.delivery_id,
        data["expected_execution_id"],
        disposition.request_id,
        disposition.actor_id,
        disposition.reason,
        disposition.created_at,
        data.get("cancelled_pending_id"),
    )


def batch_detail(session: Session, sub_id: int, batch_id: int) -> SyncBatchView:
    batch = session.scalar(
        select(SyncBatch).where(SyncBatch.subscription_id == sub_id, SyncBatch.id == batch_id)
    )
    if batch is None:
        raise domain_error(404, "subscription_batch_not_found", "Batch not found")
    activation = (
        session.get(SubscriptionActivation, batch.activation_id) if batch.activation_id else None
    )
    config = (
        session.get(SubscriptionConfigVersion, activation.config_version_id) if activation else None
    )
    controls = []
    for kind, receipt_id in zip(
        CONTROL_KINDS,
        (batch.started_receipt_id, batch.completed_receipt_id, batch.failed_receipt_id),
        strict=True,
    ):
        if receipt_id is None:
            continue
        receipt = session.get(EventReceipt, receipt_id)
        delivery = _latest_delivery(session, receipt_id)
        controls.append(
            SyncControlView(
                kind,
                receipt_id,
                receipt.state if receipt else None,
                delivery.id if delivery else None,
                delivery.ordinal if delivery else None,
                delivery.execution_id if delivery else None,
                (delivery.activation_override_id if delivery else None)
                or (receipt.activation_id if receipt else None),
                delivery.revision_override_id if delivery else None,
            )
        )
    abandonment = session.scalar(
        select(DeliveryDisposition).where(
            DeliveryDisposition.batch_id == batch.id,
            DeliveryDisposition.action == "abandon_completion",
        )
    )
    return SyncBatchView(
        batch.id,
        batch.subscription_id,
        batch.source_epoch,
        batch.activation_id,
        config.version if config else None,
        batch.scope_fingerprint,
        batch.state,
        batch.read_complete,
        batch.completion_committed,
        batch.completion_abandoned,
        _closed(batch),
        batch.drain_open,
        (batch.ready_kind,) if batch.ready_kind else (),
        RemainingReserve(batch.reserved_control_count, batch.reserved_control_bytes),
        batch.pending_gap,
        batch.failure_code,
        batch.created_at,
        batch.finished_at,
        batch.resource_version,
        batch.item_count,
        batch.page_count,
        batch.total_bytes,
        tuple(controls),
        _abandon_result(abandonment) if abandonment else None,
    )


def batch_summary(
    session: Session, sub_id: int, batch_id: int | None = None
) -> SyncBatchView | None:
    batch = _latest_batch(session, sub_id) if batch_id is None else None
    identity = batch_id if batch_id is not None else batch.id if batch else None
    return batch_detail(session, sub_id, identity) if identity is not None else None


def sync_status_snapshot(session: Session, sub_id: int) -> SyncStatus:
    sub = session.get(EventSubscription, sub_id)
    if sub is None:
        raise domain_error(404, "subscription_not_found", "Subscription not found")
    counts: dict[str, int] = {
        state: count
        for state, count in session.execute(
            select(EventReceipt.state, func.count())
            .where(EventReceipt.subscription_id == sub_id)
            .group_by(EventReceipt.state)
        ).all()
    }
    unfinished = bool(session.scalar(select(unfinished_sync_predicate(sub_id))))
    if sub.delivery_state == "repair":
        state = "repair"
    elif counts.get("failed", 0):
        state = "blocked"
    elif sub.desired_state == "disabled" and (
        counts.get("pending", 0) or counts.get("processing", 0) or unfinished
    ):
        state = "draining"
    elif counts.get("processing", 0):
        state = "processing"
    elif counts.get("pending", 0) or unfinished:
        state = "pending"
    else:
        state = "idle"
    return SyncStatus(state, unfinished, batch_summary(session, sub_id))


def native_start_decision(
    session: Session, sub_id: int, activation_id: int, source_epoch: str
) -> NativeStartDecision:
    sub = session.get(EventSubscription, sub_id)
    if sub is not None and sub.source_type != "kubernetes":
        return NativeStartDecision("allow")
    activation = session.get(SubscriptionActivation, activation_id)
    config = (
        session.get(SubscriptionConfigVersion, activation.config_version_id) if activation else None
    )
    if config is None or config.source_epoch != source_epoch or not config.source_identity:
        return NativeStartDecision("blocked", "subscription_sync_identity_missing")
    batch = _latest_batch(session, sub_id)
    if batch is None:
        return NativeStartDecision("allow")
    if batch.source_epoch != source_epoch:
        if not _closed(batch) or session.scalar(
            select(
                exists().where(
                    EventReceipt.batch_id == batch.id, EventReceipt.state.not_in(SYNC_TERMINAL)
                )
            )
        ):
            return NativeStartDecision("hold", "subscription_draining")
        return NativeStartDecision("allow")
    if (
        batch.read_complete
        and batch.state not in {"failed", "superseded"}
        and not batch.pending_gap
        and not batch.completion_abandoned
    ):
        return NativeStartDecision("allow")
    pending = session.scalar(
        select(
            exists().where(
                EventReceipt.subscription_id == sub_id, EventReceipt.state.not_in(SYNC_TERMINAL)
            )
        )
    )
    if not _closed(batch) or pending:
        return NativeStartDecision("hold", "subscription_draining")
    if not settings.event_subscriptions_enabled:
        return NativeStartDecision("hold", "source_not_available")
    return NativeStartDecision("allow")


def select_ready_control_candidates(session: Session, limit: int = 100) -> list[ControlTicket]:
    rows = session.scalars(
        select(SyncBatch)
        .where(SyncBatch.ready_kind.is_not(None), SyncBatch.ready_since.is_not(None))
        .order_by(SyncBatch.ready_since, SyncBatch.id)
        .limit(min(max(limit, 1), 100))
    ).all()
    return [
        ControlTicket(batch.id, batch.ready_kind, batch.ready_since, batch.id)
        for batch in rows
        if batch.ready_kind is not None and batch.ready_since is not None
    ]


def select_batch_maintenance_candidates(
    session: Session, cursor: int = 0, upper: int = 0, limit: int = 100
) -> tuple[list[int], int, int]:
    query = select(SyncBatch.id).where(
        SyncBatch.activation_id.is_not(None),
        or_(
            SyncBatch.finished_at.is_(None),
            SyncBatch.ready_kind.is_not(None),
            SyncBatch.reserved_control_count > 0,
        ),
    )
    if not upper:
        upper = int(session.scalar(select(func.max(SyncBatch.id))) or 0)
        cursor = 0
    ids = list(
        session.scalars(
            query.where(SyncBatch.id > cursor, SyncBatch.id <= upper)
            .order_by(SyncBatch.id)
            .limit(min(max(limit, 1), 100))
        )
    )
    if not ids or ids[-1] >= upper:
        return ids, 0, 0
    return ids, ids[-1], upper


def _context(
    session: Session, context: SourceRunContext
) -> tuple[EventSubscription, SubscriptionActivation, SubscriptionConfigVersion, SourceCheckpoint]:
    from dlr.control.services import subscription_delivery as delivery

    _, sub = delivery.lock_subscription(session, context.subscription_id)
    delivery._lease(session, sub, context.owner_id, context.generation)
    activation = session.get(SubscriptionActivation, context.activation_id)
    config = (
        session.get(SubscriptionConfigVersion, activation.config_version_id) if activation else None
    )
    if (
        sub.source_type != "kubernetes"
        or sub.activation_id != context.activation_id
        or sub.source_epoch != context.source_epoch
        or activation is None
        or activation.subscription_id != sub.id
        or config is None
        or config.version != context.config_version
        or config.source_epoch != sub.source_epoch
        or not config.source_identity
        or config.source_identity != context.identity_snapshot
    ):
        raise domain_error(409, "subscription_config_conflict", "Source activation differs")
    if not settings.event_subscriptions_enabled:
        raise domain_error(409, "source_not_available", "Source intake is disabled")
    cp = session.scalar(
        select(SourceCheckpoint)
        .where(
            SourceCheckpoint.subscription_id == sub.id,
            SourceCheckpoint.source_epoch == sub.source_epoch,
            SourceCheckpoint.shard == KUBERNETES_SHARD,
        )
        .with_for_update()
    )
    if cp is None:
        cp = SourceCheckpoint(
            subscription_id=sub.id,
            source_epoch=sub.source_epoch,
            shard=KUBERNETES_SHARD,
            position={},
            confirmed_position={},
            next_sequence=1,
            assignment_generation=1,
        )
        session.add(cp)
        session.flush()
    return sub, activation, config, cp


def _batch(session: Session, sub_id: int, batch_id: int) -> SyncBatch:
    batch = session.scalar(
        select(SyncBatch)
        .where(SyncBatch.id == batch_id, SyncBatch.subscription_id == sub_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if batch is None:
        raise domain_error(404, "subscription_sync_batch_not_found", "Batch not found")
    return batch


def _same_ref(batch: SyncBatch, context: SourceRunContext, ref: SyncRef) -> None:
    if (
        batch.id != ref.batch_id
        or batch.subscription_id != context.subscription_id
        or batch.activation_id != context.activation_id
        or batch.source_epoch != context.source_epoch
        or batch.scope_fingerprint != ref.scope_fingerprint
    ):
        raise domain_error(409, "subscription_config_conflict", "Batch identity differs")


def sync_source_plan(session: Session, context: SourceRunContext) -> SyncSourcePlan:
    sub, _, _, cp = _context(session, context)
    decision = native_start_decision(session, sub.id, context.activation_id, context.source_epoch)
    batch = _latest_batch(session, sub.id)
    sequence = cp.next_sequence - 1
    rv = cp.position.get("resource_version")
    if batch is not None and not batch.read_complete and not _closed(batch):
        if (
            batch.owner_id == context.owner_id
            and batch.owner_generation == context.generation
            and batch.failure_code is None
        ):
            return SyncSourcePlan("new_list", _ref(batch), rv, safe_sequence=sequence)
        return SyncSourcePlan("hold", _ref(batch), rv, "subscription_sync_draining", sequence)
    if decision.action != "allow":
        return SyncSourcePlan(
            decision.action,
            _ref(batch) if batch and batch.activation_id else None,
            rv,
            decision.code,
            sequence,
        )
    if (
        batch
        and batch.source_epoch == context.source_epoch
        and batch.read_complete
        and batch.failure_code is None
        and not batch.pending_gap
        and not batch.completion_abandoned
    ):
        return SyncSourcePlan("watch", _ref(batch), rv, safe_sequence=sequence)
    return SyncSourcePlan("new_list", safe_rv=rv, safe_sequence=sequence)


def begin_sync_batch(
    session: Session, context: SourceRunContext, scope_identity: dict[str, Any] | None
) -> SyncRef:
    from dlr.control.services import subscription_delivery as delivery

    sub, activation, config, _ = _context(session, context)
    if scope_identity != config.source_identity or not scope_identity:
        raise domain_error(409, "subscription_config_conflict", "Scope identity differs")
    existing = _latest_batch(session, sub.id)
    if existing and not _closed(existing):
        if (
            existing.owner_id == context.owner_id
            and existing.owner_generation == context.generation
            and existing.state == "reading"
        ):
            return _ref(existing)
        raise domain_error(409, "subscription_sync_draining", "Prior batch is unfinished")
    if existing and session.scalar(
        select(
            exists().where(
                EventReceipt.batch_id == existing.id, EventReceipt.state.not_in(SYNC_TERMINAL)
            )
        )
    ):
        raise domain_error(409, "subscription_sync_draining", "Prior batch responsibility remains")
    rows = delivery._quotas(session, sub.id)
    for quota, limits in zip(
        rows, (delivery._limits(session, activation), delivery._global_limits(session)), strict=True
    ):
        for key in (
            "pending_count",
            "pending_bytes",
            "history_count",
            "history_bytes",
            "dedupe_count",
        ):
            reserve = CONTROL_COUNT * CONTROL_BYTES if key.endswith("bytes") else CONTROL_COUNT
            current_reserve = (
                quota.reserved_control_bytes
                if key.endswith("bytes")
                else quota.reserved_control_count
            )
            if getattr(quota, key) + current_reserve + reserve > limits[key]:
                raise domain_error(
                    429, "subscription_sync_history_capacity", "Control reserve unavailable"
                )
    for quota in rows:
        quota.reserved_control_count += CONTROL_COUNT
        quota.reserved_control_bytes += CONTROL_COUNT * CONTROL_BYTES
    now = database_now(session)
    batch = SyncBatch(
        subscription_id=sub.id,
        activation_id=activation.id,
        source_epoch=sub.source_epoch,
        owner_id=context.owner_id,
        owner_generation=context.generation,
        scope_fingerprint=str(scope_identity["scope_fingerprint"]),
        state="reading",
        reserved_control_count=CONTROL_COUNT,
        reserved_control_bytes=CONTROL_COUNT * CONTROL_BYTES,
        ready_kind="sync.started",
        ready_since=now,
    )
    session.add(batch)
    session.flush()
    delivery.refresh_delivery_state(session, sub)
    return _ref(batch)


def _validate_source_envelope(
    envelope: dict[str, Any], sub: EventSubscription, config: SubscriptionConfigVersion
) -> tuple[str, int]:
    content_hash, size = validate_envelope(envelope)
    identity = config.source_identity
    assert identity is not None
    attrs = envelope["source"]["attributes"]
    if (
        envelope["source"]["type"] != "kubernetes"
        or envelope["source"]["identity"] != "kubernetes-scope-v1:" + identity["scope_fingerprint"]
        or str(envelope["subscription"]["id"]) != str(sub.id)
        or envelope["subscription"]["source_epoch"] != sub.source_epoch
        or envelope["subscription"]["config_version"] != config.version
        or (
            config.config.get("namespace") is not None
            and attrs["namespace"] != config.config["namespace"]
        )
        or attrs["cluster_id"] != config.config.get("cluster_id")
        or [attrs["group"], attrs["version"], attrs["resource"]]
        != [
            config.config.get("group", ""),
            config.config.get("version"),
            config.config.get("resource"),
        ]
    ):
        raise domain_error(409, "subscription_config_conflict", "Envelope scope differs")
    return content_hash, size


def _existing(
    session: Session, sub: EventSubscription, envelope: dict[str, Any], content_hash: str
) -> EventReceipt | None:
    receipt = session.scalar(
        select(EventReceipt)
        .where(
            EventReceipt.subscription_id == sub.id,
            EventReceipt.source_epoch == sub.source_epoch,
            EventReceipt.source_event_id == envelope["event"]["id"],
        )
        .with_for_update()
    )
    if receipt:
        if receipt.content_hash != content_hash:
            raise domain_error(409, "event_identity_conflict", "Source identity changed content")
        receipt.duplicate_count += 1
        receipt.last_seen_at = database_now(session)
    return receipt


def _insert_receipt(
    session: Session,
    sub: EventSubscription,
    activation: SubscriptionActivation,
    cp: SourceCheckpoint,
    envelope: dict[str, Any],
    content_hash: str,
    size: int,
    batch_id: int | None,
    phase: str,
    page_index: int | None = None,
) -> EventReceipt:
    from dlr.control.models import Adapter
    from dlr.control.services.subscription_activation_policy import ensure_policy

    adapter = session.get(Adapter, sub.adapter_id)
    assert adapter is not None
    ensure_policy(session, activation, adapter)
    now = database_now(session)
    object_key = envelope["event"]["object_key"]
    receipt = EventReceipt(
        subscription_id=sub.id,
        activation_id=activation.id,
        source_epoch=sub.source_epoch,
        source_event_id=envelope["event"]["id"],
        content_hash=content_hash,
        envelope=envelope,
        payload_bytes=size,
        shard=KUBERNETES_SHARD,
        sequence=cp.next_sequence,
        batch_id=batch_id,
        source_phase=phase,
        list_page_index=page_index,
        object_key_hash=hashlib.sha256(canonical(object_key)).hexdigest()
        if object_key is not None
        else None,
        first_source_event_id=envelope["event"]["id"],
        state="pending",
        first_received_at=now,
        last_seen_at=now,
    )
    accounting.receipt_budget(receipt, size)
    session.add(receipt)
    session.flush()
    first_delivery = EventDelivery(receipt_id=receipt.id, ordinal=1, state="pending")
    accounting.delivery_budget(first_delivery, size)
    session.add(first_delivery)
    cp.next_sequence += 1
    session.flush()
    return receipt


def receive_list_item(
    session: Session,
    context: SourceRunContext,
    ref: SyncRef,
    envelope: dict[str, Any],
    page_index: int,
    collection_rv: str,
) -> SyncAcceptResult:
    from dlr.control.services import subscription_delivery as delivery

    sub, activation, config, cp = _context(session, context)
    batch = _batch(session, sub.id, ref.batch_id)
    _same_ref(batch, context, ref)
    content_hash, size = _validate_source_envelope(envelope, sub, config)
    sync = envelope["sync"]
    if (
        sync
        != {
            "batch_id": str(batch.id),
            "scope_fingerprint": batch.scope_fingerprint,
            "phase": "list",
            "read_complete": False,
            "item_count": 0,
            "failure_code": None,
        }
        or envelope["source"]["attributes"]["watch_type"] != "LIST"
        or not isinstance(collection_rv, str)
        or not collection_rv
        or len(collection_rv.encode("utf-8")) > 4096
    ):
        raise domain_error(422, "subscription_sync_page_invalid", "Invalid LIST item")
    duplicate = _existing(session, sub, envelope, content_hash)
    if duplicate:
        if duplicate.batch_id != batch.id or duplicate.list_page_index != page_index:
            raise domain_error(
                409, "subscription_sync_page_invalid", "Item belongs to another page"
            )
        return SyncAcceptResult(duplicate.id, True, cp.next_sequence - 1)
    if (
        batch.read_complete
        or batch.failure_code
        or batch.completion_committed
        or batch.page_count != page_index
    ):
        raise domain_error(409, "subscription_sync_page_invalid", "LIST page is not current")
    if batch.resource_version is not None and batch.resource_version != collection_rv:
        raise domain_error(409, "subscription_sync_snapshot_changed", "Collection RV changed")
    if batch.item_count >= MAX_BATCH_ITEMS or batch.total_bytes + size > MAX_BATCH_BYTES:
        raise domain_error(429, "subscription_sync_batch_capacity", "Batch capacity reached")
    rows = delivery._quotas(session, sub.id)
    for quota, maximum in zip(
        rows, (delivery._limits(session, activation), delivery._global_limits(session)), strict=True
    ):
        if (
            quota.history_count + quota.reserved_control_count + 1 > maximum["history_count"]
            or quota.history_bytes
            + quota.reserved_control_bytes
            + accounting.responsibility_bytes(size)
            > maximum["history_bytes"]
            or quota.dedupe_count + quota.reserved_control_count + 1 > maximum["dedupe_count"]
        ):
            raise domain_error(
                429, "subscription_sync_history_capacity", "Protected LIST history is full"
            )
    delivery._reserve(session, sub, activation, accounting.responsibility_bytes(size))
    receipt = _insert_receipt(
        session, sub, activation, cp, envelope, content_hash, size, batch.id, "list", page_index
    )
    batch.resource_version = collection_rv
    batch.item_count += 1
    batch.total_bytes += size
    delivery.refresh_delivery_state(session, sub)
    return SyncAcceptResult(receipt.id, False, cp.next_sequence - 1)


def finish_list_page(
    session: Session,
    context: SourceRunContext,
    ref: SyncRef,
    page_index: int,
    collection_rv: str,
    receipt_ids: list[int],
    *,
    expected_item_count: int,
    last_page: bool,
) -> SyncStep:
    from dlr.control.services import subscription_delivery as delivery

    sub, _, _, cp = _context(session, context)
    batch = _batch(session, sub.id, ref.batch_id)
    _same_ref(batch, context, ref)
    if (
        isinstance(page_index, bool)
        or page_index < 0
        or isinstance(expected_item_count, bool)
        or not 0 <= expected_item_count <= 16
        or len(receipt_ids) != expected_item_count
        or len(set(receipt_ids)) != expected_item_count
        or not collection_rv
        or len(collection_rv.encode("utf-8")) > 4096
    ):
        raise domain_error(422, "subscription_sync_page_invalid", "Invalid page proof")
    actual = set(
        session.scalars(
            select(EventReceipt.id).where(
                EventReceipt.batch_id == batch.id,
                EventReceipt.source_phase == "list",
                EventReceipt.list_page_index == page_index,
            )
        )
    )
    if actual != set(receipt_ids) or (
        batch.resource_version is not None and batch.resource_version != collection_rv
    ):
        raise domain_error(409, "subscription_sync_page_invalid", "Incomplete page custody")
    if batch.page_count > page_index:
        return SyncStep()
    if batch.page_count != page_index or batch.read_complete or batch.failure_code:
        raise domain_error(409, "subscription_sync_page_invalid", "LIST page is not current")
    batch.resource_version = collection_rv
    batch.page_count += 1
    if last_page:
        batch.read_complete = True
        batch.state = "processing"
        cp.position = {"resource_version": collection_rv}
    delivery.refresh_delivery_state(session, sub)
    return SyncStep(changed=True)


def _has_execution(session: Session, receipt_id: int) -> bool:
    return bool(
        session.scalar(
            select(
                exists().where(
                    EventDelivery.receipt_id == receipt_id, EventDelivery.execution_id.is_not(None)
                )
            )
        )
    )


def receive_watch_event(
    session: Session, context: SourceRunContext, envelope: dict[str, Any]
) -> SyncAcceptResult:
    from dlr.control.services import subscription_delivery as delivery

    sub, activation, config, cp = _context(session, context)
    content_hash, size = _validate_source_envelope(envelope, sub, config)
    if (
        envelope["event"]["kind"].startswith("sync.")
        or envelope["source"]["attributes"]["watch_type"] == "LIST"
    ):
        raise domain_error(422, "subscription_sync_page_invalid", "Expected WATCH object")
    duplicate = _existing(session, sub, envelope, content_hash)
    if duplicate:
        return SyncAcceptResult(duplicate.id, True, cp.next_sequence - 1)
    current_batch = _latest_batch(session, sub.id)
    if current_batch is not None and not current_batch.read_complete and not _closed(current_batch):
        raise domain_error(
            409, "subscription_sync_page_invalid", "WATCH requires a complete snapshot"
        )
    sync = envelope["sync"]
    assert config.source_identity is not None
    batch = None
    if sync:
        try:
            batch = _batch(session, sub.id, int(sync["batch_id"]))
        except (TypeError, ValueError) as error:
            raise domain_error(
                422, "subscription_sync_page_invalid", "Invalid batch association"
            ) from error
        if (
            batch.source_epoch != sub.source_epoch
            or batch.scope_fingerprint != config.source_identity["scope_fingerprint"]
            or sync["scope_fingerprint"] != batch.scope_fingerprint
            or sync["phase"] != "watch"
        ):
            raise domain_error(409, "subscription_config_conflict", "WATCH batch differs")
    old = None
    object_hash = hashlib.sha256(canonical(envelope["event"]["object_key"])).hexdigest()
    if (
        config.config.get("mode") == "state_sync"
        and envelope["source"]["attributes"]["watch_type"] != "DELETED"
    ):
        old = session.scalar(
            select(EventReceipt)
            .where(
                EventReceipt.subscription_id == sub.id,
                EventReceipt.source_epoch == sub.source_epoch,
                EventReceipt.source_phase == "watch",
                EventReceipt.object_key_hash == object_hash,
                EventReceipt.state == "pending",
            )
            .order_by(EventReceipt.id.desc())
            .limit(1)
            .with_for_update()
        )
        if old and (
            old.envelope is None
            or old.envelope["source"]["attributes"]["uid"]
            != envelope["source"]["attributes"]["uid"]
            or old.envelope["source"]["attributes"]["watch_type"] == "DELETED"
            or _has_execution(session, old.id)
        ):
            old = None
    if old:
        for quota in delivery._quotas(session, sub.id):
            quota.pending_count -= 1
            quota.pending_bytes -= old.payload_bytes
    delivery._reserve(session, sub, activation, accounting.responsibility_bytes(size))
    receipt = _insert_receipt(
        session,
        sub,
        activation,
        cp,
        envelope,
        content_hash,
        size,
        batch.id if batch else None,
        "watch",
    )
    if old:
        old.state = "superseded"
        old.terminal_at = database_now(session)
        old.superseded_by_id = receipt.id
        previous = _latest_delivery(session, old.id)
        assert previous is not None
        previous.state = "superseded"
        previous.completed_at = old.terminal_at
        receipt.first_received_at = old.first_received_at
        receipt.coalesced_count = old.coalesced_count + 1
        receipt.first_source_event_id = old.first_source_event_id or old.source_event_id
    cp.position = dict(envelope["source"]["position"])
    if (
        cp.blocked_unaccepted
        and cp.blocked_unaccepted.get("code") == "subscription_capacity_exceeded"
    ):
        cp.blocked_unaccepted = None
    delivery.refresh_delivery_state(session, sub)
    return SyncAcceptResult(receipt.id, False, cp.next_sequence - 1)


def advance_bookmark(
    session: Session, context: SourceRunContext, resource_version: str, expected_safe_sequence: int
) -> bool:
    _, _, _, cp = _context(session, context)
    if not resource_version or len(resource_version.encode("utf-8")) > 4096:
        raise domain_error(422, "subscription_sync_page_invalid", "Invalid bookmark")
    if cp.next_sequence - 1 != expected_safe_sequence:
        return False
    cp.position = {"resource_version": resource_version}
    return True


def mark_sync_backpressure(session: Session, context: SourceRunContext) -> None:
    from dlr.control.services import subscription_delivery as delivery

    sub, activation, _, cp = _context(session, context)
    rows = delivery._quotas(session, sub.id)
    # A rate/ready-control wait does not by itself justify sticky capacity
    # hysteresis. Mark genuinely pressured ledgers; no new source position.
    for quota, limits in zip(
        rows, (delivery._limits(session, activation), delivery._global_limits(session)), strict=True
    ):
        if any(
            getattr(quota, key)
            + (
                quota.reserved_control_bytes
                if key.endswith("bytes")
                else quota.reserved_control_count
            )
            >= limits[key]
            for key in (
                "pending_count",
                "pending_bytes",
                "history_count",
                "history_bytes",
                "dedupe_count",
            )
        ):
            quota.blocked_reason = "subscription_capacity_exceeded"
    cp.blocked_unaccepted = {
        "code": "subscription_capacity_exceeded",
        "position": dict(cp.position),
    }


def _release_reserve(session: Session, sub: EventSubscription, batch: SyncBatch) -> None:
    from dlr.control.services import subscription_delivery as delivery

    for quota in delivery._quotas(session, sub.id):
        quota.reserved_control_count -= batch.reserved_control_count
        quota.reserved_control_bytes -= batch.reserved_control_bytes
    batch.reserved_control_count = 0
    batch.reserved_control_bytes = 0


def _recompute_blocker(session: Session, sub: EventSubscription, cp: SourceCheckpoint) -> None:
    # A closing control can temporarily own this pointer. Restore ordinary
    # failed/active responsibility after it resolves, rather than erasing it.
    session.flush()
    cp.blocked_receipt_id = session.scalar(
        select(EventReceipt.id)
        .where(
            EventReceipt.subscription_id == sub.id,
            EventReceipt.source_epoch == cp.source_epoch,
            EventReceipt.shard == cp.shard,
            EventReceipt.state.in_(["processing", "failed"]),
        )
        .order_by(
            case(
                (
                    EventReceipt.source_phase.in_(["sync.completed", "sync.failed"])
                    & (EventReceipt.state == "failed"),
                    0,
                ),
                (EventReceipt.state == "processing", 1),
                else_=2,
            ),
            EventReceipt.sequence,
        )
        .limit(1)
    )


def _invalidate(session: Session, sub: EventSubscription, batch: SyncBatch, code: str) -> None:
    from dlr.control.services import subscription_delivery as delivery

    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
        raise domain_error(422, "subscription_sync_gap_invalid", "Invalid failure code")
    now = database_now(session)
    if batch.completion_committed:
        if batch.pending_gap is None:
            batch.pending_gap = {
                "code": code,
                "detected_at": now.isoformat(),
                "after_completion_committed": True,
                "relist_required": True,
            }
        return
    if batch.failure_code is not None:
        return
    batch.failure_code = code
    batch.state = "failed"
    batch.pending_gap = {
        "code": code,
        "detected_at": now.isoformat(),
        "after_completion_committed": False,
        "relist_required": True,
    }
    # A materialized but unfrozen completion loses permission atomically.
    if batch.completed_receipt_id:
        receipt = session.get(EventReceipt, batch.completed_receipt_id, with_for_update=True)
        latest = _latest_delivery(session, receipt.id) if receipt else None
        if receipt and latest and latest.execution_id is None and latest.state == "pending":
            receipt.state = latest.state = "superseded"
            receipt.terminal_at = latest.completed_at = now
            for quota in delivery._quotas(session, sub.id):
                quota.pending_count -= 1
                quota.pending_bytes -= receipt.payload_bytes
    if batch.started_receipt_id:
        started = session.get(EventReceipt, batch.started_receipt_id, with_for_update=True)
        latest_started = _latest_delivery(session, started.id) if started else None
        if (
            started
            and latest_started
            and latest_started.execution_id is None
            and latest_started.state == "pending"
        ):
            started.state = latest_started.state = "superseded"
            started.terminal_at = latest_started.completed_at = now
            for quota in delivery._quotas(session, sub.id):
                quota.pending_count -= 1
                quota.pending_bytes -= started.payload_bytes
    if batch.failed_receipt_id is None:
        batch.ready_kind = "sync.failed"
        batch.ready_since = now


def note_sync_gap(session: Session, context: SourceRunContext, code: str) -> SyncStep:
    sub, _, _, _ = _context(session, context)
    batch = _latest_batch(session, sub.id)
    if batch is None:
        raise domain_error(409, "subscription_sync_batch_not_found", "No batch to invalidate")
    batch = _batch(session, sub.id, batch.id)
    _invalidate(session, sub, batch, code)
    return SyncStep(changed=True)


def _control_success(session: Session, receipt_id: int | None) -> bool:
    if receipt_id is None:
        return False
    delivery = _latest_delivery(session, receipt_id)
    if delivery is None or delivery.execution_id is None:
        return False
    execution = session.get(Execution, delivery.execution_id)
    return (
        delivery.state == "succeeded" and execution is not None and execution.status == "succeeded"
    )


def _members_succeeded(session: Session, batch: SyncBatch) -> bool:
    # Receipt.state is the projection of its latest Delivery. Observation is
    # required; an actual succeeded Execution alone must not bypass projection.
    return not bool(
        session.scalar(
            select(
                exists().where(
                    EventReceipt.batch_id == batch.id,
                    EventReceipt.source_phase == "list",
                    EventReceipt.state != "succeeded",
                )
            )
        )
    )


def maintain_batch(session: Session, batch_id: int) -> SyncStep:
    from dlr.control.services import subscription_delivery as delivery

    sub_id = session.scalar(select(SyncBatch.subscription_id).where(SyncBatch.id == batch_id))
    if sub_id is None:
        return SyncStep()
    _, sub = delivery.lock_subscription(session, sub_id)
    lease = session.get(SubscriptionLease, sub.id, with_for_update=True)
    cp = session.scalar(
        select(SourceCheckpoint)
        .where(
            SourceCheckpoint.subscription_id == sub.id,
            SourceCheckpoint.shard == KUBERNETES_SHARD,
            SourceCheckpoint.source_epoch == sub.source_epoch,
        )
        .with_for_update()
    )
    batch = _batch(session, sub.id, batch_id)
    if batch.activation_id is None or _closed(batch):
        return SyncStep(closed=_closed(batch))
    now = database_now(session)
    if (
        not batch.read_complete
        and batch.failure_code is None
        and (
            not settings.event_subscriptions_enabled
            or sub.desired_state != "enabled"
            or lease is None
            or lease.lease_until <= now
            or lease.owner_id != batch.owner_id
            or lease.generation != batch.owner_generation
        )
    ):
        _invalidate(session, sub, batch, "source_list_interrupted")
    if batch.failure_code:
        failed = (
            session.get(EventReceipt, batch.failed_receipt_id) if batch.failed_receipt_id else None
        )
        if failed and (failed.state == "skipped" or _control_success(session, failed.id)):
            batch.drain_open = True
            batch.ready_kind = batch.ready_since = None
            batch.finished_at = now
            _release_reserve(session, sub, batch)
    elif _control_success(session, batch.completed_receipt_id):
        batch.drain_open = True
        batch.state = "completed"
        batch.ready_kind = batch.ready_since = None
        batch.finished_at = now
        _release_reserve(session, sub, batch)
    elif (
        batch.read_complete
        and not batch.completion_committed
        and batch.completed_receipt_id is None
        and _control_success(session, batch.started_receipt_id)
        and _members_succeeded(session, batch)
    ):
        if batch.ready_kind is None:
            batch.ready_kind = "sync.completed"
            batch.ready_since = now
    if cp:
        _recompute_blocker(session, sub, cp)
    delivery.refresh_delivery_state(session, sub)
    return SyncStep(changed=True, closed=_closed(batch))


def _rate_available(session: Session, rows: list[Any], limits: dict[str, int]) -> bool:
    from dlr.control.services.subscription_delivery import _global_limits

    now = database_now(session)
    for row, maximum in zip(rows, (limits, _global_limits(session)), strict=True):
        if row.rate_window_at is None or now - row.rate_window_at >= timedelta(seconds=1):
            row.rate_window_at = now
            row.rate_count = 0
        if row.rate_count >= maximum["rate"]:
            return False
    return True


def materialize_ready_control(session: Session, ticket: ControlTicket) -> SyncStep:
    from dlr.control.services import subscription_delivery as delivery

    sub_id = session.scalar(
        select(SyncBatch.subscription_id).where(SyncBatch.id == ticket.batch_id)
    )
    if sub_id is None:
        return SyncStep()
    _, sub = delivery.lock_subscription(session, sub_id)
    cp = session.scalar(
        select(SourceCheckpoint)
        .where(
            SourceCheckpoint.subscription_id == sub.id,
            SourceCheckpoint.shard == KUBERNETES_SHARD,
            SourceCheckpoint.source_epoch
            == session.scalar(
                select(SyncBatch.source_epoch).where(SyncBatch.id == ticket.batch_id)
            ),
        )
        .with_for_update()
    )
    batch = _batch(session, sub.id, ticket.batch_id)
    if batch.ready_kind != ticket.kind or batch.ready_since != ticket.ready_since or _closed(batch):
        return SyncStep()
    # Do not hold the global quota and then seek another Adapter. This is a
    # read-only oldest-intent check under the existing Adapter/Sub lock.
    oldest = session.scalar(
        select(SyncBatch.id)
        .where(SyncBatch.ready_kind.is_not(None))
        .order_by(SyncBatch.ready_since, SyncBatch.id)
        .limit(1)
    )
    if oldest != batch.id:
        return SyncStep(deferred_code="subscription_sync_control_wait")
    assert cp is not None and batch.activation_id is not None
    activation = session.get(SubscriptionActivation, batch.activation_id)
    assert activation is not None
    config = session.get(SubscriptionConfigVersion, activation.config_version_id)
    assert config is not None and config.source_identity is not None
    kind = batch.ready_kind
    if kind == "sync.completed" and (
        batch.failure_code
        or not batch.read_complete
        or not _control_success(session, batch.started_receipt_id)
        or not _members_succeeded(session, batch)
    ):
        batch.ready_kind = batch.ready_since = None
        return SyncStep(changed=True)
    if kind == "sync.failed" and batch.completion_committed:
        batch.ready_kind = batch.ready_since = None
        return SyncStep(changed=True)
    rows = delivery._quotas(session, sub.id)
    if not _rate_available(session, rows, delivery._limits(session, activation)):
        return SyncStep(deferred_code="subscription_capacity_exceeded")
    now = database_now(session)
    cfg = config.config
    envelope: dict[str, Any] = {
        "schema": "dlr.event.v1",
        "subscription": {
            "id": str(sub.id),
            "config_version": config.version,
            "source_epoch": batch.source_epoch,
        },
        "event": {"id": "", "kind": kind, "object_key": None},
        "source": {
            "type": "kubernetes",
            "identity": "kubernetes-scope-v1:" + batch.scope_fingerprint,
            "position": {"resource_version": batch.resource_version},
            "attributes": {
                "cluster_id": cfg["cluster_id"],
                "group": cfg.get("group", ""),
                "version": cfg["version"],
                "resource": cfg["resource"],
                "namespace": cfg.get("namespace"),
                "name": None,
                "uid": None,
                "watch_type": None,
            },
        },
        "received_at": now.isoformat(),
        "data": {"encoding": "json", "content_type": "application/json", "value": None},
        "sync": {
            "batch_id": str(batch.id),
            "scope_fingerprint": batch.scope_fingerprint,
            "phase": kind.removeprefix("sync."),
            "read_complete": batch.read_complete,
            "item_count": batch.item_count,
            "failure_code": batch.failure_code,
        },
    }
    envelope["event"]["id"] = event_id(envelope)
    content_hash, size = validate_envelope(envelope)
    if (
        size > CONTROL_ENVELOPE_BYTES
        or batch.reserved_control_count < 1
        or batch.reserved_control_bytes < CONTROL_BYTES
    ):
        raise domain_error(409, "subscription_sync_reserve_invalid", "Control reserve unavailable")
    for quota in rows:
        quota.reserved_control_count -= 1
        quota.reserved_control_bytes -= CONTROL_BYTES
        quota.pending_count += 1
        quota.pending_bytes += accounting.responsibility_bytes(size)
        quota.history_count += 1
        quota.history_bytes += accounting.responsibility_bytes(size)
        quota.dedupe_count += 1
        quota.rate_count += 1
    batch.reserved_control_count -= 1
    batch.reserved_control_bytes -= CONTROL_BYTES
    receipt = _insert_receipt(
        session, sub, activation, cp, envelope, content_hash, size, batch.id, kind
    )
    setattr(
        batch,
        {
            "sync.started": "started_receipt_id",
            "sync.completed": "completed_receipt_id",
            "sync.failed": "failed_receipt_id",
        }[kind],
        receipt.id,
    )
    batch.ready_kind = batch.ready_since = None
    delivery.refresh_delivery_state(session, sub)
    return SyncStep(changed=True)


def delivery_eligible(
    session: Session, sub: EventSubscription, cp: SourceCheckpoint, receipt: EventReceipt
) -> bool:
    """Called under Adapter/Sub/CP/Batch locks before T2 Admission."""
    if receipt.source_phase is None:
        return True
    batch = _batch(session, sub.id, receipt.batch_id) if receipt.batch_id else None
    phase = receipt.source_phase
    if (
        batch
        and phase == "sync.completed"
        and (
            batch.completion_abandoned
            or (batch.state == "completed" and _closed(batch))
            or batch.failure_code
            or not batch.read_complete
            or not _control_success(session, batch.started_receipt_id)
            or not _members_succeeded(session, batch)
        )
    ):
        return False
    # Closing controls may cross earlier ordinary failures, never an active
    # queued/running/retry_wait responsibility (Admission also guards Slot).
    if phase in {"sync.completed", "sync.failed"}:
        return not bool(
            session.scalar(
                select(
                    exists().where(
                        EventReceipt.subscription_id == sub.id,
                        EventReceipt.id != receipt.id,
                        EventReceipt.state == "processing",
                    )
                )
            )
        )
    if (
        batch
        and phase == "list"
        and not batch.drain_open
        and not _control_success(session, batch.started_receipt_id)
    ):
        return False
    if phase == "watch":
        if batch and not batch.drain_open:
            return False
        # Independent WATCH is still behind the latest open LIST protocol.
        latest = _latest_batch(session, sub.id)
        if latest and not latest.drain_open and not _closed(latest):
            return False
    return True


def freeze_completion(session: Session, receipt: EventReceipt, execution_id: int) -> None:
    if receipt.source_phase != "sync.completed" or receipt.batch_id is None:
        return
    batch = _batch(session, receipt.subscription_id, receipt.batch_id)
    if batch.failure_code or batch.completion_abandoned:
        raise domain_error(
            409, "subscription_sync_completion_conflict", "Completion lost eligibility"
        )
    batch.completion_committed = True
    batch.completed_execution_id = execution_id


def disposition_guard(session: Session, receipt: EventReceipt, *, skip: bool = False) -> None:
    if receipt.batch_id is None:
        return
    batch = _batch(session, receipt.subscription_id, receipt.batch_id)
    if receipt.source_phase == "sync.completed" and (
        batch.completion_abandoned
        or batch.failure_code is not None
        or (batch.state == "completed" and _closed(batch))
        or (skip and batch.completion_committed)
    ):
        raise domain_error(
            409,
            "subscription_sync_completion_conflict",
            "Completion requires explicit settlement or a new batch",
        )


def after_skip(
    session: Session, sub: EventSubscription, receipt: EventReceipt, cp: SourceCheckpoint
) -> None:
    if receipt.batch_id:
        batch = _batch(session, sub.id, receipt.batch_id)
        if receipt.source_phase in {"list", "sync.started"}:
            _invalidate(session, sub, batch, "source_member_skipped")
        elif receipt.source_phase == "sync.completed":
            _invalidate(session, sub, batch, "source_completion_skipped")
        elif receipt.source_phase == "sync.failed":
            batch.drain_open = True
            batch.finished_at = database_now(session)
            _release_reserve(session, sub, batch)
    _recompute_blocker(session, sub, cp)


def abandon_completion(
    session: Session,
    sub_id: int,
    *,
    batch_id: int,
    source_epoch: str,
    receipt_id: int,
    expected_config_version: int,
    expected_latest_delivery_id: int,
    expected_delivery_ordinal: int,
    expected_execution_id: int | None,
    request_id: str,
    actor_id: int | None,
    reason: str,
    confirm_impact: bool,
) -> CompletionAbandonmentResult:
    from dlr.control.services import subscription_delivery as delivery

    _, sub = delivery.lock_subscription(session, sub_id)
    binding = {
        "batch_id": batch_id,
        "source_epoch": source_epoch,
        "receipt_id": receipt_id,
        "expected_config_version": expected_config_version,
        "expected_latest_delivery_id": expected_latest_delivery_id,
        "expected_delivery_ordinal": expected_delivery_ordinal,
        "expected_execution_id": expected_execution_id,
        "confirm_impact": confirm_impact,
    }
    old = session.scalar(
        select(DeliveryDisposition).where(
            DeliveryDisposition.subscription_id == sub.id,
            DeliveryDisposition.request_id == request_id,
        )
    )
    if old is not None:
        if (
            old.action != "abandon_completion"
            or old.actor_id != actor_id
            or old.reason != reason
            or old.details.get("binding") != binding
        ):
            raise domain_error(409, "event_disposition_conflict", "Disposition request differs")
        return _abandon_result(old)
    if (
        not request_id
        or len(request_id) > 128
        or not reason.strip()
        or len(reason.encode("utf-8")) > 4096
        or confirm_impact is not True
    ):
        raise domain_error(
            422, "event_disposition_invalid", "Explicit bounded confirmation required"
        )
    if sub.config_version != expected_config_version or sub.source_epoch != source_epoch:
        raise domain_error(
            409, "subscription_config_conflict", "Subscription configuration changed"
        )
    lease = session.get(SubscriptionLease, sub.id, with_for_update=True)
    cp = session.scalar(
        select(SourceCheckpoint)
        .where(
            SourceCheckpoint.subscription_id == sub.id,
            SourceCheckpoint.source_epoch == source_epoch,
            SourceCheckpoint.shard == KUBERNETES_SHARD,
        )
        .with_for_update()
    )
    batch = _batch(session, sub.id, batch_id)
    if (
        sub.desired_state != "disabled"
        or sub.connection_state != "stopped"
        or (lease is not None and (lease.closing_generation is not None or bool(lease.owner_id)))
    ):
        raise domain_error(409, "subscription_listener_not_closed", "Stop and close listener first")
    receipt = session.get(EventReceipt, receipt_id, with_for_update=True, populate_existing=True)
    latest = _latest_delivery(session, receipt_id)
    if (
        batch.source_epoch != source_epoch
        or batch.completed_receipt_id != receipt_id
        or not batch.completion_committed
        or batch.completion_abandoned
        or receipt is None
        or receipt.batch_id != batch.id
        or receipt.source_phase != "sync.completed"
        or latest is None
        or latest.id != expected_latest_delivery_id
        or latest.ordinal != expected_delivery_ordinal
        or latest.execution_id != expected_execution_id
    ):
        raise domain_error(
            409, "subscription_sync_completion_conflict", "Completion responsibility changed"
        )
    # Every associated Execution, including an old attempt not currently in the
    # latest projection, must be terminal. Adapter lock serializes T2 creation.
    active = session.scalar(
        select(Execution.id)
        .join(EventDelivery, EventDelivery.execution_id == Execution.id)
        .join(EventReceipt, EventReceipt.id == EventDelivery.receipt_id)
        .where(
            EventReceipt.subscription_id == sub.id,
            Execution.status.not_in(delivery.EXECUTION_TERMINAL),
        )
        .limit(1)
        .with_for_update(of=Execution)
    )
    execution = (
        session.get(Execution, latest.execution_id, with_for_update=True)
        if latest.execution_id
        else None
    )
    if active is not None or (execution is not None and execution.status == "succeeded"):
        raise domain_error(
            409, "subscription_sync_completion_conflict", "Completion is active or successful"
        )
    if latest.state == "processing" or (latest.execution_id is None and latest.state != "pending"):
        raise domain_error(
            409, "subscription_sync_completion_conflict", "Completion cannot be settled"
        )
    details = {
        "binding": binding,
        "expected_execution_id": expected_execution_id,
        "cancelled_pending_id": latest.id if latest.execution_id is None else None,
        "completion_committed": True,
        "business_result": "unconfirmed",
    }
    size = len(
        canonical(
            {"request_id": request_id, "actor_id": actor_id, "reason": reason, "details": details}
        )
    )
    if (
        size > CONTROL_ENVELOPE_BYTES
        or batch.reserved_control_count < 1
        or batch.reserved_control_bytes < CONTROL_BYTES
    ):
        raise domain_error(
            409, "subscription_sync_reserve_invalid", "Prepaid settlement reserve unavailable"
        )
    rows = delivery._quotas(session, sub.id)
    now = database_now(session)
    if latest.execution_id is None:
        latest.state = "abandoned"
        latest.completed_at = now
        for quota in rows:
            quota.pending_count -= 1
            quota.pending_bytes -= receipt.payload_bytes
    # Keep the original failed Execution/Delivery unchanged. Only Receipt's
    # protocol resolution and the unexecuted latest replay can be abandoned.
    receipt.state = "abandoned"
    receipt.terminal_at = now
    disposition = DeliveryDisposition(
        subscription_id=sub.id,
        request_id=request_id,
        action="abandon_completion",
        actor_id=actor_id,
        reason=reason,
        receipt_id=receipt.id,
        delivery_id=latest.id,
        batch_id=batch.id,
        audit_bytes=size,
        details=details,
    )
    session.add(disposition)
    for quota in rows:
        quota.reserved_control_count -= 1
        quota.reserved_control_bytes -= CONTROL_BYTES
        quota.history_count += 1
        quota.history_bytes += size
        quota.dedupe_count += 1
    batch.reserved_control_count -= 1
    batch.reserved_control_bytes -= CONTROL_BYTES
    batch.completion_abandoned = True
    batch.drain_open = True
    batch.state = "abandoned"
    batch.ready_kind = batch.ready_since = None
    batch.finished_at = now
    _release_reserve(session, sub, batch)
    if cp:
        _recompute_blocker(session, sub, cp)
    session.flush()
    delivery.refresh_delivery_state(session, sub)
    return _abandon_result(disposition)
