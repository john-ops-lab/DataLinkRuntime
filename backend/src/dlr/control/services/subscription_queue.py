"""Shared T1 custody for broker-redelivered, stable message-ID sources."""

from __future__ import annotations

import base64
import logging
import ssl
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from fastapi import HTTPException
from sqlalchemy import func, select

from dlr.control.models import EventDelivery, EventReceipt, SubscriptionQuota
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services.event_envelope import event_id, validate_envelope
from dlr.control.services.input_config import database_now
from dlr.control.services.subscription_queue_failure import (
    CONFIGURATION_FAILURES,
    MESSAGE_FAILURES,
    RETRYABLE_FAILURES,
)
from dlr.control.services.subscription_source import SourceFailure, SourceRunContext

SHARD = "messages"


@dataclass(frozen=True)
class Message:
    message_id: str
    body: bytes
    attributes: dict[str, Any]
    handle: Any
    content_type: str | None = None


class Transport(Protocol):
    def poll(self) -> Message | None: ...
    def ack(self, message: Message) -> None: ...
    def close(self) -> None: ...
    def diagnostic(self) -> dict[str, Any]: ...


def silence_sdk(name: str) -> None:
    # Source exceptions can contain addresses/passwords. Only bounded codes cross this boundary.
    for key, logger in list(logging.Logger.manager.loggerDict.items()):
        if (key == name or key.startswith(name + ".")) and isinstance(logger, logging.Logger):
            logger.handlers = [logging.NullHandler()]
            logger.propagate = False
    logging.getLogger(name).handlers = [logging.NullHandler()]
    logging.getLogger(name).propagate = False


_PIKA_LOG_CONTEXT = threading.local()


class _SourcePikaFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(_PIKA_LOG_CONTEXT, "depth", 0)


_PIKA_FILTER = _SourcePikaFilter()


class SourcePikaLogBoundary:
    """Suppress only source-owner Pika records; internal dispatcher logging stays intact."""

    def __init__(self) -> None:
        self.closed = False
        _PIKA_LOG_CONTEXT.depth = getattr(_PIKA_LOG_CONTEXT, "depth", 0) + 1
        for name, logger in list(logging.Logger.manager.loggerDict.items()):
            if (name == "pika" or name.startswith("pika.")) and isinstance(logger, logging.Logger):
                logger.addFilter(_PIKA_FILTER)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            _PIKA_LOG_CONTEXT.depth -= 1


def classify(error: Exception) -> SourceFailure:
    if isinstance(error, HTTPException):
        value = error.detail.get("code") if isinstance(error.detail, dict) else None
        code = value if isinstance(value, str) else "source_runtime_error"
        if code == "subscription_capacity_exceeded":
            return SourceFailure(code, "capacity")
        if code in MESSAGE_FAILURES or code in CONFIGURATION_FAILURES:
            return SourceFailure(code)
        return SourceFailure(code, "backoff")
    if isinstance(error, SourceFailure):
        if error.code in RETRYABLE_FAILURES:
            return SourceFailure(error.code, "backoff", closed=error.closed)
        return error
    if isinstance(error, ssl.SSLError):
        return SourceFailure("source_tls_failed")
    name = type(error).__name__
    if "Authentication" in name or "AccessDenied" in name:
        return SourceFailure("source_authentication_failed")
    if "ChannelClosed" in name:
        return SourceFailure("source_queue_unavailable")
    return SourceFailure("source_unavailable", "backoff")


def envelope(context: SourceRunContext, message: Message) -> dict[str, Any]:
    result = {
        "schema": "dlr.event.v1",
        "subscription": {
            "id": str(context.subscription_id),
            "config_version": context.config_version,
            "source_epoch": context.source_epoch,
        },
        "event": {"id": "", "kind": "message", "object_key": None},
        "source": {
            "type": context.source_config["source_type"],
            "identity": context.source_config["cluster_id"],
            "position": {"message_id": message.message_id},
            "attributes": message.attributes,
        },
        "received_at": datetime.now(UTC).isoformat(),
        "data": {
            "encoding": "base64",
            "content_type": message.content_type,
            "value": base64.b64encode(message.body).decode("ascii"),
        },
        "sync": None,
    }
    result["event"]["id"] = event_id(result)  # type: ignore[index]
    return result


def capacity_ready(context: SourceRunContext) -> bool:
    with context.session_factory() as session:
        now = database_now(session)
        oldest = session.scalar(
            select(func.min(EventDelivery.created_at))
            .join(EventReceipt, EventDelivery.receipt_id == EventReceipt.id)
            .where(
                EventReceipt.subscription_id == context.subscription_id,
                EventDelivery.state == "pending",
            )
        )
        if oldest is not None and oldest <= now - timedelta(minutes=5):
            return False
        limits = {**delivery.DEFAULT_LIMITS, **context.source_config.get("limits", {})}
        for scope, values in (
            (f"sub:{context.subscription_id}", limits),
            ("global", delivery.GLOBAL_LIMITS),
        ):
            quota = session.get(SubscriptionQuota, scope)
            if quota is None:
                continue
            for key, maximum in values.items():
                if key == "rate":
                    if (
                        quota.rate_window_at is not None
                        and quota.rate_window_at > now - timedelta(seconds=1)
                        and quota.rate_count >= maximum
                    ):
                        return False
                else:
                    reserved = (
                        quota.reserved_control_count
                        if key in {"pending_count", "history_count", "dedupe_count"}
                        else quota.reserved_control_bytes
                    )
                    if getattr(quota, key) + reserved > maximum * 0.8:
                        return False
        return True


def take_custody(context: SourceRunContext, message: Message) -> bool:
    """The caller must ACK only after this function returns True (committed T1)."""
    value = envelope(context, message)
    _, size = validate_envelope(value)
    if not context.buffer.reserve(size):
        raise SourceFailure("subscription_capacity_exceeded", "capacity")
    try:
        with context.session_factory() as session:
            if context.stop_event.is_set():
                return False
            delivery.receive_event(
                session,
                context.subscription_id,
                context.owner_id,
                context.generation,
                value,
                SHARD,
                {"message_id": message.message_id},
            )
            if context.stop_event.is_set():
                session.rollback()
                return False
            session.commit()
            return True
    finally:
        context.buffer.release(size)


def record_fault(context: SourceRunContext, error: Exception, message: Message | None) -> None:
    code = classify(error).code
    # Never store raw broker attributes/body or exception text in fault summaries.
    identity = "unaccepted"
    position: dict[str, str] = {}
    if message is not None and isinstance(message.message_id, str):
        import hashlib

        identity = hashlib.sha256(message.message_id.encode("utf-8", errors="replace")).hexdigest()
        if len(message.message_id.encode("utf-8", errors="replace")) <= 256 and all(
            ord(c) >= 32 for c in message.message_id
        ):
            position = {"message_id": message.message_id}
    with context.session_factory.begin() as session:
        delivery.record_rejection(
            session,
            context.subscription_id,
            context.owner_id,
            context.generation,
            SHARD,
            code,
            identity,
            position,
            size=len(message.body) if message else 0,
        )


class QueueSourceRunner:
    def open(
        self, config: dict[str, Any], credentials: dict[str, str], *, diagnostic: bool
    ) -> Transport:
        raise NotImplementedError

    def run(self, context: SourceRunContext) -> None:
        transport: Transport | None = None
        failure: SourceFailure | None = None
        message: Message | None = None
        custody_committed = False
        try:
            if not capacity_ready(context):
                raise SourceFailure("subscription_capacity_exceeded", "capacity")
            transport = self.open(
                context.source_config, context.credential_fields, diagnostic=False
            )
            context.connected()
            while not context.stop_event.is_set():
                message = None
                custody_committed = False
                if not capacity_ready(context):
                    raise SourceFailure("subscription_capacity_exceeded", "capacity")
                message = transport.poll()
                if message is None:
                    continue
                if not take_custody(context, message):
                    break
                custody_committed = True
                # The committed receipt survives even if this ACK fails or ownership changes.
                transport.ack(message)
        except Exception as error:
            failure = classify(error)
            if (
                not custody_committed
                and not context.stop_event.is_set()
                and (
                    failure.code in MESSAGE_FAILURES
                    or (
                        isinstance(error, HTTPException)
                        and failure.code == "subscription_capacity_exceeded"
                        and message is not None
                    )
                )
            ):
                try:
                    record_fault(context, error, message)
                except Exception as diagnosis_error:
                    # Fencing or DB loss during diagnosis does not imply native close failed.
                    failure = classify(diagnosis_error)
        finally:
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    failure = SourceFailure("source_close_unconfirmed", closed=False)
        if failure is not None:
            raise failure from None

    def test_connection(
        self, source_config: dict[str, Any], credential_fields: dict[str, str]
    ) -> dict[str, Any]:
        transport: Transport | None = None
        try:
            transport = self.open(source_config, credential_fields, diagnostic=True)
            return transport.diagnostic()
        except Exception as error:
            raise classify(error) from None
        finally:
            if transport is not None:
                transport.close()
