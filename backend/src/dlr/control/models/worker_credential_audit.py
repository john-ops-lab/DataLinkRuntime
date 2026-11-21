"""Transactional audit facts for the Worker credential lifecycle."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.model_base import Base


class WorkerCredentialAudit(Base):
    """Record credential identifiers and actor, never a secret or its digest."""

    __tablename__ = "worker_credential_audit"
    __table_args__ = (
        ForeignKeyConstraint(
            ["worker_id", "credential_record_id"],
            ["worker_credentials.worker_id", "worker_credentials.id"],
            name="fk_worker_credential_audit_record",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["worker_id", "related_credential_record_id"],
            ["worker_credentials.worker_id", "worker_credentials.id"],
            name="fk_worker_credential_audit_related",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "event_kind IN ('enrollment', 'exchange', 'rotate', 'revoke', 'recover')",
            name="ck_worker_credential_audit_event",
        ),
        CheckConstraint(
            "(actor_kind = 'superadmin' AND actor_id IS NULL) OR "
            "(actor_kind = 'account' AND actor_id IS NOT NULL AND actor_id > 0) OR "
            "(actor_kind = 'worker' AND actor_id IS NULL)",
            name="ck_worker_credential_audit_actor",
        ),
        CheckConstraint(
            "(event_kind = 'exchange' AND actor_kind = 'worker') OR "
            "(event_kind <> 'exchange' AND actor_kind <> 'worker')",
            name="ck_worker_credential_audit_exchange_actor",
        ),
        CheckConstraint(
            "(event_kind IN ('exchange', 'rotate') AND related_credential_record_id IS NOT NULL "
            "AND related_credential_record_id <> credential_record_id) OR "
            "(event_kind IN ('enrollment', 'revoke', 'recover') "
            "AND related_credential_record_id IS NULL)",
            name="ck_worker_credential_audit_related_shape",
        ),
        CheckConstraint(
            "(request_id IS NULL AND request_fingerprint IS NULL AND result_metadata IS NULL) OR "
            "(request_id IS NOT NULL AND "
            "request_id <> '00000000-0000-0000-0000-000000000000'::uuid "
            "AND request_fingerprint IS NOT NULL AND request_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND result_metadata IS NOT NULL AND jsonb_typeof(result_metadata) = 'object' "
            "AND octet_length(result_metadata::text) <= 2048 "
            "AND event_kind IN ('rotate', 'revoke'))",
            name="ck_worker_credential_operation",
        ),
        Index("ix_worker_credential_audit_worker", "worker_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    worker_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credential_record_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    related_credential_record_id: Mapped[int | None] = mapped_column(BigInteger)
    event_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.pg_catalog.clock_timestamp()
    )

    request_id: Mapped[UUID | None] = mapped_column(Uuid, unique=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    result_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
