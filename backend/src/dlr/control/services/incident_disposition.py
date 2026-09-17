"""Transactional manual disposition of persisted infrastructure Incidents."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.common.jcs import canonicalize
from dlr.control.models import ExecutionIncidentDisposition
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
