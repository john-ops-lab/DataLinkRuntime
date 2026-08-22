"""Durable Worker-side cleanup requests for permanently deleted Adapters."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class WorkerCleanupRequest(Base):
    """One adapter-scoped environment cleanup delivered through a Worker.

    ``adapter_id`` intentionally has no foreign key: the Control transaction
    removes the Adapter first, while the outbound Worker task must remain
    claimable afterwards. Only the dedicated adapter directory is removed;
    shared package caches are never part of this request.
    """

    __tablename__ = "worker_cleanup_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_worker_cleanup_requests_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    worker_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("workers.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Deliberately not an FK; the Adapter is already permanently deleted.
    adapter_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # Stable machine code only; raw Worker filesystem errors never return to
    # the Control API or enter platform logs.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
