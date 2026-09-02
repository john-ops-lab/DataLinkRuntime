"""Infrastructure DLQ reconciliation for RabbitMQ dispatch envelopes.

Business failures never use this path.  A DLQ delivery is first recorded as a
durable Incident, then either the current queued generation is returned to the
PostgreSQL Outbox or the message is left as an explicit manual-review fact.
The Broker message is acknowledged only after that database transaction
commits, so an interrupted reconciler cannot leave a Broker-only orphan.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    Execution,
    ExecutionInfrastructureIncident,
    ExecutionOutbox,
)
from dlr.control.services import outbox, rabbitmq
from dlr.control.services.dispatch import INFRASTRUCTURE_DLQ, deserialize_dispatch_message
from dlr.control.services.input_config import database_now

logger = logging.getLogger("dlr.control.infrastructure_dlq")
DLQ_POLL_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class InfrastructureReconcileResult:
    action: str
    kind: str
    incident_id: int
    execution_id: int | None
    dispatch_generation: int | None


def _json_mapping(body: bytes) -> Mapping[str, Any]:
    try:
        raw = json.loads(body)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, Mapping) else {}


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _message_id(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        return None


def _has_delivery_limit(headers: object) -> bool:
    if not isinstance(headers, Mapping):
        return False
    death = headers.get("x-death")
    if isinstance(death, list) and death:
        return True
    return any(str(key).lower() in {"x-delivery-limit", "delivery-limit"} for key in headers)


def _incident(
    session: Session,
    *,
    kind: str,
    message_id: uuid.UUID | None,
    execution_id: int | None,
    dispatch_generation: int | None,
) -> ExecutionInfrastructureIncident:
    query = select(ExecutionInfrastructureIncident).where(
        ExecutionInfrastructureIncident.kind == kind,
        ExecutionInfrastructureIncident.status == "open",
    )
    if message_id is not None:
        query = query.where(ExecutionInfrastructureIncident.message_id == message_id)
    elif execution_id is not None:
        query = query.where(
            ExecutionInfrastructureIncident.execution_id == execution_id,
            ExecutionInfrastructureIncident.dispatch_generation == dispatch_generation,
        )
    incident = session.scalar(query.with_for_update())
    if incident is None:
        incident = ExecutionInfrastructureIncident(
            kind=kind,
            message_id=message_id,
            execution_id=execution_id,
            dispatch_generation=dispatch_generation,
            attempts=1,
            last_error=kind,
        )
        session.add(incident)
        session.flush()
    else:
        incident.attempts += 1
        incident.last_error = kind
    return incident


def _requeue_queued_generation(
    session: Session,
    execution: Execution,
    *,
    now: datetime,
) -> bool:
    if execution.status != "queued" or execution.dispatch_backend != "rabbitmq":
        return False
    row = session.scalar(
        select(ExecutionOutbox)
        .where(
            ExecutionOutbox.execution_id == execution.id,
            ExecutionOutbox.dispatch_generation == execution.dispatch_generation,
        )
        .with_for_update()
    )
    if row is None:
        row = outbox.create_dispatch_outbox(session, execution, available_at=now)
    else:
        # A delivery-limit or routing incident is an explicit repair event.
        # Releasing a published row back to pending preserves the same
        # generation/message identity and lets the normal bounded Relay own
        # the next publish; Claim remains the duplicate guard.
        row.status = "pending"
        row.available_at = now
        row.lease_owner = None
        row.lease_expires_at = None
        row.published_at = None
        row.last_error_code = "infrastructure_dlq_requeue"
    session.flush()
    return True


def reconcile_message(
    session: Session,
    body: bytes,
    *,
    headers: Mapping[str, Any] | None = None,
) -> InfrastructureReconcileResult:
    """Record and converge one DLQ body without changing Business status."""

    raw = _json_mapping(body)
    raw_execution_id = _positive_int(raw.get("execution_id"))
    raw_generation = _positive_int(raw.get("dispatch_generation"))
    raw_message_id = _message_id(raw.get("message_id"))
    delivery_limit = _has_delivery_limit(headers or {})
    try:
        message = deserialize_dispatch_message(raw)
    except Exception:
        message = None
    kind = "delivery_limit" if delivery_limit else "dispatch_payload_invalid"
    if message is not None:
        raw_execution_id = message.execution_id
        raw_generation = message.dispatch_generation
        raw_message_id = message.message_id
        kind = "delivery_limit" if delivery_limit else "dispatch_infrastructure_error"

    execution: Execution | None = None
    adapter: Adapter | None = None
    if raw_execution_id is not None:
        execution = session.get(Execution, raw_execution_id, with_for_update=True)
        if execution is not None:
            adapter = session.get(Adapter, execution.adapter_id)
    incident = _incident(
        session,
        kind=kind,
        message_id=raw_message_id,
        execution_id=raw_execution_id,
        dispatch_generation=raw_generation,
    )
    now = database_now(session)
    action = "manual_review"
    if execution is None:
        action = "manual_review" if raw_execution_id is not None else "ignored"
    elif execution.dispatch_backend != "rabbitmq" or execution.status in {
        "succeeded",
        "dead_letter",
        "cancelled",
        "expired",
    }:
        action = "ignored"
        incident.status = "ignored"
        incident.resolved_at = now
    elif (
        message is not None
        and message.dispatch_generation == execution.dispatch_generation
        and message.adapter_id == execution.adapter_id
        and adapter is not None
        and adapter.language == message.language
        and execution.resource_class == message.resource_class
        and message.target_worker_id == execution.target_worker_id_snapshot
        and execution.status == "queued"
        and not delivery_limit
    ):
        # A delivery-limit event is not automatically hot-looped.  It remains
        # an operator decision because repeatedly publishing it could hide a
        # persistent Broker/routing defect.
        _requeue_queued_generation(session, execution, now=now)
        action = "requeue"
    session.commit()
    return InfrastructureReconcileResult(
        action=action,
        kind=kind,
        incident_id=int(incident.id),
        execution_id=raw_execution_id,
        dispatch_generation=raw_generation,
    )


def drain_once(
    session: Session,
    channel: Any,
    *,
    limit: int = 20,
) -> int:
    """Drain a bounded number of DLQ messages, acking only after each commit."""

    processed = 0
    for _ in range(max(1, min(int(limit), 100))):
        method, properties, body = channel.basic_get(queue=INFRASTRUCTURE_DLQ, auto_ack=False)
        if method is None or body is None:
            break
        headers = getattr(properties, "headers", None)
        reconcile_message(session, body, headers=headers)
        channel.basic_ack(delivery_tag=method.delivery_tag)
        processed += 1
    return processed


async def infrastructure_dlq_loop() -> None:
    """Continuously reconcile the shared DLQ with bounded polling."""

    while True:
        try:
            if settings.rabbitmq_url:
                from dlr.control.db import SessionLocal

                connection = rabbitmq.connect()
                try:
                    channel = connection.channel()
                    with SessionLocal() as session:
                        drain_once(session, channel)
                finally:
                    try:
                        if connection.is_open:
                            connection.close()
                    except Exception:
                        logger.debug("infrastructure DLQ connection close failed", exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("infrastructure DLQ reconciliation failed")
        await asyncio.sleep(DLQ_POLL_INTERVAL_SECONDS)
