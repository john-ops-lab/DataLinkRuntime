"""Immutable business execution membership, separate from Kubernetes SyncBatch."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class SubscriptionExecutionBatch(Base):
    __tablename__ = "subscription_execution_batches"
    __table_args__ = (
        CheckConstraint("member_count BETWEEN 1 AND 100", name="ck_execution_batch_count"),
        CheckConstraint(
            "input_bytes BETWEEN 1 AND 262144 AND retained_input_bytes >= input_bytes "
            "AND retained_batch_bytes BETWEEN 0 AND 819200",
            name="ck_execution_batch_bytes",
        ),
        CheckConstraint("mode = 'business_batch'", name="ck_execution_batch_mode"),
        CheckConstraint(
            "result_provenance IS NULL OR result_provenance IN "
            "('worker_validated','control_unknown','control_not_started')",
            name="ck_execution_batch_provenance",
        ),
        Index("ix_execution_batch_cleanup", "subscription_id", "created_at"),
    )
    execution_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("executions.id", ondelete="RESTRICT"), primary_key=True
    )
    batch_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="business_batch", server_default="business_batch"
    )
    subscription_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("event_subscriptions.id"), nullable=False, index=True
    )
    source_epoch: Mapped[str] = mapped_column(String(64), nullable=False)
    shard: Mapped[str] = mapped_column(String(512), nullable=False)
    activation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subscription_activations.id"), nullable=False
    )
    revision_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("adapter_versions.id"), nullable=False
    )
    revision_override_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("adapter_versions.id")
    )
    manifest: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    member_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    input_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    retained_input_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    retained_batch_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    result_counts: Mapped[dict[str, int] | None] = mapped_column(JSONB(none_as_null=True))
    result_provenance: Mapped[str | None] = mapped_column(String(32))
    result_attempt_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("execution_attempts.id", ondelete="RESTRICT")
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SubscriptionExecutionBatchMember(Base):
    __tablename__ = "subscription_execution_batch_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["delivery_id", "receipt_id", "execution_id"],
            [
                "event_deliveries.id",
                "event_deliveries.receipt_id",
                "event_deliveries.batch_execution_id",
            ],
            name="fk_execution_batch_member_delivery",
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint("execution_id", "receipt_id", name="uq_execution_batch_receipt"),
        UniqueConstraint("execution_id", "member_index", name="uq_execution_batch_member_index"),
        CheckConstraint("member_index BETWEEN 0 AND 99", name="ck_execution_batch_member_index"),
    )
    delivery_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    execution_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "subscription_execution_batches.execution_id", deferrable=True, initially="DEFERRED"
        ),
        nullable=False,
        index=True,
    )
    member_index: Mapped[int] = mapped_column(Integer, nullable=False)
