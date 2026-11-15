"""Durable idempotency and audit facts for administrator Worker maintenance."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class WorkerMaintenanceOperation(Base):
    __tablename__ = "worker_maintenance_operations"
    __table_args__ = (
        UniqueConstraint("worker_id", "result_version", name="uq_worker_maintenance_result"),
        CheckConstraint(
            "request_id <> '00000000-0000-0000-0000-000000000000'::uuid",
            name="ck_worker_maintenance_request_nonzero",
        ),
        CheckConstraint(
            "expected_version >= 1 AND result_version = expected_version + 1",
            name="ck_worker_maintenance_operation_version",
        ),
        CheckConstraint(
            "(action IN ('drain', 'activate') AND reason IS NULL AND recovery_binding IS NULL) OR "
            "(action = 'force_stop' AND reason IS NOT NULL AND "
            "length(reason) BETWEEN 1 AND 256 AND length(btrim(reason)) > 0 "
            "AND recovery_binding IS NULL) OR "
            "(action = 'reconcile' AND reason IS NULL AND recovery_binding IS NOT NULL "
            "AND jsonb_typeof(recovery_binding) = 'object')",
            name="ck_worker_maintenance_operation_action",
        ),
        CheckConstraint(
            "(actor_kind = 'superadmin' AND actor_id IS NULL) OR "
            "(actor_kind = 'account' AND actor_id IS NOT NULL AND actor_id > 0)",
            name="ck_worker_maintenance_operation_actor",
        ),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    worker_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workers.id", ondelete="RESTRICT"), nullable=False
    )
    expected_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    result_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(String(256))
    recovery_binding: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.pg_catalog.clock_timestamp()
    )
