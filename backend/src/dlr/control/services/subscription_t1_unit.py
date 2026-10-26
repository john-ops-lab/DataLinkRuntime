"""Bounded opt-in custody; one transaction, independent durable identities.

The caller authenticates HTTP items under this unit's locked scope. The unit
never commits, waits for more input or talks to a source. A failed database
operation must roll back the caller's whole transaction, not just one item.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import exists, func, insert, select
from sqlalchemy.orm import Session, defer

from dlr.common.config import settings
from dlr.control.models import (
    EventDelivery,
    EventReceipt,
    Execution,
    SourceCheckpoint,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionLease,
    SubscriptionQuota,
    SyncBatch,
)
from dlr.control.services import subscription_accounting as accounting
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services.adapter import domain_error
from dlr.control.services.event_envelope import validate_envelope
from dlr.control.services.ingress_capacity import (
    CapacityProfile,
    require_active_profile,
    require_disk_capacity,
    runtime_limits_for_profile,
)
from dlr.control.services.input_config import database_now
from dlr.control.services.subscription_activation_policy import ensure_policy
from dlr.control.services.subscription_batch_view import delivery_records
from dlr.control.services.subscription_registry import source_definition

MAX_ITEMS = 16
HTTP_MAX_ITEMS = 4
MAX_PAYLOAD_BYTES = 1024**2
SourceType = Literal["http", "rabbitmq"]


@dataclass(frozen=True)
class T1Input:
    index: int
    identity: str
    envelope: dict[str, Any] | None = field(repr=False)
    position: dict[str, Any]
    raw_body: bytes | None = field(default=None, repr=False)
    error: HTTPException | None = None
    raw_size: int | None = None


@dataclass(frozen=True)
class T1Outcome:
    index: int
    status_code: int
    receipt_id: int | None
    duplicate: bool
    status: dict[str, Any] | None
    error: HTTPException | None = None


@dataclass(frozen=True)
class T1Result:
    outcomes: tuple[T1Outcome, ...]
    accepted_prefix_count: int
    stopped_index: int | None


@dataclass
class _Quota:
    pending_count: int
    pending_bytes: int
    history_count: int
    history_bytes: int
    dedupe_count: int
    reserved_control_count: int
    reserved_control_bytes: int
    blocked_reason: str | None
    rate_count: int
    rate_window_at: datetime | None

    @classmethod
    def read(cls, row: SubscriptionQuota) -> _Quota:
        return cls(**{key: getattr(row, key) for key in cls.__dataclass_fields__})

    def apply(self, row: SubscriptionQuota) -> None:
        for key in self.__dataclass_fields__:
            setattr(row, key, getattr(self, key))


@dataclass
class _Prepared:
    item: T1Input
    content_hash: str | None
    envelope_bytes: int
    raw_bytes: int
    error: HTTPException | None


@dataclass
class _New:
    item: T1Input
    receipt: EventReceipt
    budget: accounting.BatchBudget
    envelope_bytes: int


class T1Unit:
    def __init__(
        self,
        session: Session,
        subscription_id: int,
        *,
        source_type: SourceType,
        source_epoch: str,
        shard: str,
        owner_id: str | None = None,
        generation: int | None = None,
        expected_activation_id: int | None = None,
        expected_config_version: int | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ):
        if source_type not in {"http", "rabbitmq"}:
            raise domain_error(409, "subscription_config_conflict", "Unsupported T1 unit source")
        self.session = session
        self.adapter, self.subscription = delivery.lock_subscription(session, subscription_id)
        self.source_type, self.source_epoch, self.shard = source_type, source_epoch, shard
        self.owner_id, self.generation = owner_id, generation
        self.expected_activation_id = expected_activation_id
        self.expected_config_version = expected_config_version
        self.stop_requested = stop_requested
        self._transaction = session.get_transaction()
        self._binding = self._scope_binding()
        self._used = False
        self._lease_row: SubscriptionLease | None = None
        self._lease_loaded = False
        self._lease_identity: tuple[str | None, int] | None = None
        self._checkpoint: SourceCheckpoint | None = None
        self._checkpoint_loaded = False
        self._shard_checked = False
        self._activation: SubscriptionActivation | None = None
        self._config: SubscriptionConfigVersion | None = None
        self._profile: CapacityProfile | None = None
        self._quota_rows: list[SubscriptionQuota] = []
        self._quota_values: list[_Quota] = []
        self._maxima: tuple[dict[str, int], dict[str, int]] | None = None
        self._oldest: datetime | None = None
        self._new: dict[str, _New] = {}
        self._existing: dict[str, EventReceipt] = {}
        self._policy_verified = False

    def _scope_binding(self) -> tuple[Any, ...]:
        sub = self.subscription
        return (
            sub.id,
            sub.adapter_id,
            sub.source_type,
            sub.source_epoch,
            sub.activation_id,
            sub.config_version,
            sub.deleted_at,
        )

    def _active_transaction(self) -> None:
        if (
            self._transaction is None
            or not self._transaction.is_active
            or self.session.get_transaction() is not self._transaction
        ):
            raise domain_error(409, "subscription_owner_stale", "T1 unit transaction has ended")
        if self._scope_binding() != self._binding:
            raise domain_error(409, "subscription_config_conflict", "T1 scope binding changed")
        if self.stop_requested is not None and self.stop_requested():
            raise domain_error(409, "subscription_owner_stale", "Receiving was stopped")

    def _prepare(self, item: T1Input) -> _Prepared:
        raw_size = len(item.raw_body) if item.raw_body is not None else (item.raw_size or 0)
        if type(raw_size) is not int or raw_size < 0:
            raise domain_error(422, "t1_unit_bounds_exceeded", "Invalid reception byte count")
        if self.source_type == "http":
            digest = (
                hashlib.sha256(item.raw_body).hexdigest() if item.raw_body is not None else None
            )
            size = accounting.compact_bytes(item.envelope) if item.envelope is not None else 0
            return _Prepared(item, digest, size, raw_size, item.error)
        if item.error is not None:
            return _Prepared(item, None, 0, raw_size, item.error)
        try:
            if item.envelope is None:
                raise domain_error(422, "event_content_invalid", "Event envelope is missing")
            digest, size = validate_envelope(item.envelope)
            value = item.envelope["data"]["value"]
            decoded_bytes = len(value) // 4 * 3 - len(value) + len(value.rstrip("="))
            if item.raw_size is not None and item.raw_size != decoded_bytes:
                raise domain_error(422, "event_content_invalid", "Original byte count differs")
            return _Prepared(item, digest, size, decoded_bytes, None)
        except HTTPException as error:
            return _Prepared(item, None, 0, raw_size, error)

    def _load_lease(self) -> SubscriptionLease | None:
        if not self._lease_loaded:
            self._lease_row = self.session.get(
                SubscriptionLease,
                self.subscription.id,
                with_for_update=True,
                populate_existing=True,
            )
            self._lease_loaded = True
            if self._lease_row is not None:
                self._lease_identity = (self._lease_row.owner_id, self._lease_row.generation)
        return self._lease_row

    def _owner(self, now: datetime) -> None:
        self._active_transaction()
        sub, lease = self.subscription, self._load_lease()
        expected_owner = (
            self.owner_id if self.owner_id is not None else (self._lease_identity or (None, 0))[0]
        )
        expected_generation = (
            self.generation
            if self.generation is not None
            else (self._lease_identity or (None, 0))[1]
        )
        if (
            lease is None
            or lease.owner_id != expected_owner
            or not lease.owner_id
            or lease.generation != expected_generation
            or lease.lease_until <= now
            or sub.desired_state != "enabled"
            or sub.deleted_at is not None
        ):
            raise domain_error(409, "subscription_owner_stale", "Listener ownership expired")
        if self.source_type == "http" and (
            lease.closing_generation is not None or sub.connection_state != "listening"
        ):
            raise domain_error(503, "ingress_unavailable", "Reception owner is not ready")

    def _load_checkpoint(self) -> SourceCheckpoint | None:
        if not self._checkpoint_loaded:
            self._checkpoint = self.session.scalar(
                select(SourceCheckpoint)
                .where(
                    SourceCheckpoint.subscription_id == self.subscription.id,
                    SourceCheckpoint.source_epoch == self.source_epoch,
                    SourceCheckpoint.shard == self.shard,
                )
                .with_for_update()
            )
            self._checkpoint_loaded = True
        return self._checkpoint

    def _new_context(self) -> None:
        sub = self.subscription
        if self.source_epoch != sub.source_epoch:
            raise domain_error(
                410 if self.source_type == "http" else 409,
                "ingress_epoch_retired"
                if self.source_type == "http"
                else "subscription_config_conflict",
                "New reception requires the current source epoch",
            )
        if not settings.event_subscriptions_enabled:
            raise domain_error(409, "source_not_available", "Event subscriptions are disabled")
        if self.source_type == "http":
            if not settings.http_ingress_enabled:
                raise domain_error(503, "ingress_unavailable", "HTTP reception is disabled")
            if sub.desired_state != "enabled":
                raise domain_error(409, "ingress_disabled", "Receiving is stopped")
        if self._activation is None:
            activation = (
                self.session.get(SubscriptionActivation, sub.activation_id)
                if sub.activation_id
                else None
            )
            if activation is None or activation.subscription_id != sub.id:
                raise domain_error(
                    409, "subscription_activation_missing", "Activation is unavailable"
                )
            config = self.session.get(SubscriptionConfigVersion, activation.config_version_id)
            if (
                config is None
                or config.config.get("source_type") != self.source_type
                or config.config.get("mode") != "business_batch"
                or (
                    self.expected_activation_id is not None
                    and activation.id != self.expected_activation_id
                )
                or (
                    self.expected_config_version is not None
                    and config.version != self.expected_config_version
                )
            ):
                raise domain_error(
                    409, "subscription_config_conflict", "T1 unit requires its exact batch binding"
                )
            self._activation, self._config = activation, config
        checkpoint = self._load_checkpoint()
        if (
            checkpoint is not None
            and checkpoint.blocked_unaccepted is not None
            and checkpoint.blocked_unaccepted["code"] != "subscription_capacity_exceeded"
        ):
            raise domain_error(409, "source_shard_blocked", "Source fault requires disposition")
        if checkpoint is None and not self._shard_checked:
            count = (
                self.session.scalar(
                    select(func.count())
                    .select_from(SourceCheckpoint)
                    .where(
                        SourceCheckpoint.subscription_id == sub.id,
                        SourceCheckpoint.source_epoch == sub.source_epoch,
                    )
                )
                or 0
            )
            if count >= source_definition(self.source_type).max_shards:
                raise domain_error(
                    429, "subscription_capacity_exceeded", "Source shard budget reached"
                )
            self._shard_checked = True

    def _load_capacity(self) -> None:
        if self._profile is not None:
            return
        assert self._config is not None
        self._quota_rows = delivery._quotas(self.session, self.subscription.id)
        self._profile = require_active_profile(self.session)
        self._quota_values = [_Quota.read(row) for row in self._quota_rows]
        self._maxima = (
            runtime_limits_for_profile(self._profile, self._config.config),
            dict(self._profile.global_limits),
        )
        self._oldest = self.session.scalar(
            select(func.min(EventReceipt.first_received_at)).where(
                EventReceipt.subscription_id == self.subscription.id,
                EventReceipt.state == "pending",
            )
        )

    def _reserve(self, size: int, now: datetime, ready: bool) -> list[_Quota]:
        if ready:
            raise domain_error(
                429, "subscription_capacity_exceeded", "Ready control owns the next token"
            )
        self._load_capacity()
        require_disk_capacity()
        assert self._maxima is not None
        oldest = self._oldest
        if oldest is not None and now - oldest >= timedelta(minutes=30):
            raise domain_error(
                429, "subscription_capacity_exceeded", "Oldest pending exceeded limit"
            )
        values = [replace(value) for value in self._quota_values]
        for value, maximum in zip(values, self._maxima, strict=True):
            if value.blocked_reason is not None:
                low = all(
                    getattr(value, key)
                    + (
                        value.reserved_control_bytes
                        if key.endswith("bytes")
                        else value.reserved_control_count
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
                if not low or (oldest is not None and now - oldest >= timedelta(minutes=5)):
                    raise domain_error(
                        429, "subscription_capacity_exceeded", "Waiting for low water"
                    )
                value.blocked_reason = None
            for key, delta in (
                ("pending_count", 1),
                ("pending_bytes", size),
                ("history_count", 1),
                ("history_bytes", size),
                ("dedupe_count", 1),
            ):
                reserved = (
                    value.reserved_control_bytes
                    if key.endswith("bytes")
                    else value.reserved_control_count
                )
                if getattr(value, key) + delta + reserved > maximum[key]:
                    raise domain_error(
                        429, "subscription_capacity_exceeded", f"Capacity {key} reached"
                    )
            if value.rate_window_at is None or now - value.rate_window_at >= timedelta(seconds=1):
                value.rate_window_at, value.rate_count = now, 0
            if value.rate_count >= maximum["rate"]:
                raise domain_error(429, "subscription_capacity_exceeded", "Receipt rate reached")
        for value in values:
            value.pending_count += 1
            value.pending_bytes += size
            value.history_count += 1
            value.history_bytes += size
            value.dedupe_count += 1
            value.rate_count += 1
        return values

    def _clock(self) -> tuple[datetime, bool]:
        now, ready = self.session.execute(
            select(func.clock_timestamp(), exists().where(SyncBatch.ready_kind.is_not(None)))
        ).one()
        return now, ready

    def _validate_new(self, prepared: _Prepared) -> tuple[accounting.BatchBudget, datetime, bool]:
        self._new_context()
        self._load_lease()
        if prepared.error is not None:
            self._owner(database_now(self.session))
            raise prepared.error
        item = prepared.item
        if item.envelope is None:
            raise domain_error(422, "event_content_invalid", "Event envelope is missing")
        if self.source_type == "http":
            if item.raw_body is None or prepared.raw_bytes > 64 * 1024:
                raise domain_error(422, "event_payload_invalid", "Invalid HTTP original body")
            validate_envelope(item.envelope)
        assert self._config is not None and self._activation is not None
        envelope = item.envelope
        if (
            envelope["event"]["id"] != item.identity
            or envelope["source"]["type"] != self.source_type
            or envelope["subscription"]
            != {
                "id": str(self.subscription.id),
                "source_epoch": self.source_epoch,
                "config_version": self._config.version,
            }
        ):
            raise domain_error(
                409, "subscription_config_conflict", "Event activation metadata differs"
            )
        if not self._policy_verified:
            ensure_policy(self.session, self._activation, self.adapter)
            self._policy_verified = True
        budget = accounting.batch_budget(
            envelope, prepared.raw_bytes if self.source_type == "http" else 0
        )
        # Read time only after the potentially contended global quota lock.
        # A group never carries a pre-lock rate/lease timestamp past that wait.
        self._load_capacity()
        now, ready = self._clock()
        self._owner(now)
        return budget, now, ready

    def _existing_status(self, receipt: EventReceipt) -> dict[str, Any]:
        latest = delivery_records(self.session, receipt, latest_only=True)
        item = latest[0] if latest else {}
        execution_id = item.get("execution_id")
        execution = self.session.get(Execution, execution_id) if execution_id else None
        code = (execution.error_code or execution.last_error_code) if execution else None
        if item.get("processing_mode") == "business_batch":
            code = item.get("batch_member_code")
        if code is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            code = "execution_failed"
        return {
            "receipt_id": receipt.id,
            "received_at": receipt.first_received_at.isoformat(),
            "processing_status": receipt.state,
            "execution_id": execution_id,
            "error_code": code,
            **{
                key: item.get(key)
                for key in (
                    "processing_mode",
                    "batch_execution_id",
                    "batch_member_index",
                    "batch_member_count",
                    "batch_member_outcome",
                    "batch_member_code",
                )
            },
        }

    @staticmethod
    def _new_status(receipt: EventReceipt) -> dict[str, Any]:
        return {
            "receipt_id": receipt.id,
            "received_at": receipt.first_received_at.isoformat(),
            "processing_status": "pending",
            "execution_id": None,
            "error_code": None,
            "processing_mode": "business_batch",
            "batch_execution_id": None,
            "batch_member_index": None,
            "batch_member_count": None,
            "batch_member_outcome": None,
            "batch_member_code": None,
        }

    def _persist(self) -> None:
        if not self._new:
            return
        assert self._activation is not None
        checkpoint = self._checkpoint
        if checkpoint is None:
            checkpoint = SourceCheckpoint(
                subscription_id=self.subscription.id,
                source_epoch=self.source_epoch,
                shard=self.shard,
                position={},
                confirmed_position={},
                next_sequence=1,
                assignment_generation=1,
            )
            self.session.add(checkpoint)
            self._checkpoint = checkpoint
        sequence = checkpoint.next_sequence
        records = []
        for new in self._new.values():
            receipt = new.receipt
            receipt.sequence = sequence
            sequence += 1
            records.append(
                {
                    key: getattr(receipt, key)
                    for key in (
                        "subscription_id",
                        "activation_id",
                        "source_epoch",
                        "source_event_id",
                        "content_hash",
                        "envelope",
                        "source_raw_body",
                        "payload_bytes",
                        "retained_payload_bytes",
                        "retained_metadata_bytes",
                        "shard",
                        "sequence",
                        "state",
                        "first_received_at",
                        "last_seen_at",
                        "duplicate_count",
                    )
                }
            )
        returned = self.session.execute(
            insert(EventReceipt).returning(EventReceipt.id, EventReceipt.source_event_id), records
        ).all()
        identities = {identity: row_id for row_id, identity in returned}
        if identities.keys() != self._new.keys():
            raise RuntimeError("T1 RETURNING differs from accepted identities")
        deliveries = []
        for identity, new in self._new.items():
            new.receipt.id = identities[identity]
            item = EventDelivery(receipt_id=new.receipt.id, ordinal=1, state="pending")
            accounting.delivery_budget(item, new.envelope_bytes, batch=new.budget)
            deliveries.append(
                {
                    "receipt_id": new.receipt.id,
                    "ordinal": 1,
                    "state": "pending",
                    "retained_execution_bytes": item.retained_execution_bytes,
                    "retained_metadata_bytes": item.retained_metadata_bytes,
                    "retained_batch_bytes": item.retained_batch_bytes,
                }
            )
        returned_deliveries = self.session.execute(
            insert(EventDelivery).returning(EventDelivery.receipt_id, EventDelivery.id), deliveries
        ).all()
        delivery_ids = {receipt_id: item_id for receipt_id, item_id in returned_deliveries}
        if delivery_ids.keys() != set(identities.values()) or len(returned_deliveries) != len(
            self._new
        ):
            raise RuntimeError("T1 Delivery RETURNING differs from accepted identities")
        checkpoint.next_sequence = sequence
        checkpoint.position = next(reversed(self._new.values())).item.position
        if (
            checkpoint.blocked_unaccepted is not None
            and checkpoint.blocked_unaccepted["code"] == "subscription_capacity_exceeded"
        ):
            checkpoint.blocked_unaccepted = None
        for row, value in zip(self._quota_rows, self._quota_values, strict=True):
            value.apply(row)
        self.session.flush()
        delivery.refresh_delivery_state(self.session, self.subscription)

    def receive(self, inputs: Sequence[T1Input]) -> T1Result:
        self._active_transaction()
        if self._used:
            raise domain_error(409, "subscription_owner_stale", "T1 unit was already consumed")
        self._used = True
        maximum = HTTP_MAX_ITEMS if self.source_type == "http" else MAX_ITEMS
        if not 1 <= len(inputs) <= maximum or len({item.index for item in inputs}) != len(inputs):
            raise domain_error(
                422, "t1_unit_bounds_exceeded", "T1 unit exceeds its source member bound"
            )
        if (
            self.subscription.source_type != self.source_type
            or self.subscription.deleted_at is not None
        ):
            raise domain_error(409, "subscription_config_conflict", "T1 source scope differs")
        prepared = [self._prepare(item) for item in inputs]
        if sum(item.envelope_bytes + item.raw_bytes for item in prepared) > MAX_PAYLOAD_BYTES:
            raise domain_error(422, "t1_unit_bounds_exceeded", "T1 unit payload exceeds one MiB")
        if self.source_type == "rabbitmq":
            self._owner(database_now(self.session))
            if (
                self.source_epoch != self.subscription.source_epoch
                or (
                    self.expected_activation_id is not None
                    and self.expected_activation_id != self.subscription.activation_id
                )
                or (
                    self.expected_config_version is not None
                    and self.expected_config_version != self.subscription.config_version
                )
            ):
                raise domain_error(
                    409, "subscription_config_conflict", "Queue source binding changed"
                )
            self._load_checkpoint()
        self._existing = {
            row.source_event_id: row
            for row in self.session.scalars(
                select(EventReceipt)
                .options(defer(EventReceipt.source_raw_body), defer(EventReceipt.envelope))
                .where(
                    EventReceipt.subscription_id == self.subscription.id,
                    EventReceipt.source_epoch == self.source_epoch,
                    EventReceipt.source_event_id.in_([item.identity for item in inputs]),
                )
                .with_for_update()
            )
        }
        answers: list[tuple[T1Input, bool, EventReceipt | None, HTTPException | None]] = []
        stopped = None
        for value in prepared:
            item = value.item
            try:
                self._active_transaction()
                if self.source_type == "rabbitmq" and value.error is not None:
                    raise value.error
                existing = self._existing.get(item.identity)
                new = self._new.get(item.identity)
                receipt = existing or (new.receipt if new else None)
                if receipt is not None:
                    if self.source_type == "rabbitmq":
                        self._owner(database_now(self.session))
                    if value.content_hash is None or receipt.content_hash != value.content_hash:
                        raise domain_error(
                            409, "event_identity_conflict", "Same source identity changed content"
                        )
                    receipt.duplicate_count += 1
                    receipt.last_seen_at = database_now(self.session)
                    answers.append((item, True, receipt, None))
                    continue
                budget, now, ready = self._validate_new(value)
                assert (
                    item.envelope is not None
                    and value.content_hash is not None
                    and self._activation is not None
                )
                receipt = EventReceipt(
                    subscription_id=self.subscription.id,
                    activation_id=self._activation.id,
                    source_epoch=self.source_epoch,
                    source_event_id=item.identity,
                    content_hash=value.content_hash,
                    envelope=item.envelope,
                    source_raw_body=item.raw_body if self.source_type == "http" else None,
                    payload_bytes=budget.total_bytes,
                    shard=self.shard,
                    sequence=1,
                    state="pending",
                    first_received_at=now,
                    last_seen_at=now,
                    duplicate_count=0,
                )
                accounting.receipt_budget(
                    receipt,
                    value.envelope_bytes,
                    value.raw_bytes if self.source_type == "http" else 0,
                    batch=budget,
                )
                future_delivery = EventDelivery(
                    receipt_id=9223372036854775807, ordinal=1, state="pending"
                )
                accounting.delivery_budget(future_delivery, value.envelope_bytes, batch=budget)
                reserved_values = self._reserve(budget.total_bytes, now, ready)
                self._quota_values = reserved_values
                self._new[item.identity] = _New(item, receipt, budget, value.envelope_bytes)
                if self._oldest is None:
                    self._oldest = now
                answers.append((item, False, receipt, None))
            except HTTPException as error:
                answers.append((item, False, None, error))
                if self.source_type == "rabbitmq":
                    stopped = item.index
                    break
        self._active_transaction()
        self._persist()
        if self._new or self.source_type == "rabbitmq":
            self._owner(database_now(self.session))
            if self._new and (
                not settings.event_subscriptions_enabled
                or (self.source_type == "http" and not settings.http_ingress_enabled)
            ):
                raise domain_error(409, "source_not_available", "New reception was stopped")
        outcomes = []
        statuses: dict[int, dict[str, Any]] = {}
        for item, duplicate, receipt, outcome_error in answers:
            if outcome_error is not None:
                outcomes.append(
                    T1Outcome(
                        item.index, outcome_error.status_code, None, False, None, outcome_error
                    )
                )
            else:
                assert receipt is not None
                if receipt.id not in statuses:
                    statuses[receipt.id] = (
                        self._new_status(receipt)
                        if item.identity in self._new
                        else self._existing_status(receipt)
                    )
                outcomes.append(
                    T1Outcome(
                        item.index,
                        200 if duplicate else 202,
                        receipt.id,
                        duplicate,
                        statuses[receipt.id],
                    )
                )
        self.session.flush()
        prefix = next(
            (index for index, answer in enumerate(outcomes) if answer.error is not None),
            len(outcomes),
        )
        return T1Result(tuple(outcomes), prefix, stopped)
