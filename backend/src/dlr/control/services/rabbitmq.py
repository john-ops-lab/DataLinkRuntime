"""RabbitMQ 4.3.5 topology and connection boundary for the B1 Outbox Relay."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

import pika
from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import RabbitMQRuntimeCapability, Worker
from dlr.control.services.dispatch import (
    DISPATCH_EXCHANGE,
    INFRASTRUCTURE_DLQ,
    INFRASTRUCTURE_DLX,
    worker_routing_key,
)

logger = logging.getLogger("dlr.control.rabbitmq")

RABBITMQ_BASELINE_VERSION = "4.3.5"
REQUIRED_FEATURE_FLAGS = frozenset(
    {
        "feature_flags_v2",
        "quorum_queue",
        "stream_queue",
        "rabbitmq_4.3.0",
    }
)
RABBITMQ_CONFIGURATION_ERROR_CODES = frozenset(
    {
        "rabbitmq_configuration_invalid",
        "rabbitmq_capability_probe_failed",
        "rabbitmq_capability_persistence_failed",
        "topology_drift",
    }
)
RABBITMQ_CAPABILITY_INVALIDATION_ERROR_CODES = frozenset(
    {
        "rabbitmq_configuration_invalid",
        "rabbitmq_capability_probe_failed",
        "topology_drift",
    }
)
WORK_QUEUE_PREFIX = "dlr.worker."
WORK_QUEUE_SUFFIX = ".q"
INFRASTRUCTURE_ROUTING_KEY = "infrastructure"


class RabbitMQTopologyError(RuntimeError):
    """A broker topology/configuration mismatch that must fail health closed."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or ("topology_drift" if "drift" in message else "topology_unavailable")


class RabbitMQCapabilityPersistenceError(RuntimeError):
    """The durable capability invalidation could not be committed."""

    code = "rabbitmq_capability_persistence_failed"


class _TopologyDeadlineExceeded(TimeoutError):
    """Internal signal raised after a topology transport is force-aborted."""


def _abort_connection(connection: pika.BlockingConnection, error: Exception) -> None:
    """Abort a disposable Pika connection without another graceful flush."""

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


def _schedule_topology_deadline(
    connection: pika.BlockingConnection, callback: Callable[[], None]
) -> tuple[Any, Any]:
    """Schedule a direct I/O-loop deadline for all AMQP topology handshakes."""

    implementation = getattr(connection, "_impl", None)
    ioloop = getattr(implementation, "ioloop", None)
    call_later = getattr(ioloop, "call_later", None)
    if not callable(call_later):
        raise RabbitMQTopologyError(
            "RabbitMQ topology deadline is unavailable", code="topology_unavailable"
        )
    return ioloop, call_later(settings.rabbitmq_publish_timeout_seconds, callback)


def _remove_topology_deadline(timer: tuple[Any, Any] | None) -> None:
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
class RabbitMQRuntimeCapabilities:
    """The non-secret broker facts required before topology is used."""

    version: str
    feature_flags: frozenset[str]


_runtime_status: dict[str, object] = {
    "status": "disabled",
    "last_error_code": None,
    "worker_count": 0,
    "capability_verified": False,
    "configuration_fingerprint": None,
    "verified_worker_ids": None,
    "broker_observations": {},
}


def _set_runtime_status(
    status: str, *, error_code: str | None = None, worker_count: int | None = None
) -> None:
    _runtime_status["status"] = status
    _runtime_status["last_error_code"] = error_code
    if worker_count is not None:
        _runtime_status["worker_count"] = worker_count


def _invalidate_persisted_capability() -> None:
    """Delete stale capability evidence in its own short DB transaction."""

    from dlr.control.db import SessionLocal

    try:
        with SessionLocal() as session:
            state = session.get(RabbitMQRuntimeCapability, 1, with_for_update=True)
            if state is not None:
                session.delete(state)
            session.commit()
    except Exception:
        # Do not claim that a drifted generation was durably disabled when the
        # invalidation transaction failed.  The caller receives a stable
        # failure, while the process-local gate remains closed below.
        logger.error(
            "RabbitMQ capability invalidation failed; code=rabbitmq_capability_persistence_failed"
        )
        raise RabbitMQCapabilityPersistenceError(
            "RabbitMQ capability invalidation could not be committed"
        ) from None


def mark_runtime_failure(error_code: str) -> None:
    """Record a stable non-sensitive Broker/topology failure for health."""
    _set_runtime_status("degraded", error_code=error_code)
    if error_code in RABBITMQ_CAPABILITY_INVALIDATION_ERROR_CODES:
        _runtime_status["capability_verified"] = False
        try:
            _invalidate_persisted_capability()
        except RabbitMQCapabilityPersistenceError:
            _runtime_status["last_error_code"] = "rabbitmq_capability_persistence_failed"
            raise


def mark_runtime_ready(
    worker_count: int | None = None, *, worker_ids: list[int] | None = None
) -> None:
    """Record that the last topology bootstrap completed successfully."""
    _set_runtime_status("ready", worker_count=worker_count)
    _runtime_status["capability_verified"] = True
    _runtime_status["configuration_fingerprint"] = configuration_fingerprint(worker_ids or [])
    _runtime_status["verified_worker_ids"] = (
        frozenset(worker_ids) if worker_ids is not None else None
    )


def effective_rabbitmq_url() -> str:
    """Build the AMQP URL from the single raw vhost configuration value."""

    if not settings.rabbitmq_url:
        raise RabbitMQTopologyError("RabbitMQ URL is not configured")
    if settings.rabbitmq_vhost is None:
        return settings.rabbitmq_url
    parsed = urlsplit(settings.rabbitmq_url)
    if parsed.path not in {"", "/"}:
        raise RabbitMQTopologyError(
            "RabbitMQ URL contains a vhost path alongside the raw vhost setting",
            code="rabbitmq_configuration_invalid",
        )
    return parsed._replace(path=f"/{quote(settings.rabbitmq_vhost, safe='')}").geturl()


def configuration_fingerprint(worker_ids: list[int]) -> str:
    """Hash the non-secret deployment/topology generation used for verification."""

    from dlr.common.jcs import canonicalize

    if any(
        isinstance(worker_id, bool) or not isinstance(worker_id, int) for worker_id in worker_ids
    ):
        raise ValueError("verified worker ids must be integers")
    facts = {
        # Credentials are not persisted; their one-way digest still makes a
        # password/user rotation a new generation that must be re-verified.
        "amqp_url_sha256": hashlib.sha256(effective_rabbitmq_url().encode()).hexdigest(),
        "management_url_sha256": hashlib.sha256(
            (settings.rabbitmq_management_url or "").encode()
        ).hexdigest(),
        "baseline_version": RABBITMQ_BASELINE_VERSION,
        "required_feature_flags": sorted(REQUIRED_FEATURE_FLAGS),
        "worker_ids": sorted(set(worker_ids)),
        "queue_arguments": work_queue_arguments(),
        "infrastructure_queue_arguments": infrastructure_queue_arguments(),
        "publisher_channel_count": settings.rabbitmq_publisher_channel_count,
        "publisher_max_concurrency": settings.rabbitmq_publisher_max_concurrency,
        "publisher_max_confirm_inflight": settings.rabbitmq_publisher_max_confirm_inflight,
        "broker_headroom_messages": settings.rabbitmq_broker_headroom_messages,
        "broker_headroom_bytes": settings.rabbitmq_broker_headroom_bytes,
    }
    return hashlib.sha256(canonicalize(facts)).hexdigest()


def _persist_runtime_capabilities(
    worker_ids: list[int], capabilities: RabbitMQRuntimeCapabilities
) -> None:
    """Persist the last live capability probe in a short DB transaction."""

    from dlr.control.db import SessionLocal
    from dlr.control.services.input_config import database_now

    with SessionLocal() as session:
        state = session.get(RabbitMQRuntimeCapability, 1, with_for_update=True)
        if state is None:
            state = RabbitMQRuntimeCapability(
                id=1,
                configuration_fingerprint=configuration_fingerprint(worker_ids),
                broker_version=capabilities.version,
                feature_flags=sorted(capabilities.feature_flags),
                worker_ids=sorted(set(worker_ids)),
                verified_at=database_now(session),
            )
            session.add(state)
        else:
            state.configuration_fingerprint = configuration_fingerprint(worker_ids)
            state.broker_version = capabilities.version
            state.feature_flags = sorted(capabilities.feature_flags)
            state.worker_ids = sorted(set(worker_ids))
            state.verified_at = database_now(session)
        session.commit()


def _persisted_ingress_ready(session: Session, *, worker_id: int | None) -> bool:
    """Use only a matching, recent live-verification generation after restart."""

    state = session.get(RabbitMQRuntimeCapability, 1)
    if state is None or state.broker_version != RABBITMQ_BASELINE_VERSION:
        return False
    try:
        worker_ids = list(state.worker_ids)
        if state.configuration_fingerprint != configuration_fingerprint(worker_ids):
            return False
        stored_flags = frozenset(state.feature_flags)
        stored_workers = frozenset(worker_ids)
    except (TypeError, ValueError):
        return False
    if not REQUIRED_FEATURE_FLAGS.issubset(stored_flags):
        return False
    current_workers = frozenset(session.scalars(select(Worker.id)).all())
    if current_workers != stored_workers:
        return False
    if worker_id is not None and worker_id not in stored_workers:
        return False
    from dlr.control.services.input_config import database_now

    age = (database_now(session) - state.verified_at).total_seconds()
    return age <= settings.rabbitmq_capability_cache_seconds


def _runtime_worker_set_is_current(session: Session, worker_ids: frozenset[int]) -> bool:
    """Reject a process-local generation after Worker topology membership changes."""

    return frozenset(session.scalars(select(Worker.id)).all()) == worker_ids


def ingress_configuration_ready(
    session: Session | None = None,
    *,
    worker_id: int | None = None,
    allow_disabled: bool = False,
) -> bool:
    """Return whether live or restart-persisted capability permits ingress.

    ``topology_unavailable`` is an outage and may use the last matching
    generation.  A known configuration/topology error never falls back to
    that cache.  Without a DB session (for example a pure health helper), a
    cold process cannot claim a persisted generation.
    """

    if (
        not settings.rabbitmq_execution_enabled and not allow_disabled
    ) or not settings.rabbitmq_url:
        return False
    if _runtime_status["last_error_code"] in RABBITMQ_CONFIGURATION_ERROR_CODES:
        return False
    runtime_fingerprint = _runtime_status["configuration_fingerprint"]
    runtime_workers = cast(
        frozenset[int] | None,
        _runtime_status["verified_worker_ids"],
    )
    if _runtime_status["capability_verified"] and runtime_fingerprint == configuration_fingerprint(
        list(runtime_workers) if runtime_workers is not None else []
    ):
        if (
            runtime_workers is not None
            and session is not None
            and not _runtime_worker_set_is_current(session, runtime_workers)
        ):
            return False
        # A targetless readiness check (used by health) must never treat a
        # target-scoped in-memory probe as proof of the complete fixed-Worker
        # topology.  A DB-backed check can compare the full current set; a
        # check without a session has no such evidence and must fail closed.
        if worker_id is None and runtime_workers is not None and session is None:
            return False
        return runtime_workers is None or worker_id is None or worker_id in runtime_workers
    return session is not None and _persisted_ingress_ready(session, worker_id=worker_id)


def _broker_health_payload() -> dict[str, object]:
    """Expose queue-bound observations as operations-only, low-cardinality data."""
    raw_observations = _runtime_status.get("broker_observations", {})
    observations = raw_observations if isinstance(raw_observations, dict) else {}
    normalized: dict[str, dict[str, int]] = {}
    for worker_id, values in observations.items():
        if not isinstance(values, dict):
            continue
        messages_ready = values.get("messages_ready")
        message_bytes_ready = values.get("message_bytes_ready")
        if isinstance(messages_ready, int) and isinstance(message_bytes_ready, int):
            normalized[str(worker_id)] = {
                "messages_ready": messages_ready,
                "message_bytes_ready": message_bytes_ready,
            }
    max_messages = max(
        (values["messages_ready"] for values in normalized.values()),
        default=None,
    )
    max_bytes = max(
        (values["message_bytes_ready"] for values in normalized.values()),
        default=None,
    )
    headroom_messages = (
        settings.rabbitmq_queue_max_length - max_messages if max_messages is not None else None
    )
    headroom_bytes = (
        settings.rabbitmq_queue_max_bytes - max_bytes if max_bytes is not None else None
    )
    alerts: list[str] = []
    if (
        headroom_messages is not None
        and headroom_messages < settings.rabbitmq_broker_headroom_messages
    ):
        alerts.append("broker_queue_headroom_messages_low")
    if headroom_bytes is not None and headroom_bytes < settings.rabbitmq_broker_headroom_bytes:
        alerts.append("broker_queue_headroom_bytes_low")
    if max_messages is not None and max_messages > settings.rabbitmq_queue_max_length:
        alerts.append("broker_queue_length_overshoot")
    if max_bytes is not None and max_bytes > settings.rabbitmq_queue_max_bytes:
        alerts.append("broker_queue_bytes_overshoot")
    return {
        "queue_max_length": settings.rabbitmq_queue_max_length,
        "queue_max_bytes": settings.rabbitmq_queue_max_bytes,
        "configured_headroom_messages": settings.rabbitmq_broker_headroom_messages,
        "configured_headroom_bytes": settings.rabbitmq_broker_headroom_bytes,
        "observed_queues": normalized,
        "headroom_messages": headroom_messages,
        "headroom_bytes": headroom_bytes,
        "alerts": alerts,
    }


def runtime_health(session: Session | None = None) -> dict[str, object]:
    """Return a redacted health view; never include the RabbitMQ URL/userinfo."""
    configured = bool(settings.rabbitmq_url)
    runtime_status = str(_runtime_status["status"])
    worker_count = cast(int, _runtime_status["worker_count"])
    capability_verified = bool(_runtime_status["capability_verified"])
    runtime_error = _runtime_status["last_error_code"]
    if not configured:
        repair_status = "disabled"
        repair_error: object = None
        repair_ready = False
    else:
        repair_status = runtime_status
        repair_error = runtime_error
        runtime_workers = cast(
            frozenset[int] | None,
            _runtime_status["verified_worker_ids"],
        )
        if (
            runtime_status == "ready"
            and capability_verified
            and runtime_workers is not None
            and (session is None or not _runtime_worker_set_is_current(session, runtime_workers))
        ):
            # A process-local ready state is only a repair-health claim when
            # it covers the complete current Worker set.  In particular, a
            # Relay batch must not make a subset look globally ready.
            repair_status = "degraded"
            repair_error = "rabbitmq_not_verified"
        if not capability_verified and repair_error is None:
            # A fresh deployment has no Worker queue to reconcile yet.  Keep
            # that bounded, responsibility-free state distinct from a
            # Broker/configuration failure so Control can become healthy and
            # let the fixed Worker register.  Ingress remains fail-closed
            # because capability_verified is still false.
            current_worker_count = (
                len(session.scalars(select(Worker.id)).all())
                if session is not None
                else worker_count
            )
            if runtime_status in {"disabled", "waiting_for_worker"} and current_worker_count == 0:
                repair_status = "waiting_for_worker"
            else:
                repair_status = "degraded"
                repair_error = "rabbitmq_not_verified"
        repair_ready = repair_status == "ready" and capability_verified

    ingress_enabled = bool(settings.rabbitmq_execution_enabled)
    ingress_ready = ingress_configuration_ready(session)
    ingress_error: object = repair_error
    if ingress_enabled and not configured and ingress_error is None:
        ingress_error = "rabbitmq_not_configured"
    if ingress_enabled and not ingress_ready and ingress_error is None:
        ingress_error = "rabbitmq_not_verified"
    ingress_status = ("ready" if ingress_ready else "degraded") if ingress_enabled else "disabled"
    # Keep the legacy top-level fields while making the two independent
    # responsibilities explicit.  A configured but unhealthy repair path is
    # intentionally visible even when new RabbitMQ ingress is disabled.
    top_level_status = repair_status if not ingress_enabled and configured else ingress_status
    top_level_error = repair_error if not ingress_enabled else ingress_error
    return {
        "enabled": ingress_enabled,
        "status": top_level_status,
        "ready": ingress_ready,
        "last_error_code": top_level_error,
        "worker_count": worker_count,
        "ingress": {
            "enabled": ingress_enabled,
            "status": ingress_status,
            "ready": ingress_ready,
            "last_error_code": ingress_error,
        },
        "repair": {
            "configured": configured,
            "status": repair_status,
            "ready": repair_ready,
            "last_error_code": repair_error,
            "worker_count": worker_count,
        },
        "broker": _broker_health_payload(),
    }


@dataclass(frozen=True)
class TopologyNames:
    """Stable names for one fixed Worker and the shared infrastructure DLQ."""

    worker_id: int

    @property
    def queue(self) -> str:
        return f"{WORK_QUEUE_PREFIX}{self.worker_id}{WORK_QUEUE_SUFFIX}"

    @property
    def routing_key(self) -> str:
        return worker_routing_key(self.worker_id)


def topology_names(worker_id: int) -> TopologyNames:
    if isinstance(worker_id, bool) or not isinstance(worker_id, int) or worker_id <= 0:
        raise ValueError("target worker id must be a positive integer")
    return TopologyNames(worker_id)


def _retry_delay_milliseconds(value: object, *, setting_name: str) -> int:
    """Convert a positive retry setting to a bounded AMQP integer delay."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{setting_name} must be a finite positive number")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{setting_name} must be a finite positive number")
    # Settings cap these values at one hour.  Keep the conversion explicit and
    # non-zero even when a sub-millisecond value is supplied by a direct test
    # or an already-created Settings singleton.
    return max(1, int(seconds * 1_000))


def work_queue_arguments() -> dict[str, Any]:
    """Return a fresh bounded Quorum Queue argument map."""

    retry_min_ms = _retry_delay_milliseconds(
        settings.rabbitmq_retry_base_seconds,
        setting_name="DLR_RABBITMQ_RETRY_BASE_SECONDS",
    )
    retry_max_ms = _retry_delay_milliseconds(
        settings.rabbitmq_retry_max_seconds,
        setting_name="DLR_RABBITMQ_RETRY_MAX_SECONDS",
    )
    if retry_min_ms > retry_max_ms:
        raise ValueError(
            "DLR_RABBITMQ_RETRY_BASE_SECONDS must not exceed DLR_RABBITMQ_RETRY_MAX_SECONDS"
        )

    return {
        "x-queue-type": "quorum",
        "x-max-length": settings.rabbitmq_queue_max_length,
        "x-max-length-bytes": settings.rabbitmq_queue_max_bytes,
        "x-overflow": "reject-publish",
        "x-delivery-limit": settings.rabbitmq_delivery_limit,
        "x-dead-letter-exchange": INFRASTRUCTURE_DLX,
        "x-dead-letter-routing-key": INFRASTRUCTURE_ROUTING_KEY,
        "x-dead-letter-strategy": "at-least-once",
        "x-consumer-timeout": settings.rabbitmq_consumer_timeout_ms,
        # RabbitMQ 4.3 Quorum Queue delayed retry is native to the queue.  The
        # v3 Consumer returns DEFER with basic.nack(requeue=True); it never
        # republishes an x-delay copy.  ``all`` additionally delays messages
        # returned by connection/channel recovery, while ``returned`` is the
        # narrower explicit-return mode.
        "x-delayed-retry-type": settings.rabbitmq_delayed_retry_type,
        "x-delayed-retry-min": retry_min_ms,
        "x-delayed-retry-max": retry_max_ms,
    }


def infrastructure_queue_arguments() -> dict[str, Any]:
    return {
        "x-queue-type": "quorum",
        "x-max-length": settings.rabbitmq_queue_max_length,
        "x-max-length-bytes": settings.rabbitmq_queue_max_bytes,
        "x-overflow": "reject-publish",
        "x-delivery-limit": settings.rabbitmq_delivery_limit,
    }


def connection_parameters() -> pika.ConnectionParameters:
    """Build Pika parameters without logging or exposing the broker URL."""

    if not settings.rabbitmq_url:
        raise RabbitMQTopologyError("RabbitMQ URL is not configured")
    try:
        parameters = pika.URLParameters(effective_rabbitmq_url())
    except (TypeError, ValueError) as exc:
        raise RabbitMQTopologyError("RabbitMQ URL is invalid") from exc
    parameters.heartbeat = 30
    parameters.blocked_connection_timeout = settings.rabbitmq_publish_timeout_seconds
    parameters.socket_timeout = settings.rabbitmq_stack_timeout_seconds
    parameters.stack_timeout = settings.rabbitmq_stack_timeout_seconds
    parameters.connection_attempts = 1
    parameters.retry_delay = 0
    return parameters


def connect() -> pika.BlockingConnection:
    """Open one bounded Relay connection; callers own its close lifecycle."""

    return pika.BlockingConnection(connection_parameters())


def _management_feature_flags_url() -> str:
    """Return the explicitly configured management API endpoint."""

    return _management_resource_url("feature-flags")


def _management_resource_url(resource: str) -> str:
    """Build one management API URL without deriving an unconfigured port."""

    if not settings.rabbitmq_management_url:
        raise RabbitMQTopologyError(
            "RabbitMQ feature flag probe is not configured",
            code="rabbitmq_capability_probe_failed",
        )
    try:
        parsed = urlsplit(settings.rabbitmq_management_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        path = parsed.path.rstrip("/")
        if not path.endswith("/api"):
            path = f"{path}/api"
        path = f"{path}/{resource.lstrip('/')}"
        return parsed._replace(path=path, query="", fragment="").geturl()
    except (TypeError, ValueError):
        raise RabbitMQTopologyError(
            "RabbitMQ management probe URL is invalid",
            code="rabbitmq_capability_probe_failed",
        ) from None


def _management_auth_header() -> str:
    """Build Basic auth from the AMQP URL without returning it in errors."""

    if not settings.rabbitmq_url:
        raise RabbitMQTopologyError(
            "RabbitMQ management probe is not configured",
            code="rabbitmq_capability_probe_failed",
        )
    parsed = urlsplit(settings.rabbitmq_url)
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not username or not password:
        raise RabbitMQTopologyError(
            "RabbitMQ management credentials are not configured",
            code="rabbitmq_capability_probe_failed",
        )
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {credentials}"


def _configured_vhost() -> str:
    """Return the AMQP vhost using the management API's decoded contract."""
    if settings.rabbitmq_vhost is not None:
        return settings.rabbitmq_vhost
    if not settings.rabbitmq_url:
        raise RabbitMQTopologyError(
            "RabbitMQ management probe is not configured",
            code="rabbitmq_capability_probe_failed",
        )
    parsed = urlsplit(settings.rabbitmq_url)
    raw_path = parsed.path.lstrip("/")
    return unquote(raw_path or "/")


def _management_request_json(resource: str) -> object:
    """Read one bounded management response with stable redacted failures."""

    try:
        request = Request(
            _management_resource_url(resource),
            headers={
                "Accept": "application/json",
                "Authorization": _management_auth_header(),
            },
        )
        with urlopen(request, timeout=settings.rabbitmq_management_timeout_seconds) as response:
            return json.loads(response.read(1_048_576))
    except RabbitMQTopologyError:
        raise
    except Exception:
        raise RabbitMQTopologyError(
            "RabbitMQ management probe failed",
            code="topology_unavailable",
        ) from None


def _fetch_queue_details(queue_name: str) -> dict[str, object]:
    """Read one queue's actual arguments and effective policy from management."""
    resource = "queues/{}/{}".format(
        quote(_configured_vhost(), safe=""),
        quote(queue_name, safe=""),
    )
    payload = _management_request_json(resource)
    if not isinstance(payload, dict):
        raise RabbitMQTopologyError(
            "RabbitMQ queue inspection response is invalid",
            code="topology_unavailable",
        )
    return payload


def _policy_definitions(payload: dict[str, object]) -> list[dict[str, object]]:
    """Collect policy definitions returned by RabbitMQ management variants."""
    definitions: list[dict[str, object]] = []
    for field in ("effective_policy_definition", "operator_policy_definition"):
        definition = payload.get(field)
        if isinstance(definition, dict):
            definitions.append(definition)
    operator_policy = payload.get("operator_policy")
    if isinstance(operator_policy, dict):
        definition = operator_policy.get("definition")
        if isinstance(definition, dict):
            definitions.append(definition)
    return definitions


def _assert_queue_policy(payload: dict[str, object], expected: dict[str, Any]) -> None:
    """Fail closed when the broker's effective queue contract has drifted."""
    if payload.get("type") != expected["x-queue-type"]:
        raise RabbitMQTopologyError("RabbitMQ topology policy drift", code="topology_drift")
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        raise RabbitMQTopologyError("RabbitMQ topology policy drift", code="topology_drift")
    for key, value in expected.items():
        if key != "x-queue-type" and arguments.get(key) != value:
            raise RabbitMQTopologyError("RabbitMQ topology policy drift", code="topology_drift")
    for definition in _policy_definitions(payload):
        for key, value in expected.items():
            short_key = key.removeprefix("x-")
            if short_key in definition and definition[short_key] != value:
                raise RabbitMQTopologyError("RabbitMQ topology policy drift", code="topology_drift")


def _record_broker_queue_observation(worker_id: int, payload: dict[str, object]) -> None:
    """Keep only bounded queue counters for health/operations, never payloads."""
    messages_ready = payload.get("messages_ready")
    message_bytes_ready = payload.get("message_bytes_ready")
    if not isinstance(messages_ready, int) or not isinstance(message_bytes_ready, int):
        return
    observations = cast(dict[int, dict[str, int]], _runtime_status["broker_observations"])
    observations[worker_id] = {
        "messages_ready": max(0, messages_ready),
        "message_bytes_ready": max(0, message_bytes_ready),
    }


def inspect_topology_policies(worker_id: int) -> None:
    """Inspect effective queue policy without deleting or recreating queues."""
    names = topology_names(worker_id)
    worker_queue = _fetch_queue_details(names.queue)
    _assert_queue_policy(worker_queue, work_queue_arguments())
    _record_broker_queue_observation(worker_id, worker_queue)
    _assert_queue_policy(_fetch_queue_details(INFRASTRUCTURE_DLQ), infrastructure_queue_arguments())


def _fetch_feature_flags() -> frozenset[str]:
    """Read enabled feature flags through the internal management API."""

    try:
        payload = _management_request_json("feature-flags")
    except RabbitMQTopologyError:
        raise
    except Exception:
        # Do not chain urllib/Pika exceptions: their repr may contain the
        # broker URL and its userinfo.  Callers expose only this stable code.
        raise RabbitMQTopologyError(
            "RabbitMQ feature flag probe failed",
            code="rabbitmq_capability_probe_failed",
        ) from None

    entries: object = payload
    if isinstance(payload, dict):
        entries = payload.get("feature_flags", payload.get("flags", []))
    if not isinstance(entries, list):
        raise RabbitMQTopologyError(
            "RabbitMQ feature flag response is invalid",
            code="rabbitmq_capability_probe_failed",
        )
    enabled: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        state = entry.get("state")
        if isinstance(name, str) and state == "enabled":
            enabled.add(name)
    return frozenset(enabled)


def verify_runtime_capabilities(connection: pika.BlockingConnection) -> RabbitMQRuntimeCapabilities:
    """Fail closed unless the live Broker matches the frozen 4.3.5 baseline."""

    # ``BlockingConnection`` exposes the negotiated server properties on its
    # private ``SelectConnection`` implementation in Pika 1.3.2; a fake or a
    # future wrapper may expose them directly.  Read the public shape first,
    # then the actual Pika storage, otherwise every real connection would be
    # misclassified as an unknown-version configuration drift.
    properties = getattr(connection, "server_properties", None)
    if not isinstance(properties, dict) or not properties.get("version"):
        implementation = getattr(connection, "_impl", None)
        properties = getattr(implementation, "server_properties", properties)
    if not isinstance(properties, dict):
        properties = {}

    def property_value(name: str) -> object:
        return properties.get(name, properties.get(name.encode()))

    product = property_value("product")
    version = property_value("version")
    if isinstance(product, bytes):
        product = product.decode("utf-8", errors="replace")
    if isinstance(version, bytes):
        version = version.decode("utf-8", errors="replace")
    if product != "RabbitMQ" or version != RABBITMQ_BASELINE_VERSION:
        raise RabbitMQTopologyError(
            "RabbitMQ runtime configuration does not match the frozen baseline",
            code="rabbitmq_configuration_invalid",
        )

    feature_flags = _fetch_feature_flags()
    missing = REQUIRED_FEATURE_FLAGS - feature_flags
    if missing:
        raise RabbitMQTopologyError(
            "RabbitMQ runtime configuration is missing required feature flags",
            code="rabbitmq_configuration_invalid",
        )
    return RabbitMQRuntimeCapabilities(version=version, feature_flags=feature_flags)


def _declare(channel: Any, operation: str, callback: Any) -> Any:
    try:
        return callback()
    except pika.exceptions.ChannelClosedByBroker as exc:
        # A passive/active redeclare with incompatible durable arguments
        # closes only this channel.  Never delete or recreate a queue that may
        # contain accepted dispatches.
        raise RabbitMQTopologyError(
            f"RabbitMQ topology drift: {operation}", code="topology_drift"
        ) from exc
    except pika.exceptions.AMQPError as exc:
        raise RabbitMQTopologyError(
            f"RabbitMQ topology unavailable: {operation}", code="topology_unavailable"
        ) from exc


def bootstrap_topology(channel: Any, worker_id: int) -> TopologyNames:
    """Idempotently declare shared and fixed-Worker durable topology."""

    names = topology_names(worker_id)
    _declare(
        channel,
        "dispatch exchange",
        lambda: channel.exchange_declare(
            exchange=DISPATCH_EXCHANGE,
            exchange_type="direct",
            durable=True,
            auto_delete=False,
        ),
    )
    _declare(
        channel,
        "infrastructure exchange",
        lambda: channel.exchange_declare(
            exchange=INFRASTRUCTURE_DLX,
            exchange_type="direct",
            durable=True,
            auto_delete=False,
        ),
    )
    _declare(
        channel,
        "infrastructure queue",
        lambda: channel.queue_declare(
            queue=INFRASTRUCTURE_DLQ,
            durable=True,
            auto_delete=False,
            arguments=infrastructure_queue_arguments(),
        ),
    )
    _declare(
        channel,
        "worker queue",
        lambda: channel.queue_declare(
            queue=names.queue,
            durable=True,
            auto_delete=False,
            arguments=work_queue_arguments(),
        ),
    )
    _declare(
        channel,
        "infrastructure queue passive check",
        lambda: channel.queue_declare(queue=INFRASTRUCTURE_DLQ, passive=True),
    )
    _declare(
        channel,
        "worker queue passive check",
        lambda: channel.queue_declare(queue=names.queue, passive=True),
    )
    _declare(
        channel,
        "worker binding",
        lambda: channel.queue_bind(
            exchange=DISPATCH_EXCHANGE,
            queue=names.queue,
            routing_key=names.routing_key,
        ),
    )
    _declare(
        channel,
        "infrastructure binding",
        lambda: channel.queue_bind(
            exchange=INFRASTRUCTURE_DLX,
            queue=INFRASTRUCTURE_DLQ,
            routing_key=INFRASTRUCTURE_ROUTING_KEY,
        ),
    )
    return names


def bootstrap_worker_topology(
    connection: pika.BlockingConnection,
    worker_id: int,
    *,
    capabilities: RabbitMQRuntimeCapabilities | None = None,
) -> TopologyNames:
    """Bootstrap one Worker using a short-lived channel."""

    try:
        if capabilities is None:
            verify_runtime_capabilities(connection)
    except RabbitMQTopologyError as exc:
        mark_runtime_failure(exc.code)
        raise
    deadline_fired = False
    timer: tuple[Any, Any] | None = None
    channel: Any | None = None
    names: TopologyNames | None = None
    failure: RabbitMQTopologyError | None = None

    def expire_topology_deadline() -> None:
        nonlocal deadline_fired
        deadline_fired = True
        _abort_connection(connection, _TopologyDeadlineExceeded("topology deadline"))

    try:
        # Start before channel.open: every declare, passive check, bind and
        # cleanup handshake eventually enters Pika's _flush_output loop.
        timer = _schedule_topology_deadline(connection, expire_topology_deadline)
        channel = connection.channel()
        names = bootstrap_topology(channel, worker_id)
        inspect_topology_policies(worker_id)
    except RabbitMQTopologyError as exc:
        failure = exc
    except (pika.exceptions.AMQPError, TimeoutError):
        failure = RabbitMQTopologyError(
            "RabbitMQ topology operation exceeded its deadline",
            code="topology_unavailable",
        )
    except Exception:
        # Do not expose a Pika exception whose repr may contain URL userinfo.
        failure = RabbitMQTopologyError(
            "RabbitMQ topology operation failed", code="topology_unavailable"
        )
    finally:
        if channel is not None and getattr(channel, "is_open", True):
            try:
                channel.close()
            except Exception:
                if failure is None:
                    failure = RabbitMQTopologyError(
                        "RabbitMQ topology cleanup exceeded its deadline",
                        code="topology_unavailable",
                    )
        if deadline_fired and failure is None:
            failure = RabbitMQTopologyError(
                "RabbitMQ topology operation exceeded its deadline",
                code="topology_unavailable",
            )
        if deadline_fired or (failure is not None and timer is None):
            _abort_connection(connection, _TopologyDeadlineExceeded("topology deadline"))
        _remove_topology_deadline(timer)
    if failure is not None:
        mark_runtime_failure(failure.code)
        raise failure
    assert names is not None
    return names


def _close_connection_bounded(connection: pika.BlockingConnection) -> None:
    """Close a topology connection without an unbounded graceful flush."""

    if not getattr(connection, "is_open", True):
        return
    deadline_fired = False
    timer: tuple[Any, Any] | None = None

    def expire_close_deadline() -> None:
        nonlocal deadline_fired
        deadline_fired = True
        _abort_connection(connection, _TopologyDeadlineExceeded("connection close deadline"))

    try:
        try:
            timer = _schedule_topology_deadline(connection, expire_close_deadline)
        except RabbitMQTopologyError:
            # No direct I/O-loop timer means BlockingConnection.close() is not
            # safe to call: it may enter an unbounded flush.
            _abort_connection(connection, _TopologyDeadlineExceeded("connection close deadline"))
            return
        connection.close()
    except Exception:
        if deadline_fired:
            logger.debug("RabbitMQ topology connection close exceeded deadline")
        else:
            logger.debug("RabbitMQ topology connection close failed")
    finally:
        if deadline_fired:
            _abort_connection(connection, _TopologyDeadlineExceeded("connection close deadline"))
        _remove_topology_deadline(timer)


def bootstrap_configured_topology() -> int:
    """Bootstrap every configured Worker queue for a configured repair URL.

    The database lookup is deliberately outside the Broker connection.  A
    missing schema or Broker outage is reported as a degraded runtime state;
    no accepted Outbox responsibility is deleted or converted to success.
    """
    if not settings.rabbitmq_url:
        mark_runtime_failure("rabbitmq_not_configured")
        return 0
    from dlr.control.db import SessionLocal

    with SessionLocal() as session:
        worker_ids = [int(worker_id) for worker_id in session.scalars(select(Worker.id)).all()]
    if not worker_ids:
        _runtime_status["capability_verified"] = False
        _set_runtime_status("waiting_for_worker", worker_count=0)
        return 0
    connection: pika.BlockingConnection | None = None
    try:
        connection = connect()
        capabilities = verify_runtime_capabilities(connection)
        for worker_id in worker_ids:
            bootstrap_worker_topology(connection, worker_id, capabilities=capabilities)
        _persist_runtime_capabilities(worker_ids, capabilities)
    except RabbitMQTopologyError as exc:
        mark_runtime_failure(exc.code)
        raise
    except RabbitMQCapabilityPersistenceError:
        # A drift report whose durable invalidation failed is not a transient
        # Broker outage.  Preserve the persistence failure for health and the
        # caller instead of remapping it through the broad fallback below.
        raise
    except Exception:
        mark_runtime_failure("topology_unavailable")
        raise RabbitMQTopologyError(
            "RabbitMQ topology bootstrap failed", code="topology_unavailable"
        ) from None
    finally:
        if connection is not None:
            _close_connection_bounded(connection)
    mark_runtime_ready(len(worker_ids), worker_ids=worker_ids)
    return len(worker_ids)


async def topology_bootstrap_loop() -> None:
    """Keep fixed Worker topology reconciled while a repair URL is configured."""
    while True:
        try:
            if settings.rabbitmq_url:
                await asyncio.to_thread(bootstrap_configured_topology)
        except asyncio.CancelledError:
            raise
        except RabbitMQTopologyError as exc:
            logger.error("RabbitMQ topology bootstrap cycle failed: code=%s", exc.code)
        except RabbitMQCapabilityPersistenceError as exc:
            logger.error("RabbitMQ topology bootstrap cycle failed: code=%s", exc.code)
        except Exception:
            logger.error("RabbitMQ topology bootstrap cycle failed: code=topology_unavailable")
        await asyncio.sleep(5.0)
