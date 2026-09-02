"""Additive Issue #130 persistence models.

The B2 Attempt/Slot tables are the PostgreSQL authority for a v3 Worker run.
An Outbox row is still only dispatch responsibility, while an Adapter Slot is
bound to an Attempt by the Claim transaction. Resource sandbox enforcement is
deliberately not implemented in this module; Worker capability facts remain
fail-closed until the Batch 3 Linux gate.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base

_ZERO_TOKEN_DIGEST = "0" * 64


class ExecutionIdempotencyRecord(Base):
    """One bounded, hash-only Idempotency-Key record."""

    __tablename__ = "execution_idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "adapter_id",
            "key_hash",
            name="uq_execution_idempotency_records_adapter_key",
        ),
        CheckConstraint(
            "octet_length(key_hash) = 32",
            name="ck_execution_idempotency_records_key_hash_length",
        ),
        CheckConstraint(
            "octet_length(payload_hash) = 32",
            name="ck_execution_idempotency_records_payload_hash_length",
        ),
        Index("ix_execution_idempotency_records_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    adapter_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("adapters.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Raw key material is deliberately never stored.
    key_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    payload_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    execution_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("executions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AdapterExecutionAdmission(Base):
    """Per-Adapter business outstanding counter."""

    __tablename__ = "adapter_execution_admission"
    __table_args__ = (
        CheckConstraint(
            "outstanding_count >= 0",
            name="ck_adapter_execution_admission_count_nonnegative",
        ),
        CheckConstraint(
            "outstanding_bytes >= 0",
            name="ck_adapter_execution_admission_bytes_nonnegative",
        ),
    )

    adapter_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("adapters.id", ondelete="CASCADE"),
        primary_key=True,
    )
    outstanding_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    outstanding_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    @property
    def outstanding_logical_bytes(self) -> int:
        """Compatibility spelling used by the ingress contract."""

        return self.outstanding_bytes


class GlobalExecutionAdmission(Base):
    """Singleton global business outstanding counter."""

    __tablename__ = "global_execution_admission"
    __table_args__ = (
        CheckConstraint(
            "singleton_key = 'global'",
            name="ck_global_execution_admission_singleton_key",
        ),
        CheckConstraint(
            "outstanding_count >= 0",
            name="ck_global_execution_admission_count_nonnegative",
        ),
        CheckConstraint(
            "outstanding_bytes >= 0",
            name="ck_global_execution_admission_bytes_nonnegative",
        ),
    )

    singleton_key: Mapped[str] = mapped_column(
        String(16), primary_key=True, default="global", server_default=text("'global'")
    )
    outstanding_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    outstanding_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    @property
    def outstanding_logical_bytes(self) -> int:
        """Compatibility spelling used by the ingress contract."""

        return self.outstanding_bytes


class RabbitMQRuntimeCapability(Base):
    """Last verified non-secret RabbitMQ configuration generation.

    This row is a restart-safe capability fact, not a Broker availability
    claim.  A matching, recent row permits PostgreSQL-only ingress during a
    temporary outage; a changed configuration or a newly targeted Worker
    remains fail-closed until a live topology probe succeeds.
    """

    __tablename__ = "rabbitmq_runtime_capabilities"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_rabbitmq_runtime_capabilities_singleton"),
        CheckConstraint(
            "configuration_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_rabbitmq_runtime_capabilities_fingerprint",
        ),
        CheckConstraint(
            "broker_version = '4.3.5'", name="ck_rabbitmq_runtime_capabilities_version"
        ),
        CheckConstraint(
            "jsonb_typeof(feature_flags) = 'array'",
            name="ck_rabbitmq_runtime_capabilities_feature_flags_array",
        ),
        CheckConstraint(
            "jsonb_typeof(worker_ids) = 'array'",
            name="ck_rabbitmq_runtime_capabilities_worker_ids_array",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    configuration_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    broker_version: Mapped[str] = mapped_column(String(32), nullable=False)
    feature_flags: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    worker_ids: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ExecutionCredentialBindingSnapshot(Base):
    """Durable credential references frozen for one Execution.

    ``binding_id`` is deliberately not a foreign key: replacing an Adapter's
    current binding may remove that row, while this snapshot remains the
    immutable source for a future Claim.  ``credential_id`` is restricted so
    a queued/nonterminal Execution can never lose the Credential it names.
    """

    __tablename__ = "execution_credential_binding_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "binding_id",
            name="uq_execution_credential_snapshots_execution_binding",
        ),
        UniqueConstraint(
            "execution_id",
            "env_key",
            name="uq_execution_credential_snapshots_execution_env_key",
        ),
        CheckConstraint("binding_id > 0", name="ck_execution_credential_snapshots_binding_id"),
        CheckConstraint(
            "credential_id > 0", name="ck_execution_credential_snapshots_credential_id"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    execution_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # This is the binding identity at acceptance time, not a live binding FK.
    binding_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credential_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("credentials.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    env_key: Mapped[str] = mapped_column(String(128), nullable=False)
    field: Mapped[str] = mapped_column(String(64), nullable=False)


class ExecutionOutbox(Base):
    """One immutable dispatch generation pending or confirmed at the Broker."""

    __tablename__ = "execution_outbox"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "dispatch_generation",
            name="uq_execution_outbox_execution_generation",
        ),
        UniqueConstraint("message_id", name="uq_execution_outbox_message_id"),
        CheckConstraint(
            "status IN ('pending', 'published')",
            name="ck_execution_outbox_status",
        ),
        CheckConstraint("dispatch_generation >= 1", name="ck_execution_outbox_generation"),
        CheckConstraint("payload_bytes >= 0", name="ck_execution_outbox_payload_bytes"),
        CheckConstraint("publish_attempts >= 0", name="ck_execution_outbox_publish_attempts"),
        Index(
            "ix_execution_outbox_due",
            "status",
            "available_at",
            "lease_expires_at",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    execution_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    dispatch_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    routing_key: Mapped[str] = mapped_column(String(256), nullable=False)
    payload_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    payload_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    publish_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ExecutionAttempt(Base):
    """One immutable actual-run record created by a v3 Claim transaction."""

    __tablename__ = "execution_attempts"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "attempt_no",
            name="uq_execution_attempts_execution_attempt_no",
        ),
        CheckConstraint("attempt_no > 0", name="ck_execution_attempts_attempt_no_positive"),
        CheckConstraint("fencing_token > 0", name="ck_execution_attempts_fencing_positive"),
        CheckConstraint(
            "claim_token_hash ~ '^[0-9a-f]{64}$' AND cleanup_token_hash ~ '^[0-9a-f]{64}$'",
            name="ck_execution_attempts_token_hashes_sha256",
        ),
        CheckConstraint(
            "status IN ('claimed', 'running', 'succeeded', 'failed', 'timed_out', "
            "'cancelled', 'worker_lost', 'resource_exceeded')",
            name="ck_execution_attempts_status",
        ),
        Index(
            "uq_execution_attempts_active_execution",
            "execution_id",
            unique=True,
            postgresql_where=text("status IN ('claimed', 'running')"),
        ),
        Index("ix_execution_attempts_lease", "status", "lease_expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    execution_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    adapter_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("adapters.id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("workers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Raw Claim/Cleanup credentials are returned only to the Worker over
    # authenticated HTTP. PostgreSQL stores SHA-256 digests and the service
    # compares them with hmac.compare_digest.
    # The zero digests are only a compatibility default for pre-B2/manual
    # data-contract rows. Real v3 Claim always replaces them with generated
    # per-Attempt digests before commit.
    claim_token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default=_ZERO_TOKEN_DIGEST,
        server_default=text(f"'{_ZERO_TOKEN_DIGEST}'"),
    )
    cleanup_token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default=_ZERO_TOKEN_DIGEST,
        server_default=text(f"'{_ZERO_TOKEN_DIGEST}'"),
    )
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_usage_json: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    output_summary: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    cleanup_summary: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AdapterExecutionSlot(Base):
    """Explicit per-Adapter concurrency authority; B1 seeds Slot 0 only."""

    __tablename__ = "adapter_execution_slots"
    __table_args__ = (
        CheckConstraint("slot_no >= 0", name="ck_adapter_execution_slots_slot_no"),
        CheckConstraint(
            "fencing_token >= 0", name="ck_adapter_execution_slots_fencing_nonnegative"
        ),
        UniqueConstraint("active_attempt_id", name="uq_adapter_execution_slots_active_attempt"),
        Index("ix_adapter_execution_slots_lease", "lease_expires_at"),
    )

    adapter_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("adapters.id", ondelete="CASCADE"),
        primary_key=True,
    )
    slot_no: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=0)
    active_attempt_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("execution_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fencing_token: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )


class ScheduleDispatchOutcome(Base):
    """Bounded audit range for Schedule points crossed by a scheduler."""

    __tablename__ = "schedule_dispatch_outcomes"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('enqueued', 'coalesced', 'skipped', 'expired')",
            name="ck_schedule_dispatch_outcomes_outcome",
        ),
        CheckConstraint(
            "occurrence_count > 0", name="ck_schedule_dispatch_outcomes_count_positive"
        ),
        CheckConstraint(
            "occurrence_count <= 10082",
            name="ck_schedule_dispatch_outcomes_count_bounded",
        ),
        CheckConstraint(
            "last_scheduled_for >= first_scheduled_for",
            name="ck_schedule_dispatch_outcomes_range_order",
        ),
        Index(
            "ix_schedule_dispatch_outcomes_schedule_time",
            "schedule_id",
            "first_scheduled_for",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    schedule_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("adapter_schedules.id", ondelete="CASCADE"),
        nullable=False,
    )
    first_scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cron_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    timezone_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("executions.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExecutionInfrastructureIncident(Base):
    """Visible record for malformed/routing/delivery infrastructure facts."""

    __tablename__ = "execution_infrastructure_incidents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'resolved', 'ignored')",
            name="ck_execution_infrastructure_incidents_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_execution_infrastructure_incidents_attempts"),
        Index(
            "ix_execution_infrastructure_incidents_execution_generation",
            "execution_id",
            "dispatch_generation",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    execution_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=True,
    )
    dispatch_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default=text("'open'")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExecutionArtifactHold(Base):
    """Independent managed-file retention hold for a future dead-letter replay."""

    __tablename__ = "execution_artifact_holds"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "artifact_id",
            "reason",
            name="uq_execution_artifact_holds_execution_artifact_reason",
        ),
        CheckConstraint(
            "reason IN ('dead_letter_replay')",
            name="ck_execution_artifact_holds_reason",
        ),
        Index("ix_execution_artifact_holds_expiry", "expires_at", "purged_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    execution_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("managed_input_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="dead_letter_replay",
        server_default=text("'dead_letter_replay'"),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Snapshot the logical bytes protected by this hold.  This is a governance
    # counter only; it never duplicates the ArtifactStore's physical bytes.
    held_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purged_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    purge_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
