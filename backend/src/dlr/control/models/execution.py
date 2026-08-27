"""Worker and Execution persistence models.

M2 contracts kept by these models:

- Every Execution is pinned to one immutable ``version_id`` at creation
  time; later Save operations never change it.
- Execution history is removed by the Adapter's permanent-delete transaction.
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
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
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
    __table_args__ = (
        CheckConstraint("status IN ('online', 'offline')", name="ck_workers_status"),
        CheckConstraint("protocol_version BETWEEN 1 AND 2", name="ck_workers_protocol_version"),
        Index("ix_workers_protocol_version", "protocol_version"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    last_heartbeat: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Runtime names detected by the Worker at registration time.
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # Missing protocol_version on the wire is the v1 compatibility contract.
    protocol_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1, server_default=text("1")
    )
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
        CheckConstraint(
            "timeout_seconds_snapshot IS NULL OR timeout_seconds_snapshot BETWEEN 1 AND 86400",
            name="ck_executions_timeout_seconds_snapshot",
        ),
        CheckConstraint(
            "recovery_grace_seconds_snapshot IS NULL OR "
            "recovery_grace_seconds_snapshot BETWEEN 10 AND 3600",
            name="ck_executions_recovery_grace_seconds_snapshot",
        ),
        CheckConstraint(
            "workspace_cleanup_attempt_timeout_seconds_snapshot IS NULL OR "
            "workspace_cleanup_attempt_timeout_seconds_snapshot BETWEEN 1 AND 60",
            name="ck_executions_cleanup_attempt_timeout_snapshot",
        ),
        CheckConstraint(
            "workspace_cleanup_total_timeout_seconds_snapshot IS NULL OR "
            "workspace_cleanup_total_timeout_seconds_snapshot BETWEEN 5 AND 300",
            name="ck_executions_cleanup_total_timeout_snapshot",
        ),
        CheckConstraint(
            "workspace_cleanup_attempt_timeout_seconds_snapshot IS NULL OR "
            "workspace_cleanup_total_timeout_seconds_snapshot IS NULL OR "
            "workspace_cleanup_attempt_timeout_seconds_snapshot <= "
            "workspace_cleanup_total_timeout_seconds_snapshot",
            name="ck_executions_cleanup_attempt_le_total",
        ),
        CheckConstraint(
            "workspace_cleanup_total_timeout_seconds_snapshot IS NULL OR "
            "recovery_grace_seconds_snapshot IS NULL OR "
            "workspace_cleanup_total_timeout_seconds_snapshot < "
            "recovery_grace_seconds_snapshot",
            name="ck_executions_cleanup_total_lt_recovery_grace",
        ),
        CheckConstraint(
            "workspace_cleanup_status IS NULL OR "
            "workspace_cleanup_status IN ('completed', 'deferred')",
            name="ck_executions_workspace_cleanup_status",
        ),
        CheckConstraint(
            "claim_token_hash IS NULL OR claim_token_hash ~ '^[0-9a-f]{64}$'",
            name="ck_executions_claim_token_hash_sha256",
        ),
        CheckConstraint(
            "cleanup_receipt_token_hash IS NULL OR cleanup_receipt_token_hash ~ '^[0-9a-f]{64}$'",
            name="ck_executions_cleanup_receipt_token_hash_sha256",
        ),
        Index("ix_executions_claim_deadline_at", "status", "claim_deadline_at"),
        Index("ix_executions_execution_deadline_at", "status", "execution_deadline_at"),
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
    # A1: immutable Adapter input facts captured in the same transaction as
    # the raw runtime input.  The snapshot is deliberately public metadata;
    # later waves may add file summaries without exposing operational IDs.
    input_source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="json")
    input_config_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    input_snapshot: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=lambda: {"source_type": "json", "revision": 1},
    )
    # C0 immutable deployment facts. Historical rows remain nullable after
    # the additive migration; newly created rows fill these fields.
    timeout_seconds_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recovery_grace_seconds_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    workspace_cleanup_attempt_timeout_seconds_snapshot: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    workspace_cleanup_total_timeout_seconds_snapshot: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    claim_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    execution_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cleanup_receipt_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workspace_cleanup_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    workspace_cleanup_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
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


class ExecutionInputArtifactLease(Base):
    """One immutable file authorization held for an active Execution."""

    __tablename__ = "execution_input_artifact_leases"
    __table_args__ = (
        CheckConstraint(
            "ordinal BETWEEN 0 AND 7",
            name="ck_execution_input_artifact_leases_ordinal",
        ),
        UniqueConstraint(
            "execution_id",
            "ordinal",
            name="uq_execution_input_artifact_leases_execution_ordinal",
        ),
        Index("ix_execution_input_artifact_leases_artifact_id", "artifact_id"),
    )

    execution_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("executions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    artifact_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("managed_input_artifacts.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(SmallInteger, nullable=False)
