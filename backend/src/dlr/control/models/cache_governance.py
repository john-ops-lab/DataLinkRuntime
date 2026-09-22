"""Durable cache guard authority independent of Adapter retention."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
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
