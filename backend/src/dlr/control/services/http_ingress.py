"""HTTP request identities feed the existing Receipt/Delivery authority.

The caller owns commit. No response indicating custody may precede that commit.
Routing identifiers are permanent and are never credentials.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import Credential, Execution
from dlr.control.models.subscription import (
    EventReceipt,
    EventSubscription,
    HttpIngressRoute,
    SubscriptionLease,
)
from dlr.control.security import _bearer_token
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_management as management
from dlr.control.services.adapter import domain_error
from dlr.control.services.event_envelope import canonical
from dlr.control.services.input_config import database_now
from dlr.control.services.secrets import decrypt_fields
from dlr.control.services.webhook import (
    _parse_finite_float,
    _reject_non_standard_constant,
    _require_jsonb_persistable,
)

MAX_BODY_BYTES = 64 * 1024
MAX_ENVELOPE_BYTES = 256 * 1024
MAX_DEPTH = 32
MAX_NODES = 4096


def ingress_path(session: Session, subscription: EventSubscription) -> str | None:
    if subscription.source_type != "http":
        return None
    public_id = session.scalar(
        select(HttpIngressRoute.public_id).where(
            HttpIngressRoute.subscription_id == subscription.id,
            HttpIngressRoute.source_epoch == subscription.source_epoch,
        )
    )
    return f"/api/ingress/{public_id}" if public_id else None


def require_key(value: str | None) -> str:
    if value is None or not 1 <= len(value) <= 128 or any(not 32 <= ord(c) <= 126 for c in value):
        raise domain_error(
            422, "ingress_id_invalid", "Idempotency-Key requires 1-128 printable ASCII bytes"
        )
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _parse_http_float(text: str) -> float:
    value = _parse_finite_float(text)
    # JSONB expands positive exponents to integers on readback. Values outside
    # JCS's safe integer domain would then fail T2 despite a successful T1.
    if value.is_integer() and abs(value) > 9007199254740991:
        raise ValueError("JSON number cannot round-trip through the event storage domain")
    return value


def parse_body(body: bytes) -> Any:
    if len(body) > MAX_BODY_BYTES:
        raise domain_error(413, "ingress_body_too_large", "JSON body exceeds 64 KiB")
    try:
        payload = json.loads(
            body.decode("utf-8"),
            parse_float=_parse_http_float,
            parse_constant=_reject_non_standard_constant,
            object_pairs_hook=_unique_object,
        )
        _require_jsonb_persistable(payload)
    except (ValueError, RecursionError, UnicodeError):
        raise domain_error(
            422, "ingress_json_invalid", "Body must be losslessly storable UTF-8 JSON"
        ) from None
    stack = [(payload, 1)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise domain_error(
                413, "ingress_structure_too_large", "JSON structure exceeds the configured boundary"
            )
        if isinstance(value, dict):
            # Object member names count too, so maps cannot bypass the node cap.
            stack.extend((child, depth + 1) for child in value.values())
            nodes += len(value)
            if nodes > MAX_NODES:
                raise domain_error(
                    413,
                    "ingress_structure_too_large",
                    "JSON structure exceeds the configured boundary",
                )
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
    # The existing Envelope contract also bounds numeric values to lossless JCS.
    canonical(payload)
    return payload


def _authenticated(
    session: Session,
    public_id: str,
    authorization: str | None,
) -> tuple[HttpIngressRoute, EventSubscription]:
    route = session.get(HttpIngressRoute, public_id)
    if route is None or route.subscription_id is None:
        raise domain_error(404, "ingress_not_found", "Ingress is unavailable")
    _, sub = delivery.lock_subscription(session, route.subscription_id)
    if sub.deleted_at is not None or sub.source_type != "http":
        raise domain_error(404, "ingress_not_found", "Ingress is unavailable")
    current = management.current_config(session, sub)
    credential = (
        session.get(Credential, current.source_credential_id, with_for_update=True)
        if current.source_credential_id
        else None
    )
    provided = _bearer_token(authorization)
    try:
        expected = (
            decrypt_fields(credential.ciphertext).get("token")
            if credential and credential.type == "token"
            else None
        )
    except Exception:
        expected = None
    if not provided or not expected or not secrets.compare_digest(provided, expected):
        raise domain_error(401, "unauthorized", "Invalid or missing bearer token")
    return route, sub


def _status(session: Session, public_id: str, receipt: EventReceipt) -> dict[str, Any]:
    from dlr.control.services.subscription_batch_view import delivery_records

    records = delivery_records(session, receipt, latest_only=True)
    latest = records[0] if records else {}
    execution_id = latest.get("execution_id")
    execution = session.get(Execution, execution_id) if execution_id else None
    error_code = (execution.error_code or execution.last_error_code) if execution else None
    if latest.get("processing_mode") == "business_batch":
        error_code = latest.get("batch_member_code")
    if error_code is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
        error_code = "execution_failed"
    return {
        "receipt_id": receipt.id,
        "received_at": receipt.first_received_at.isoformat(),
        "processing_status": receipt.state,
        "execution_id": execution_id,
        **{
            key: latest.get(key)
            for key in (
                "processing_mode",
                "batch_execution_id",
                "batch_member_index",
                "batch_member_count",
                "batch_member_outcome",
                "batch_member_code",
            )
        },
        "error_code": error_code,
        "status_url": f"/api/ingress/{public_id}/receipts/{receipt.id}",
        "dedupe_retention_seconds": 30 * 86400,
        "raw_retention_seconds": 7 * 86400,
    }


def receipt_status(
    session: Session, public_id: str, authorization: str | None, receipt_id: int
) -> dict[str, Any]:
    route, sub = _authenticated(session, public_id, authorization)
    receipt = session.scalar(
        select(EventReceipt).where(
            EventReceipt.id == receipt_id,
            EventReceipt.subscription_id == sub.id,
            EventReceipt.source_epoch == route.source_epoch,
        )
    )
    if receipt is None:
        raise domain_error(404, "ingress_receipt_not_found", "Receipt is unavailable")
    return _status(session, public_id, receipt)


def receive(
    session: Session,
    public_id: str,
    authorization: str | None,
    key: str | None,
    body: bytes,
) -> tuple[int, dict[str, Any]]:
    route, sub = _authenticated(session, public_id, authorization)
    identity = require_key(key)
    digest = hashlib.sha256(body).hexdigest()
    # Retained records are available despite disabled reception, a full quota,
    # an expired native lease, or an unavailable internal RabbitMQ.
    existing = session.scalar(
        select(EventReceipt)
        .where(
            EventReceipt.subscription_id == sub.id,
            EventReceipt.source_epoch == route.source_epoch,
            EventReceipt.source_event_id == identity,
        )
        .with_for_update()
    )
    if existing is not None:
        if existing.content_hash != digest:
            raise domain_error(
                409,
                "event_identity_conflict",
                "The request ID already has different original bytes",
            )
        existing.duplicate_count += 1
        existing.last_seen_at = database_now(session)
        return 200, _status(session, public_id, existing)
    if route.source_epoch != sub.source_epoch:
        raise domain_error(410, "ingress_epoch_retired", "Use the current ingress for new requests")
    if not settings.http_ingress_enabled or not settings.event_subscriptions_enabled:
        raise domain_error(503, "ingress_unavailable", "New reception is temporarily unavailable")
    if sub.desired_state != "enabled":
        raise domain_error(409, "ingress_disabled", "Receiving is stopped")
    config = management.current_config(session, sub)
    lease = session.get(SubscriptionLease, sub.id)
    if (
        lease is None
        or not lease.owner_id
        or lease.closing_generation is not None
        or lease.lease_until <= database_now(session)
        or sub.connection_state != "listening"
    ):
        raise domain_error(503, "ingress_unavailable", "Reception owner is not ready")
    payload = parse_body(body)
    envelope = {
        "schema": "dlr.event.v1",
        "subscription": {
            "id": str(sub.id),
            "config_version": config.version,
            "source_epoch": route.source_epoch,
        },
        "event": {"id": identity, "kind": "message", "object_key": None},
        "source": {
            "type": "http",
            "identity": public_id,
            "position": {"idempotency_key": identity},
            "attributes": {"public_id": public_id},
        },
        "received_at": datetime.now(UTC).isoformat(),
        "data": {"encoding": "json", "content_type": "application/json", "payload": payload},
        "sync": None,
    }
    if (
        len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode())
        > MAX_ENVELOPE_BYTES
    ):
        raise domain_error(413, "ingress_envelope_too_large", "Complete event exceeds 256 KiB")
    try:
        receipt, duplicate = delivery.receive_event(
            session,
            sub.id,
            lease.owner_id,
            lease.generation,
            envelope,
            "requests",
            {"idempotency_key": identity},
            source_raw_body=body,
            content_hash_override=digest,
            prevent_conflict_block=True,
        )
    except HTTPException as error:
        code = error.detail.get("code") if isinstance(error.detail, dict) else None
        if code in {
            "subscription_owner_stale",
            "subscription_config_conflict",
            "subscription_activation_missing",
            "source_not_available",
        }:
            raise domain_error(
                503, "ingress_unavailable", "Reception configuration is not ready"
            ) from None
        raise
    assert receipt is not None
    session.flush()
    return (200 if duplicate else 202), _status(session, public_id, receipt)
