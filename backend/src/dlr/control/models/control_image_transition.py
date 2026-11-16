"""Append-only image transition audit; never business execution authority."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class ControlImageTransition(Base):
    __tablename__ = "control_image_transitions"
    __table_args__ = (
        CheckConstraint("writer_xid > 0", name="ck_control_image_transition_xid"),
        CheckConstraint(
            "operation_id <> '00000000-0000-0000-0000-000000000000'::uuid "
            "AND cluster_id <> '00000000-0000-0000-0000-000000000000'::uuid "
            "AND authority_epoch <> '00000000-0000-0000-0000-000000000000'::uuid",
            name="ck_control_image_transition_ids",
        ),
        CheckConstraint(
            "source_digest ~ '^sha256:[0-9a-f]{64}$' "
            "AND target_digest ~ '^sha256:[0-9a-f]{64}$' AND source_digest <> target_digest "
            "AND intent_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_control_image_transition_digests",
        ),
        Index(
            "uq_control_image_transition_root",
            text("(true)"),
            unique=True,
            postgresql_where=text("previous_id IS NULL"),
        ),
    )

    operation_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    cluster_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    authority_epoch: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    previous_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("control_image_transitions.operation_id", ondelete="RESTRICT"), unique=True
    )
    source_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    target_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    source_schema: Mapped[str] = mapped_column(String(32), nullable=False)
    target_schema: Mapped[str] = mapped_column(String(32), nullable=False)
    intent_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    writer_xid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
