"""Worker and Execution persistence models.

M2 contracts kept by these models:

- Every Execution is pinned to one immutable ``version_id`` at creation
  time; later Save operations never change it.
- Execution history survives the Adapter's soft deletion.
- Workers carry no credentials; the platform-wide Worker token lives only
  in the Control configuration.

M3.2 scheduling and lifecycle fields:

- ``target_worker_id`` is the desired Worker; ``worker_id`` stays the
  Worker that actually claimed and runs the Execution.
- ``trigger`` distinguishes ``manual``, ``schedule`` and ``webhook`` runs.
- The partial unique index guarantees at most one active Execution per
  Adapter across every trigger type (M5.4.1).

M5.2 Schedule Trigger fields:

- ``scheduled_for`` records the planned point a ``trigger=schedule``
  Execution represents; NULL for every other trigger.
- The partial unique index on ``(adapter_id, scheduled_for)`` guarantees
  one planned point yields at most one Schedule Execution, even under
  Control restarts or multiple competing scheduler instances.
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
    # Runtime names detected by the Worker at registration time.
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
        CheckConstraint(
            "trigger IN ('manual', 'schedule', 'webhook')",
            name="ck_executions_trigger",
        ),
        CheckConstraint("locale IN ('zh-CN', 'en')", name="ck_executions_locale"),
        # Supports the claim query: pending rows ordered by (created_at, id).
        Index("ix_executions_claim", "status", "created_at", "id"),
        # One active Execution per Adapter across every trigger source.
        Index(
            "uq_executions_active_adapter",
            "adapter_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        # M5.2: one planned point yields at most one Schedule Execution;
        # the final defense against duplicate creation across Control
        # restarts or concurrent scheduler loops.
        Index(
            "uq_executions_schedule_point",
            "adapter_id",
            "scheduled_for",
            unique=True,
            postgresql_where=text("trigger = 'schedule'"),
        ),
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
    # Desired Worker; only this Worker may claim the Execution. NULL keeps
    # the legacy compatibility path where any Worker may claim.
    target_worker_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("workers.id", ondelete="SET NULL", name="fk_executions_target_worker_id"),
        nullable=True,
        index=True,
    )
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    # M5.2: the planned point this Schedule Execution represents; NULL for
    # every non-schedule trigger.
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
    # Set by an admin cancel request; the Worker observes it through the
    # progress response and terminates the subprocess.
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
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
    # Locale captured when the Execution was created. Worker platform messages
    # must not change when the deployment locale changes mid-run.
    locale: Mapped[str] = mapped_column(
        String(10), nullable=False, default="zh-CN", server_default=text("'zh-CN'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
