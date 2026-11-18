"""Cleanup intent and experimental completion marker, not physical proof."""

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

from dlr.control.db import Base


class WorkerCleanupIntentV4(Base):
    __tablename__ = "worker_cleanup_intents_v4"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "attempt_id",
                "worker_id",
                "worker_identity_id",
                "original_instance_id",
                "original_generation",
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
            name="fk_worker_cleanup_intent_attempt_binding",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["worker_id", "instance_id", "instance_generation"],
            [
                "worker_instance_history.worker_id",
                "worker_instance_history.instance_id",
                "worker_instance_history.generation",
            ],
            name="fk_worker_cleanup_intent_instance",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("intent_id", name="uq_worker_cleanup_intent_v4_id"),
        UniqueConstraint("completion_id", name="uq_worker_cleanup_completion_v4_id"),
        CheckConstraint(
            "original_generation > 0 AND instance_generation > 0 AND fencing_token > 0 "
            "AND instance_revision > 0 AND writer_xid > 0",
            name="ck_worker_cleanup_intent_v4_positive",
        ),
        CheckConstraint(
            "(completion_id IS NULL AND completion_xid IS NULL) OR "
            "(completion_id IS NOT NULL AND completion_xid IS NOT NULL "
            "AND completion_xid > 0 AND completion_xid = writer_xid)",
            name="ck_worker_cleanup_completion_v4_complete",
        ),
    )

    attempt_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    worker_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    worker_identity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    original_instance_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    original_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    instance_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    instance_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    instance_candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    intent_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    proof_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    writer_xid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completion_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    completion_xid: Mapped[int | None] = mapped_column(BigInteger)
