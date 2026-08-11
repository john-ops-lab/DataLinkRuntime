"""Worker and Execution persistence models.

M2 contracts kept by these models:

- Every Execution is pinned to one immutable ``version_id`` at creation
  time; later Save/Publish never changes it.
- Execution history must survive deletion attempts: ``adapter_id`` and
  ``version_id`` foreign keys restrict deletion, and the service layer
  answers 409 ``adapter_has_executions`` instead of deleting.
- Workers carry no credentials; the platform-wide Worker token lives only
  in the Control configuration.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class Worker(Base):
    """A registered Worker Node. Re-registration with the same name reuses
    the existing row instead of creating a new one."""

    __tablename__ = "workers"
    __table_args__ = (CheckConstraint("status IN ('online', 'offline')", name="ck_workers_status"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    last_heartbeat: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # M2 always stores ["python"].
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Execution(Base):
    """One Adapter run, permanently bound to a version snapshot."""

    __tablename__ = "executions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'timeout', 'cancelled')",
            name="ck_executions_status",
        ),
        # M2 only creates manual executions; the value space stays open.
        CheckConstraint("trigger IN ('manual')", name="ck_executions_trigger"),
        # Supports the claim query: pending rows ordered by (created_at, id).
        Index("ix_executions_claim", "status", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    adapter_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("adapters.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("adapter_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # NULL keeps the "not claimed yet" semantics.
    worker_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("workers.id", ondelete="RESTRICT"),
        nullable=True,
    )
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
    # Any JSON value is valid input, including JSON null.
    input: Mapped[object] = mapped_column(JSONB, nullable=False)
    # Full output, only stored when it fits the big-field limit.
    output: Mapped[object | None] = mapped_column(JSONB, nullable=True)
    # UTF-8 byte size of the complete JSON output.
    output_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Human-readable preview when the full output was not stored.
    output_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    stdout: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    stdout_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    stderr: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    stderr_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Failure/timeout summary.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
