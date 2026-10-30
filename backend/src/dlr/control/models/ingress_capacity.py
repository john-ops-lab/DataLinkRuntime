"""Deployment capacity configuration; contains no source credentials or payloads."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class IngressCapacityConfiguration(Base):
    __tablename__ = "ingress_capacity_configurations"
    __table_args__ = (CheckConstraint("id = 1", name="ck_ingress_capacity_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    adopted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IngressCapacityAudit(Base):
    __tablename__ = "ingress_capacity_audits"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    previous_fingerprint: Mapped[str | None] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
