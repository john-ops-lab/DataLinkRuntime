"""Bounded Attempt lease candidate; never authorization by itself."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.model_base import Base


class AttemptLeaseCandidateV4(Base):
    __tablename__ = "attempt_lease_candidates_v4"
    __table_args__ = (
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
            name="fk_attempt_lease_candidate_attempt_binding",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["worker_id", "instance_id", "instance_generation"],
            [
                "worker_instance_history.worker_id",
                "worker_instance_history.instance_id",
                "worker_instance_history.generation",
            ],
            name="fk_attempt_lease_candidate_instance",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("candidate_id", name="uq_attempt_lease_candidate_v4_id"),
        CheckConstraint(
            "instance_generation > 0 AND fencing_token > 0 AND revision > 0 AND writer_xid > 0",
            name="ck_attempt_lease_candidate_v4_positive",
        ),
        CheckConstraint("proof_before <= guard_until", name="ck_attempt_lease_candidate_v4_guard"),
    )

    attempt_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    worker_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    worker_identity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    instance_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    nominal_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    guard_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    proof_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    writer_xid: Mapped[int] = mapped_column(BigInteger, nullable=False)
