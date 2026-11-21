"""Digest-only result intent and experimental committed-finalization marker."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.model_base import Base


class AttemptResultIntentV4(Base):
    __tablename__ = "attempt_result_intents_v4"
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
            name="fk_attempt_result_intent_attempt_binding",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["worker_id", "instance_id", "instance_generation"],
            [
                "worker_instance_history.worker_id",
                "worker_instance_history.instance_id",
                "worker_instance_history.generation",
            ],
            name="fk_attempt_result_intent_instance",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("intent_id", name="uq_attempt_result_intent_v4_id"),
        UniqueConstraint("finalization_id", name="uq_attempt_result_finalization_v4_id"),
        CheckConstraint(
            "instance_generation > 0 AND fencing_token > 0 AND attempt_revision > 0 "
            "AND instance_revision > 0 AND writer_xid > 0",
            name="ck_attempt_result_intent_v4_positive",
        ),
        CheckConstraint("length(body_mac) = 32", name="ck_attempt_result_intent_v4_mac"),
        CheckConstraint(
            "(finalization_id IS NULL AND finalization_xid IS NULL "
            "AND final_attempt_status IS NULL AND final_execution_status IS NULL) OR "
            "(finalization_id IS NOT NULL AND finalization_xid IS NOT NULL "
            "AND finalization_xid > 0 AND finalization_xid = writer_xid "
            "AND final_attempt_status IS NOT NULL AND final_attempt_status IN "
            "('succeeded', 'failed', 'timed_out', 'cancelled', 'worker_lost', "
            "'resource_exceeded') AND final_execution_status IS NOT NULL "
            "AND final_execution_status IN "
            "('succeeded', 'dead_letter', 'cancelled', 'retry_wait'))",
            name="ck_attempt_result_finalization_v4_complete",
        ),
    )

    attempt_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    worker_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    worker_identity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    instance_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempt_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempt_candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    instance_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    instance_candidate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    intent_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    body_mac: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    proof_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    writer_xid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    finalization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    finalization_xid: Mapped[int | None] = mapped_column(BigInteger)
    final_attempt_status: Mapped[str | None] = mapped_column(String(32))
    final_execution_status: Mapped[str | None] = mapped_column(String(32))
