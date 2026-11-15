"""Durable process identity used only by the isolated Control protocol."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, LargeBinary, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class ControlProcessInstance(Base):
    __tablename__ = "control_process_instances"
    __table_args__ = (
        CheckConstraint(
            "octet_length(instance_token_digest) = 32", name="ck_control_instance_token_digest"
        ),
    )

    instance_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    cluster_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    authority_epoch: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    instance_token_digest: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    image_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    managed_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    builtin_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    attachment_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    capacity_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
