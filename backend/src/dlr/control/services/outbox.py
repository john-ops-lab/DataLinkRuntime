"""Transactional Outbox leasing and bounded Pika publishing.

The database transaction owns responsibility.  Relay publication is always
outside the transaction that leases a row, so a network wait can never hold a
PostgreSQL row lock.  A confirm-before-mark crash intentionally leaves the
row pending for a later duplicate publish.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import pika
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import Adapter, Execution, ExecutionOutbox
from dlr.control.services import rabbitmq
from dlr.control.services.adapter import domain_error
from dlr.control.services.dispatch import (
    DISPATCH_EXCHANGE,
    assert_dispatch_message_safe,
    build_dispatch_message,
    serialize_dispatch_message,
    worker_routing_key,
)

logger = logging.getLogger("dlr.control.outbox")


class OutboxPublishError(RuntimeError):
    """A bounded publish failure with a stable, non-sensitive code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ConfirmDeadlineExceeded(TimeoutError):
    """Internal signal raised after the Pika transport is force-aborted."""


class _PublisherWindow:
    """Keep Relay channels, publishes, and confirms finite across workers."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active = {"channels": 0, "publishes": 0, "confirms": 0}
        self._events = {
            "reject": 0,
            "return": 0,
            "nack": 0,
            "timeout": 0,
            "connection_loss": 0,
        }

    @contextmanager
    def acquire(
        self,
        resource: Literal["channels", "publishes", "confirms"],
        limit: int,
    ) -> Iterator[None]:
        bounded_limit = max(1, int(limit))
        with self._condition:
            while self._active[resource] >= bounded_limit:
                self._condition.wait()
            self._active[resource] += 1
        try:
            yield
        finally:
            with self._condition:
                self._active[resource] -= 1
                self._condition.notify_all()

    def record(self, *events: str) -> None:
        with self._condition:
            for event in events:
                if event in self._events:
                    self._events[event] += 1

    def snapshot(self) -> tuple[dict[str, int], dict[str, int]]:
        with self._condition:
            return dict(self._active), dict(self._events)


_publisher_window = _PublisherWindow()


def _record_publish_failure(code: str) -> None:
    if code == "mandatory_return":
        _publisher_window.record("return")
    elif code == "publisher_nack":
        _publisher_window.record("nack", "reject")
    elif code in {"publisher_confirm_timeout", "publisher_confirm_timeout_unavailable"}:
        _publisher_window.record("timeout")
    elif code in {"publish_timeout_or_connection", "publish_failed"}:
        _publisher_window.record("connection_loss")


def publisher_health() -> dict[str, object]:
    """Expose finite publisher windows and low-cardinality Broker protection facts."""

    active, events = _publisher_window.snapshot()
    confirm_window = settings.rabbitmq_publisher_max_confirm_inflight
    available_messages = (
        settings.rabbitmq_queue_max_length
        - confirm_window
        - settings.rabbitmq_broker_headroom_messages
    )
    available_bytes = (
        settings.rabbitmq_queue_max_bytes
        - confirm_window * settings.rabbitmq_dispatch_message_max_bytes
        - settings.rabbitmq_broker_headroom_bytes
    )
    alerts: list[str] = []
    if active["channels"] >= settings.rabbitmq_publisher_channel_count:
        alerts.append("publisher_channels_exhausted")
    if active["publishes"] >= settings.rabbitmq_publisher_max_concurrency:
        alerts.append("publisher_concurrency_exhausted")
    if active["confirms"] >= settings.rabbitmq_publisher_max_confirm_inflight:
        alerts.append("publisher_confirms_exhausted")
    if available_messages < 0:
        alerts.append("broker_headroom_messages_exhausted")
    if available_bytes < 0:
        alerts.append("broker_headroom_bytes_exhausted")
    return {
        "channel_limit": settings.rabbitmq_publisher_channel_count,
        "channels_in_use": active["channels"],
        "publish_concurrency_limit": settings.rabbitmq_publisher_max_concurrency,
        "publish_concurrency_in_use": active["publishes"],
        "confirm_inflight_limit": confirm_window,
        "confirm_inflight": active["confirms"],
        "queue_max_length": settings.rabbitmq_queue_max_length,
        "queue_max_bytes": settings.rabbitmq_queue_max_bytes,
        "configured_headroom_messages": settings.rabbitmq_broker_headroom_messages,
        "configured_headroom_bytes": settings.rabbitmq_broker_headroom_bytes,
        "available_headroom_messages": max(0, available_messages),
        "available_headroom_bytes": max(0, available_bytes),
        "reject_count": events["reject"],
        "return_count": events["return"],
        "nack_count": events["nack"],
        "timeout_count": events["timeout"],
        "connection_loss_count": events["connection_loss"],
        "alerts": alerts,
    }


def _abort_connection(connection: pika.BlockingConnection, error: Exception) -> None:
    """Abort a Pika connection without entering another unbounded flush."""
    implementation = getattr(connection, "_impl", None)
    terminate_stream = getattr(implementation, "_terminate_stream", None)
    if callable(terminate_stream):
        try:
            if getattr(connection, "is_closed", False) is False:
                terminate_stream(error)
        except Exception:  # noqa: BLE001 - the connection is already disposable
            return
        return
    disconnect_stream = getattr(implementation, "_adapter_disconnect_stream", None)
    if callable(disconnect_stream):
        try:
            disconnect_stream()
        except Exception:  # noqa: BLE001 - the connection is already disposable
            return


def _schedule_confirm_deadline(
    connection: pika.BlockingConnection, callback: Callable[[], None]
) -> tuple[Any, Any]:
    """Schedule a direct I/O-loop callback; BlockingConnection.call_later is too indirect."""
    implementation = getattr(connection, "_impl", None)
    ioloop = getattr(implementation, "ioloop", None)
    call_later = getattr(ioloop, "call_later", None)
    if not callable(call_later):
        raise OutboxPublishError("publisher_confirm_timeout_unavailable")
    return ioloop, call_later(settings.rabbitmq_publish_timeout_seconds, callback)


def _remove_confirm_deadline(timer: tuple[Any, Any] | None) -> None:
    if timer is None:
        return
    ioloop, handle = timer
    remove_timeout = getattr(ioloop, "remove_timeout", None)
    if callable(remove_timeout):
        try:
            remove_timeout(handle)
        except Exception:  # noqa: BLE001 - timer is already fired/removed
            return


@dataclass(frozen=True)
class OutboxBacklog:
    pending_count: int
    pending_bytes: int
    oldest_created_at: datetime | None

    def oldest_age_seconds(self, now: datetime) -> float:
        if self.oldest_created_at is None:
            return 0.0
        oldest = self.oldest_created_at
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        return max(0.0, (current - oldest).total_seconds())


@dataclass(frozen=True)
class OutboxRelayResult:
    leased: int
    published: int
    failed: int


@dataclass(frozen=True)
class LeasedOutbox:
    """Detached immutable values needed after the lease transaction commits."""

    id: uuid.UUID
    execution_id: int
    message_id: uuid.UUID
    routing_key: str
    payload_json: dict[str, object]


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _now(session: Session) -> datetime:
    """Read the PostgreSQL clock used by all default Outbox timestamps."""
    from dlr.control.services.input_config import database_now

    return database_now(session)


def dispatch_payload_for_execution(
    session: Session, execution: Execution
) -> tuple[dict[str, Any], bytes, str, uuid.UUID]:
    """Freeze the minimal dispatch body for one RabbitMQ Execution."""

    if execution.dispatch_backend != "rabbitmq" or execution.dispatch_generation < 1:
        raise ValueError("only RabbitMQ generations can create an Outbox row")
    adapter = session.get(Adapter, execution.adapter_id)
    target_worker_id = execution.target_worker_id_snapshot or execution.target_worker_id
    if adapter is None or target_worker_id is None:
        raise ValueError("RabbitMQ Execution has no valid dispatch target")
    message = build_dispatch_message(
        execution_id=execution.id,
        dispatch_generation=execution.dispatch_generation,
        adapter_id=execution.adapter_id,
        language=cast(Literal["python", "javascript", "java"], adapter.language),
        resource_class=execution.resource_class or "default",
        target_worker_id=target_worker_id,
    )
    assert_dispatch_message_safe(message)
    payload = message.model_dump(mode="json")
    body = serialize_dispatch_message(message)
    return (
        payload,
        body,
        worker_routing_key(target_worker_id),
        message.message_id,
    )


def create_dispatch_outbox(
    session: Session,
    execution: Execution,
    *,
    available_at: datetime | None = None,
) -> ExecutionOutbox:
    """Insert at most one immutable Outbox row for the current generation."""

    existing = session.scalar(
        select(ExecutionOutbox)
        .where(
            ExecutionOutbox.execution_id == execution.id,
            ExecutionOutbox.dispatch_generation == execution.dispatch_generation,
        )
        .with_for_update()
    )
    if existing is not None:
        return existing
    payload, body, routing_key, message_id = dispatch_payload_for_execution(session, execution)
    row = ExecutionOutbox(
        execution_id=execution.id,
        dispatch_generation=execution.dispatch_generation,
        message_id=message_id,
        routing_key=routing_key,
        payload_json=payload,
        payload_bytes=len(body),
        available_at=_as_utc(available_at if available_at is not None else _now(session)),
    )
    session.add(row)
    session.flush()
    return row


def _pending_filter(now: datetime) -> Any:
    return (
        ExecutionOutbox.status == "pending",
        ExecutionOutbox.available_at <= now,
        (ExecutionOutbox.lease_expires_at.is_(None) | (ExecutionOutbox.lease_expires_at <= now)),
    )


def lease_due_outbox(
    session: Session,
    owner: str,
    *,
    limit: int = 10,
    now: datetime | None = None,
) -> list[LeasedOutbox]:
    """Claim due rows in a short transaction using ``SKIP LOCKED``."""

    if not owner or len(owner) > 128:
        raise ValueError("outbox lease owner is invalid")
    effective_now = _as_utc(now if now is not None else _now(session))
    limit = max(1, min(limit, 100))
    rows = list(
        session.scalars(
            select(ExecutionOutbox)
            .where(*_pending_filter(effective_now))
            .order_by(ExecutionOutbox.available_at, ExecutionOutbox.created_at, ExecutionOutbox.id)
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
    )
    lease_until = effective_now + timedelta(seconds=settings.rabbitmq_outbox_lease_seconds)
    leased: list[LeasedOutbox] = []
    for row in rows:
        row.lease_owner = owner
        row.lease_expires_at = lease_until
        row.publish_attempts += 1
        # Copy every value needed by the network path before commit.  Relay
        # publication must never dereference an ORM row after this transaction
        # ends, because an expired instance could implicitly open a new DB
        # transaction while the Broker is waiting.
        leased.append(
            LeasedOutbox(
                id=row.id,
                execution_id=row.execution_id,
                message_id=row.message_id,
                routing_key=row.routing_key,
                payload_json=dict(row.payload_json),
            )
        )
    session.commit()
    return leased


def bounded_backoff_seconds(publish_attempts: int) -> float:
    """Return finite exponential backoff for one failed Relay attempt."""

    attempts = max(1, min(publish_attempts, 31))
    return float(
        min(
            settings.rabbitmq_retry_max_seconds,
            settings.rabbitmq_retry_base_seconds * (2 ** (attempts - 1)),
        )
    )


def release_outbox_lease(
    session: Session,
    outbox_id: uuid.UUID,
    owner: str,
    *,
    error_code: str,
    now: datetime | None = None,
    backoff_seconds: float | None = None,
) -> bool:
    """Release only a still-owned pending row and schedule bounded retry."""

    effective_now = _as_utc(now if now is not None else _now(session))
    row = session.scalar(
        select(ExecutionOutbox)
        .where(
            ExecutionOutbox.id == outbox_id,
            ExecutionOutbox.status == "pending",
            ExecutionOutbox.lease_owner == owner,
        )
        .with_for_update()
    )
    if row is None:
        session.rollback()
        return False
    delay = (
        bounded_backoff_seconds(row.publish_attempts)
        if backoff_seconds is None
        else max(0.0, backoff_seconds)
    )
    row.lease_owner = None
    row.lease_expires_at = None
    row.available_at = effective_now + timedelta(
        seconds=min(delay, settings.rabbitmq_retry_max_seconds)
    )
    row.last_error_code = error_code[:64]
    session.commit()
    return True


def mark_outbox_published(
    session: Session, outbox_id: uuid.UUID, owner: str, *, now: datetime | None = None
) -> bool:
    """Mark published only if this Relay still owns the lease."""

    row = session.scalar(
        select(ExecutionOutbox)
        .where(
            ExecutionOutbox.id == outbox_id,
            ExecutionOutbox.status == "pending",
            ExecutionOutbox.lease_owner == owner,
        )
        .with_for_update()
    )
    if row is None:
        session.rollback()
        return False
    row.status = "published"
    row.published_at = _as_utc(now if now is not None else _now(session))
    row.lease_owner = None
    row.lease_expires_at = None
    row.last_error_code = None
    session.commit()
    return True


def settle_pending_outbox(
    session: Session,
    execution_id: int,
    *,
    disposition: str,
    now: datetime | None = None,
) -> int:
    """Settle pending generations with a bounded terminal disposition.

    ``published`` is the only settled state in the frozen B1 schema.  A
    terminal disposition therefore prevents a later Relay from publishing a
    message whose authoritative Execution is no longer claimable; it does not
    claim that the Broker accepted the body.
    """
    if not disposition or len(disposition) > 64:
        raise ValueError("outbox terminal disposition is invalid")
    rows = list(
        session.scalars(
            select(ExecutionOutbox)
            .where(
                ExecutionOutbox.execution_id == execution_id,
                ExecutionOutbox.status == "pending",
            )
            .with_for_update()
        ).all()
    )
    effective_now = _as_utc(now if now is not None else _now(session))
    for row in rows:
        row.status = "published"
        row.published_at = effective_now
        row.lease_owner = None
        row.lease_expires_at = None
        row.last_error_code = disposition
    return len(rows)


def settle_cancelled_outbox(
    session: Session, execution_id: int, *, now: datetime | None = None
) -> int:
    """Settle pending generations after a queued Execution is cancelled."""
    return settle_pending_outbox(
        session,
        execution_id,
        disposition="execution_cancelled",
        now=now,
    )


def backlog(session: Session) -> OutboxBacklog:
    """Read authoritative pending count/bytes/oldest from PostgreSQL."""

    count, payload_bytes, oldest = session.execute(
        select(
            func.count(ExecutionOutbox.id),
            func.coalesce(func.sum(ExecutionOutbox.payload_bytes), 0),
            func.min(ExecutionOutbox.created_at),
        ).where(ExecutionOutbox.status == "pending")
    ).one()
    return OutboxBacklog(int(count or 0), int(payload_bytes or 0), oldest)


def backlog_health(session: Session, *, now: datetime | None = None) -> dict[str, object]:
    """Return bounded Outbox backlog facts and protection-line status.

    The values are read from PostgreSQL on every call; no in-process counter
    is used for health because a restarted Relay must not hide existing
    responsibility.
    """
    current = backlog(session)
    effective_now = _as_utc(now if now is not None else _now(session))
    oldest_age = current.oldest_age_seconds(effective_now)
    reasons: list[str] = []
    if current.pending_count >= settings.outbox_max_pending_count:
        reasons.append("pending_count")
    if current.pending_bytes >= settings.outbox_max_pending_bytes:
        reasons.append("pending_bytes")
    if current.pending_count > 0 and oldest_age >= settings.outbox_max_oldest_seconds:
        reasons.append("oldest_age")
    return {
        "status": "degraded" if reasons else "ok",
        "pending_count": current.pending_count,
        "pending_bytes": current.pending_bytes,
        "oldest_age_seconds": oldest_age,
        "pending_oldest_age_seconds": oldest_age,
        "protection_reasons": reasons,
        "publisher": publisher_health(),
    }


def require_outbox_capacity(
    session: Session,
    *,
    additional_count: int = 1,
    additional_bytes: int = 0,
    now: datetime | None = None,
) -> None:
    """Raise the stable 503 before an ingress creates new responsibility."""

    current = backlog(session)
    effective_now = _as_utc(now if now is not None else _now(session))
    would_be_old = current.oldest_age_seconds(effective_now)
    if (
        current.pending_count + additional_count > settings.outbox_max_pending_count
        or current.pending_bytes + additional_bytes > settings.outbox_max_pending_bytes
        or (current.pending_count > 0 and would_be_old >= settings.outbox_max_oldest_seconds)
    ):
        raise domain_error(
            503,
            "outbox_backlog_full",
            "Reliable dispatch is temporarily at capacity",
            {"retry_after": settings.outbox_max_oldest_seconds},
        )


def _publish_row(
    connection: pika.BlockingConnection,
    row: LeasedOutbox | ExecutionOutbox,
    *,
    message: Any | None = None,
) -> None:
    """Publish one frozen row with persistent mandatory confirm semantics."""

    deadline_fired = False
    timer: tuple[Any, Any] | None = None
    channel: Any | None = None

    def expire_confirm_deadline() -> None:
        nonlocal deadline_fired
        deadline_fired = True
        _abort_connection(connection, _ConfirmDeadlineExceeded("publisher confirm deadline"))

    try:
        # Start before opening the channel: Pika's channel open and close
        # handshakes both use _flush_output and are otherwise unbounded.
        timer = _schedule_confirm_deadline(connection, expire_confirm_deadline)
        channel = connection.channel()
        channel.confirm_delivery()
        properties = pika.BasicProperties(
            delivery_mode=pika.DeliveryMode.Persistent,
            message_id=str(row.message_id),
            content_type="application/json",
        )
        channel.basic_publish(
            exchange=DISPATCH_EXCHANGE,
            routing_key=row.routing_key,
            body=serialize_dispatch_message(
                message if message is not None else deserialize_outbox_payload(row.payload_json)
            ),
            properties=properties,
            mandatory=True,
        )
        if getattr(channel, "is_open", True):
            # Keep the same deadline active through cleanup.  A graceful
            # channel close is allowed only while the disposable connection
            # can still be force-aborted by the timer.
            channel.close()
        if deadline_fired:
            raise _ConfirmDeadlineExceeded("publisher confirm deadline")
    except pika.exceptions.UnroutableError as exc:
        _record_publish_failure("mandatory_return")
        raise OutboxPublishError("mandatory_return") from exc
    except pika.exceptions.NackError as exc:
        _record_publish_failure("publisher_nack")
        raise OutboxPublishError("publisher_nack") from exc
    except _ConfirmDeadlineExceeded:
        _record_publish_failure("publisher_confirm_timeout")
        raise OutboxPublishError("publisher_confirm_timeout") from None
    except OutboxPublishError as exc:
        _record_publish_failure(exc.code)
        raise
    except (
        pika.exceptions.AMQPConnectionError,
        pika.exceptions.AMQPChannelError,
        TimeoutError,
    ) as exc:
        error_code = (
            "publisher_confirm_timeout" if deadline_fired else "publish_timeout_or_connection"
        )
        _record_publish_failure(error_code)
        raise OutboxPublishError(error_code) from exc
    except Exception as exc:  # noqa: BLE001 - map a fired lifecycle deadline
        if deadline_fired:
            _record_publish_failure("publisher_confirm_timeout")
            raise OutboxPublishError("publisher_confirm_timeout") from None
        raise exc
    finally:
        if deadline_fired:
            _abort_connection(connection, _ConfirmDeadlineExceeded("publisher confirm deadline"))
        _remove_confirm_deadline(timer)


def _close_connection_bounded(connection: pika.BlockingConnection) -> None:
    """Close a Relay connection without an unbounded graceful flush."""

    if not getattr(connection, "is_open", True):
        return
    deadline_fired = False
    timer: tuple[Any, Any] | None = None

    def expire_close_deadline() -> None:
        nonlocal deadline_fired
        deadline_fired = True
        _abort_connection(connection, _ConfirmDeadlineExceeded("connection close deadline"))

    try:
        try:
            timer = _schedule_confirm_deadline(connection, expire_close_deadline)
        except OutboxPublishError:
            # There is no bounded I/O-loop timer, so do not fall back to the
            # potentially blocking BlockingConnection.close().
            _abort_connection(connection, _ConfirmDeadlineExceeded("connection close deadline"))
            return
        connection.close()
    except Exception:
        if deadline_fired:
            logger.debug("outbox relay connection close exceeded deadline")
        else:
            logger.debug("outbox relay connection close failed")
    finally:
        if deadline_fired:
            _abort_connection(connection, _ConfirmDeadlineExceeded("connection close deadline"))
        _remove_confirm_deadline(timer)


def deserialize_outbox_payload(payload: object) -> Any:
    """Validate stored payload before a Relay sends it to the Broker."""

    from dlr.control.services.dispatch import deserialize_dispatch_message

    if not isinstance(payload, dict):
        raise ValueError("stored dispatch payload is not an object")
    message = deserialize_dispatch_message(payload)
    return message


def relay_once(
    session: Session,
    owner: str,
    *,
    limit: int = 10,
    connection_factory: Callable[[], pika.BlockingConnection] | None = None,
) -> OutboxRelayResult:
    """Run one bounded Relay cycle without exceeding the publisher window."""

    if not settings.rabbitmq_url:
        return OutboxRelayResult(0, 0, 0)
    with _publisher_window.acquire("channels", settings.rabbitmq_publisher_channel_count):
        return _relay_once(
            session,
            owner,
            limit=limit,
            connection_factory=connection_factory,
        )


def _relay_once(
    session: Session,
    owner: str,
    *,
    limit: int,
    connection_factory: Callable[[], pika.BlockingConnection] | None,
) -> OutboxRelayResult:
    """Lease rows, publish outside the DB transaction, then settle each row."""

    limit = max(1, min(limit, settings.rabbitmq_publisher_max_concurrency))
    rows = lease_due_outbox(session, owner, limit=limit)
    if not rows:
        return OutboxRelayResult(0, 0, 0)
    published = 0
    failed = 0
    valid_rows: list[tuple[LeasedOutbox, Any]] = []
    # Validate each persisted body before opening a Broker connection. A
    # malformed row is its own durable responsibility; it must not convert a
    # healthy row in the same lease batch into a topology failure.
    for row in rows:
        try:
            message = deserialize_outbox_payload(row.payload_json)
            # Re-serialize here so a valid-looking but oversized/tampered
            # payload is isolated before topology/publish starts.
            serialize_dispatch_message(message)
        except Exception:  # noqa: BLE001 - persisted poison is row-local
            failed += 1
            logger.warning(
                "outbox dispatch payload rejected: id=%s code=dispatch_payload_invalid",
                row.id,
            )
            release_outbox_lease(
                session,
                row.id,
                owner,
                error_code="dispatch_payload_invalid",
            )
        else:
            valid_rows.append((row, message))
    if not valid_rows:
        return OutboxRelayResult(len(rows), published, failed)

    connection: pika.BlockingConnection | None = None
    try:
        connection = (connection_factory or rabbitmq.connect)()
    except Exception:
        _record_publish_failure("publish_timeout_or_connection")
        rabbitmq.mark_runtime_failure("topology_unavailable")
        logger.warning(
            "outbox broker connection failed; code=publish_timeout_or_connection; "
            "releasing publish leases"
        )
        for row, _ in valid_rows:
            failed += 1
            release_outbox_lease(
                session,
                row.id,
                owner,
                error_code="publish_timeout_or_connection",
            )
        return OutboxRelayResult(len(rows), published, failed)
    try:
        try:
            targets = {message.target_worker_id for _, message in valid_rows}
            for target_worker_id in targets:
                rabbitmq.bootstrap_worker_topology(connection, target_worker_id)
        except rabbitmq.RabbitMQTopologyError as exc:
            error_code = exc.code
            if error_code == "topology_unavailable":
                _record_publish_failure("publish_timeout_or_connection")
            try:
                rabbitmq.mark_runtime_failure(exc.code)
            except rabbitmq.RabbitMQCapabilityPersistenceError:
                # Keep the leased rows recoverable, but do not hide that the
                # durable drift invalidation itself failed.
                error_code = "rabbitmq_capability_persistence_failed"
            for row, _ in valid_rows:
                failed += 1
                release_outbox_lease(
                    session,
                    row.id,
                    owner,
                    error_code=error_code,
                )
            return OutboxRelayResult(len(rows), published, failed)
        except Exception:
            _record_publish_failure("publish_timeout_or_connection")
            rabbitmq.mark_runtime_failure("topology_unavailable")
            for row, _ in valid_rows:
                failed += 1
                release_outbox_lease(
                    session,
                    row.id,
                    owner,
                    error_code="topology_unavailable",
                )
            return OutboxRelayResult(len(rows), published, failed)
        # A Relay batch only probes the target subset it happened to lease.
        # It must not overwrite the process-wide capability generation for
        # the complete fixed-Worker topology; the topology bootstrap loop is
        # the authority that records a full verified Worker set.
        for index, (row, _message) in enumerate(valid_rows):
            execution_status = session.scalar(
                select(Execution.status).where(Execution.id == row.execution_id)
            )
            # This is an independent, completed DB read.  In particular, it
            # must not leave the Relay session in a transaction while the
            # detached row is being published to RabbitMQ.
            session.commit()
            if execution_status in {
                "cancelled",
                "expired",
            }:
                settle_cancelled_outbox(session, row.execution_id)
                session.commit()
                continue
            try:
                # The message was validated above; keep the historical
                # two-argument call shape for test/extension compatibility.
                with (
                    _publisher_window.acquire(
                        "publishes", settings.rabbitmq_publisher_max_concurrency
                    ),
                    _publisher_window.acquire(
                        "confirms", settings.rabbitmq_publisher_max_confirm_inflight
                    ),
                ):
                    _publish_row(connection, row)
            except OutboxPublishError as exc:
                failed += 1
                release_outbox_lease(session, row.id, owner, error_code=exc.code)
                if exc.code in {
                    "publisher_confirm_timeout",
                    "publisher_confirm_timeout_unavailable",
                }:
                    rabbitmq.mark_runtime_failure(exc.code)
                    for remaining, _ in valid_rows[index + 1 :]:
                        failed += 1
                        release_outbox_lease(
                            session,
                            remaining.id,
                            owner,
                            error_code=exc.code,
                        )
                    break
            except Exception:
                failed += 1
                logger.error("outbox publish failed: id=%s code=publish_failed", row.id)
                release_outbox_lease(session, row.id, owner, error_code="publish_failed")
            else:
                if mark_outbox_published(session, row.id, owner):
                    published += 1
    finally:
        if connection is not None:
            _close_connection_bounded(connection)
    return OutboxRelayResult(len(rows), published, failed)


async def outbox_relay_loop() -> None:
    """Repair pending Outbox rows while a Broker URL is configured."""

    owner = f"control-relay-{uuid.uuid4()}"
    while True:
        try:
            if settings.rabbitmq_url:
                from dlr.control.db import SessionLocal

                with SessionLocal() as session:
                    await asyncio.to_thread(relay_once, session, owner)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("outbox relay cycle failed: code=topology_unavailable")
        await asyncio.sleep(1.0)
