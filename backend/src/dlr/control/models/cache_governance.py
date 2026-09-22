"""Durable cache guard authority independent of Adapter retention."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class WorkerCacheGuard(Base):
    """Stable serialization point for one Worker's immutable Revision cache."""

    __tablename__ = "worker_cache_guards"
    __table_args__ = (
        CheckConstraint("worker_id > 0", name="ck_worker_cache_guards_worker_positive"),
        CheckConstraint("version_id > 0", name="ck_worker_cache_guards_version_positive"),
        CheckConstraint("adapter_id > 0", name="ck_worker_cache_guards_adapter_positive"),
        CheckConstraint("generation >= 0", name="ck_worker_cache_guards_generation_nonnegative"),
        CheckConstraint("phase IN ('idle', 'acquired')", name="ck_worker_cache_guards_phase"),
        CheckConstraint(
            "(phase = 'idle' AND operation_id IS NULL) OR "
            "(phase = 'acquired' AND operation_id IS NOT NULL)",
            name="ck_worker_cache_guards_operation_phase",
        ),
        Index("ix_worker_cache_guards_operation", "operation_id", unique=True),
    )

    worker_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    version_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Deliberately no FK: this remains the authority after Adapter/Version deletion.
    adapter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    operation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    phase: Mapped[str] = mapped_column(
        String(16), nullable=False, default="idle", server_default=text("'idle'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WorkerCacheOperation(Base):
    """Durable receipt for one exact guard generation."""

    __tablename__ = "worker_cache_operations"
    __table_args__ = (
        CheckConstraint("worker_id > 0", name="ck_worker_cache_operations_worker_positive"),
        CheckConstraint("version_id > 0", name="ck_worker_cache_operations_version_positive"),
        CheckConstraint("adapter_id > 0", name="ck_worker_cache_operations_adapter_positive"),
        CheckConstraint("generation > 0", name="ck_worker_cache_operations_generation_positive"),
        CheckConstraint(
            "phase IN ('acquired', 'completed', 'aborted')",
            name="ck_worker_cache_operations_phase",
        ),
        CheckConstraint(
            "operation_kind IN ('gc', 'cleanup', 'replacement')",
            name="ck_worker_cache_operations_kind",
        ),
        CheckConstraint(
            "((cleanup_id IS NULL) = (cleanup_claim_attempt IS NULL)) AND "
            "((operation_kind = 'cleanup') = (cleanup_id IS NOT NULL))",
            name="ck_worker_cache_operations_cleanup_pair",
        ),
        CheckConstraint(
            "((operation_kind = 'replacement') = (replacement_context IS NOT NULL))",
            name="ck_worker_cache_operations_replacement_context",
        ),
        Index(
            "uq_worker_cache_operations_generation",
            "worker_id",
            "version_id",
            "generation",
            unique=True,
        ),
        Index("ix_worker_cache_operations_active", "worker_id", "phase"),
    )

    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    worker_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    adapter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    operation_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="gc", server_default=text("'gc'")
    )
    cleanup_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cleanup_claim_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_identity: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    replacement_context: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkerCacheManagementOperation(Base):
    """Administrator command with an identity independent of guard generations."""

    __tablename__ = "worker_cache_management_operations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('preview', 'clean', 'protect', 'retry')",
            name="ck_worker_cache_management_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_worker_cache_management_status",
        ),
        CheckConstraint("claim_epoch >= 0", name="ck_worker_cache_management_claim_epoch"),
        Index(
            "uq_worker_cache_management_idempotency", "worker_id", "idempotency_key", unique=True
        ),
        Index(
            "uq_worker_cache_management_active",
            "worker_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        Index("ix_worker_cache_management_audit", "worker_id", "created_at", "operation_id"),
    )

    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    worker_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workers.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    actor: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    claim_epoch: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    target_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_operation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    target_cleanup_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    child_operations: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    result: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WorkerCacheSnapshot(Base):
    """Latest bounded observation from one Worker; never a deletion lease."""

    __tablename__ = "worker_cache_snapshots"
    __table_args__ = (
        CheckConstraint("sequence > 0", name="ck_worker_cache_snapshots_sequence"),
        CheckConstraint(
            "state IN ('complete', 'incomplete', 'owner_unconfirmed')",
            name="ck_worker_cache_snapshots_state",
        ),
    )

    worker_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workers.id", ondelete="CASCADE"), primary_key=True
    )
    sample_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    complete: Mapped[bool] = mapped_column(nullable=False)
    cursor: Mapped[str | None] = mapped_column(String(256), nullable=True)
    summary: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)


class WorkerCacheSnapshotItem(Base):
    """Latest non-sensitive per-key fact, bounded by one complete sample."""

    __tablename__ = "worker_cache_snapshot_items"
    __table_args__ = (
        CheckConstraint(
            "adapter_id > 0 AND version_id > 0", name="ck_worker_cache_snapshot_items_ids"
        ),
        Index("ix_worker_cache_snapshot_items_sample", "worker_id", "sample_id"),
    )

    worker_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workers.id", ondelete="CASCADE"), primary_key=True
    )
    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    sample_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    adapter_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    identity: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pinned: Mapped[bool] = mapped_column(nullable=False)
    rebuildability: Mapped[str] = mapped_column(String(32), nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkerCacheManagementChild(Base):
    """Explicit parent link that keeps guard authority separate from admin identity."""

    __tablename__ = "worker_cache_management_children"
    __table_args__ = (
        CheckConstraint("generation > 0", name="ck_worker_cache_management_children_generation"),
        Index("ix_worker_cache_management_children_guard", "guard_operation_id"),
    )

    management_operation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("worker_cache_management_operations.operation_id", ondelete="CASCADE"),
        primary_key=True,
    )
    guard_operation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("worker_cache_operations.operation_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
