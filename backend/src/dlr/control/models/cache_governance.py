"""Durable cache guard authority independent of Adapter retention."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, String, Uuid, func, text
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
