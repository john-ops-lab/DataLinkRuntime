"""Current, bounded Worker lease candidate; not an authorization by itself."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.model_base import Base


class WorkerLeaseCandidateV4(Base):
    __tablename__ = "worker_lease_candidates_v4"
    __table_args__ = (
        ForeignKeyConstraint(
            ["worker_id", "worker_identity_id"],
            ["workers.id", "workers.identity_id"],
            name="fk_worker_lease_candidate_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["worker_id", "instance_id", "instance_generation"],
            [
                "worker_instance_history.worker_id",
                "worker_instance_history.instance_id",
                "worker_instance_history.generation",
            ],
            name="fk_worker_lease_candidate_instance",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("candidate_id", name="uq_worker_lease_candidate_v4_id"),
        CheckConstraint(
            "instance_generation > 0 AND revision > 0 AND writer_xid > 0",
            name="ck_worker_lease_candidate_v4_positive",
        ),
        CheckConstraint("proof_before <= guard_until", name="ck_worker_lease_candidate_v4_guard"),
    )

    worker_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    worker_identity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    instance_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    nominal_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    guard_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    proof_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    writer_xid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    retired: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
