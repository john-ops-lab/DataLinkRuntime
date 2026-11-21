"""Latest untrusted Worker self-report, not a placement authority."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.model_base import Base


class WorkerCapabilityObservation(Base):
    __tablename__ = "worker_capability_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["worker_id", "worker_identity_id"],
            ["workers.id", "workers.identity_id"],
            name="fk_worker_observation_identity",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "instance_generation > 0 AND report_version > 0", name="ck_worker_observation_versions"
        ),
        CheckConstraint("evidence_state = 'self_reported'", name="ck_worker_observation_evidence"),
        CheckConstraint(
            "jsonb_typeof(observation) = 'object' AND octet_length(observation::text) <= 8192",
            name="ck_worker_observation_payload",
        ),
    )

    worker_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workers.id", ondelete="RESTRICT"), primary_key=True
    )
    worker_identity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    instance_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    report_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence_state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'self_reported'")
    )
