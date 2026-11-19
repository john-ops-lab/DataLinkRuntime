"""Verified instance envelopes and durable Worker resource charges."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base

RESOURCE_DIMENSIONS = ("cpu_millis", "memory_bytes", "pids", "tmp_bytes", "slots")
OCCUPIED_RESERVATION_STATES = ("held", "claimed", "reclaiming")


class WorkerPlacementCutover(Base):
    __tablename__ = "worker_placement_cutover"
    __table_args__ = (
        CheckConstraint(
            "id = 1 AND ((mode = 'fixed' AND paused_at IS NULL AND enabled_at IS NULL) "
            "OR (mode = 'paused' AND paused_at IS NOT NULL AND enabled_at IS NULL) "
            "OR (mode = 'reserved' AND enabled_at >= paused_at AND paused_at IS NOT NULL "
            "AND enabled_at IS NOT NULL))",
            name="ck_worker_placement_cutover_state",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExecutionPlacementAction(Base):
    """One audited explicit action on a frozen placement generation."""

    __tablename__ = "execution_placement_actions"
    __table_args__ = (
        CheckConstraint(
            "dispatch_generation > 0 AND action IN ('retry', 'cancel') AND "
            "((actor_kind = 'superadmin' AND actor_id IS NULL) OR "
            "(actor_kind = 'account' AND actor_id IS NOT NULL AND actor_id > 0))",
            name="ck_execution_placement_actions_binding",
        ),
    )

    execution_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("executions.id", ondelete="CASCADE"), primary_key=True
    )
    dispatch_generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    action: Mapped[str] = mapped_column(String(16), primary_key=True)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkerCapacityVerifier(Base):
    __tablename__ = "worker_capacity_verifiers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["worker_id", "worker_identity_id"],
            ["workers.id", "workers.identity_id"],
            name="fk_worker_capacity_verifiers_identity",
        ),
        CheckConstraint("octet_length(public_key)=32", name="ck_worker_capacity_verifier_key"),
    )

    worker_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    worker_identity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    environment_name: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("worker_environment_classes.name", ondelete="RESTRICT"),
        nullable=False,
    )


class WorkerResourceColumns:
    cpu_millis: Mapped[int] = mapped_column(BigInteger, nullable=False)
    memory_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pids: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tmp_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    slots: Mapped[int] = mapped_column(BigInteger, nullable=False)


class WorkerCapacities(WorkerResourceColumns, Base):
    __tablename__ = "worker_capacities"
    __table_args__ = (
        ForeignKeyConstraint(
            ["worker_id", "worker_identity_id"],
            ["workers.id", "workers.identity_id"],
            name="fk_worker_capacities_identity",
        ),
        ForeignKeyConstraint(
            ["worker_id", "instance_id", "instance_generation"],
            [
                "worker_instance_history.worker_id",
                "worker_instance_history.instance_id",
                "worker_instance_history.generation",
            ],
            name="fk_worker_capacities_instance",
        ),
        CheckConstraint(
            "capacity_version >= 1 AND nofile > 0 AND sampled_at <= received_at AND "
            + " AND ".join(f"{name} >= 0" for name in RESOURCE_DIMENSIONS),
            name="ck_worker_capacities_values",
        ),
    )

    worker_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    worker_identity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    instance_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    capacity_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    envelope_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    verified_environment: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    nofile: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_ready: Mapped[bool] = mapped_column(nullable=False)
    last_assignment_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    placement_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quarantine_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    task_cpu_cores: Mapped[float | None] = mapped_column(Float)


class WorkerResourceReservation(WorkerResourceColumns, Base):
    __tablename__ = "worker_resource_reservations"
    __table_args__ = (
        UniqueConstraint(
            "execution_id", "dispatch_generation", name="uq_worker_reservations_generation"
        ),
        UniqueConstraint(
            "id", "execution_id", "dispatch_generation", name="uq_worker_reservations_binding"
        ),
        UniqueConstraint("attempt_id", name="uq_worker_reservations_attempt"),
        ForeignKeyConstraint(
            ["attempt_id", "execution_id"],
            ["execution_attempts.id", "execution_attempts.execution_id"],
            name="fk_worker_reservations_attempt_execution",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["worker_id", "worker_identity_id"],
            ["workers.id", "workers.identity_id"],
            name="fk_worker_reservations_identity",
        ),
        ForeignKeyConstraint(
            ["worker_id", "instance_id", "instance_generation"],
            [
                "worker_instance_history.worker_id",
                "worker_instance_history.instance_id",
                "worker_instance_history.generation",
            ],
            name="fk_worker_reservations_instance",
        ),
        ForeignKeyConstraint(
            [
                "attempt_id",
                "worker_id",
                "worker_identity_id",
                "instance_id",
                "instance_generation",
                "fencing_token",
            ],
            [
                "execution_attempts.id",
                "execution_attempts.worker_id",
                "execution_attempts.worker_identity_id",
                "execution_attempts.worker_instance_id",
                "execution_attempts.worker_instance_generation",
                "execution_attempts.fencing_token",
            ],
            name="fk_worker_reservations_attempt",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "status IN ('held', 'claimed', 'reclaiming', 'released') AND "
            "((status = 'released') = (released_at IS NOT NULL)) AND "
            "((attempt_id IS NULL) = (fencing_token IS NULL)) AND "
            "(status <> 'held' OR attempt_id IS NULL) AND "
            "(status NOT IN ('claimed', 'reclaiming') OR attempt_id IS NOT NULL)",
            name="ck_worker_reservations_state",
        ),
        CheckConstraint(
            "capacity_version > 0 AND dispatch_generation > 0 AND slots = 1 AND "
            "cpu_millis >= 0 AND memory_bytes > 0 AND pids > 0 AND tmp_bytes > 0",
            name="ck_worker_reservations_values",
        ),
        Index(
            "ix_worker_reservations_occupied",
            "worker_id",
            postgresql_where=text("status IN ('held', 'claimed', 'reclaiming')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    execution_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("executions.id", ondelete="CASCADE"), nullable=False
    )
    dispatch_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    worker_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    worker_identity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    instance_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    capacity_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    envelope_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    cpu_accounting_version: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    held_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_id: Mapped[int | None] = mapped_column(BigInteger)
    fencing_token: Mapped[int | None] = mapped_column(BigInteger)
