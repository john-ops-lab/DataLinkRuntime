"""Shared T1 custody for broker-redelivered, stable message-ID sources."""

from __future__ import annotations

import base64
import logging
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, cast

from fastapi import HTTPException
from sqlalchemy import func, select

from dlr.control.models import EventDelivery, EventReceipt, SubscriptionQuota
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services.event_envelope import event_id, validate_envelope
from dlr.control.services.ingress_capacity import (
    effective_profile,
    global_limits,
    runtime_limits_for_profile,
)
from dlr.control.services.subscription_queue_failure import (
    CONFIGURATION_FAILURES,
    MESSAGE_FAILURES,
    RETRYABLE_FAILURES,
)
from dlr.control.services.subscription_source import SourceFailure, SourceRunContext

if TYPE_CHECKING:
    from dlr.control.services.subscription_rabbit_budget import NativeToken

SHARD = "messages"


@dataclass(frozen=True)
class Message:
    message_id: str
    body: bytes
    attributes: dict[str, Any]
    handle: Any
    content_type: str | None = None
    native_token: NativeToken | None = None


class Transport(Protocol):
    def poll(self) -> Message | None: ...
    def ack(self, message: Message) -> None: ...
    def close(self) -> None: ...
    def diagnostic(self) -> dict[str, Any]: ...


class MicroTransport(Transport, Protocol):
    micro_t1: bool
    failed_message: Message | None

    def poll_bounded(self, timeout_seconds: float) -> Message | None: ...
    def ack_handles(self, tags: list[int]) -> None: ...


@dataclass
class _MicroPrepared:
    message: Message | None
    value: dict[str, Any] | None
    size: int
    error: HTTPException | None
    token: NativeToken


def _error_without_frames(error: HTTPException) -> HTTPException:
    result = HTTPException(error.status_code, error.detail, error.headers)
    error.__traceback__ = None
    error.__context__ = None
    error.__cause__ = None
    return result


def _prepare_micro(context: SourceRunContext, message: Message) -> _MicroPrepared | None:
    from dlr.control.services.adapter import domain_error
    from dlr.control.services.subscription_rabbit_budget import MAX_INLINE
    from dlr.control.services.subscription_rabbit_metadata import envelope_workspace, json_shape

    token = message.native_token
    if token is None:
        raise SourceFailure("source_buffer_capacity", "backoff")
    value = None
    try:
        # The small closed frame shares metadata. Compute the exact complete
        # byte size before allocating base64 or full JSON representations.
        value = envelope(context, message, body_value="")
        shape = json_shape(value)
        metadata = json_shape(message.attributes)
        encoded = 4 * ((len(message.body) + 2) // 3)
        if shape.invalid:
            raise domain_error(422, "event_content_invalid", "Unsupported canonical event JSON")
        if metadata.utf8 > 64 * 1024 or shape.utf8 + encoded > MAX_INLINE:
            raise domain_error(422, "event_payload_too_large", "Event exceeds inline byte limit")
        if not token.begin_processing(envelope_workspace(shape, metadata, len(message.body))):
            return None
        value["data"]["value"] = base64.b64encode(message.body).decode("ascii")
        _, size = validate_envelope(value)
        return _MicroPrepared(message, value, size, None, token)
    except HTTPException as error:
        # Preserve only the closed domain error, not a traceback retaining raw
        # bodies, Envelope objects and an already-ended unit transaction.
        return _MicroPrepared(message, None, 0, _error_without_frames(error), token)


def _receive_micro(
    context: SourceRunContext, group: list[_MicroPrepared]
) -> tuple[int, HTTPException | None, int | None]:
    from dlr.control.services.subscription_t1_unit import T1Input, T1Unit

    inputs = [
        T1Input(
            index=index,
            identity=item.value["event"]["id"] if item.value else "",
            envelope=item.value,
            position={"message_id": item.message.message_id},
            error=item.error,
            raw_size=len(item.message.body),
        )
        for index, item in enumerate(group)
        if item.message is not None
    ]
    try:
        with context.session_factory() as session:
            if context.stop_event.is_set():
                return 0, None, None
            unit = T1Unit(
                session,
                context.subscription_id,
                source_type="rabbitmq",
                source_epoch=context.source_epoch,
                shard=SHARD,
                owner_id=context.owner_id,
                generation=context.generation,
                expected_activation_id=context.activation_id,
                expected_config_version=context.config_version,
                stop_requested=context.stop_event.is_set,
            )
            result = unit.receive(inputs)
            first = next((item for item in result.outcomes if item.error is not None), None)
            failure = _error_without_frames(first.error) if first and first.error else None
            if context.stop_event.is_set():
                session.rollback()
                return 0, None, None
            session.commit()
            return result.accepted_prefix_count, failure, result.stopped_index
    finally:
        inputs.clear()


def _recordable(error: Exception, message: Message | None) -> bool:
    failure = classify(error)
    return failure.code in MESSAGE_FAILURES or (
        isinstance(error, HTTPException)
        and failure.code == "subscription_capacity_exceeded"
        and message is not None
    )


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


def envelope(
    context: SourceRunContext, message: Message, *, body_value: str | None = None
) -> dict[str, Any]:
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
            "value": body_value
            if body_value is not None
            else base64.b64encode(message.body).decode("ascii"),
        },
        "sync": None,
    }
    result["event"]["id"] = event_id(result)  # type: ignore[index]
    return result


@dataclass(frozen=True)
class _CapacityDecision:
    hard_ready: bool
    rate_tokens: int
    wait_seconds: float = 0


def _capacity_decision(context: SourceRunContext) -> _CapacityDecision:
    """One advisory snapshot; locked T1 still checks every item's real limits."""
    with context.session_factory() as session:
        oldest_pending = (
            select(func.min(EventDelivery.created_at))
            .join(EventReceipt, EventDelivery.receipt_id == EventReceipt.id)
            .where(
                EventReceipt.subscription_id == context.subscription_id,
                EventDelivery.state == "pending",
            )
            .scalar_subquery()
        )
        now, oldest = session.execute(select(func.clock_timestamp(), oldest_pending)).one()
        if oldest is not None and oldest <= now - timedelta(minutes=5):
            return _CapacityDecision(False, 0)
        profile = effective_profile(session)
        limits = runtime_limits_for_profile(profile, context.source_config)
        quota_rows = {
            row.scope: row
            for row in session.scalars(
                select(SubscriptionQuota).where(
                    SubscriptionQuota.scope.in_((f"sub:{context.subscription_id}", "global"))
                )
            )
        }
        scopes = (
            (f"sub:{context.subscription_id}", limits),
            ("global", global_limits(profile=profile)),
        )
        # A transient rate pause must never hide any original hard/high-water
        # refusal, even if an earlier scope has already used its rate tokens.
        for scope, values in scopes:
            quota = quota_rows.get(scope)
            if quota is None:
                continue
            for key, maximum in values.items():
                if key != "rate":
                    reserved = (
                        quota.reserved_control_count
                        if key in {"pending_count", "history_count", "dedupe_count"}
                        else quota.reserved_control_bytes
                    )
                    if getattr(quota, key) + reserved > maximum * 0.8:
                        return _CapacityDecision(False, 0)
        tokens = min(values["rate"] for _, values in scopes)
        wait_seconds = 0.0
        for scope, values in scopes:
            quota = quota_rows.get(scope)
            if (
                quota is not None
                and quota.rate_window_at is not None
                and quota.rate_window_at > now - timedelta(seconds=1)
            ):
                remaining = max(0, values["rate"] - quota.rate_count)
                tokens = min(tokens, remaining)
                if remaining == 0:
                    wait_seconds = max(
                        wait_seconds,
                        (quota.rate_window_at + timedelta(seconds=1) - now).total_seconds(),
                    )
        return _CapacityDecision(True, tokens, min(1.0, wait_seconds))


def capacity_ready(context: SourceRunContext) -> bool:
    decision = _capacity_decision(context)
    return decision.hard_ready and decision.rate_tokens > 0


def _wait_for_rate(context: SourceRunContext, seconds: float) -> bool:
    """No SDK/DB work or credit release while the current owner waits."""
    return context.stop_event.wait(seconds)


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
    def open_context(self, context: SourceRunContext) -> Transport:
        return self.open(context.source_config, context.credential_fields, diagnostic=False)

    def open(
        self, config: dict[str, Any], credentials: dict[str, str], *, diagnostic: bool
    ) -> Transport:
        raise NotImplementedError

    def run(self, context: SourceRunContext) -> None:
        transport: Transport | None = None
        failure: SourceFailure | None = None
        message: Message | None = None
        custody_committed = False
        micro_path = False
        try:
            if not capacity_ready(context):
                raise SourceFailure("subscription_capacity_exceeded", "capacity")
            transport = self.open_context(context)
            context.connected()
            if (
                context.source_config.get("source_type") == "rabbitmq"
                and context.source_config.get("mode") == "business_batch"
                and getattr(transport, "micro_t1", False)
            ):
                micro_path = True
                self._run_micro(context, cast(MicroTransport, transport))
            else:
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
            if micro_path:
                failure = SourceFailure(failure.code, failure.disposition, closed=failure.closed)
                error.__traceback__ = None
                error.__context__ = None
                error.__cause__ = None
            if (
                not micro_path
                and not custody_committed
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

    def _run_micro(self, context: SourceRunContext, transport: MicroTransport) -> None:
        carry: Message | None = None
        group: list[_MicroPrepared] = []
        message: Message | None = None
        prepared: _MicroPrepared | None = None
        failure: Exception | None = None
        failed_message: Message | None = None
        rate_deadline: float | None = None
        try:
            while not context.stop_event.is_set():
                decision = _capacity_decision(context)
                if not decision.hard_ready:
                    failure = SourceFailure("subscription_capacity_exceeded", "capacity")
                    break
                if decision.rate_tokens == 0:
                    now = time.monotonic()
                    if rate_deadline is None:
                        rate_deadline = now + 1
                    wait = min(decision.wait_seconds, rate_deadline - now)
                    if wait <= 0:
                        # Repeated global-rate contention must not starve the
                        # dedicated Pika IOLoop/heartbeat indefinitely. Retain
                        # the original close/backoff once this bounded pause
                        # is exhausted, including all unaccepted SDK credits.
                        failure = SourceFailure("subscription_capacity_exceeded", "capacity")
                        break
                    if _wait_for_rate(context, wait):
                        break
                    continue
                rate_deadline = None
                max_items = min(16, decision.rate_tokens)
                started: float | None = None
                total_bytes = 0
                deferred: Exception | None = None
                deferred_message: Message | None = None
                while len(group) < max_items and not context.stop_event.is_set():
                    try:
                        if carry is not None:
                            message, carry = carry, None
                        elif started is None:
                            message = transport.poll()
                        else:
                            message = transport.poll_bounded(
                                max(0, 0.005 - (time.monotonic() - started))
                            )
                        if message is None:
                            break
                        if started is None:
                            started = time.monotonic()
                        prepared = _prepare_micro(context, message)
                        if prepared is None:
                            if group:
                                carry = message
                                message = None
                                break
                            raise SourceFailure("source_buffer_capacity", "backoff")
                        cost = len(message.body) + prepared.size
                        if total_bytes + cost > 1024**2:
                            prepared.value = None
                            prepared.token.end_processing()
                            prepared = None
                            if group:
                                carry = message
                                message = None
                                break
                            raise SourceFailure("source_buffer_capacity", "backoff")
                        group.append(prepared)
                        total_bytes += cost
                        message = None
                        prepared = None
                        if group[-1].error is not None:
                            break
                    except Exception as error:
                        # Raw SDK prefixes survive a later malformed frame or
                        # byte refusal. Their T1 result remains explainable.
                        if isinstance(error, HTTPException):
                            deferred = _error_without_frames(error)
                        else:
                            classified = classify(error)
                            deferred = SourceFailure(classified.code, classified.disposition)
                            error.__traceback__ = None
                            error.__context__ = None
                            error.__cause__ = None
                        deferred_message = message or transport.failed_message
                        message = None
                        break
                if not group:
                    if deferred is not None:
                        failure, failed_message = deferred, deferred_message
                        break
                    continue
                try:
                    accepted, domain_error, stopped = _receive_micro(context, group)
                except Exception as error:
                    # Neither a database error nor an uncertain COMMIT permits
                    # an ACK, including items whose INSERT already ran.
                    classified = classify(error)
                    failure = SourceFailure(classified.code, classified.disposition)
                    error.__traceback__ = None
                    error.__context__ = None
                    error.__cause__ = None
                    break
                tags = [int(item.message.handle) for item in group[:accepted] if item.message]
                if domain_error is not None:
                    failure = domain_error
                    failed_message = group[stopped].message if stopped is not None else None
                elif deferred is not None:
                    failure, failed_message = deferred, deferred_message
                # Drop actual Envelope/ORM/body references before handing only
                # tags to ACK. Rejected raw bodies keep their native token.
                for item in group:
                    item.value = None
                    item.message = None
                    item.error = None
                    item.token.end_processing()
                group.clear()
                prepared = None
                message = None
                if tags:
                    try:
                        transport.ack_handles(tags)
                    except Exception as error:
                        if failure is None:
                            classified = classify(error)
                            failure = SourceFailure(classified.code, classified.disposition)
                        error.__traceback__ = None
                        error.__context__ = None
                        error.__cause__ = None
                if failure is not None:
                    break
            if failure is not None and not context.stop_event.is_set():
                if _recordable(failure, failed_message):
                    record_fault(context, failure, failed_message)
                classified = classify(failure)
                raise SourceFailure(classified.code, classified.disposition)
        finally:
            for item in group:
                item.value = None
                item.message = None
                item.error = None
                item.token.end_processing()
            group.clear()
            prepared = None
            carry = None
            message = None
            failed_message = None
            deferred_message = None
            deferred = None
            domain_error = None

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
