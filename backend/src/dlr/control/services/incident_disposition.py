"""Transactional manual disposition of persisted infrastructure Incidents."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.common.jcs import canonicalize
from dlr.control.models import (
    Execution,
    ExecutionIncidentDisposition,
    ExecutionInfrastructureIncident,
)
from dlr.control.schemas.reliable_runtime import IncidentDispositionBody
from dlr.control.services.adapter import domain_error


@dataclass(frozen=True)
class DispositionIdempotency:
    """Canonical request identity and an optional existing durable receipt."""

    key: uuid.UUID
    request_hash: str
    receipt: ExecutionIncidentDisposition | None


def request_hash(body: IncidentDispositionBody) -> str:
    """Hash only the closed disposition intent, never actor or material facts."""

    canonical = canonicalize(
        {
            "action": body.action,
            "expected_generation": body.expected_generation,
            "reason_code": body.reason_code,
        }
    )
    return hashlib.sha256(canonical).hexdigest()


def lookup_idempotency(
    session: Session,
    *,
    incident_id: int,
    idempotency_key: uuid.UUID,
    body: IncidentDispositionBody,
) -> DispositionIdempotency:
    """Lock and compare a receipt before mutable generation/material checks."""

    digest = request_hash(body)
    receipt = session.scalar(
        select(ExecutionIncidentDisposition)
        .where(
            ExecutionIncidentDisposition.incident_id == incident_id,
            ExecutionIncidentDisposition.idempotency_key == idempotency_key,
        )
        .with_for_update()
    )
    if receipt is not None and receipt.request_hash != digest:
        raise domain_error(
            409,
            "idempotency_key_conflict",
            "Idempotency-Key was already used for a different disposition request",
        )
    return DispositionIdempotency(idempotency_key, digest, receipt)


def converge_terminal_dispositions_locked(
    session: Session,
    execution: Execution,
    locked_incidents: tuple[ExecutionInfrastructureIncident, ...],
    *,
    now: datetime,
) -> int:
    """Resolve pending terminate receipts in place after a real terminal transition."""

    if execution.status not in {"succeeded", "dead_letter", "cancelled", "expired"}:
        return 0
    incident_by_id = {
        incident.id: incident
        for incident in locked_incidents
        if incident.execution_id == execution.id
    }
    if not incident_by_id:
        return 0
    receipts = tuple(
        session.scalars(
            select(ExecutionIncidentDisposition)
            .where(
                ExecutionIncidentDisposition.incident_id.in_(incident_by_id),
                ExecutionIncidentDisposition.action == "terminate",
                ExecutionIncidentDisposition.outcome == "cancellation_requested",
            )
            .order_by(
                ExecutionIncidentDisposition.created_at.asc(),
                ExecutionIncidentDisposition.id.asc(),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    code = execution.error_code or execution.last_error_code or f"execution_{execution.status}"
    for receipt in receipts:
        receipt.outcome = "execution_terminal"
        receipt.code = code[:64]
        receipt.execution_status = execution.status
        incident = incident_by_id[receipt.incident_id]
        incident.status = "resolved"
        incident.resolved_at = now
    return len(receipts)
