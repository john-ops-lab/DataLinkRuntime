"""Hash-only Idempotency-Key persistence for reliable ingress."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.common.jcs import (
    CanonicalizationInputError,
    key_hash,
    payload_hash,
    validate_idempotency_key,
)
from dlr.control.models import Execution, ExecutionIdempotencyRecord
from dlr.control.services.adapter import domain_error


@dataclass(frozen=True)
class IdempotencyLookup:
    key_hash: bytes
    payload_hash: bytes
    record: ExecutionIdempotencyRecord | None


def normalize_key(value: str | None) -> str | None:
    """Return a validated optional header; raw values never enter logs/DB."""

    if value is None:
        return None
    try:
        return validate_idempotency_key(value)
    except ValueError as exc:
        raise domain_error(422, "idempotency_key_invalid", str(exc)) from None


def lookup(
    session: Session,
    adapter_id: int,
    trigger: str,
    body: Any,
    key: str | None,
) -> IdempotencyLookup:
    """Lock and compare an existing record before resolving mutable config."""

    normalized = normalize_key(key)
    try:
        request_hash = payload_hash(trigger, body)
    except CanonicalizationInputError:
        raise domain_error(
            422,
            "input_invalid",
            "Request input is outside the canonical JSON number domain",
        ) from None
    if normalized is None:
        return IdempotencyLookup(b"", request_hash, None)
    raw_key_hash = key_hash(normalized)
    record = session.scalar(
        select(ExecutionIdempotencyRecord)
        .where(
            ExecutionIdempotencyRecord.adapter_id == adapter_id,
            ExecutionIdempotencyRecord.key_hash == raw_key_hash,
        )
        .with_for_update()
    )
    if record is not None and record.payload_hash != request_hash:
        raise domain_error(
            409,
            "idempotency_conflict",
            "Idempotency-Key was already used for a different request",
        )
    return IdempotencyLookup(raw_key_hash, request_hash, record)


def create_record(
    session: Session,
    *,
    adapter_id: int,
    execution_id: int,
    key_digest: bytes,
    request_digest: bytes,
    now: datetime | None = None,
) -> ExecutionIdempotencyRecord:
    """Create one record inside the same transaction as its Execution."""

    if len(key_digest) != 32 or len(request_digest) != 32:
        raise ValueError("Idempotency digests must be SHA-256 values")
    if now is None:
        from dlr.control.services.input_config import database_now

        effective_now = database_now(session)
    else:
        effective_now = now
    record = ExecutionIdempotencyRecord(
        adapter_id=adapter_id,
        key_hash=key_digest,
        payload_hash=request_digest,
        execution_id=execution_id,
        created_at=effective_now,
        expires_at=effective_now + timedelta(seconds=settings.idempotency_retention_seconds),
    )
    session.add(record)
    session.flush()
    return record


def cleanup_expired_records(
    session: Session, *, now: datetime | None = None, limit: int = 100
) -> int:
    """Delete expired records only after their associated Execution is terminal."""

    if now is None:
        from dlr.control.services.input_config import database_now

        effective_now = database_now(session)
    else:
        effective_now = now
    ids = list(
        session.scalars(
            select(ExecutionIdempotencyRecord.id)
            .join(Execution, Execution.id == ExecutionIdempotencyRecord.execution_id)
            .where(
                ExecutionIdempotencyRecord.expires_at <= effective_now,
                Execution.status.in_(
                    ("succeeded", "failed", "timeout", "dead_letter", "cancelled", "expired")
                ),
            )
            .order_by(ExecutionIdempotencyRecord.expires_at, ExecutionIdempotencyRecord.id)
            .limit(max(1, min(limit, 1000)))
        )
    )
    if not ids:
        return 0
    session.execute(
        delete(ExecutionIdempotencyRecord).where(ExecutionIdempotencyRecord.id.in_(ids))
    )
    session.commit()
    return len(ids)
