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

from dlr.control.model_base import Base

_RESULT_DISPOSITION = """
    (recovery_disposition_id IS NULL AND recovery_disposition_xid IS NULL
     AND recovery_disposition IS NULL) OR
    (recovery_disposition_id IS NOT NULL
     AND recovery_disposition_id <> '00000000-0000-0000-0000-000000000000'::uuid
     AND recovery_disposition_xid > 0 AND recovery_disposition_xid IS NOT NULL
     AND recovery_disposition IS NOT NULL AND action = 'reconcile'
     AND (jsonb_typeof(recovery_disposition) = 'object'
      AND recovery_disposition ?& ARRAY['kind','reason','stopped_sha256','result_intent_id',
          'body_mac','attempt_status','execution_status','acknowledgement',
          'dispatch_generation','attempt_no']
      AND recovery_disposition - ARRAY['kind','reason','stopped_sha256','result_intent_id',
          'body_mac','attempt_status','execution_status','acknowledgement',
          'dispatch_generation','attempt_no'] = '{}'::jsonb
      AND recovery_disposition->>'kind' = 'platform_terminal'
      AND recovery_disposition->>'reason' = 'result_uncommitted_outcome_unknown'
      AND recovery_disposition->>'acknowledgement' = 'result_uncommitted_outcome_unknown'
      AND recovery_disposition->>'stopped_sha256' ~ '^[0-9a-f]{64}$'
      AND recovery_disposition->>'body_mac' ~ '^[0-9a-f]{64}$'
      AND recovery_disposition->>'result_intent_id' ~
          '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      AND recovery_disposition->>'result_intent_id' <>
          '00000000-0000-0000-0000-000000000000'
      AND ((recovery_disposition->>'attempt_status' = 'worker_lost'
            AND recovery_disposition->>'execution_status'
                IN ('dead_letter','cancelled','retry_wait'))
           OR (recovery_disposition->>'attempt_status' = 'cancelled'
               AND recovery_disposition->>'execution_status' = 'cancelled'))
      AND jsonb_typeof(recovery_disposition->'dispatch_generation') = 'number'
      AND jsonb_typeof(recovery_disposition->'attempt_no') = 'number'
      AND recovery_disposition->>'dispatch_generation' ~ '^[1-9][0-9]{0,18}$'
      AND recovery_disposition->>'attempt_no' ~ '^[1-9][0-9]{0,18}$'
     ) IS TRUE)
"""

_FORCE_DISPOSITION = """
    recovery_disposition_id IS NOT NULL
    AND recovery_disposition_id <> '00000000-0000-0000-0000-000000000000'::uuid
    AND recovery_disposition_xid IS NOT NULL AND recovery_disposition_xid > 0
    AND action = 'reconcile'
    AND jsonb_typeof(recovery_disposition) = 'object'
    AND recovery_disposition ?& ARRAY['kind','reason','stopped_sha256','force_stop_operation_id',
        'attempt_status','execution_status','acknowledgement','dispatch_generation','attempt_no']
    AND recovery_disposition - ARRAY['kind','reason','stopped_sha256','force_stop_operation_id',
        'attempt_status','execution_status','acknowledgement','dispatch_generation','attempt_no']
        = '{}'::jsonb
    AND recovery_disposition->>'kind' = 'force_stopped_no_result'
    AND recovery_disposition->>'reason' = 'force_stop_no_result_outcome_unknown'
    AND recovery_disposition->>'acknowledgement' = 'force_stop_no_result_outcome_unknown'
    AND recovery_disposition->>'stopped_sha256' ~ '^[0-9a-f]{64}$'
    AND recovery_disposition->>'force_stop_operation_id' ~
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    AND recovery_disposition->>'force_stop_operation_id' <>
        '00000000-0000-0000-0000-000000000000'
    AND ((recovery_disposition->>'attempt_status' = 'worker_lost'
          AND recovery_disposition->>'execution_status' IN ('dead_letter','cancelled','retry_wait'))
         OR (recovery_disposition->>'attempt_status' = 'cancelled'
             AND recovery_disposition->>'execution_status' = 'cancelled'))
    AND jsonb_typeof(recovery_disposition->'dispatch_generation') = 'number'
    AND jsonb_typeof(recovery_disposition->'attempt_no') = 'number'
    AND recovery_disposition->>'dispatch_generation' ~ '^[1-9][0-9]{0,18}$'
    AND recovery_disposition->>'attempt_no' ~ '^[1-9][0-9]{0,18}$'
"""
DISPOSITION_CHECK = f"({_RESULT_DISPOSITION}) OR (({_FORCE_DISPOSITION}) IS TRUE)"

_FORCE_BINDING = """
    jsonb_typeof(recovery_binding) = 'object'
    AND recovery_binding ?& ARRAY['attempt_id','fencing_token','worker_identity_id',
        'original_instance_id','original_instance_generation']
    AND recovery_binding - ARRAY['attempt_id','fencing_token','worker_identity_id',
        'original_instance_id','original_instance_generation'] = '{}'::jsonb
    AND recovery_binding->>'worker_identity_id' ~
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    AND recovery_binding->>'worker_identity_id' <> '00000000-0000-0000-0000-000000000000'
    AND recovery_binding->>'original_instance_id' ~
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    AND recovery_binding->>'original_instance_id' <> '00000000-0000-0000-0000-000000000000'
    AND recovery_binding->>'attempt_id' ~ '^[1-9][0-9]{0,18}$'
    AND jsonb_typeof(recovery_binding->'attempt_id') = 'number'
    AND recovery_binding->>'fencing_token' ~ '^[1-9][0-9]{0,18}$'
    AND jsonb_typeof(recovery_binding->'fencing_token') = 'number'
    AND recovery_binding->>'original_instance_generation' ~ '^[1-9][0-9]{0,18}$'
    AND jsonb_typeof(recovery_binding->'original_instance_generation') = 'number'
"""
ACTION_CHECK = (
    "(action IN ('drain', 'activate') AND reason IS NULL AND recovery_binding IS NULL) OR "
    "(action = 'force_stop' AND reason IS NOT NULL AND "
    "length(reason) BETWEEN 1 AND 256 AND length(btrim(reason)) > 0 "
    f"AND (({_FORCE_BINDING}) IS TRUE)) OR "
    "(action = 'reconcile' AND reason IS NULL AND recovery_binding IS NOT NULL "
    "AND jsonb_typeof(recovery_binding) = 'object')"
)


class WorkerMaintenanceOperation(Base):
    __tablename__ = "worker_maintenance_operations"
    __table_args__ = (
        UniqueConstraint("worker_id", "result_version", name="uq_worker_maintenance_result"),
        UniqueConstraint("recovery_disposition_id", name="uq_worker_recovery_disposition"),
        CheckConstraint(DISPOSITION_CHECK, name="ck_worker_recovery_disposition_complete"),
        CheckConstraint(
            "request_id <> '00000000-0000-0000-0000-000000000000'::uuid",
            name="ck_worker_maintenance_request_nonzero",
        ),
        CheckConstraint(
            "expected_version >= 1 AND result_version = expected_version + 1",
            name="ck_worker_maintenance_operation_version",
        ),
        CheckConstraint(ACTION_CHECK, name="ck_worker_maintenance_operation_action"),
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
    recovery_disposition_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    recovery_disposition_xid: Mapped[int | None] = mapped_column(BigInteger)
    recovery_disposition: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.pg_catalog.clock_timestamp()
    )
