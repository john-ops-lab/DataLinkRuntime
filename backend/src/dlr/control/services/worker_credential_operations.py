"""Administrator HTTP credential CAS and non-secret transactional receipts.

The router owns authorization, Control fences, commit and one-time delivery.
These operations never change maintenance, business ownership or local proofs.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import Session

from dlr.control.models import (
    ExecutionAttempt,
    Worker,
    WorkerCredential,
    WorkerCredentialAudit,
    WorkerIdentityReservation,
)
from dlr.control.security import Principal
from dlr.control.services import worker_credentials as credentials
from dlr.control.services.attempt import ACTIVE_ATTEMPT_STATUSES
from dlr.control.services.worker_identity_reservation import _actor


class OperationConflict(Exception):
    """Fixed non-secret conflict; caller rolls back the whole operation."""


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: UUID
    worker_identity_id: UUID
    expected_latest_credential_record_id: StrictInt = Field(gt=0, lt=2**63)

    @field_validator("request_id", "worker_identity_id")
    @classmethod
    def nonzero(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("nonzero identifier required")
        return value


class RotateRequest(RevokeRequest):
    expected_maintenance_version: StrictInt = Field(ge=1, lt=2**63 - 1)
    overlap_seconds: StrictInt = Field(default=600, ge=0, le=600)


class ReceiptMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1]
    worker_identity_id: UUID
    action: Literal["rotate", "revoke"]
    expected_latest_credential_record_id: StrictInt
    expected_maintenance_version: StrictInt | None
    overlap_seconds: StrictInt | None
    credential_record_id: StrictInt
    previous_credential_record_id: StrictInt | None
    previous_expires_at: datetime | None
    cluster_id: UUID
    authority_epoch: UUID
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


def worker(session: Session, worker_id: int, *, lock: bool) -> Worker:
    if not 0 < worker_id < 2**63:
        raise OperationConflict
    query = select(Worker).where(Worker.id == worker_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    result = session.scalar(query)
    if (
        result is None
        or result.protocol_version != 4
        or result.identity_lifecycle != "reserved"
        or result.identity_enrolled_at is None
        or result.identity_id is None
        or result.identity_id.int == 0
        or session.scalar(
            select(WorkerIdentityReservation.worker_id).where(
                WorkerIdentityReservation.worker_id == worker_id,
                WorkerIdentityReservation.name == result.name,
            )
        )
        is None
    ):
        raise OperationConflict
    return result


def latest(session: Session, worker_id: int) -> WorkerCredential:
    result = session.scalar(
        select(WorkerCredential)
        .where(WorkerCredential.worker_id == worker_id, WorkerCredential.kind == "runtime")
        .order_by(WorkerCredential.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if result is None:
        raise OperationConflict
    return result


def fingerprint(worker_id: int, action: str, payload: RevokeRequest) -> str:
    value = {"worker_id": worker_id, "action": action, **payload.model_dump(mode="json")}
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _unsettled(session: Session, row: Worker) -> bool:
    return bool(
        session.scalar(
            select(ExecutionAttempt.id)
            .where(
                or_(
                    ExecutionAttempt.worker_id == row.id,
                    ExecutionAttempt.worker_identity_id == row.identity_id,
                ),
                or_(
                    ExecutionAttempt.cleanup_settled_at.is_(None),
                    ExecutionAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
                    ExecutionAttempt.worker_id != row.id,
                    ExecutionAttempt.worker_identity_id.is_distinct_from(row.identity_id),
                ),
            )
            .limit(1)
        )
    )


def execute(
    session: Session,
    worker_id: int,
    payload: RevokeRequest,
    principal: Principal,
    *,
    action: Literal["rotate", "revoke"],
) -> tuple[WorkerCredentialAudit, credentials.IssuedWorkerCredential | None]:
    row = worker(session, worker_id, lock=True)
    actor_kind, actor_id = _actor(principal)
    digest = fingerprint(worker_id, action, payload)
    previous = session.scalar(
        select(WorkerCredentialAudit).where(WorkerCredentialAudit.request_id == payload.request_id)
    )
    if previous is not None:
        if (
            previous.worker_id != worker_id
            or previous.actor_kind != actor_kind
            or previous.actor_id != actor_id
            or previous.request_fingerprint != digest
        ):
            raise OperationConflict
        return previous, None
    current = latest(session, worker_id)
    if (
        row.identity_id != payload.worker_identity_id
        or current.id != payload.expected_latest_credential_record_id
    ):
        raise OperationConflict
    issued = None
    old_id = None
    deadline = None
    if action == "rotate":
        if (
            not isinstance(payload, RotateRequest)
            or row.scheduling_state != "disabled"
            or row.maintenance_version != payload.expected_maintenance_version
            or _unsettled(session, row)
            or not credentials._valid(current, credentials._now(session))
        ):
            raise OperationConflict
        # Detect corrupt/unsupported active sets without loading an unbounded history.
        valid_ids = list(
            session.scalars(
                select(WorkerCredential.id)
                .where(
                    WorkerCredential.worker_id == worker_id,
                    WorkerCredential.kind == "runtime",
                    WorkerCredential.revoked_at.is_(None),
                    or_(
                        WorkerCredential.expires_at.is_(None),
                        WorkerCredential.expires_at > func.pg_catalog.clock_timestamp(),
                    ),
                )
                .limit(3)
            )
        )
        if len(valid_ids) > 2:
            raise OperationConflict
        rotated = credentials.rotate_runtime_credential_with_previous(
            session, worker_id, overlap_seconds=payload.overlap_seconds
        )
        issued = rotated.issued
        record_id = issued.credential_id
        old_id, deadline = rotated.previous_credential_record_id, rotated.previous_expires_at
    else:
        # Worker lock serializes with all old enrollment/recover/rotation callers.
        session.execute(
            update(WorkerCredential)
            .where(
                WorkerCredential.worker_id == worker_id,
                WorkerCredential.kind == "runtime",
                WorkerCredential.revoked_at.is_(None),
            )
            .values(revoked_at=func.pg_catalog.clock_timestamp())
        )
        record_id = current.id
    authority = (
        session.execute(
            text(
                "SELECT cluster_id,authority_epoch,image_digest "
                "FROM control_cluster_authority WHERE singleton=1"
            )
        )
        .mappings()
        .one()
    )
    metadata = ReceiptMetadata(
        version=1,
        worker_identity_id=row.identity_id,
        action=action,
        expected_latest_credential_record_id=payload.expected_latest_credential_record_id,
        expected_maintenance_version=payload.expected_maintenance_version
        if isinstance(payload, RotateRequest)
        else None,
        overlap_seconds=payload.overlap_seconds if isinstance(payload, RotateRequest) else None,
        credential_record_id=record_id,
        previous_credential_record_id=old_id,
        previous_expires_at=deadline,
        **authority,
    ).model_dump(mode="json")
    if len(json.dumps(metadata).encode()) > 2048:
        raise OperationConflict
    receipt = WorkerCredentialAudit(
        worker_id=worker_id,
        credential_record_id=record_id,
        related_credential_record_id=old_id,
        event_kind=action,
        actor_kind=actor_kind,
        actor_id=actor_id,
        request_id=payload.request_id,
        request_fingerprint=digest,
        result_metadata=metadata,
    )
    session.add(receipt)
    session.flush()
    return receipt, issued


def read_receipt(
    session: Session,
    worker_id: int,
    request_id: UUID,
    principal: Principal,
) -> dict[str, object]:
    row = worker(session, worker_id, lock=False)
    receipt = session.scalar(
        select(WorkerCredentialAudit).where(WorkerCredentialAudit.request_id == request_id)
    )
    if receipt is None:
        return {"status": "not_observed", "operation_id": str(request_id)}
    if receipt.worker_id != worker_id or (receipt.actor_kind, receipt.actor_id) != _actor(
        principal
    ):
        raise OperationConflict
    metadata = ReceiptMetadata.model_validate(receipt.result_metadata)
    current = latest(session, worker_id)
    valid = credentials._valid(current, credentials._now(session))
    return {
        "status": "committed",
        "operation_id": str(request_id),
        "worker_id": worker_id,
        "metadata": metadata.model_dump(mode="json"),
        "currently_usable": bool(
            valid
            and current.id == receipt.credential_record_id
            and row.identity_id == metadata.worker_identity_id
        ),
    }
