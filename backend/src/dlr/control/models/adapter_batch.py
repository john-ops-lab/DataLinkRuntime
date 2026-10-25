"""Small durable receipts for bounded Adapter operations, without storing user input."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class AdapterBatch(Base):
    __tablename__ = "adapter_batches"
    __table_args__ = (
        CheckConstraint("action IN ('start', 'stop')", name="ck_adapter_batch_action"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdapterBatchItem(Base):
    __tablename__ = "adapter_batch_items"
    __table_args__ = (
        CheckConstraint("adapter_id > 0", name="ck_adapter_batch_item_adapter"),
        CheckConstraint("position BETWEEN 0 AND 99", name="ck_adapter_batch_item_position"),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'stopping', 'succeeded', 'no_change', 'failed')",
            name="ck_adapter_batch_item_status",
        ),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("adapter_batches.id", ondelete="CASCADE"), primary_key=True
    )
    # Receipts outlive deleted Adapters, Executions and subscriptions. Reading
    # them still requires current Adapter access; these are deliberately not FKs.
    adapter_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_token: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    execution_ids: Mapped[list[int]] = mapped_column(JSONB, nullable=False, default=list)
    subscription_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    activation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
