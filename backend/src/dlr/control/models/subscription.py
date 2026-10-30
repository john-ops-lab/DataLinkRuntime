"""Durable event responsibilities. Source I/O never runs inside these transactions."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


def identity() -> Any:
    return mapped_column(BigInteger, Identity(), primary_key=True)


def reference(table: str, *, nullable: bool = False, ondelete: str = "RESTRICT") -> Any:
    return mapped_column(
        BigInteger, ForeignKey(f"{table}.id", ondelete=ondelete), nullable=nullable
    )


def clock() -> Any:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def counter() -> Any:
    return mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))


class EventSubscription(Base):
    __tablename__ = "event_subscriptions"
    __table_args__ = (
        Index(
            "uq_event_subscriptions_adapter",
            "adapter_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_subscription_source_group",
            "source_group_key",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND source_group_key IS NOT NULL"),
        ),
        CheckConstraint("source_type ~ '^[a-z][a-z0-9_]{0,31}$'", name="ck_subscription_source"),
        CheckConstraint("desired_state IN ('disabled','enabled')", name="ck_subscription_desired"),
        CheckConstraint("config_version >= 1", name="ck_subscription_version"),
    )
    source_group_key: Mapped[str | None] = mapped_column(String(64))
    id: Mapped[int] = identity()
    adapter_id: Mapped[int] = reference("adapters")
    source_type: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    desired_state: Mapped[str] = mapped_column(
        String(16), default="disabled", server_default="disabled"
    )
    connection_state: Mapped[str] = mapped_column(
        String(32), default="stopped", server_default="stopped"
    )
    delivery_state: Mapped[str] = mapped_column(String(32), default="idle", server_default="idle")
    config_version: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    source_epoch: Mapped[str] = mapped_column(String(64))
    # Explicitly non-FK backpointer avoids circular insert ordering; all activation writers
    # check subscription ownership under the Adapter/Subscription lock.
    activation_id: Mapped[int | None] = mapped_column(BigInteger)
    repair_revision_id: Mapped[int | None] = reference("adapter_versions", nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = clock()
    updated_at: Mapped[datetime] = clock()


class SubscriptionConfigVersion(Base):
    __tablename__ = "subscription_config_versions"
    __table_args__ = (
        UniqueConstraint("subscription_id", "version", name="uq_subscription_config_version"),
    )
    id: Mapped[int] = identity()
    subscription_id: Mapped[int] = reference("event_subscriptions")
    version: Mapped[int] = mapped_column(BigInteger)
    source_epoch: Mapped[str] = mapped_column(String(64))
    source_credential_id: Mapped[int | None] = reference("credentials", nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source_identity: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = clock()


class HttpIngressRoute(Base):
    """Permanent route identity. Retired routes never acquire a different epoch."""

    __tablename__ = "http_ingress_routes"
    __table_args__ = (
        UniqueConstraint("subscription_id", "source_epoch", name="uq_http_ingress_epoch"),
    )
    public_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subscription_id: Mapped[int | None] = reference(
        "event_subscriptions", nullable=True, ondelete="SET NULL"
    )
    source_epoch: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = clock()


class SubscriptionActivation(Base):
    __tablename__ = "subscription_activations"
    __table_args__ = (
        Index(
            "ix_subscription_activation_policy_legacy",
            "id",
            postgresql_where=text("execution_policy_snapshot IS NULL"),
        ),
    )
    id: Mapped[int] = identity()
    subscription_id: Mapped[int] = reference("event_subscriptions")
    config_version_id: Mapped[int] = reference("subscription_config_versions")
    revision_id: Mapped[int] = reference("adapter_versions")
    worker_id: Mapped[int] = reference("workers")
    execution_policy_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    created_at: Mapped[datetime] = clock()


class ActivationCredentialReference(Base):
    __tablename__ = "activation_credential_references"
    __table_args__ = (
        UniqueConstraint("activation_id", "env_key", name="uq_activation_credential_env"),
    )
    id: Mapped[int] = identity()
    activation_id: Mapped[int] = reference("subscription_activations")
    credential_id: Mapped[int] = reference("credentials")
    binding_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    env_key: Mapped[str] = mapped_column(String(128))
    field: Mapped[str] = mapped_column(String(128))


class SubscriptionLease(Base):
    __tablename__ = "subscription_leases"
    __table_args__ = (CheckConstraint("generation >= 1", name="ck_subscription_lease_generation"),)
    subscription_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("event_subscriptions.id"), primary_key=True
    )
    owner_id: Mapped[str] = mapped_column(String(128))
    generation: Mapped[int] = mapped_column(BigInteger, default=1)
    closing_generation: Mapped[int | None] = mapped_column(BigInteger)
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourceCheckpoint(Base):
    __tablename__ = "source_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id", "source_epoch", "shard", name="uq_subscription_checkpoint_shard"
        ),
    )
    id: Mapped[int] = identity()
    subscription_id: Mapped[int] = reference("event_subscriptions")
    source_epoch: Mapped[str] = mapped_column(String(64))
    shard: Mapped[str] = mapped_column(String(512))
    position: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    confirmed_position: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    next_sequence: Mapped[int] = mapped_column(BigInteger, default=1)
    assignment_generation: Mapped[int] = mapped_column(BigInteger, default=1)
    blocked_receipt_id: Mapped[int | None] = mapped_column(BigInteger)
    blocked_unaccepted: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))


class SyncBatch(Base):
    __tablename__ = "subscription_sync_batches"
    __table_args__ = (
        Index(
            "uq_subscription_open_sync_batch",
            "subscription_id",
            unique=True,
            postgresql_where=text("activation_id IS NOT NULL AND finished_at IS NULL"),
        ),
        Index(
            "ix_sync_ready_control",
            "ready_since",
            "id",
            postgresql_where=text("ready_kind IS NOT NULL"),
        ),
        CheckConstraint(
            "reserved_control_count >= 0 AND reserved_control_count <= 3 "
            "AND reserved_control_bytes >= 0 AND reserved_control_bytes <= 62976",
            name="ck_sync_control_reserve",
        ),
    )
    id: Mapped[int] = identity()
    subscription_id: Mapped[int] = reference("event_subscriptions")
    # Nullable only for pre-connector historical skeletons. New batches require
    # the original activation/epoch and never infer either from current config.
    activation_id: Mapped[int | None] = reference("subscription_activations", nullable=True)
    source_epoch: Mapped[str | None] = mapped_column(String(64))
    owner_id: Mapped[str | None] = mapped_column(String(128))
    owner_generation: Mapped[int | None] = mapped_column(BigInteger)
    scope_fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="reading")
    resource_version: Mapped[str | None] = mapped_column(Text)
    item_count: Mapped[int] = counter()
    total_bytes: Mapped[int] = counter()
    page_count: Mapped[int] = counter()
    read_complete: Mapped[bool] = mapped_column(default=False)
    completion_committed: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    completion_abandoned: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    drain_open: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    reserved_control_count: Mapped[int] = counter()
    reserved_control_bytes: Mapped[int] = counter()
    ready_kind: Mapped[str | None] = mapped_column(String(32))
    ready_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_gap: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    # Logical control backpointers are checked under the Batch lock. Retaining
    # their IDs after lawful Receipt cleanup must not resurrect qualification.
    started_receipt_id: Mapped[int | None] = mapped_column(BigInteger)
    completed_receipt_id: Mapped[int | None] = mapped_column(BigInteger)
    failed_receipt_id: Mapped[int | None] = mapped_column(BigInteger)
    completed_execution_id: Mapped[int | None] = reference(
        "executions", nullable=True, ondelete="SET NULL"
    )
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = clock()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EventReceipt(Base):
    __tablename__ = "event_receipts"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "source_epoch",
            "source_event_id",
            name="uq_subscription_source_event",
        ),
        UniqueConstraint(
            "subscription_id",
            "source_epoch",
            "shard",
            "sequence",
            name="uq_subscription_shard_sequence",
        ),
        CheckConstraint("payload_bytes >= 0", name="ck_receipt_payload_bytes"),
        CheckConstraint(
            "retained_payload_bytes >= 0 AND retained_metadata_bytes BETWEEN 0 AND 4096",
            name="ck_receipt_retained_budget",
        ),
        Index("ix_receipt_cleanup", "subscription_id", "terminal_at"),
        Index(
            "ix_receipt_active_state",
            "subscription_id",
            "state",
            postgresql_where=text("state IN ('pending','processing','failed')"),
        ),
        Index(
            "ix_receipt_pending_age",
            "subscription_id",
            "first_received_at",
            postgresql_where=text("state = 'pending'"),
        ),
        Index(
            "ix_receipt_unsettled_shard",
            "subscription_id",
            "source_epoch",
            "shard",
            "sequence",
            "id",
            postgresql_where=text("state NOT IN ('succeeded','skipped','superseded','abandoned')"),
        ),
        Index("ix_receipt_sync_members", "batch_id", "source_phase", "state", "id"),
        Index(
            "ix_receipt_pending_object",
            "subscription_id",
            "source_epoch",
            "object_key_hash",
            "id",
            postgresql_where=text("source_phase = 'watch' AND state = 'pending'"),
        ),
    )
    id: Mapped[int] = identity()
    subscription_id: Mapped[int] = reference("event_subscriptions")
    activation_id: Mapped[int] = reference("subscription_activations")
    source_epoch: Mapped[str] = mapped_column(String(64))
    source_event_id: Mapped[str] = mapped_column(String(128))
    content_hash: Mapped[str] = mapped_column(String(64))
    source_raw_body: Mapped[bytes | None] = mapped_column(LargeBinary)
    envelope: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    payload_bytes: Mapped[int] = mapped_column(BigInteger)
    retained_payload_bytes: Mapped[int] = counter()
    retained_metadata_bytes: Mapped[int] = counter()
    shard: Mapped[str] = mapped_column(String(512))
    sequence: Mapped[int] = mapped_column(BigInteger)
    batch_id: Mapped[int | None] = reference("subscription_sync_batches", nullable=True)
    source_phase: Mapped[str | None] = mapped_column(String(32))
    list_page_index: Mapped[int | None] = mapped_column(BigInteger)
    object_key_hash: Mapped[str | None] = mapped_column(String(64))
    coalesced_count: Mapped[int] = counter()
    first_source_event_id: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(32), default="pending")
    first_received_at: Mapped[datetime] = clock()
    last_seen_at: Mapped[datetime] = clock()
    duplicate_count: Mapped[int] = counter()
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload_expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_id: Mapped[int | None] = reference("event_receipts", nullable=True)


class EventDelivery(Base):
    __tablename__ = "event_deliveries"
    __table_args__ = (
        UniqueConstraint("receipt_id", "ordinal", name="uq_receipt_delivery_ordinal"),
        UniqueConstraint("execution_id", name="uq_delivery_execution"),
        UniqueConstraint(
            "id", "receipt_id", "batch_execution_id", name="uq_delivery_batch_identity"
        ),
        CheckConstraint(
            "execution_id IS NULL OR batch_execution_id IS NULL", name="ck_delivery_single_or_batch"
        ),
        CheckConstraint("retained_batch_bytes BETWEEN 0 AND 8192", name="ck_delivery_batch_bytes"),
        Index(
            "ix_delivery_batch_execution",
            "batch_execution_id",
            postgresql_where=text("batch_execution_id IS NOT NULL"),
        ),
        CheckConstraint(
            "batch_member_outcome IS NULL OR batch_member_outcome IN "
            "('succeeded','failed','unknown','not_run')",
            name="ck_delivery_batch_outcome",
        ),
        Index("ix_subscription_delivery_state", "state", "id"),
        CheckConstraint(
            "retained_execution_bytes >= 0 AND retained_metadata_bytes BETWEEN 0 AND 512",
            name="ck_delivery_retained_budget",
        ),
    )
    id: Mapped[int] = identity()
    receipt_id: Mapped[int] = reference("event_receipts")
    ordinal: Mapped[int] = mapped_column(BigInteger, default=1)
    execution_id: Mapped[int | None] = reference("executions", nullable=True, ondelete="SET NULL")
    batch_execution_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "subscription_execution_batches.execution_id", deferrable=True, initially="DEFERRED"
        ),
    )
    batch_member_outcome: Mapped[str | None] = mapped_column(String(16))
    batch_member_code: Mapped[str | None] = mapped_column(String(64))
    retained_batch_bytes: Mapped[int] = counter()
    state: Mapped[str] = mapped_column(String(32), default="pending")
    activation_override_id: Mapped[int | None] = reference(
        "subscription_activations", nullable=True
    )
    revision_override_id: Mapped[int | None] = reference("adapter_versions", nullable=True)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = clock()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retained_execution_bytes: Mapped[int] = counter()
    retained_metadata_bytes: Mapped[int] = counter()


class DeliveryDisposition(Base):
    __tablename__ = "delivery_dispositions"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id", "request_id", name="uq_subscription_disposition_request"
        ),
        Index(
            "uq_sync_abandon_audit",
            "batch_id",
            unique=True,
            postgresql_where=text("action = 'abandon_completion' AND batch_id IS NOT NULL"),
        ),
    )
    id: Mapped[int] = identity()
    subscription_id: Mapped[int] = reference("event_subscriptions")
    request_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[int | None] = reference("users", nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    receipt_id: Mapped[int | None] = reference("event_receipts", nullable=True)
    delivery_id: Mapped[int | None] = reference("event_deliveries", nullable=True)
    batch_id: Mapped[int | None] = reference("subscription_sync_batches", nullable=True)
    # Nonzero only for the one prepaid completion-abandonment audit. This is
    # actual retained history/dedupe, not a request for additional free quota.
    audit_bytes: Mapped[int] = counter()
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = clock()


class SubscriptionQuota(Base):
    __tablename__ = "subscription_quotas"
    __table_args__ = (
        CheckConstraint(
            "pending_count >= 0 AND pending_bytes >= 0 AND history_count >= 0 "
            "AND history_bytes >= 0 AND dedupe_count >= 0 "
            "AND reserved_control_count >= 0 AND reserved_control_bytes >= 0",
            name="ck_subscription_quota_nonnegative",
        ),
    )
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(64))
    pending_count: Mapped[int] = counter()
    pending_bytes: Mapped[int] = counter()
    history_count: Mapped[int] = counter()
    history_bytes: Mapped[int] = counter()
    dedupe_count: Mapped[int] = counter()
    reserved_control_count: Mapped[int] = counter()
    reserved_control_bytes: Mapped[int] = counter()
    rate_count: Mapped[int] = counter()
    rate_window_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
