"""Adapter Schedule persistence model (M5.2).

Contracts kept by this model:

- At most one Schedule per Adapter (``adapter_id`` is unique); no generic
  Trigger table and no Schedule history table — Executions are the run
  history.
- The Schedule row is deployment configuration of the Adapter: it is removed
  with the Adapter (ON DELETE CASCADE) and never participates in the
  execution-history delete protection.
- ``next_run_at`` is the single scheduler cursor, persisted timezone-aware
  (timestamptz). ``NULL`` means the Schedule is disabled.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class AdapterSchedule(Base):
    """The single Cron Schedule of one Adapter (singleton per Adapter)."""

    __tablename__ = "adapter_schedules"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    adapter_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("adapters.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # Validated 5-field cron expression (minute hour dom month dow).
    cron: Mapped[str] = mapped_column(Text, nullable=False)
    # Validated IANA timezone name; the business timezone of the cron.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    # Execution input snapshot contract: any JSON value, including JSON null.
    input: Mapped[object] = mapped_column(
        JSONB, nullable=False, default=None, server_default=text("'null'::jsonb")
    )
    # A structural input failure consumes one due point and remains visible
    # for repair; the legacy ``input`` column stays during the compatibility
    # window and is not the Scheduler's long-term source of truth.
    last_blocked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_blocked_detail: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    last_blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_processed_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Next planned point in UTC; NULL while disabled.
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
