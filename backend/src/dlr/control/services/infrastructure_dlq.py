"""Infrastructure DLQ reconciliation for RabbitMQ dispatch envelopes.

Business failures never use this path.  A DLQ delivery is first recorded as a
durable Incident, then either the current queued generation is returned to the
PostgreSQL Outbox or the message is left as an explicit manual-review fact.
The Broker message is acknowledged only after that database transaction
commits, so an interrupted reconciler cannot leave a Broker-only orphan.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    Execution,
    ExecutionInfrastructureIncident,
    ExecutionOutbox,
)
from dlr.control.services import outbox, rabbitmq
from dlr.control.services.control_ha_capacity import assert_local_control, recheck_local_control
from dlr.control.services.dispatch import INFRASTRUCTURE_DLQ, deserialize_dispatch_message
from dlr.control.services.execution_cancellation import lock_execution_in_admission_order
from dlr.control.services.input_config import database_now

logger = logging.getLogger("dlr.control.infrastructure_dlq")
DLQ_POLL_INTERVAL_SECONDS = 5.0
_asyncio_to_thread = asyncio.to_thread
_asyncio_sleep = asyncio.sleep
_DEATH_REASONS = frozenset({"delivery_limit", "rejected", "expired", "maxlen"})


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


def _death_reason(headers: object, *, queue_name: str | None) -> str:
    """Return the newest structured x-death reason for the dispatch queue."""

    if not isinstance(headers, Mapping) or queue_name is None:
        return "unknown"
    death = headers.get("x-death")
    if not isinstance(death, list):
        return "unknown"
    for event in death:
        if not isinstance(event, Mapping) or event.get("queue") != queue_name:
            continue
        count = event.get("count")
        reason = event.get("reason")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            or not isinstance(reason, str)
        ):
            return "unknown"
        return reason if reason in _DEATH_REASONS else "unknown"
    return "unknown"


def _incident(
    session: Session,
    *,
    kind: str,
    message_id: uuid.UUID | None,
    execution_id: int | None,
    dispatch_generation: int | None,
    transport_event_key: str | None = None,
) -> ExecutionInfrastructureIncident:
    query = select(ExecutionInfrastructureIncident).where(
        ExecutionInfrastructureIncident.kind == kind,
        ExecutionInfrastructureIncident.status == "open",
    )
    if transport_event_key is not None:
        query = query.where(
            ExecutionInfrastructureIncident.transport_event_key == transport_event_key
        )
    elif message_id is not None:
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
            transport_event_key=transport_event_key,
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
    publish_attempt: int | None = None,
) -> bool:
    from dlr.control.services.worker_placement_cutover import mode

    if execution.status != "queued" or execution.dispatch_backend != "rabbitmq":
        return False
    if mode(session, hold=True) != "fixed":
        return False
    row = session.scalar(
        select(ExecutionOutbox)
        .where(
            ExecutionOutbox.execution_id == execution.id,
            ExecutionOutbox.dispatch_generation == execution.dispatch_generation,
        )
        .with_for_update()
    )
    if settings.control_ha_slot is not None and (
        row is None or publish_attempt != row.publish_attempts
    ):
        return False
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


def _transport_event_key(
    body: bytes, headers: Mapping[str, Any], queue: str | None, publish_attempt: int | None
) -> str:
    death = headers.get("x-death")
    identity: tuple[int | None, str | int | None] = (None, None)
    if isinstance(death, list) and queue is not None:
        for event in death[:64]:
            if isinstance(event, Mapping) and event.get("queue") == queue:
                occurred = event.get("last-time", event.get("time"))
                identity = (
                    _positive_int(event.get("count")),
                    occurred.isoformat()
                    if isinstance(occurred, datetime)
                    else (_positive_int(occurred)),
                )
                break
    binding = json.dumps((publish_attempt, _death_reason(headers, queue_name=queue), identity))
    return hashlib.sha256(hashlib.sha256(body).digest() + binding.encode("utf-8")).hexdigest()


def reconcile_message(
    session: Session,
    body: bytes,
    *,
    headers: Mapping[str, Any] | None = None,
) -> InfrastructureReconcileResult:
    """Record and converge one DLQ body without changing Business status."""

    instance_id = assert_local_control(session)
    raw = _json_mapping(body)
    raw_execution_id = _positive_int(raw.get("execution_id"))
    raw_generation = _positive_int(raw.get("dispatch_generation"))
    raw_message_id = _message_id(raw.get("message_id"))
    raw_worker_id = _positive_int(raw.get("target_worker_id"))
    try:
        message = deserialize_dispatch_message(raw)
    except Exception:
        message = None
    target_worker_id = message.target_worker_id if message is not None else raw_worker_id
    queue_name = (
        rabbitmq.topology_names(target_worker_id).queue if target_worker_id is not None else None
    )
    broker_reason = _death_reason(headers or {}, queue_name=queue_name)
    kind = broker_reason if message is not None else "invalid"
    delivery_limit = broker_reason == "delivery_limit"
    if message is not None:
        raw_execution_id = message.execution_id
        raw_generation = message.dispatch_generation
        raw_message_id = message.message_id

    event_key = None
    publish_attempt = _positive_int((headers or {}).get("x-dlr-publish-attempt"))
    if instance_id is not None:
        event_key = _transport_event_key(body, headers or {}, queue_name, publish_attempt)
        session.execute(
            text("SELECT pg_advisory_xact_lock(:event)"),
            {"event": int.from_bytes(bytes.fromhex(event_key[:16]), signed=True)},
        )
        previous = session.scalar(
            select(ExecutionInfrastructureIncident.id).where(
                ExecutionInfrastructureIncident.transport_event_key == event_key
            )
        )
        if previous is not None:
            session.rollback()
            return InfrastructureReconcileResult(
                "duplicate", kind, previous, raw_execution_id, raw_generation
            )

    execution: Execution | None = None
    adapter: Adapter | None = None
    cache_reclamation_blocked = False
    if raw_execution_id is not None:
        candidate_status = session.scalar(
            select(Execution.status).where(Execution.id == raw_execution_id)
        )
        try:
            execution = lock_execution_in_admission_order(
                session,
                raw_execution_id,
                guard_reactivation=(
                    message is not None and not delivery_limit and candidate_status == "queued"
                ),
            )
        except HTTPException as error:
            if not isinstance(error.detail, Mapping) or (
                error.detail.get("code") != "cache_reclamation_in_progress"
            ):
                raise
            # The guard/admission prefix remains locked. Preserve a durable
            # manual-review fact without reactivating the guarded reference.
            cache_reclamation_blocked = True
            execution = lock_execution_in_admission_order(session, raw_execution_id)
        if execution is not None:
            adapter = session.get(Adapter, execution.adapter_id)
    incident = _incident(
        session,
        kind=kind,
        message_id=raw_message_id,
        execution_id=raw_execution_id,
        dispatch_generation=raw_generation,
        transport_event_key=event_key,
    )
    if message is None:
        # Only the fixed allowlisted transport reason is retained; never copy
        # malformed payloads or arbitrary Broker headers into diagnostics.
        incident.last_error = f"invalid; broker_reason={broker_reason}"
    elif cache_reclamation_blocked:
        incident.last_error = f"cache_reclamation_in_progress; broker_reason={broker_reason}"
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
        and not execution.requires_worker_reservation
        and message.dispatch_generation == execution.dispatch_generation
        and message.adapter_id == execution.adapter_id
        and adapter is not None
        and adapter.language == message.language
        and execution.resource_class == message.resource_class
        and message.target_worker_id == execution.target_worker_id_snapshot
        and execution.status == "queued"
        and not delivery_limit
        and not cache_reclamation_blocked
        and _requeue_queued_generation(session, execution, now=now, publish_attempt=publish_attempt)
    ):
        # A delivery-limit event is not automatically hot-looped.  It remains
        # an operator decision because repeatedly publishing it could hide a
        # persistent Broker/routing defect.
        action = "requeue"
    recheck_local_control(session)
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


def _infrastructure_dlq_tick() -> int:
    """Run blocking Broker and database I/O in one worker thread."""
    if not settings.rabbitmq_url:
        return 0
    from dlr.control.db import SessionLocal

    connection = rabbitmq.connect()
    try:
        channel = connection.channel()
        with SessionLocal() as session:
            return drain_once(session, channel)
    finally:
        try:
            if connection.is_open:
                connection.close()
        except Exception:
            logger.debug("infrastructure DLQ connection close failed", exc_info=True)


async def infrastructure_dlq_loop() -> None:
    """Continuously reconcile the shared DLQ with bounded polling."""

    while True:
        try:
            await _asyncio_to_thread(_infrastructure_dlq_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("infrastructure DLQ reconciliation failed")
        await _asyncio_sleep(DLQ_POLL_INTERVAL_SECONDS)
