"""Immutable used-instance identities for Worker generation fencing."""

import uuid

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.model_base import Base


class WorkerInstanceHistory(Base):
    __tablename__ = "worker_instance_history"
    __table_args__ = (
        CheckConstraint("generation > 0", name="ck_worker_instance_history_generation"),
        UniqueConstraint("worker_id", "generation", name="uq_worker_instance_history_generation"),
        UniqueConstraint(
            "worker_id", "instance_id", "generation", name="uq_worker_instance_history_binding"
        ),
    )

    worker_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workers.id", ondelete="RESTRICT"), primary_key=True
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
