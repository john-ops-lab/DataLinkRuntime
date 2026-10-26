"""Kafka source custody, on one owner thread; callbacks never touch SQL or RPC.

The DB checkpoint is authoritative. A poll position is deliberately never used
as a commit position. No source RPC occurs inside a database transaction.
"""

from __future__ import annotations

import base64
import logging
import ssl
import threading
import time
import uuid
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from fastapi import HTTPException
from kafka import KafkaAdminClient, KafkaConsumer, TopicPartition
from kafka.consumer.subscription_state import ConsumerRebalanceListener
from kafka.errors import NoOffsetForPartitionError, OffsetOutOfRangeError
from kafka.net.compat import KafkaNetClient
from kafka.net.manager import KafkaConnectionManager
from kafka.net.selector import NetworkSelector
from kafka.structs import OffsetAndMetadata
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dlr.control.models import (
    EventDelivery,
    EventReceipt,
    SourceCheckpoint,
    SubscriptionConfigVersion,
    SubscriptionLease,
    SubscriptionQuota,
)
from dlr.control.schemas.subscription import KafkaSourceConfig
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services.adapter import domain_error
from dlr.control.services.event_envelope import event_id, validate_envelope
from dlr.control.services.input_config import database_now
from dlr.control.services.subscription_queue_failure import (
    LEGACY_LIFECYCLE_FAILURES,
    PLATFORM_RECEPTION_FAILURES,
)
from dlr.control.services.subscription_source import SourceFailure, SourceRunContext

CLIENT_BUDGET: dict[str, Any] = {
    "fetch_max_bytes": 1024**2,
    "max_partition_fetch_bytes": 1024**2,
    "receive_message_max_bytes": 2 * 1024**2,
    "max_poll_records": 1,
    "request_timeout_ms": 10000,
    "bootstrap_timeout_ms": 3000,
    "session_timeout_ms": 6000,
    "heartbeat_interval_ms": 2000,
    "max_poll_interval_ms": 30000,
    "fetch_max_wait_ms": 250,
    "metadata_max_age_ms": 5000,
    "enable_auto_commit": False,
    "auto_offset_reset": "none",
    "allow_auto_create_topics": False,
    "isolation_level": "read_committed",
}
RPC_TIMEOUT_MS = 3000
POLL_MS = 200
_SDK_LOG_SINK = logging.NullHandler()
_SDK_LOG_CONFIG_LOCK = threading.RLock()


class _DiscardSdkRecord(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Filter at the originating logger, before any direct/shared handler
        # receives the original message, args, exception or fetched record.
        return False


_SDK_LOG_FILTER = _DiscardSdkRecord()


def isolate_sdk_logging() -> None:
    # SDK debug logs include config passwords and fetched record bodies. Only
    # our bounded SourceFailure codes cross into the platform log pipeline.
    with _SDK_LOG_CONFIG_LOCK:
        sdk_logger = logging.getLogger("kafka")
        loggers = sdk_logger.manager.loggerDict.copy()
        for name, logger in loggers.items():
            if (name == "kafka" or name.startswith("kafka.")) and isinstance(
                logger, logging.Logger
            ):
                # Ancestor filters do not run on propagated child records.
                # Install on every loaded SDK source, including direct handlers.
                logger.addFilter(_SDK_LOG_FILTER)
                # App/test logging configuration may attach capture/file handlers
                # directly even when propagation is disabled. Replace this list;
                # never close handlers shared with root or mutate a shared list.
                logger.handlers = [_SDK_LOG_SINK]
                logger.propagate = False


@dataclass(frozen=True, order=True)
class Partition:
    topic: str
    partition: int

    @property
    def shard(self) -> str:
        return f"{self.topic}:{self.partition}"


@dataclass(frozen=True)
class SourceIdentity:
    cluster_id: str
    topic_ids: dict[str, str]
    partitions: tuple[Partition, ...]


@dataclass(frozen=True)
class SourceOffsets:
    beginning: dict[Partition, int]
    end: dict[Partition, int]
    committed: dict[Partition, int | None]


@dataclass(frozen=True)
class CheckpointPlan:
    partition: Partition
    offset: int
    assignment_generation: int
    position: dict[str, Any]
    reset_pending: bool
    blocked: bool


class KafkaTransport(Protocol):
    def metadata(self) -> SourceIdentity: ...
    def members(self) -> tuple[str, ...]: ...
    def member_id(self) -> str: ...
    def subscribe(self, listener: ConsumerRebalanceListener) -> None: ...
    def poll(self) -> dict[Partition, list[Any]]: ...
    def offsets(self, partitions: tuple[Partition, ...]) -> SourceOffsets: ...
    def seek(self, partition: Partition, offset: int) -> None: ...
    def pause(self, partitions: tuple[Partition, ...]) -> None: ...
    def resume(self, partitions: tuple[Partition, ...]) -> None: ...
    def commit(self, offsets: dict[Partition, int]) -> None: ...
    def close(self) -> None: ...


class TransportFactory(Protocol):
    def __call__(
        self, config: dict[str, Any], credential_fields: dict[str, str], client_id: str
    ) -> KafkaTransport: ...


def _code(error: HTTPException) -> str:
    detail = error.detail
    if isinstance(detail, dict):
        return str(detail.get("code", "source_database_unavailable"))
    return "source_database_unavailable"


def classify_error(error: Exception) -> SourceFailure:
    """Do not expose exception text, hostnames, broker messages or credentials."""
    if isinstance(error, SourceFailure):
        return error
    if isinstance(error, ValidationError):
        return SourceFailure("source_configuration_invalid", disposition="blocked")
    name = type(error).__name__
    if name in {"UnsupportedVersionError", "IncompatibleBrokerVersion", "KafkaConfigurationError"}:
        return SourceFailure("source_protocol_unsupported", disposition="blocked")
    if "Authentication" in name or "Sasl" in name:
        return SourceFailure("source_authentication_failed", disposition="blocked")
    if "Authorization" in name:
        return SourceFailure("source_authorization_failed", disposition="blocked")
    if isinstance(error, ssl.SSLError) or "SSL" in name or "Certificate" in name:
        return SourceFailure("source_tls_failed", disposition="blocked")
    if name in {"OffsetOutOfRangeError", "NoOffsetForPartitionError"}:
        return SourceFailure("source_history_gap", disposition="blocked")
    if name in {"RecordTooLargeError", "InvalidMessageSizeError", "CorruptRecordError"}:
        return SourceFailure("source_record_unreadable", disposition="blocked")
    if isinstance(error, HTTPException):
        code = _code(error)
        if code in PLATFORM_RECEPTION_FAILURES:
            return SourceFailure(code, disposition="backoff")
        return SourceFailure(
            code,
            disposition=("capacity" if code == "subscription_capacity_exceeded" else "blocked"),
        )
    return SourceFailure("source_temporarily_unavailable", disposition="backoff")


def connection_options(config: KafkaSourceConfig, fields: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "bootstrap_servers": list(config.bootstrap_servers),
        "security_protocol": config.security_protocol,
        "request_timeout_ms": CLIENT_BUDGET["request_timeout_ms"],
        "bootstrap_timeout_ms": CLIENT_BUDGET["bootstrap_timeout_ms"],
        "receive_message_max_bytes": CLIENT_BUDGET["receive_message_max_bytes"],
    }
    if config.security_protocol in {"SSL", "SASL_SSL"}:
        result["ssl_context"] = ssl.create_default_context(cadata=config.ca_pem)
    if config.security_protocol == "SASL_SSL":
        if (
            config.sasl_mechanism not in {"SCRAM-SHA-256", "SCRAM-SHA-512"}
            or not fields.get("username")
            or not fields.get("password")
        ):
            raise SourceFailure("source_credential_invalid", disposition="blocked")
        result.update(
            sasl_mechanism=config.sasl_mechanism,
            sasl_plain_username=fields["username"],
            sasl_plain_password=fields["password"],
        )
    elif config.sasl_mechanism is not None:
        raise SourceFailure("source_credential_invalid", disposition="blocked")
    return result


class ObservedConnectionManager(KafkaConnectionManager):  # type: ignore[misc]
    """Pinned 3.0.11 transport hook; preserve typed errors that bootstrap swallows.

    This is an explicitly tested internal SDK adapter, not a public Kafka API
    guarantee. Callbacks retain one machine code and never perform SQL or log.
    """

    def __init__(self, net: Any, **configs: Any) -> None:
        self.failure_code: str | None = None
        self._observed: weakref.WeakSet[Any] = weakref.WeakSet()
        super().__init__(net, **configs)

    def _observe_failure(self, error: Any) -> None:
        try:
            seen: set[int] = set()
            while isinstance(error, Exception) and id(error) not in seen:
                seen.add(id(error))
                failure = classify_error(error)
                if failure.code in {
                    "source_authentication_failed",
                    "source_authorization_failed",
                    "source_tls_failed",
                }:
                    self.failure_code = failure.code
                    return
                error = error.__cause__ or error.__context__
        except Exception:
            self.failure_code = "source_protocol_unsupported"

    def get_connection(self, *args: Any, **kwargs: Any) -> Any:
        connection = super().get_connection(*args, **kwargs)
        if connection not in self._observed:
            future = getattr(connection, "init_future", None)
            if future is None or not callable(getattr(future, "add_errback", None)):
                self.failure_code = "source_protocol_unsupported"
                raise SourceFailure("source_protocol_unsupported", disposition="blocked")
            self._observed.add(connection)
            future.add_errback(self._observe_failure)
        return connection

    async def _build_transport(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return await super()._build_transport(*args, **kwargs)
        except Exception as error:
            self._observe_failure(error)
            raise


class KafkaPythonTransport:
    """Only the owner thread calls this object, including construction/close."""

    def __init__(
        self, config: dict[str, Any], credential_fields: dict[str, str], client_id: str
    ) -> None:
        isolate_sdk_logging()
        self.config = KafkaSourceConfig.model_validate(config)
        if not self.config.group_id:
            raise SourceFailure("source_configuration_invalid", disposition="blocked")
        self.consumer: Any = None
        self.admin: Any = None
        self._native_clients: list[Any] = []
        try:
            options = connection_options(self.config, credential_fields)
            self.admin = KafkaAdminClient(
                **{**options, "request_timeout_ms": RPC_TIMEOUT_MS},
                client_id=client_id + "-metadata",
                kafka_client=self._native_client,
            )
            consumer_options = {**options, **CLIENT_BUDGET}
            self.consumer = KafkaConsumer(
                **consumer_options,
                group_id=self.config.group_id,
                client_id=client_id,
                kafka_client=self._native_client,
            )
        except Exception as error:
            failure = self._failure(error)
            self.close()
            raise failure from None

    def _native_client(self, **configs: Any) -> Any:
        # Both documented SDK constructors accept kafka_client. Retain the
        # transport before Admin's eager bootstrap can raise out of __init__;
        # otherwise a partially constructed SDK object can leak its IO thread.
        net = NetworkSelector(**configs)
        manager = ObservedConnectionManager(net, **configs)
        client = KafkaNetClient(net=net, manager=manager, **configs)
        self._native_clients.append(client)
        return client

    def _failure(self, error: Exception) -> SourceFailure:
        for native in self._native_clients:
            code = native._manager.failure_code
            if code is not None:
                return SourceFailure(code, disposition="blocked")
        return classify_error(error)

    def metadata(self) -> SourceIdentity:
        cluster = self.admin.describe_cluster()
        if cluster.get("cluster_id") != self.config.cluster_id:
            raise SourceFailure("source_identity_changed", disposition="blocked")
        topics = self.admin.describe_topics(list(self.config.topics))
        ids: dict[str, str] = {}
        partitions: list[Partition] = []
        for topic in topics:
            name = topic.get("name") or topic.get("topic")
            if topic.get("error_code") or name not in self.config.topics:
                raise SourceFailure("source_topic_unavailable", disposition="blocked")
            try:
                topic_id = uuid.UUID(str(topic["topic_id"]))
            except (ValueError, KeyError, TypeError):
                raise SourceFailure("source_identity_unavailable", disposition="blocked") from None
            if not topic_id.int:
                raise SourceFailure("source_identity_unavailable", disposition="blocked")
            ids[name] = str(topic_id)
            for part in topic["partitions"]:
                if part.get("error_code"):
                    raise SourceFailure("source_topic_unavailable", disposition="blocked")
                partitions.append(Partition(name, int(part["partition_index"])))
        if set(ids) != set(self.config.topics) or not partitions:
            raise SourceFailure("source_topic_unavailable", disposition="blocked")
        if len(partitions) > 16:
            raise SourceFailure("source_partition_limit", disposition="blocked")
        return SourceIdentity(str(cluster["cluster_id"]), ids, tuple(sorted(partitions)))

    def members(self) -> tuple[str, ...]:
        group = self.admin.describe_groups([self.config.group_id])[self.config.group_id]
        error = group.get("error")
        if isinstance(error, str) and error.startswith("[Error 69] GroupIdNotFoundError:"):
            return ()
        if error:
            raise SourceFailure("source_group_unavailable", disposition="blocked")
        return tuple(str(member["member_id"]) for member in group.get("members", []))

    def member_id(self) -> str:
        return str(self.consumer.group_metadata().member_id)

    def subscribe(self, listener: ConsumerRebalanceListener) -> None:
        self.consumer.subscribe(list(self.config.topics), listener=listener)
        # Refresh the exact allowlist before joining, avoiding an initially
        # empty leader assignment while metadata is still arriving.
        self.consumer.bootstrap(timeout_ms=RPC_TIMEOUT_MS, refresh=True)

    def poll(self) -> dict[Partition, list[Any]]:
        return {
            Partition(tp.topic, tp.partition): records
            for tp, records in self.consumer.poll(timeout_ms=POLL_MS, max_records=1).items()
        }

    def offsets(self, partitions: tuple[Partition, ...]) -> SourceOffsets:
        tps = [TopicPartition(part.topic, part.partition) for part in partitions]
        beginning = self.consumer.beginning_offsets(tps, timeout_ms=RPC_TIMEOUT_MS)
        end = self.consumer.end_offsets(tps, timeout_ms=RPC_TIMEOUT_MS)
        commits = self.admin.list_group_offsets({self.config.group_id: tps})[self.config.group_id]
        return SourceOffsets(
            {p: int(beginning[tp]) for p, tp in zip(partitions, tps, strict=True)},
            {p: int(end[tp]) for p, tp in zip(partitions, tps, strict=True)},
            {
                p: (int(commits[tp].offset) if tp in commits and commits[tp].offset >= 0 else None)
                for p, tp in zip(partitions, tps, strict=True)
            },
        )

    def seek(self, partition: Partition, offset: int) -> None:
        self.consumer.seek(TopicPartition(partition.topic, partition.partition), offset)

    def pause(self, partitions: tuple[Partition, ...]) -> None:
        if partitions:
            self.consumer.pause(*(TopicPartition(p.topic, p.partition) for p in partitions))

    def resume(self, partitions: tuple[Partition, ...]) -> None:
        if partitions:
            self.consumer.resume(*(TopicPartition(p.topic, p.partition) for p in partitions))

    def commit(self, offsets: dict[Partition, int]) -> None:
        if offsets:
            self.consumer.commit(
                {
                    TopicPartition(p.topic, p.partition): OffsetAndMetadata(offset, "")
                    for p, offset in offsets.items()
                },
                timeout_ms=RPC_TIMEOUT_MS,
            )

    def close(self) -> None:
        failed = False
        for client in (self.consumer, self.admin):
            if client is None:
                continue
            try:
                if client is self.consumer:
                    client.close(autocommit=False, timeout_ms=RPC_TIMEOUT_MS)
                else:
                    client.close()
            except Exception:
                failed = True
        for client in self._native_clients:
            try:
                client.close()
            except Exception:
                failed = True
        if failed:
            raise SourceFailure("source_close_unconfirmed", disposition="blocked", closed=False)


class AssignmentTracker(ConsumerRebalanceListener):  # type: ignore[misc]
    """One bounded state map, not an unbounded callback/event queue."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token = 0
        self._partitions: frozenset[Partition] = frozenset()
        self._failed = False

    def snapshot(self) -> tuple[int, frozenset[Partition]]:
        with self._lock:
            if self._failed:
                raise SourceFailure("source_assignment_invalid", disposition="blocked")
            return self._token, self._partitions

    def _update(self, partitions: Any, assigned: bool) -> None:
        try:
            values = frozenset(Partition(str(p.topic), int(p.partition)) for p in partitions)
            with self._lock:
                self._token += 1
                self._partitions = (
                    self._partitions | values if assigned else self._partitions - values
                )
                if len(self._partitions) > 16:
                    self._partitions = frozenset()
                    self._failed = True
        except Exception:
            with self._lock:
                self._failed = True
                self._token += 1

    def on_partitions_revoked(self, revoked: Any) -> None:
        self._update(revoked, False)

    def on_partitions_assigned(self, assigned: Any) -> None:
        self._update(assigned, True)


def _lock_context(session: Session, context: SourceRunContext) -> None:
    _, sub = delivery.lock_subscription(session, context.subscription_id)
    lease = session.get(SubscriptionLease, sub.id, with_for_update=True, populate_existing=True)
    if (
        lease is None
        or lease.owner_id != context.owner_id
        or lease.generation != context.generation
        or lease.lease_until <= database_now(session)
        or sub.desired_state != "enabled"
        or sub.deleted_at is not None
        or sub.activation_id != context.activation_id
        or sub.source_epoch != context.source_epoch
    ):
        raise SourceFailure("subscription_owner_stale", disposition="blocked")


def _checkpoint(
    session: Session, context: SourceRunContext, part: Partition
) -> SourceCheckpoint | None:
    return session.scalar(
        select(SourceCheckpoint)
        .where(
            SourceCheckpoint.subscription_id == context.subscription_id,
            SourceCheckpoint.source_epoch == context.source_epoch,
            SourceCheckpoint.shard == part.shard,
        )
        .with_for_update()
    )


def _identity_position(identity: SourceIdentity, part: Partition, offset: int) -> dict[str, Any]:
    return {
        "offset": str(offset),
        "cluster_id": identity.cluster_id,
        "topic_id": identity.topic_ids[part.topic],
    }


def initialize_assignment(
    session: Session,
    context: SourceRunContext,
    identity: SourceIdentity,
    offsets: SourceOffsets,
    partitions: tuple[Partition, ...],
) -> dict[Partition, CheckpointPlan]:
    """Resolve initial positions once, preserving per-epoch source UUID baselines."""
    _lock_context(session, context)
    count = (
        session.scalar(
            select(func.count())
            .select_from(SourceCheckpoint)
            .where(
                SourceCheckpoint.subscription_id == context.subscription_id,
                SourceCheckpoint.source_epoch == context.source_epoch,
            )
        )
        or 0
    )
    # An immutable prior ConfigVersion is durable proof of explicit new_epoch/reset.
    authorized_reset = (
        session.scalar(
            select(SubscriptionConfigVersion.id)
            .where(
                SubscriptionConfigVersion.subscription_id == context.subscription_id,
                SubscriptionConfigVersion.version < context.config_version,
                SubscriptionConfigVersion.source_epoch != context.source_epoch,
            )
            .limit(1)
        )
        is not None
    )
    result: dict[Partition, CheckpointPlan] = {}
    for part in sorted(partitions):
        if part not in identity.partitions:
            raise SourceFailure("source_assignment_invalid", disposition="blocked")
        cp = _checkpoint(session, context, part)
        if cp is None:
            count += 1
            if count > 16:
                raise SourceFailure("source_partition_limit", disposition="blocked")
            offset = (
                offsets.beginning[part]
                if context.source_config["initial_position"] == "earliest"
                else offsets.end[part]
            )
            cp = SourceCheckpoint(
                subscription_id=context.subscription_id,
                source_epoch=context.source_epoch,
                shard=part.shard,
                position={
                    **_identity_position(identity, part, offset),
                    "reset_pending": authorized_reset,
                },
                confirmed_position={},
                next_sequence=1,
                assignment_generation=1,
            )
            session.add(cp)
        else:
            baseline = cp.position if cp.position.get("topic_id") else cp.confirmed_position
            if not baseline.get("topic_id"):
                raise SourceFailure("source_identity_unavailable", disposition="blocked")
            if (
                baseline.get("topic_id") != identity.topic_ids[part.topic]
                or baseline.get("cluster_id") != identity.cluster_id
            ):
                raise SourceFailure("source_identity_changed", disposition="blocked")
            cp.assignment_generation += 1
        offset = int(cp.position["offset"])
        reset_pending = bool(cp.position.get("reset_pending")) and cp.next_sequence == 1
        if offset < offsets.beginning[part] or offset > offsets.end[part]:
            raise SourceFailure("source_history_gap", disposition="blocked")
        committed = offsets.committed.get(part)
        if committed is not None and committed > offset and not reset_pending:
            raise SourceFailure("source_checkpoint_ahead", disposition="blocked")
        result[part] = CheckpointPlan(
            part,
            offset,
            cp.assignment_generation,
            dict(cp.position),
            reset_pending,
            cp.blocked_unaccepted is not None,
        )
    session.flush()
    return result


def revoke_assignment(
    session: Session, context: SourceRunContext, plans: dict[Partition, CheckpointPlan]
) -> None:
    _lock_context(session, context)
    for part in sorted(plans):
        cp = _checkpoint(session, context, part)
        if cp is not None and cp.assignment_generation == plans[part].assignment_generation:
            cp.assignment_generation += 1


def confirm_checkpoint(session: Session, context: SourceRunContext, plan: CheckpointPlan) -> bool:
    """An ACK snapshot cannot update another owner/assignment or advance custody."""
    _lock_context(session, context)
    cp = _checkpoint(session, context, plan.partition)
    if cp is None or cp.assignment_generation != plan.assignment_generation:
        return False
    safe = int(cp.position["offset"])
    previous = int(cp.confirmed_position.get("offset", -1))
    can_rewind = plan.reset_pending and cp.position.get("reset_pending") and cp.next_sequence == 1
    if plan.offset > safe or (plan.offset < previous and not can_rewind):
        return False
    cp.confirmed_position = {**plan.position, "offset": str(plan.offset), "reset_pending": False}
    if cp.position.get("reset_pending") and plan.offset == safe:
        cp.position = {**cp.position, "reset_pending": False}
    return True


def read_checkpoint(
    session: Session, context: SourceRunContext, part: Partition, assignment_generation: int
) -> CheckpointPlan:
    _lock_context(session, context)
    cp = _checkpoint(session, context, part)
    if cp is None or cp.assignment_generation != assignment_generation:
        raise SourceFailure("subscription_owner_stale", disposition="blocked")
    position = dict(cp.position)
    # Explicit poison skip changes the next offset; the original baseline remains
    # in confirmed_position, even if the disposition supplied only {offset}.
    for key in ("cluster_id", "topic_id"):
        position.setdefault(key, cp.confirmed_position.get(key))
    return CheckpointPlan(
        part,
        int(position["offset"]),
        cp.assignment_generation,
        position,
        bool(position.get("reset_pending")),
        cp.blocked_unaccepted is not None,
    )


def build_envelope(
    context: SourceRunContext, identity: SourceIdentity, record: Any
) -> dict[str, Any]:
    def encoded(value: bytes | None) -> str | None:
        return None if value is None else base64.b64encode(value).decode("ascii")

    result: dict[str, Any] = {
        "schema": "dlr.event.v1",
        "subscription": {
            "id": str(context.subscription_id),
            "config_version": context.config_version,
            "source_epoch": context.source_epoch,
        },
        "event": {"id": "", "kind": "message", "object_key": None},
        "source": {
            "type": "kafka",
            "identity": identity.cluster_id,
            "position": {
                "topic": record.topic,
                "partition": str(record.partition),
                "offset": str(record.offset),
            },
            "attributes": {
                "cluster_id": identity.cluster_id,
                "topic_incarnation": identity.topic_ids[record.topic],
                "timestamp_ms": record.timestamp,
                "timestamp_type": {0: "create_time", 1: "log_append_time"}.get(
                    record.timestamp_type, "not_available"
                ),
                "key_base64": encoded(record.key),
                "headers": [
                    {"name": name, "value_base64": encoded(value)} for name, value in record.headers
                ],
            },
        },
        "received_at": datetime.now(UTC).isoformat(),
        "data": {
            "encoding": "base64",
            "content_type": "application/octet-stream",
            "value": encoded(record.value),
        },
        "sync": None,
    }
    result["event"]["id"] = event_id(result)
    return result


def capacity_ready(session: Session, context: SourceRunContext) -> bool:
    """Read-only low-water eligibility. T1 remains the atomic reservation gate."""
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
    local = {**delivery.DEFAULT_LIMITS, **context.source_config.get("limits", {})}
    for scope, limits in (
        (f"sub:{context.subscription_id}", local),
        ("global", delivery.GLOBAL_LIMITS),
    ):
        quota = session.get(SubscriptionQuota, scope)
        if quota is None:
            continue
        if any(
            getattr(quota, key)
            + (
                quota.reserved_control_count
                if key in {"pending_count", "history_count", "dedupe_count"}
                else quota.reserved_control_bytes
            )
            > limit * 0.8
            for key, limit in limits.items()
            if key != "rate"
        ):
            return False
        if (
            quota.rate_window_at is not None
            and quota.rate_window_at > now - timedelta(seconds=1)
            and quota.rate_count >= limits["rate"]
        ):
            return False
    return True


class KafkaSourceRunner:
    def __init__(self, transport_factory: TransportFactory = KafkaPythonTransport) -> None:
        self.transport_factory = transport_factory

    def test_connection(
        self, config: dict[str, Any], credential_fields: dict[str, str]
    ) -> dict[str, Any]:
        transport = self.transport_factory(config, credential_fields, "dlr-source-test")
        try:
            identity = transport.metadata()
            if transport.members():
                raise SourceFailure("source_group_conflict", disposition="blocked")
            return {
                "source_type": "kafka",
                "cluster_id": identity.cluster_id,
                "topic_count": len(identity.topic_ids),
                "partition_count": len(identity.partitions),
                "group_available": True,
                "identity_verified": True,
            }
        except Exception as error:
            raise (
                transport._failure(error)
                if isinstance(transport, KafkaPythonTransport)
                else classify_error(error)
            ) from None
        finally:
            transport.close()

    def run(self, context: SourceRunContext) -> None:
        transport: KafkaTransport | None = None
        try:
            transport = self.transport_factory(
                context.source_config,
                context.credential_fields,
                f"dlr-sub-{context.subscription_id}-{context.generation}",
            )
            self._run(context, transport)
        except Exception as error:
            raise (
                transport._failure(error)
                if isinstance(transport, KafkaPythonTransport)
                else classify_error(error)
            ) from None
        finally:
            if transport is not None:
                transport.close()

    def _run(self, context: SourceRunContext, transport: KafkaTransport) -> None:
        tracker = AssignmentTracker()
        identity = transport.metadata()
        if transport.members():
            raise SourceFailure("source_group_conflict", disposition="blocked")
        transport.subscribe(tracker)
        plans: dict[Partition, CheckpointPlan] = {}
        token = -1
        paused: set[Partition] = set()
        next_probe = 0.0
        next_resume = 0.0
        while not context.stop_event.is_set():
            try:
                records = transport.poll()
            except (NoOffsetForPartitionError, OffsetOutOfRangeError):
                # With auto_offset_reset=none the first poll can raise after the
                # IO callback assigned partitions but before owner initialization.
                # Resolve only this new assignment from DB; never reset a running one.
                changed_token, changed_parts = tracker.snapshot()
                if changed_token == token or not changed_parts:
                    raise
                records = {}
            current_token, assigned = tracker.snapshot()
            if current_token != token:
                transport.pause(tuple(sorted(assigned)))
                if plans:
                    with context.session_factory() as session:
                        revoke_assignment(session, context, plans)
                        session.commit()
                token, plans, paused = current_token, {}, set()
                if assigned:
                    identity = transport.metadata()
                    self._exclusive(transport)
                    offsets = transport.offsets(tuple(sorted(assigned)))
                    if tracker.snapshot()[0] != token or context.stop_event.is_set():
                        continue
                    with context.session_factory() as session:
                        plans = initialize_assignment(
                            session, context, identity, offsets, tuple(sorted(assigned))
                        )
                        if tracker.snapshot()[0] != token or context.stop_event.is_set():
                            session.rollback()
                            plans = {}
                            continue
                        session.commit()
                    # All initial starts are durable before source commit/seek.
                    self._ack(context, transport, tracker, token, plans)
                    for part, plan in plans.items():
                        transport.seek(part, plan.offset)
                        if plan.blocked:
                            paused.add(part)
                    transport.resume(tuple(p for p in plans if p not in paused))
                    context.connected()
                # poll may have returned pre-initialization data. Always discard it.
                continue
            if not plans:
                continue
            if records or time.monotonic() >= next_probe:
                fresh = transport.metadata()
                if fresh.cluster_id != identity.cluster_id or fresh.topic_ids != identity.topic_ids:
                    raise SourceFailure("source_identity_changed", disposition="blocked")
                self._exclusive(transport)
                next_probe = time.monotonic() + 5
            if tracker.snapshot()[0] != token:
                continue
            for part, batch in records.items():
                if part not in plans or part in paused:
                    continue
                for record in batch:
                    if context.stop_event.is_set() or tracker.snapshot()[0] != token:
                        break
                    try:
                        received_plan = self._receive(
                            context, tracker, token, identity, part, plans[part], record
                        )
                    except HTTPException as error:
                        code = _code(error)
                        if code in (
                            LEGACY_LIFECYCLE_FAILURES
                            | PLATFORM_RECEPTION_FAILURES
                            | {"subscription_owner_stale", "subscription_activation_invalid"}
                        ):
                            raise
                        if not self._reject(
                            context,
                            identity,
                            part,
                            record,
                            code,
                            plans[part].assignment_generation,
                            tracker,
                            token,
                        ):
                            break
                        transport.pause((part,))
                        paused.add(part)
                        break
                    if received_plan is not None:
                        plans[part] = received_plan
                        self._ack(context, transport, tracker, token, {part: received_plan})
            # No rejected raw record is retained: periodically resume from DB custody.
            if paused and time.monotonic() >= next_resume:
                next_resume = time.monotonic() + 1
                for part in tuple(sorted(paused)):
                    with context.session_factory() as session:
                        plan = read_checkpoint(
                            session, context, part, plans[part].assignment_generation
                        )
                        cp = _checkpoint(session, context, part)
                        assert cp is not None
                        fault = cp.blocked_unaccepted
                        ready = fault is None or (
                            fault["code"] == "subscription_capacity_exceeded"
                            and capacity_ready(session, context)
                        )
                    if ready and tracker.snapshot()[0] == token:
                        # A disposition can advance DB custody with no following
                        # message. Confirm that safe snapshot before resuming.
                        self._ack(context, transport, tracker, token, {part: plan})
                        if tracker.snapshot()[0] != token or context.stop_event.is_set():
                            continue
                        transport.seek(part, plan.offset)
                        transport.resume((part,))
                        plans[part] = plan
                        paused.remove(part)
                if not paused:
                    context.connected()

    @staticmethod
    def _exclusive(transport: KafkaTransport) -> None:
        member_id = transport.member_id()
        members = transport.members()
        if not member_id or members != (member_id,):
            raise SourceFailure("source_group_conflict", disposition="blocked")

    @staticmethod
    def _ack(
        context: SourceRunContext,
        transport: KafkaTransport,
        tracker: AssignmentTracker,
        token: int,
        plans: dict[Partition, CheckpointPlan],
    ) -> None:
        if context.stop_event.is_set() or tracker.snapshot()[0] != token:
            return
        transport.commit({part: plan.offset for part, plan in plans.items()})
        if context.stop_event.is_set() or tracker.snapshot()[0] != token:
            return
        with context.session_factory() as session:
            for plan in plans.values():
                confirm_checkpoint(session, context, plan)
            if context.stop_event.is_set() or tracker.snapshot()[0] != token:
                session.rollback()
            else:
                session.commit()

    @staticmethod
    def _receive(
        context: SourceRunContext,
        tracker: AssignmentTracker,
        token: int,
        identity: SourceIdentity,
        part: Partition,
        previous: CheckpointPlan,
        record: Any,
    ) -> CheckpointPlan | None:
        if raw_size(record) > 512 * 1024:
            raise domain_error(422, "event_payload_too_large", "Event exceeds raw byte limit")
        envelope = build_envelope(context, identity, record)
        _, size = validate_envelope(envelope)
        if not context.buffer.reserve(size):
            raise domain_error(429, "subscription_capacity_exceeded", "Application buffer is full")
        try:
            with context.session_factory() as session:
                if tracker.snapshot()[0] != token or context.stop_event.is_set():
                    return None
                position = _identity_position(identity, part, record.offset + 1)
                delivery.receive_event(
                    session,
                    context.subscription_id,
                    context.owner_id,
                    context.generation,
                    envelope,
                    part.shard,
                    position,
                    previous.assignment_generation,
                )
                if tracker.snapshot()[0] != token or context.stop_event.is_set():
                    session.rollback()
                    return None
                plan = read_checkpoint(session, context, part, previous.assignment_generation)
                session.commit()
                return plan
        finally:
            context.buffer.release(size)

    @staticmethod
    def _reject(
        context: SourceRunContext,
        identity: SourceIdentity,
        part: Partition,
        record: Any,
        code: str,
        assignment_generation: int,
        tracker: AssignmentTracker,
        token: int,
    ) -> bool:
        # Identity calculation must not base64-copy an already oversized payload.
        source: dict[str, Any] = {
            "type": "kafka",
            "identity": identity.cluster_id,
            "position": {
                "topic": record.topic,
                "partition": str(record.partition),
                "offset": str(record.offset),
            },
            "attributes": {
                "cluster_id": identity.cluster_id,
                "topic_incarnation": identity.topic_ids[record.topic],
            },
        }
        with context.session_factory() as session:
            if context.stop_event.is_set() or tracker.snapshot()[0] != token:
                return False
            read_checkpoint(session, context, part, assignment_generation)
            delivery.record_rejection(
                session,
                context.subscription_id,
                context.owner_id,
                context.generation,
                part.shard,
                code,
                event_id({"source": source}),
                source["position"],
                size=raw_size(record),
                content_hash=None,
            )
            if context.stop_event.is_set() or tracker.snapshot()[0] != token:
                session.rollback()
                return False
            session.commit()
            return True


def raw_size(record: Any) -> int:
    return sum(len(v) for v in (record.key, record.value) if v is not None) + sum(
        len(k.encode()) + (len(v) if v is not None else 0) for k, v in record.headers
    )
