"""Durable, non-secret administrator request for a new Worker identity."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class WorkerIdentityReservation(Base):
    __tablename__ = "worker_identity_reservations"
    __table_args__ = (
        CheckConstraint(
            "(actor_kind = 'superadmin' AND actor_id IS NULL) OR "
            "(actor_kind = 'account' AND actor_id IS NOT NULL AND actor_id > 0)",
            name="ck_worker_identity_reservation_actor",
        ),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    worker_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workers.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
