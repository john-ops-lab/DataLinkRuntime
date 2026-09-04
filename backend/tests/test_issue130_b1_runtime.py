"""Focused Batch 1 lifecycle, topology and admission boundary tests."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pika
import pytest
from croniter import croniter
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings, validate_deployment_configuration
from dlr.control.models import (
    AdapterCredentialBinding,
    AdapterExecutionAdmission,
    AdapterExecutionSlot,
    AdapterSchedule,
    Credential,
    Execution,
    ExecutionArtifactHold,
    ExecutionAttempt,
    ExecutionCredentialBindingSnapshot,
    ExecutionIdempotencyRecord,
    ExecutionOutbox,
    GlobalExecutionAdmission,
    ManagedInputArtifact,
    ManagedInputUploadReservation,
    RabbitMQRuntimeCapability,
    ScheduleDispatchOutcome,
    Worker,
)
from dlr.control.services import adapter as adapter_service
from dlr.control.services import (
    admission,
    execution_cancellation,
    outbox,
    rabbitmq,
    reliable_execution,
)
from dlr.control.services import execution as execution_service
from dlr.control.services.schedule import (
    SCHEDULE_AUDIT_PAGE_SIZE,
    ScheduleOutcomeValidationError,
    _due_points,
    scheduler_tick,
    validate_schedule_outcome,
    validate_schedule_outcomes,
)
from test_adapters import create_adapter, save_version
from test_credentials import create_credential


def _enable_rabbitmq_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rabbitmq_execution_enabled", True)
    monkeypatch.setattr(settings, "rabbitmq_url", "amqp://dlr:test-password@rabbitmq:5672/%2F")
    monkeypatch.setattr(settings, "rabbitmq_management_url", "http://rabbitmq:15672")
    rabbitmq.mark_runtime_ready()


def _register_reliable_worker(client: TestClient, name: str) -> dict:
    response = client.post(
        "/api/workers/register",
        json={"name": name, "capabilities": ["python"], "protocol_version": 3},
        headers={"Authorization": "Bearer test-worker-token"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _execution_count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(Execution)) or 0)


def _prepare_reliable_task(
    client: TestClient,
    worker: dict,
    name: str,
    *,
    schedule: bool = False,
) -> dict:
    """Create a RabbitMQ task fixture with a fixed v3 Worker target."""
    adapter = create_adapter(client, name=name)
    save_version(client, adapter["id"])
    patched = client.patch(
        f"/api/adapters/{adapter['id']}",
        json={
            "runtime_worker_id": worker["id"],
            **({"run_mode": "schedule"} if schedule else {}),
        },
    )
    assert patched.status_code == 200, patched.text
    if schedule:
        configured = client.put(
            f"/api/adapters/{adapter['id']}/schedule",
            json={
                "enabled": False,
                "cron": "* * * * *",
                "timezone": "UTC",
                "input": {},
            },
        )
        assert configured.status_code == 200, configured.text
    return adapter


def _prepare_reliable_webhook(
    client: TestClient,
    worker: dict,
    name: str,
) -> tuple[dict, str]:
    """Create an enabled Webhook fixture targeting a v3 Worker."""
    adapter = create_adapter(client, name=name, adapter_type="webhook")
    credential = create_credential(
        client,
        name=f"{name}-token",
        type_="token",
        fields={"token": f"{name}-token-value"},
    )
    current = client.get(f"/api/adapters/{adapter['id']}/webhook")
    assert current.status_code == 200, current.text
    public_id = current.json()["public_id"]
    stopped = client.put(
        f"/api/adapters/{adapter['id']}/webhook",
        json={
            "enabled": False,
            "public_id": public_id,
            "credential_id": credential["id"],
        },
    )
    assert stopped.status_code == 200, stopped.text
    save_version(client, adapter["id"])
    patched = client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"runtime_worker_id": worker["id"]},
    )
    assert patched.status_code == 200, patched.text
    enabled = client.put(
        f"/api/adapters/{adapter['id']}/webhook",
        json={
            "enabled": True,
            "public_id": public_id,
            "credential_id": credential["id"],
        },
    )
    assert enabled.status_code == 200, enabled.text
    return adapter, public_id


class _FakeConnection:
    server_properties = {"product": "RabbitMQ", "version": "4.3.5"}

    def close(self) -> None:
        return None


class _DriftChannel:
    is_open = True

    def __init__(self) -> None:
        self.close_calls = 0

    def exchange_declare(self, **_: object) -> None:
        self.is_open = False
        raise pika.exceptions.ChannelClosedByBroker(406, "PRECONDITION_FAILED")

    def close(self) -> None:
        self.close_calls += 1
        raise AssertionError("closed channel must not be closed a second time")


class _DriftConnection(_FakeConnection):
    def __init__(self) -> None:
        self.channel_instance = _DriftChannel()
        self.is_closed = False
        self._impl = SimpleNamespace(ioloop=_ThreadedTimerLoop())

    def channel(self) -> _DriftChannel:
        return self.channel_instance


class _ThreadedTimerLoop:
    def __init__(self) -> None:
        self.timer_thread: threading.Timer | None = None

    def call_later(self, delay: float, callback: object) -> threading.Timer:
        assert callable(callback)
        self.timer_thread = threading.Timer(delay, callback)
        self.timer_thread.daemon = True
        self.timer_thread.start()
        return self.timer_thread

    @staticmethod
    def remove_timeout(handle: threading.Timer) -> None:
        handle.cancel()


class _HangingChannel:
    is_open = True

    def __init__(self, connection: "_HangingConnection") -> None:
        self.connection = connection

    def _wait_for_abort(self) -> None:
        assert self.connection.aborted.wait(1.0)
        error = self.connection.abort_error
        assert error is not None
        raise error

    def confirm_delivery(self) -> None:
        if self.connection.hang_at == "confirm":
            self._wait_for_abort()

    def basic_publish(self, **_: object) -> None:
        if self.connection.hang_at == "publish":
            self._wait_for_abort()

    def exchange_declare(self, **_: object) -> None:
        if self.connection.hang_at == "declare":
            self._wait_for_abort()

    def queue_declare(self, **_: object) -> None:
        if self.connection.hang_at == "declare":
            self._wait_for_abort()

    def queue_bind(self, **_: object) -> None:
        if self.connection.hang_at == "bind":
            self._wait_for_abort()

    def close(self) -> None:
        if self.connection.hang_at == "close":
            self._wait_for_abort()
        self.is_open = False


class _HangingImplementation:
    def __init__(self, connection: "_HangingConnection") -> None:
        self.connection = connection
        self.ioloop = _ThreadedTimerLoop()

    def _terminate_stream(self, error: Exception) -> None:
        self.connection.abort_error = error
        self.connection.is_closed = True
        self.connection.is_open = False
        self.connection.aborted.set()


class _HangingConnection:
    server_properties = {"product": "RabbitMQ", "version": "4.3.5"}

    def __init__(self, hang_at: str) -> None:
        self.hang_at = hang_at
        self.is_open = True
        self.is_closed = False
        self.aborted = threading.Event()
        self.abort_error: Exception | None = None
        self._impl = _HangingImplementation(self)
        self.channel_instance = _HangingChannel(self)

    def channel(self) -> _HangingChannel:
        if self.hang_at == "channel":
            assert self.aborted.wait(1.0)
            error = self.abort_error
            assert error is not None
            raise error
        return self.channel_instance

    def close(self) -> None:
        if self.hang_at == "connection_close":
            assert self.aborted.wait(1.0)
            error = self.abort_error
            assert error is not None
            raise error
        self.is_open = False
        self.is_closed = True


def test_runtime_capability_probe_requires_exact_version_and_feature_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "rabbitmq_url", "amqp://dlr:test-password@rabbitmq:5672/%2F")
    monkeypatch.setattr(settings, "rabbitmq_management_url", "http://rabbitmq:15672")
    monkeypatch.setattr(rabbitmq, "_fetch_feature_flags", lambda: rabbitmq.REQUIRED_FEATURE_FLAGS)

    capabilities = rabbitmq.verify_runtime_capabilities(_FakeConnection())
    assert capabilities.version == "4.3.5"
    assert capabilities.feature_flags == rabbitmq.REQUIRED_FEATURE_FLAGS

    monkeypatch.setattr(rabbitmq, "_fetch_feature_flags", lambda: frozenset({"quorum_queue"}))
    with pytest.raises(rabbitmq.RabbitMQTopologyError) as error:
        rabbitmq.verify_runtime_capabilities(_FakeConnection())
    assert error.value.code == "rabbitmq_configuration_invalid"


def test_runtime_capability_probe_reads_pika_blocking_connection_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pika 1.3.2 stores negotiated properties on BlockingConnection._impl."""
    _enable_rabbitmq_test(monkeypatch)
    connection = SimpleNamespace(
        _impl=SimpleNamespace(server_properties={"product": "RabbitMQ", "version": "4.3.5"})
    )
    monkeypatch.setattr(rabbitmq, "_fetch_feature_flags", lambda: rabbitmq.REQUIRED_FEATURE_FLAGS)

    capabilities = rabbitmq.verify_runtime_capabilities(connection)  # type: ignore[arg-type]

    assert capabilities == rabbitmq.RabbitMQRuntimeCapabilities(
        version="4.3.5",
        feature_flags=rabbitmq.REQUIRED_FEATURE_FLAGS,
    )


def test_runtime_probe_requires_explicit_management_url_without_guessing_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    monkeypatch.setattr(settings, "rabbitmq_management_url", None)

    with pytest.raises(rabbitmq.RabbitMQTopologyError) as error:
        rabbitmq.verify_runtime_capabilities(_FakeConnection())

    assert error.value.code == "rabbitmq_capability_probe_failed"
    monkeypatch.setattr(settings, "rabbitmq_management_url", "https://rabbitmq:9443/base")
    assert rabbitmq._management_feature_flags_url() == (
        "https://rabbitmq:9443/base/api/feature-flags"
    )
    assert "15671" not in rabbitmq._management_feature_flags_url()


def test_rabbitmq_url_validation_rejects_percent_decoded_guest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    monkeypatch.setattr(
        settings,
        "rabbitmq_url",
        "amqp://%67%75%65%73%74:test-password@rabbitmq:5672/%2F",
    )

    with pytest.raises(ValueError, match="non-guest user"):
        validate_deployment_configuration(settings)


def test_raw_rabbitmq_vhost_is_encoded_once_for_the_amqp_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    raw_vhost = "tenant/蓝色?queue#1"
    monkeypatch.setattr(settings, "rabbitmq_url", "amqp://dlr:test-password@rabbitmq:5672")
    monkeypatch.setattr(settings, "rabbitmq_vhost", raw_vhost)

    effective_url = rabbitmq.effective_rabbitmq_url()
    assert effective_url.startswith("amqp://dlr:test-password@rabbitmq:5672/")
    assert "/tenant%2F" in effective_url
    assert "%E8%93%9D%E8%89%B2" in effective_url
    assert "%3Fqueue%231" in effective_url
    assert pika.URLParameters(effective_url).virtual_host == raw_vhost
    assert rabbitmq.connection_parameters().virtual_host == raw_vhost
    assert rabbitmq._configured_vhost() == raw_vhost


@pytest.mark.parametrize("raw_vhost", ["", "bad\nname", "x" * 256])
def test_rabbitmq_raw_vhost_validation_rejects_unsafe_values(
    monkeypatch: pytest.MonkeyPatch,
    raw_vhost: str,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    monkeypatch.setattr(settings, "rabbitmq_vhost", raw_vhost)

    with pytest.raises(ValueError, match="DLR_RABBITMQ_VHOST"):
        validate_deployment_configuration(settings)


def test_rabbitmq_vhost_is_the_only_url_path_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    monkeypatch.setattr(settings, "rabbitmq_vhost", "tenant-a")

    with pytest.raises(ValueError, match="must omit its vhost path"):
        monkeypatch.setattr(
            settings,
            "rabbitmq_url",
            "amqp://dlr:test-password@rabbitmq:5672/%2F",
        )
        validate_deployment_configuration(settings)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {
                "rabbitmq_publisher_channel_count": 1,
                "rabbitmq_publisher_max_concurrency": 2,
            },
            "must not exceed DLR_RABBITMQ_PUBLISHER_CHANNEL_COUNT",
        ),
        (
            {"rabbitmq_publisher_max_confirm_inflight": 5},
            "must not exceed DLR_RABBITMQ_PUBLISHER_MAX_CONCURRENCY",
        ),
        (
            {"rabbitmq_queue_max_length": 19},
            "must leave room for publisher confirms",
        ),
        (
            {"rabbitmq_queue_max_bytes": 327_679},
            "must leave room for publisher confirms",
        ),
    ],
)
def test_publisher_windows_and_broker_headroom_fail_fast(
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, object],
    message: str,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    for name, value in changes.items():
        monkeypatch.setattr(settings, name, value)

    with pytest.raises(ValueError, match=message):
        validate_deployment_configuration(settings)


def test_publisher_health_exposes_finite_windows_and_failure_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    before = outbox.publisher_health()
    assert before["channel_limit"] == 4
    assert before["publish_concurrency_limit"] == 4
    assert before["confirm_inflight_limit"] == 4
    assert before["channels_in_use"] == 0
    assert before["publish_concurrency_in_use"] == 0
    assert before["confirm_inflight"] == 0
    assert before["configured_headroom_messages"] == 16
    assert before["configured_headroom_bytes"] == 262_144

    outbox._record_publish_failure("publisher_nack")
    outbox._record_publish_failure("mandatory_return")
    outbox._record_publish_failure("publisher_confirm_timeout")
    outbox._record_publish_failure("publish_timeout_or_connection")
    after = outbox.publisher_health()
    assert after["reject_count"] == before["reject_count"] + 1
    assert after["nack_count"] == before["nack_count"] + 1
    assert after["return_count"] == before["return_count"] + 1
    assert after["timeout_count"] == before["timeout_count"] + 1
    assert after["connection_loss_count"] == before["connection_loss_count"] + 1
    assert after["alerts"] == []


def test_broker_health_exposes_observed_queue_headroom_and_overshoot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    monkeypatch.setitem(rabbitmq._runtime_status, "broker_observations", {})
    monkeypatch.setattr(settings, "rabbitmq_queue_max_length", 10)
    monkeypatch.setattr(settings, "rabbitmq_queue_max_bytes", 1_024)
    monkeypatch.setattr(settings, "rabbitmq_broker_headroom_messages", 2)
    monkeypatch.setattr(settings, "rabbitmq_broker_headroom_bytes", 256)

    rabbitmq._record_broker_queue_observation(
        7,
        {"messages_ready": 9, "message_bytes_ready": 900},
    )
    payload = rabbitmq._broker_health_payload()

    assert payload["observed_queues"] == {"7": {"messages_ready": 9, "message_bytes_ready": 900}}
    assert payload["headroom_messages"] == 1
    assert payload["headroom_bytes"] == 124
    assert payload["alerts"] == [
        "broker_queue_headroom_messages_low",
        "broker_queue_headroom_bytes_low",
    ]

    rabbitmq._record_broker_queue_observation(
        7,
        {"messages_ready": 11, "message_bytes_ready": 1_100},
    )
    assert "broker_queue_length_overshoot" in rabbitmq._broker_health_payload()["alerts"]
    assert "broker_queue_bytes_overshoot" in rabbitmq._broker_health_payload()["alerts"]


def test_relay_limits_lease_batch_to_finite_publish_concurrency(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "rabbitmq_publisher_channel_count", 1)
    monkeypatch.setattr(settings, "rabbitmq_publisher_max_concurrency", 1)
    monkeypatch.setattr(settings, "rabbitmq_publisher_max_confirm_inflight", 1)
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "bounded-publisher-worker")
    adapter = _prepare_reliable_task(api_client, worker, "bounded-publisher")
    accepted = [
        api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}) for _ in range(2)
    ]
    assert all(response.status_code == 202 for response in accepted), [
        response.text for response in accepted
    ]

    monkeypatch.setattr(rabbitmq, "bootstrap_worker_topology", lambda *_: None)
    monkeypatch.setattr(outbox, "_publish_row", lambda *_: None)
    with session_factory() as session:
        result = outbox.relay_once(
            session,
            "bounded-publisher-relay",
            limit=10,
            connection_factory=_FakeConnection,
        )

    assert result == outbox.OutboxRelayResult(1, 1, 0)
    with session_factory() as session:
        pending = list(
            session.scalars(select(ExecutionOutbox).where(ExecutionOutbox.status == "pending"))
        )
        assert len(pending) == 1
        assert pending[0].lease_owner is None


@pytest.mark.parametrize(
    "error_code",
    [
        "publisher_nack",
        "mandatory_return",
        "publisher_confirm_timeout",
        "publish_timeout_or_connection",
    ],
)
def test_relay_failures_keep_pending_and_schedule_capped_retry(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, f"relay-failure-{error_code}")
    adapter = _prepare_reliable_task(api_client, worker, f"relay-failure-{error_code}")
    accepted = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert accepted.status_code == 202, accepted.text

    def fail_publish(*_: object) -> None:
        raise outbox.OutboxPublishError(error_code)

    monkeypatch.setattr(rabbitmq, "bootstrap_worker_topology", lambda *_: None)
    monkeypatch.setattr(outbox, "_publish_row", fail_publish)
    with session_factory() as session:
        result = outbox.relay_once(
            session,
            f"relay-failure-owner-{error_code}",
            connection_factory=_FakeConnection,
        )
        assert result == outbox.OutboxRelayResult(1, 0, 1)
        row = session.scalar(select(ExecutionOutbox))
        assert row is not None
        assert row.status == "pending"
        assert row.lease_owner is None
        assert row.last_error_code == error_code
        assert row.publish_attempts == 1
        assert row.available_at > row.created_at


def test_runtime_capability_mismatch_closes_new_ingress_and_health_is_stable(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    monkeypatch.setattr(rabbitmq, "_fetch_feature_flags", lambda: frozenset())
    with pytest.raises(rabbitmq.RabbitMQTopologyError) as error:
        rabbitmq.bootstrap_worker_topology(_FakeConnection(), 1)
    assert error.value.code == "rabbitmq_configuration_invalid"
    assert rabbitmq.ingress_configuration_ready() is False
    assert rabbitmq.runtime_health()["last_error_code"] == "rabbitmq_configuration_invalid"


def test_unverified_runtime_is_fail_closed_before_first_async_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    monkeypatch.setitem(rabbitmq._runtime_status, "status", "disabled")
    monkeypatch.setitem(rabbitmq._runtime_status, "last_error_code", None)
    monkeypatch.setitem(rabbitmq._runtime_status, "capability_verified", False)
    assert rabbitmq.ingress_configuration_ready() is False
    assert rabbitmq.runtime_health()["last_error_code"] == "rabbitmq_not_verified"


def test_persisted_capability_survives_cold_outage_without_batch_local_or_drift_bypass(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the complete, matching Worker generation can reopen ingress."""
    _enable_rabbitmq_test(monkeypatch)
    worker_one = _register_reliable_worker(api_client, "persisted-capability-worker-one")
    worker_two = _register_reliable_worker(api_client, "persisted-capability-worker-two")
    adapter = create_adapter(api_client, name="persisted-capability-adapter")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker_one["id"]},
        ).status_code
        == 200
    )

    with session_factory.begin() as session:
        session.add(
            RabbitMQRuntimeCapability(
                id=1,
                configuration_fingerprint=rabbitmq.configuration_fingerprint(
                    [worker_one["id"], worker_two["id"]]
                ),
                broker_version="4.3.5",
                feature_flags=sorted(rabbitmq.REQUIRED_FEATURE_FLAGS),
                worker_ids=sorted([worker_one["id"], worker_two["id"]]),
                verified_at=datetime.now(UTC) - timedelta(seconds=5),
            )
        )

    # A transient Broker outage is not a configuration drift and must retain
    # the restart-safe generation.
    rabbitmq.mark_runtime_failure("topology_unavailable")
    with session_factory() as session:
        assert session.get(RabbitMQRuntimeCapability, 1) is not None

    # Model a freshly restarted Control whose Broker is temporarily down:
    # there is no process-local capability, but the DB generation still
    # matches the configured complete Worker set.
    for key, value in {
        "status": "disabled",
        "last_error_code": None,
        "worker_count": 0,
        "capability_verified": False,
        "configuration_fingerprint": None,
        "verified_worker_ids": None,
    }.items():
        monkeypatch.setitem(rabbitmq._runtime_status, key, value)

    accepted = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert accepted.status_code == 202, accepted.text
    execution_id = accepted.json()["id"]
    with session_factory() as session:
        assert rabbitmq.ingress_configuration_ready(session, worker_id=worker_two["id"])
        assert rabbitmq.runtime_health(session)["ingress"]["ready"] is True

    # Restore the complete process-local generation before the batch-local
    # Relay probe.  The Relay must leave this full capability untouched.
    rabbitmq.mark_runtime_ready(2, worker_ids=sorted([worker_one["id"], worker_two["id"]]))
    with session_factory() as session:
        assert rabbitmq.runtime_health(session)["ingress"]["ready"] is True

    # Relay only the W1 row.  It must not replace the complete persisted W1+W2
    # generation with this batch-local target subset.
    monkeypatch.setattr(rabbitmq, "bootstrap_worker_topology", lambda *_: None)
    monkeypatch.setattr(outbox, "_publish_row", lambda *_: None)
    with session_factory() as session:
        result = outbox.relay_once(
            session,
            "persisted-capability-relay",
            limit=1,
            connection_factory=_FakeConnection,
        )
    assert result == outbox.OutboxRelayResult(1, 1, 0)
    with session_factory() as session:
        assert rabbitmq.ingress_configuration_ready(session, worker_id=worker_two["id"])
        assert rabbitmq.runtime_health(session)["ingress"]["ready"] is True
        state = session.get(RabbitMQRuntimeCapability, 1)
        assert state is not None
        assert state.worker_ids == sorted([worker_one["id"], worker_two["id"]])
    assert rabbitmq._runtime_status["verified_worker_ids"] == frozenset(
        [worker_one["id"], worker_two["id"]]
    )

    # A configuration generation change is fail-closed even while the Broker
    # is unavailable; no second responsibility may be accepted.
    before = _execution_count(session_factory)
    monkeypatch.setattr(settings, "rabbitmq_management_url", "http://changed:15672")
    drifted = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert drifted.status_code == 503
    assert _execution_count(session_factory) == before

    # Restore the URL, then add a new Worker.  The unverified membership change
    # invalidates the complete generation, even if the old W1 target remains.
    monkeypatch.setattr(settings, "rabbitmq_management_url", "http://rabbitmq:15672")
    worker_three = _register_reliable_worker(api_client, "persisted-capability-worker-three")
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker_three["id"]},
        ).status_code
        == 200
    )
    unverified = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert unverified.status_code == 503
    assert _execution_count(session_factory) == before
    with session_factory() as session:
        assert session.get(Execution, execution_id) is not None


def test_live_drift_invalidates_persisted_capability_before_cold_restart(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed drift cannot reopen ingress from stale DB evidence."""
    _enable_rabbitmq_test(monkeypatch)
    worker_one = _register_reliable_worker(api_client, "drift-persisted-worker-one")
    worker_two = _register_reliable_worker(api_client, "drift-persisted-worker-two")
    adapter = create_adapter(api_client, name="drift-persisted-adapter")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker_one["id"]},
        ).status_code
        == 200
    )

    with session_factory.begin() as session:
        session.add(
            RabbitMQRuntimeCapability(
                id=1,
                configuration_fingerprint=rabbitmq.configuration_fingerprint(
                    [worker_one["id"], worker_two["id"]]
                ),
                broker_version="4.3.5",
                feature_flags=sorted(rabbitmq.REQUIRED_FEATURE_FLAGS),
                worker_ids=sorted([worker_one["id"], worker_two["id"]]),
                verified_at=datetime.now(UTC) - timedelta(seconds=5),
            )
        )

    rabbitmq.mark_runtime_failure("topology_drift")
    with session_factory() as session:
        assert session.get(RabbitMQRuntimeCapability, 1) is None

    # A restarted process has no transient error in memory, but the stale
    # capability row was durably removed and the DB still has Workers.
    for key, value in {
        "status": "disabled",
        "last_error_code": None,
        "worker_count": 0,
        "capability_verified": False,
        "configuration_fingerprint": None,
        "verified_worker_ids": None,
    }.items():
        monkeypatch.setitem(rabbitmq._runtime_status, key, value)

    with session_factory() as session:
        status = rabbitmq.runtime_health(session)
        assert status["ready"] is False
        assert status["repair"]["status"] == "degraded"
        assert status["repair"]["last_error_code"] == "rabbitmq_not_verified"

    assert api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}).status_code == 503
    assert api_client.get("/api/health").status_code == 503


def test_bootstrap_preserves_capability_persistence_failure(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bootstrap must not remap failed drift invalidation to an outage."""
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "bootstrap-persistence-failure-worker")
    with session_factory.begin() as session:
        session.add(
            RabbitMQRuntimeCapability(
                id=1,
                configuration_fingerprint=rabbitmq.configuration_fingerprint([worker["id"]]),
                broker_version="4.3.5",
                feature_flags=sorted(rabbitmq.REQUIRED_FEATURE_FLAGS),
                worker_ids=[worker["id"]],
                verified_at=datetime.now(UTC),
            )
        )

    def fail_invalidation() -> None:
        raise rabbitmq.RabbitMQCapabilityPersistenceError(
            "RabbitMQ capability invalidation could not be committed"
        )

    def drift_bootstrap(*_: object, **__: object) -> None:
        rabbitmq.mark_runtime_failure("topology_drift")

    monkeypatch.setattr(rabbitmq, "_invalidate_persisted_capability", fail_invalidation)
    monkeypatch.setattr(rabbitmq, "connect", lambda: _FakeConnection())
    monkeypatch.setattr(
        rabbitmq,
        "verify_runtime_capabilities",
        lambda _: rabbitmq.RabbitMQRuntimeCapabilities(
            version="4.3.5", feature_flags=rabbitmq.REQUIRED_FEATURE_FLAGS
        ),
    )
    monkeypatch.setattr(rabbitmq, "bootstrap_worker_topology", drift_bootstrap)

    with pytest.raises(rabbitmq.RabbitMQCapabilityPersistenceError) as error:
        rabbitmq.bootstrap_configured_topology()

    assert error.value.code == "rabbitmq_capability_persistence_failed"
    assert rabbitmq._runtime_status["last_error_code"] == "rabbitmq_capability_persistence_failed"
    with session_factory() as session:
        assert session.get(RabbitMQRuntimeCapability, 1) is not None
        status = rabbitmq.runtime_health(session)
        assert status["ready"] is False
        assert status["last_error_code"] == "rabbitmq_capability_persistence_failed"


def test_relay_preserves_capability_persistence_failure_and_releases_lease(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relay keeps Outbox responsibility recoverable when drift invalidation fails."""
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "relay-persistence-failure-worker")
    adapter = create_adapter(api_client, name="relay-persistence-failure")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker["id"]},
        ).status_code
        == 200
    )
    accepted = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert accepted.status_code == 202, accepted.text

    with session_factory.begin() as session:
        session.add(
            RabbitMQRuntimeCapability(
                id=1,
                configuration_fingerprint=rabbitmq.configuration_fingerprint([worker["id"]]),
                broker_version="4.3.5",
                feature_flags=sorted(rabbitmq.REQUIRED_FEATURE_FLAGS),
                worker_ids=[worker["id"]],
                verified_at=datetime.now(UTC),
            )
        )

    def fail_invalidation() -> None:
        raise rabbitmq.RabbitMQCapabilityPersistenceError(
            "RabbitMQ capability invalidation could not be committed"
        )

    def drift_bootstrap(*_: object, **__: object) -> None:
        raise rabbitmq.RabbitMQTopologyError(
            "RabbitMQ topology policy drift", code="topology_drift"
        )

    monkeypatch.setattr(rabbitmq, "_invalidate_persisted_capability", fail_invalidation)
    monkeypatch.setattr(rabbitmq, "bootstrap_worker_topology", drift_bootstrap)
    with session_factory() as session:
        result = outbox.relay_once(
            session,
            "relay-persistence-failure",
            connection_factory=_FakeConnection,
        )
        assert result == outbox.OutboxRelayResult(1, 0, 1)
        row = session.scalar(select(ExecutionOutbox))
        assert row is not None
        assert row.status == "pending"
        assert row.lease_owner is None
        assert row.last_error_code == "rabbitmq_capability_persistence_failed"
        assert rabbitmq.runtime_health(session)["last_error_code"] == (
            "rabbitmq_capability_persistence_failed"
        )

    with session_factory() as session:
        assert session.get(RabbitMQRuntimeCapability, 1) is not None


def test_targetless_health_rejects_batch_local_worker_generation(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-Worker probe cannot claim a complete topology is healthy."""
    _enable_rabbitmq_test(monkeypatch)
    worker_one = _register_reliable_worker(api_client, "targetless-health-worker-one")
    worker_two = _register_reliable_worker(api_client, "targetless-health-worker-two")
    rabbitmq.mark_runtime_ready(1, worker_ids=[worker_one["id"]])

    with session_factory() as session:
        status = rabbitmq.runtime_health(session)
        assert status["ready"] is False
        assert status["ingress"]["ready"] is False
        assert status["repair"]["ready"] is False
        assert status["last_error_code"] == "rabbitmq_not_verified"
        assert not rabbitmq.ingress_configuration_ready(session)
        assert not rabbitmq.ingress_configuration_ready(session, worker_id=worker_two["id"])

    # Without a DB session there is no evidence that a target-scoped local
    # probe covers the complete Worker set either.
    assert rabbitmq.ingress_configuration_ready() is False
    assert rabbitmq.runtime_health()["ready"] is False


def test_gate_off_does_not_hide_configured_repair_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    monkeypatch.setattr(settings, "rabbitmq_execution_enabled", False)
    rabbitmq.mark_runtime_failure("topology_unavailable")

    status = rabbitmq.runtime_health()

    assert status["enabled"] is False
    assert status["ready"] is False
    assert status["repair"]["status"] == "degraded"
    assert status["repair"]["last_error_code"] == "topology_unavailable"


def test_topology_drift_does_not_mask_original_error_with_second_channel_close(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rabbitmq, "verify_runtime_capabilities", lambda _: None)
    connection = _DriftConnection()

    with pytest.raises(rabbitmq.RabbitMQTopologyError) as error:
        rabbitmq.bootstrap_worker_topology(connection, 1)

    assert error.value.code == "topology_drift"
    assert connection.channel_instance.close_calls == 0


def _fake_dispatch_outbox_row() -> SimpleNamespace:
    return SimpleNamespace(
        message_id="00000000-0000-0000-0000-000000000001",
        routing_key="worker.1",
        payload_json={
            "schema_version": 1,
            "message_id": "00000000-0000-0000-0000-000000000001",
            "execution_id": 1,
            "dispatch_generation": 1,
            "adapter_id": 1,
            "language": "python",
            "resource_class": "standard",
            "target_worker_id": 1,
        },
    )


@pytest.mark.parametrize("hang_at", ("channel", "confirm", "publish", "close"))
def test_publish_row_deadline_covers_every_blocking_pika_phase(
    monkeypatch: pytest.MonkeyPatch,
    hang_at: str,
) -> None:
    """A stuck channel handshake, confirm or cleanup cannot hold a Relay."""

    monkeypatch.setattr(settings, "rabbitmq_publish_timeout_seconds", 0.05)
    connection = _HangingConnection(hang_at)
    started = time.monotonic()

    with pytest.raises(outbox.OutboxPublishError) as error:
        outbox._publish_row(connection, _fake_dispatch_outbox_row())

    elapsed = time.monotonic() - started
    assert error.value.code == "publisher_confirm_timeout"
    assert elapsed < 0.75
    assert connection.is_closed is True
    timer_thread = connection._impl.ioloop.timer_thread
    assert timer_thread is not None
    timer_thread.join(1.0)
    assert not timer_thread.is_alive()


def test_connection_parameters_keep_stack_timeout_separate_from_confirm_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    monkeypatch.setattr(settings, "rabbitmq_stack_timeout_seconds", 7.0)
    monkeypatch.setattr(settings, "rabbitmq_publish_timeout_seconds", 0.25)

    parameters = rabbitmq.connection_parameters()

    assert parameters.stack_timeout == 7.0
    assert parameters.socket_timeout == 7.0
    assert parameters.blocked_connection_timeout == 0.25


@pytest.mark.parametrize("hang_at", ("channel", "declare", "bind", "close"))
def test_topology_deadline_covers_each_amqp_handshake_phase(
    monkeypatch: pytest.MonkeyPatch,
    hang_at: str,
) -> None:
    monkeypatch.setattr(settings, "rabbitmq_publish_timeout_seconds", 0.05)
    monkeypatch.setattr(rabbitmq, "verify_runtime_capabilities", lambda _: None)
    monkeypatch.setattr(rabbitmq, "inspect_topology_policies", lambda _: None)
    connection = _HangingConnection(hang_at)
    started = time.monotonic()

    with pytest.raises(rabbitmq.RabbitMQTopologyError) as error:
        rabbitmq.bootstrap_worker_topology(connection, 1)

    elapsed = time.monotonic() - started
    assert error.value.code == "topology_unavailable"
    assert elapsed < 0.75
    assert connection.is_closed is True
    timer_thread = connection._impl.ioloop.timer_thread
    assert timer_thread is not None
    timer_thread.join(1.0)
    assert not timer_thread.is_alive()


def test_topology_connection_cleanup_deadline_does_not_gracefully_flush_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "rabbitmq_publish_timeout_seconds", 0.05)
    connection = _HangingConnection("connection_close")
    started = time.monotonic()

    rabbitmq._close_connection_bounded(connection)

    assert time.monotonic() - started < 0.75
    assert connection.is_closed is True
    timer_thread = connection._impl.ioloop.timer_thread
    assert timer_thread is not None
    timer_thread.join(1.0)
    assert not timer_thread.is_alive()


def test_confirm_deadline_releases_all_leases_and_allows_a_later_retry(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    monkeypatch.setattr(settings, "rabbitmq_publish_timeout_seconds", 0.05)
    worker = _register_reliable_worker(api_client, "confirm-deadline-worker")
    adapter = create_adapter(api_client, name="confirm-deadline")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker["id"]},
        ).status_code
        == 200
    )
    accepted = [
        api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}) for _ in range(2)
    ]
    assert all(response.status_code == 202 for response in accepted), [
        response.text for response in accepted
    ]

    monkeypatch.setattr(rabbitmq, "bootstrap_worker_topology", lambda *_: None)
    with session_factory() as session:
        result = outbox.relay_once(
            session,
            "confirm-deadline-test",
            connection_factory=lambda: _HangingConnection("publish"),
        )
        assert result == outbox.OutboxRelayResult(2, 0, 2)
        rows = list(session.scalars(select(ExecutionOutbox).order_by(ExecutionOutbox.id)).all())
        assert len(rows) == 2
        assert all(row.status == "pending" for row in rows)
        assert all(row.lease_owner is None for row in rows)
        assert all(row.last_error_code == "publisher_confirm_timeout" for row in rows)

    with session_factory.begin() as session:
        session.execute(
            update(ExecutionOutbox).values(available_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    monkeypatch.setattr(outbox, "_publish_row", lambda *_: None)
    with session_factory() as session:
        result = outbox.relay_once(
            session,
            "confirm-deadline-retry",
            connection_factory=_FakeConnection,
        )
        assert result == outbox.OutboxRelayResult(2, 2, 0)

    with session_factory() as session:
        assert (
            session.scalar(select(ExecutionOutbox.id).where(ExecutionOutbox.status == "pending"))
            is None
        )


def test_topology_deadline_releases_all_leases_and_allows_a_later_retry(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    monkeypatch.setattr(settings, "rabbitmq_publish_timeout_seconds", 0.05)
    worker = _register_reliable_worker(api_client, "topology-deadline-worker")
    adapter = create_adapter(api_client, name="topology-deadline")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker["id"]},
        ).status_code
        == 200
    )
    accepted = [
        api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}) for _ in range(2)
    ]
    assert all(response.status_code == 202 for response in accepted), [
        response.text for response in accepted
    ]

    monkeypatch.setattr(rabbitmq, "verify_runtime_capabilities", lambda _: None)
    monkeypatch.setattr(rabbitmq, "inspect_topology_policies", lambda _: None)
    with session_factory() as session:
        result = outbox.relay_once(
            session,
            "topology-deadline-test",
            connection_factory=lambda: _HangingConnection("declare"),
        )
        assert result == outbox.OutboxRelayResult(2, 0, 2)
        rows = list(session.scalars(select(ExecutionOutbox).order_by(ExecutionOutbox.id)).all())
        assert len(rows) == 2
        assert all(row.status == "pending" for row in rows)
        assert all(row.lease_owner is None for row in rows)
        assert all(row.last_error_code == "topology_unavailable" for row in rows)

    with session_factory.begin() as session:
        session.execute(
            update(ExecutionOutbox).values(available_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    monkeypatch.setattr(rabbitmq, "bootstrap_worker_topology", lambda *_: None)
    monkeypatch.setattr(outbox, "_publish_row", lambda *_: None)
    with session_factory() as session:
        result = outbox.relay_once(
            session,
            "topology-deadline-retry",
            connection_factory=_FakeConnection,
        )
        assert result == outbox.OutboxRelayResult(2, 2, 0)


def test_postgres_execution_attempt_has_one_active_row_under_concurrency(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real partial unique index rejects a second active Attempt."""
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "attempt-concurrency-worker")
    adapter = _prepare_reliable_task(api_client, worker, "attempt-concurrency")
    accepted = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert accepted.status_code == 202, accepted.text
    execution_id = accepted.json()["id"]
    now = datetime.now(UTC)
    barrier = threading.Barrier(2)

    def insert_active_attempt(attempt_no: int) -> str:
        with session_factory() as session:
            session.execute(text("SET lock_timeout = '2s'"))
            session.add(
                ExecutionAttempt(
                    execution_id=execution_id,
                    adapter_id=adapter["id"],
                    attempt_no=attempt_no,
                    worker_id=worker["id"],
                    fencing_token=attempt_no,
                    lease_expires_at=now + timedelta(minutes=5),
                    status="claimed",
                    claimed_at=now,
                )
            )
            barrier.wait(timeout=5)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return "rejected"
            return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(insert_active_attempt, (1, 2)))

    assert sorted(outcomes) == ["accepted", "rejected"]
    with session_factory() as session:
        active = list(
            session.scalars(
                select(ExecutionAttempt).where(
                    ExecutionAttempt.execution_id == execution_id,
                    ExecutionAttempt.status.in_(("claimed", "running")),
                )
            )
        )
        assert len(active) == 1
        assert active[0].attempt_no in {1, 2}


def test_postgres_adapter_slot_zero_has_one_active_attempt_under_concurrency(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slot 0's conditional row update serializes two active Attempt owners."""
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "slot-concurrency-worker")
    adapter = _prepare_reliable_task(api_client, worker, "slot-concurrency")
    accepted = [
        api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}) for _ in range(2)
    ]
    assert all(response.status_code == 202 for response in accepted), [
        response.text for response in accepted
    ]
    execution_ids = [response.json()["id"] for response in accepted]
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        attempts = []
        for execution_id, attempt_no in zip(execution_ids, (1, 2), strict=True):
            attempt = ExecutionAttempt(
                execution_id=execution_id,
                adapter_id=adapter["id"],
                attempt_no=attempt_no,
                worker_id=worker["id"],
                fencing_token=attempt_no,
                lease_expires_at=now + timedelta(minutes=5),
                status="claimed",
                claimed_at=now,
            )
            session.add(attempt)
            attempts.append(attempt)
        session.flush()
        attempt_ids = [attempt.id for attempt in attempts]

    barrier = threading.Barrier(2)

    def bind_slot(attempt_id: int) -> bool:
        with session_factory() as session:
            session.execute(text("SET lock_timeout = '2s'"))
            barrier.wait(timeout=5)
            result = session.execute(
                update(AdapterExecutionSlot)
                .where(
                    AdapterExecutionSlot.adapter_id == adapter["id"],
                    AdapterExecutionSlot.slot_no == 0,
                    AdapterExecutionSlot.active_attempt_id.is_(None),
                )
                .values(
                    active_attempt_id=attempt_id,
                    lease_expires_at=now + timedelta(minutes=5),
                    fencing_token=attempt_id,
                )
                .returning(AdapterExecutionSlot.active_attempt_id)
            )
            winner = result.scalar_one_or_none()
            session.commit()
            return winner == attempt_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(bind_slot, attempt_ids))

    assert sorted(outcomes) == [False, True]
    with session_factory() as session:
        slot = session.get(AdapterExecutionSlot, (adapter["id"], 0))
        assert slot is not None
        assert slot.active_attempt_id in set(attempt_ids)


def test_relay_isolates_poison_payload_and_publishes_good_row_in_same_batch(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One corrupt Outbox body cannot head-of-line block a valid responsibility."""
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "relay-poison-worker")
    adapter = create_adapter(api_client, name="relay-poison")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker["id"]},
        ).status_code
        == 200
    )
    accepted = [
        api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}) for _ in range(2)
    ]
    assert all(response.status_code == 202 for response in accepted), [
        response.text for response in accepted
    ]
    sentinel = "poison-payload-secret"
    with session_factory.begin() as session:
        rows = list(session.scalars(select(ExecutionOutbox).order_by(ExecutionOutbox.id)).all())
        assert len(rows) == 2
        rows[0].payload_json = {"schema_version": 1, "sentinel": sentinel}
        rows[0].available_at = datetime.now(UTC) - timedelta(seconds=2)
        rows[1].available_at = datetime.now(UTC) - timedelta(seconds=1)

    monkeypatch.setattr(rabbitmq, "bootstrap_worker_topology", lambda *_: None)
    monkeypatch.setattr(outbox, "_publish_row", lambda *_: None)
    with (
        caplog.at_level(logging.WARNING, logger="dlr.control.outbox"),
        session_factory() as session,
    ):
        result = outbox.relay_once(
            session,
            "relay-poison-test",
            limit=2,
            connection_factory=_FakeConnection,
        )

    assert result == outbox.OutboxRelayResult(2, 1, 1)
    assert sentinel not in caplog.text
    with session_factory() as session:
        rows = list(session.scalars(select(ExecutionOutbox).order_by(ExecutionOutbox.id)).all())
        assert rows[0].status == "pending"
        assert rows[0].last_error_code == "dispatch_payload_invalid"
        assert rows[0].lease_owner is None
        assert rows[1].status == "published"
        assert rows[1].lease_owner is None
        poison_execution = session.get(Execution, accepted[0].json()["id"])
        assert poison_execution is not None and poison_execution.status == "queued"


def test_gate_off_relay_recovers_existing_pending_outbox(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    worker = api_client.post(
        "/api/workers/register",
        json={"name": "gate-off-relay-worker", "capabilities": ["python"], "protocol_version": 3},
        headers={"Authorization": "Bearer test-worker-token"},
    ).json()
    adapter = create_adapter(api_client, name="gate-off-relay")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker["id"]},
        ).status_code
        == 200
    )
    accepted = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert accepted.status_code == 202, accepted.text
    execution_id = accepted.json()["id"]

    monkeypatch.setattr(settings, "rabbitmq_execution_enabled", False)
    monkeypatch.setattr(rabbitmq, "bootstrap_worker_topology", lambda *_: None)
    monkeypatch.setattr(outbox, "_publish_row", lambda *_: None)
    with session_factory() as session:
        result = outbox.relay_once(
            session,
            "gate-off-test",
            connection_factory=_FakeConnection,
        )
        assert result == outbox.OutboxRelayResult(1, 1, 0)

    with session_factory() as session:
        row = session.scalar(
            select(ExecutionOutbox).where(ExecutionOutbox.execution_id == execution_id)
        )
        assert row is not None and row.status == "published"
        execution = session.get(Execution, execution_id)
        assert execution is not None and execution.status == "queued"


def test_two_relays_skip_locked_leases_are_disjoint_and_network_is_outside_db_lock(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two real PostgreSQL Relay leases do not overlap or hold DB locks while publishing."""
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "two-relay-worker")
    adapter = _prepare_reliable_task(api_client, worker, "two-relay")
    accepted = [
        api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}) for _ in range(2)
    ]
    assert all(response.status_code == 202 for response in accepted), [
        response.text for response in accepted
    ]
    execution_ids = [response.json()["id"] for response in accepted]
    entered_publish = threading.Event()
    publish_release = threading.Event()
    publish_lock = threading.Lock()
    published_ids: list[object] = []
    relay_sessions: dict[int, Session] = {}

    def fake_publish(_connection: object, row: ExecutionOutbox) -> None:
        relay_session = relay_sessions.get(threading.get_ident())
        assert relay_session is not None
        assert not relay_session.in_transaction()
        with publish_lock:
            published_ids.append(row.id)
            if len(published_ids) == 2:
                entered_publish.set()
        if not publish_release.wait(5):
            raise TimeoutError("relay publish barrier timed out")

    monkeypatch.setattr(rabbitmq, "bootstrap_worker_topology", lambda *_: None)
    monkeypatch.setattr(outbox, "_publish_row", fake_publish)

    def run_relay(owner: str) -> outbox.OutboxRelayResult:
        with session_factory() as session:
            relay_sessions[threading.get_ident()] = session
            try:
                return outbox.relay_once(
                    session,
                    owner,
                    limit=1,
                    connection_factory=_FakeConnection,
                )
            finally:
                relay_sessions.pop(threading.get_ident(), None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run_relay, f"two-relay-{index}") for index in range(2)]
        assert entered_publish.wait(5), "both Relay workers did not reach publish"
        with session_factory() as probe:
            leased = list(
                probe.scalars(
                    select(ExecutionOutbox)
                    .where(ExecutionOutbox.execution_id.in_(execution_ids))
                    .with_for_update(nowait=True)
                )
            )
            assert len(leased) == 2
            assert {row.lease_owner for row in leased} == {
                "two-relay-0",
                "two-relay-1",
            }
        publish_release.set()
        results = [future.result(timeout=5) for future in futures]

    assert results == [outbox.OutboxRelayResult(1, 1, 0)] * 2
    assert len(set(published_ids)) == 2
    with session_factory() as session:
        rows = list(session.scalars(select(ExecutionOutbox).order_by(ExecutionOutbox.created_at)))
        assert len(rows) == 2
        assert all(row.status == "published" and row.lease_owner is None for row in rows)


def test_confirm_ack_before_db_mark_crash_releases_after_lease_expiry_and_republishes(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after Broker confirm leaves a pending row for safe duplicate publish."""
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "confirm-crash-worker")
    adapter = _prepare_reliable_task(api_client, worker, "confirm-crash")
    accepted = api_client.post(
        f"/api/adapters/{adapter['id']}/executions",
        json={},
        headers={"Idempotency-Key": "confirm-crash-key"},
    )
    assert accepted.status_code == 202, accepted.text
    execution_id = accepted.json()["id"]
    original_mark = outbox.mark_outbox_published
    publish_calls = 0

    def fake_publish(_connection: object, _row: ExecutionOutbox) -> None:
        nonlocal publish_calls
        publish_calls += 1

    def crash_before_mark(*_: object, **__: object) -> bool:
        raise RuntimeError("simulated response loss after confirm")

    monkeypatch.setattr(rabbitmq, "bootstrap_worker_topology", lambda *_: None)
    monkeypatch.setattr(outbox, "_publish_row", fake_publish)
    monkeypatch.setattr(outbox, "mark_outbox_published", crash_before_mark)
    with (
        session_factory() as session,
        pytest.raises(RuntimeError, match="simulated response loss after confirm"),
    ):
        outbox.relay_once(
            session,
            "confirm-crash-before-mark",
            connection_factory=_FakeConnection,
        )

    with session_factory() as session:
        row = session.scalar(
            select(ExecutionOutbox).where(ExecutionOutbox.execution_id == execution_id)
        )
        assert row is not None
        assert row.status == "pending"
        assert row.lease_owner == "confirm-crash-before-mark"
        assert row.lease_expires_at is not None

    with session_factory.begin() as session:
        session.execute(
            update(ExecutionOutbox)
            .where(ExecutionOutbox.execution_id == execution_id)
            .values(
                available_at=func.now() - timedelta(seconds=1),
                lease_expires_at=func.now() - timedelta(seconds=1),
            )
        )
    monkeypatch.setattr(outbox, "mark_outbox_published", original_mark)
    with session_factory() as session:
        result = outbox.relay_once(
            session,
            "confirm-crash-retry",
            connection_factory=_FakeConnection,
        )
        assert result == outbox.OutboxRelayResult(1, 1, 0)

    assert publish_calls == 2
    with session_factory() as session:
        row = session.scalar(
            select(ExecutionOutbox).where(ExecutionOutbox.execution_id == execution_id)
        )
        assert row is not None
        assert row.status == "published"
        assert row.lease_owner is None


def test_health_fails_closed_for_pending_outbox_without_repair_url(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, name="health-pending-worker")
    adapter = create_adapter(api_client, name="health-pending-outbox")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker["id"]},
        ).status_code
        == 200
    )
    accepted = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert accepted.status_code == 202, accepted.text

    monkeypatch.setattr(settings, "rabbitmq_execution_enabled", False)
    monkeypatch.setattr(settings, "rabbitmq_url", None)
    response = api_client.get("/api/health")

    assert response.status_code == 503
    body = response.json()
    assert body["outbox"]["pending_count"] == 1
    assert body["rabbitmq"]["repair"]["status"] == "degraded"
    assert body["rabbitmq"]["repair"]["last_error_code"] == ("rabbitmq_not_configured_for_pending")


def test_manual_idempotency_hashes_complete_parsed_envelope(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "manual-envelope-worker")
    adapter = create_adapter(api_client, name="manual-envelope")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker["id"]},
        ).status_code
        == 200
    )
    raw_key = "manual-envelope-key-sentinel"
    with caplog.at_level(logging.INFO, logger="dlr.control"):
        first = api_client.post(
            f"/api/adapters/{adapter['id']}/executions",
            json={},
            headers={"Idempotency-Key": raw_key},
        )
        conflict = api_client.post(
            f"/api/adapters/{adapter['id']}/executions",
            json={"input": None},
            headers={"Idempotency-Key": raw_key},
        )
    assert first.status_code == 202, first.text
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"
    assert raw_key not in caplog.text

    with session_factory() as session:
        assert (
            session.scalar(select(Execution.id).where(Execution.adapter_id == adapter["id"]))
            is not None
        )
        assert (
            session.scalar(
                select(Execution.id).where(Execution.adapter_id == adapter["id"]).offset(1)
            )
            is None
        )
        record = session.scalar(select(ExecutionIdempotencyRecord))
        assert record is not None
        assert record.key_hash != raw_key.encode("ascii")
        assert len(record.key_hash) == 32
        assert len(record.payload_hash) == 32

    canonical_key = "manual-envelope-canonical-key"
    first_canonical = api_client.post(
        f"/api/adapters/{adapter['id']}/executions",
        content=b' { "input" : { "b" : 2, "a" : 1 } } ',
        headers={"Content-Type": "application/json", "Idempotency-Key": canonical_key},
    )
    assert first_canonical.status_code == 202, first_canonical.text
    save_version(api_client, adapter["id"], code="def handle(context, input):\n    return 2\n")
    same_body = api_client.post(
        f"/api/adapters/{adapter['id']}/executions",
        content=b'{"input":{"a":1,"b":2}}',
        headers={"Content-Type": "application/json", "Idempotency-Key": canonical_key},
    )
    assert same_body.status_code == 202, same_body.text
    assert same_body.json()["id"] == first_canonical.json()["id"]

    with session_factory() as session:
        assert (
            session.scalar(
                select(Execution.id)
                .where(Execution.adapter_id == adapter["id"])
                .order_by(Execution.id.desc())
                .offset(1)
            )
            is not None
        )
        assert (
            session.scalar(select(ExecutionOutbox.id).where(ExecutionOutbox.status == "pending"))
            is not None
        )


def test_rabbit_ingress_rolls_back_manual_webhook_and_run_now_as_one_transaction(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected Outbox protection check leaves no partial API responsibility."""
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "ingress-rollback-worker")
    manual = _prepare_reliable_task(api_client, worker, "ingress-rollback-manual")
    run_now = _prepare_reliable_task(
        api_client,
        worker,
        "ingress-rollback-run-now",
        schedule=True,
    )
    webhook, public_id = _prepare_reliable_webhook(
        api_client,
        worker,
        "ingress-rollback-webhook",
    )
    rabbitmq.mark_runtime_ready(worker_ids=[worker["id"]])

    def reject_outbox_capacity(*_: object, **__: object) -> None:
        raise adapter_service.domain_error(
            503,
            "injected_rollback",
            "injected rollback for test",
            {"retry_after": 7},
        )

    monkeypatch.setattr(outbox, "require_outbox_capacity", reject_outbox_capacity)
    responses = [
        api_client.post(
            f"/api/adapters/{manual['id']}/executions",
            json={},
            headers={"Idempotency-Key": "rollback-manual-key"},
        ),
        # A scheduled Adapter's run-now request still uses the Manual API and
        # must receive the same atomic rollback boundary.
        api_client.post(
            f"/api/adapters/{run_now['id']}/executions",
            json={},
            headers={"Idempotency-Key": "rollback-run-now-key"},
        ),
        api_client.post(
            f"/api/hooks/{public_id}",
            content=b"{}",
            headers={
                "Authorization": "Bearer ingress-rollback-webhook-token-value",
                "Idempotency-Key": "rollback-webhook-key",
            },
        ),
    ]
    assert [response.status_code for response in responses] == [503, 503, 503], [
        response.text for response in responses
    ]
    assert all(response.json()["detail"]["code"] == "injected_rollback" for response in responses)
    assert [response.headers.get("Retry-After") for response in responses] == ["7", "7", "7"]

    adapter_ids = [manual["id"], run_now["id"], webhook["id"]]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        assert session.scalar(select(func.count()).select_from(ExecutionOutbox)) == 0
        assert session.scalar(select(func.count()).select_from(ExecutionIdempotencyRecord)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(AdapterExecutionAdmission)
                .where(AdapterExecutionAdmission.adapter_id.in_(adapter_ids))
            )
            == 0
        )
        global_counter = session.get(GlobalExecutionAdmission, "global")
        assert global_counter is None or (
            global_counter.outstanding_count,
            global_counter.outstanding_bytes,
        ) == (0, 0)


def test_rabbit_ingress_retries_after_lost_202_response_without_duplicates(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client retry after losing a 202 response replays each original Execution."""
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "ingress-response-loss-worker")
    manual = _prepare_reliable_task(api_client, worker, "ingress-response-loss-manual")
    run_now = _prepare_reliable_task(
        api_client,
        worker,
        "ingress-response-loss-run-now",
        schedule=True,
    )
    webhook, public_id = _prepare_reliable_webhook(
        api_client,
        worker,
        "ingress-response-loss-webhook",
    )
    rabbitmq.mark_runtime_ready(worker_ids=[worker["id"]])

    first_manual = api_client.post(
        f"/api/adapters/{manual['id']}/executions",
        json={},
        headers={"Idempotency-Key": "response-loss-manual-key"},
    )
    second_manual = api_client.post(
        f"/api/adapters/{manual['id']}/executions",
        content=b" { } ",
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": "response-loss-manual-key",
        },
    )
    first_run_now = api_client.post(
        f"/api/adapters/{run_now['id']}/executions",
        json={},
        headers={"Idempotency-Key": "response-loss-run-now-key"},
    )
    second_run_now = api_client.post(
        f"/api/adapters/{run_now['id']}/executions",
        json={},
        headers={"Idempotency-Key": "response-loss-run-now-key"},
    )
    first_webhook = api_client.post(
        f"/api/hooks/{public_id}",
        content=b"{}",
        headers={
            "Authorization": "Bearer ingress-response-loss-webhook-token-value",
            "Idempotency-Key": "response-loss-webhook-key",
        },
    )
    second_webhook = api_client.post(
        f"/api/hooks/{public_id}",
        content=b" { } ",
        headers={
            "Authorization": "Bearer ingress-response-loss-webhook-token-value",
            "Idempotency-Key": "response-loss-webhook-key",
        },
    )

    pairs = (
        (first_manual, second_manual),
        (first_run_now, second_run_now),
        (first_webhook, second_webhook),
    )
    assert all(first.status_code == second.status_code == 202 for first, second in pairs), [
        (first.text, second.text) for first, second in pairs
    ]
    assert second_manual.json()["id"] == first_manual.json()["id"]
    assert second_run_now.json()["id"] == first_run_now.json()["id"]
    assert second_webhook.json()["execution_id"] == first_webhook.json()["execution_id"]

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Execution)) == 3
        assert session.scalar(select(func.count()).select_from(ExecutionIdempotencyRecord)) == 3
        assert session.scalar(select(func.count()).select_from(ExecutionOutbox)) == 3
        global_counter = session.get(GlobalExecutionAdmission, "global")
        assert global_counter is not None
        assert global_counter.outstanding_count == 3


def test_rabbit_ingress_accepts_manual_webhook_and_run_now_during_broker_outage(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Broker outage does not turn a committed PostgreSQL responsibility into a 5xx."""
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "ingress-outage-worker")
    manual = _prepare_reliable_task(api_client, worker, "ingress-outage-manual")
    run_now = _prepare_reliable_task(
        api_client,
        worker,
        "ingress-outage-run-now",
        schedule=True,
    )
    webhook, public_id = _prepare_reliable_webhook(
        api_client,
        worker,
        "ingress-outage-webhook",
    )
    rabbitmq.mark_runtime_ready(worker_ids=[worker["id"]])

    connect_calls = 0

    def broker_is_down() -> _FakeConnection:
        nonlocal connect_calls
        connect_calls += 1
        raise RuntimeError("broker outage sentinel")

    monkeypatch.setattr(rabbitmq, "connect", broker_is_down)
    responses = [
        api_client.post(f"/api/adapters/{manual['id']}/executions", json={}),
        api_client.post(f"/api/adapters/{run_now['id']}/executions", json={}),
        api_client.post(
            f"/api/hooks/{public_id}",
            content=b"{}",
            headers={"Authorization": "Bearer ingress-outage-webhook-token-value"},
        ),
    ]
    assert [response.status_code for response in responses] == [202, 202, 202], [
        response.text for response in responses
    ]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ExecutionOutbox)) == 3
        assert (
            session.scalar(
                select(func.count())
                .select_from(ExecutionOutbox)
                .where(ExecutionOutbox.status == "pending")
            )
            == 3
        )

        with caplog.at_level(logging.WARNING, logger="dlr.control.outbox"):
            result = outbox.relay_once(session, "ingress-outage-relay")
        assert result == outbox.OutboxRelayResult(3, 0, 3)

    assert connect_calls == 1
    assert "broker outage sentinel" not in caplog.text
    assert "publish_timeout_or_connection" in caplog.text
    with session_factory() as session:
        rows = list(session.scalars(select(ExecutionOutbox)))
        assert len(rows) == 3
        assert all(row.status == "pending" and row.lease_owner is None for row in rows)


def test_reliable_api_returns_retry_after_for_adapter_and_global_capacity_limits(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual API capacity failures expose stable codes and Retry-After."""
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "ingress-capacity-api-worker")
    adapter = _prepare_reliable_task(api_client, worker, "ingress-capacity-api-adapter")
    rabbitmq.mark_runtime_ready(worker_ids=[worker["id"]])
    monkeypatch.setattr(settings, "admission_adapter_max_count", 1)
    monkeypatch.setattr(settings, "admission_adapter_max_bytes", 1)
    monkeypatch.setattr(settings, "admission_global_max_count", 10)
    monkeypatch.setattr(settings, "admission_global_max_bytes", 10)

    accepted = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert accepted.status_code == 202, accepted.text
    adapter_full = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert adapter_full.status_code == 429, adapter_full.text
    assert adapter_full.json()["detail"]["code"] == "adapter_queue_full"
    assert adapter_full.headers.get("Retry-After") == "1"

    other = _prepare_reliable_task(api_client, worker, "ingress-capacity-api-global")
    monkeypatch.setattr(settings, "admission_adapter_max_count", 10)
    monkeypatch.setattr(settings, "admission_adapter_max_bytes", 10)
    monkeypatch.setattr(settings, "admission_global_max_count", 1)
    monkeypatch.setattr(settings, "admission_global_max_bytes", 1)
    global_full = api_client.post(f"/api/adapters/{other['id']}/executions", json={})
    assert global_full.status_code == 503, global_full.text
    assert global_full.json()["detail"]["code"] == "runtime_capacity_full"
    assert global_full.headers.get("Retry-After") == "1"

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Execution)) == 1
        global_counter = session.get(GlobalExecutionAdmission, "global")
        assert global_counter is not None
        assert global_counter.outstanding_count == 1


def test_execution_optional_links_set_null_on_real_postgres_delete(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execution metadata links match migration SET NULL delete semantics."""
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "execution-link-delete-worker")
    adapter = create_adapter(api_client, name="execution-link-delete")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker["id"]},
        ).status_code
        == 200
    )
    plain = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    keyed = api_client.post(
        f"/api/adapters/{adapter['id']}/executions",
        json={},
        headers={"Idempotency-Key": "execution-link-delete-key"},
    )
    replay = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert plain.status_code == keyed.status_code == replay.status_code == 202

    with session_factory.begin() as session:
        plain_row = session.get(Execution, plain.json()["id"])
        keyed_row = session.get(Execution, keyed.json()["id"])
        replay_row = session.get(Execution, replay.json()["id"])
        assert plain_row is not None
        assert keyed_row is not None
        assert replay_row is not None
        record = session.scalar(select(ExecutionIdempotencyRecord))
        assert record is not None
        replay_row.replay_of_execution_id = plain_row.id
        session.flush()
        idempotency_fk = next(iter(Execution.__table__.c.idempotency_record_id.foreign_keys))
        replay_fk = next(iter(Execution.__table__.c.replay_of_execution_id.foreign_keys))
        assert idempotency_fk.ondelete == "SET NULL"
        assert replay_fk.ondelete == "SET NULL"

        # The DB must clear the optional child link before removing its record.
        session.delete(record)
        session.flush()
        session.refresh(keyed_row)
        assert keyed_row.idempotency_record_id is None

        # Deleting the replay source must clear the child's optional relation.
        session.delete(plain_row)
        session.flush()
        session.refresh(replay_row)
        assert replay_row.replay_of_execution_id is None


def test_delete_rabbitmq_adapter_cancels_all_queued_and_retry_wait_responsibility(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permanent deletion must not rely on the legacy one-active-row index."""
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "delete-rabbitmq-queued-worker")
    adapter = create_adapter(api_client, name="delete-rabbitmq-queued")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker["id"]},
        ).status_code
        == 200
    )

    accepted = [
        api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}) for _ in range(3)
    ]
    assert all(response.status_code == 202 for response in accepted), [
        response.text for response in accepted
    ]
    execution_ids = [response.json()["id"] for response in accepted]
    with session_factory.begin() as session:
        retry_wait = session.get(Execution, execution_ids[-1])
        assert retry_wait is not None
        retry_wait.status = "retry_wait"
        retry_wait.next_attempt_at = datetime.now(UTC) + timedelta(minutes=5)

    blocked = api_client.delete(f"/api/adapters/{adapter['id']}")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "adapter_runtime_locked"

    with session_factory() as session:
        executions = list(
            session.scalars(
                select(Execution).where(Execution.id.in_(execution_ids)).order_by(Execution.id)
            ).all()
        )
        assert [execution.status for execution in executions] == [
            "queued",
            "queued",
            "retry_wait",
        ]
        adapter_counter = session.get(AdapterExecutionAdmission, adapter["id"])
        global_counter = session.get(GlobalExecutionAdmission, "global")
        assert adapter_counter is not None and adapter_counter.outstanding_count == 3
        assert global_counter is not None and global_counter.outstanding_count == 3
        assert (
            session.scalar(
                select(ExecutionOutbox.id).where(
                    ExecutionOutbox.execution_id.in_(execution_ids),
                    ExecutionOutbox.status == "pending",
                )
            )
            is not None
        )

    deleted = api_client.delete(f"/api/adapters/{adapter['id']}?stop=true")
    assert deleted.status_code == 204, deleted.text
    with session_factory() as session:
        assert session.get(Execution, execution_ids[0]) is None
        assert session.get(AdapterExecutionAdmission, adapter["id"]) is None
        global_counter = session.get(GlobalExecutionAdmission, "global")
        assert global_counter is not None
        assert (global_counter.outstanding_count, global_counter.outstanding_bytes) == (0, 0)
        assert (
            session.scalar(
                select(ExecutionOutbox.id).where(ExecutionOutbox.execution_id.in_(execution_ids))
            )
            is None
        )

    repeated = api_client.delete(f"/api/adapters/{adapter['id']}?stop=true")
    assert repeated.status_code == 404


def test_delete_rabbitmq_adapter_running_waits_and_repeated_stop_does_not_release_charge(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "delete-rabbitmq-running-worker")
    adapter = create_adapter(api_client, name="delete-rabbitmq-running")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker["id"]},
        ).status_code
        == 200
    )
    accepted = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert accepted.status_code == 202, accepted.text
    execution_id = accepted.json()["id"]
    with session_factory.begin() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        execution.status = "running"
        execution.worker_id = worker["id"]
        execution.started_at = datetime.now(UTC)

    blocked = api_client.delete(f"/api/adapters/{adapter['id']}")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "adapter_runtime_locked"

    waiting = api_client.delete(f"/api/adapters/{adapter['id']}?stop=true")
    assert waiting.status_code == 202, waiting.text
    assert waiting.json()["detail"]["params"]["active_execution_id"] == execution_id
    repeated = api_client.delete(f"/api/adapters/{adapter['id']}?stop=true")
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["detail"]["params"]["active_execution_id"] == execution_id
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        assert execution.status == "running"
        assert execution.cancel_requested is True
        counter = session.get(GlobalExecutionAdmission, "global")
        assert counter is not None and counter.outstanding_count == 1

    with session_factory.begin() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        execution.status = "cancelled"
        execution.ended_at = datetime.now(UTC)
        admission.release_admission_once(session, execution)
        outbox.settle_cancelled_outbox(session, execution.id)
    deleted = api_client.delete(f"/api/adapters/{adapter['id']}?stop=true")
    assert deleted.status_code == 204, deleted.text


def test_delete_rabbitmq_adapter_removes_only_its_idempotency_records(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "delete-rabbitmq-idempotency-worker")
    adapter = create_adapter(api_client, name="delete-rabbitmq-idempotency")
    other_adapter = create_adapter(api_client, name="delete-rabbitmq-idempotency-other")
    save_version(api_client, adapter["id"])
    save_version(api_client, other_adapter["id"])
    for adapter_id in (adapter["id"], other_adapter["id"]):
        assert (
            api_client.patch(
                f"/api/adapters/{adapter_id}",
                json={"runtime_worker_id": worker["id"]},
            ).status_code
            == 200
        )

    deleted_adapter_execution = api_client.post(
        f"/api/adapters/{adapter['id']}/executions",
        json={},
        headers={"Idempotency-Key": "delete-adapter-key"},
    )
    other_execution = api_client.post(
        f"/api/adapters/{other_adapter['id']}/executions",
        json={},
        headers={"Idempotency-Key": "other-adapter-key"},
    )
    assert deleted_adapter_execution.status_code == 202, deleted_adapter_execution.text
    assert other_execution.status_code == 202, other_execution.text
    deleted_execution_id = deleted_adapter_execution.json()["id"]
    other_execution_id = other_execution.json()["id"]

    with session_factory() as session:
        records = list(
            session.scalars(
                select(ExecutionIdempotencyRecord).order_by(ExecutionIdempotencyRecord.id)
            ).all()
        )
        assert len(records) == 2
        assert {record.execution_id for record in records} == {
            deleted_execution_id,
            other_execution_id,
        }

    deleted = api_client.delete(f"/api/adapters/{adapter['id']}?stop=true")
    assert deleted.status_code == 204, deleted.text
    with session_factory() as session:
        assert session.get(Execution, deleted_execution_id) is None
        assert (
            session.scalar(
                select(ExecutionIdempotencyRecord.id).where(
                    ExecutionIdempotencyRecord.execution_id == deleted_execution_id
                )
            )
            is None
        )
        assert session.get(Execution, other_execution_id) is not None
        other_record = session.scalar(
            select(ExecutionIdempotencyRecord).where(
                ExecutionIdempotencyRecord.execution_id == other_execution_id
            )
        )
        assert other_record is not None
        global_counter = session.get(GlobalExecutionAdmission, "global")
        assert global_counter is not None and global_counter.outstanding_count == 1

    repeated = api_client.delete(f"/api/adapters/{adapter['id']}?stop=true")
    assert repeated.status_code == 404
    assert api_client.delete(f"/api/adapters/{other_adapter['id']}?stop=true").status_code == 204


def test_rabbit_failure_logs_never_include_uri_userinfo(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    worker = api_client.post(
        "/api/workers/register",
        json={"name": "redaction-worker", "capabilities": ["python"], "protocol_version": 3},
        headers={"Authorization": "Bearer test-worker-token"},
    ).json()
    adapter = create_adapter(api_client, name="redaction-outbox")
    save_version(api_client, adapter["id"])
    api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"runtime_worker_id": worker["id"]},
    )
    assert api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}).status_code == 202

    sentinel = "amqp://sentinel-user:sentinel-password@rabbitmq.invalid:5672/%2F"

    def fail_connection() -> _FakeConnection:
        raise RuntimeError(sentinel)

    monkeypatch.setattr(settings, "rabbitmq_execution_enabled", False)
    with (
        caplog.at_level(logging.WARNING, logger="dlr.control.outbox"),
        session_factory() as session,
    ):
        result = outbox.relay_once(session, "redaction-test", connection_factory=fail_connection)
    assert result == outbox.OutboxRelayResult(1, 0, 1)
    assert sentinel not in caplog.text
    assert "sentinel-password" not in caplog.text
    assert "publish_timeout_or_connection" in caplog.text


def test_reconcile_repairs_zero_outstanding_stale_adapter_counter(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    adapter = create_adapter(api_client, name="stale-admission")
    save_version(api_client, adapter["id"])
    worker = _register_reliable_worker(api_client, name="stale-admission-worker")
    with session_factory.begin() as session:
        session.execute(update(Worker).where(Worker.id == worker["id"]).values(status="online"))
    api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"runtime_worker_id": worker["id"]},
    )
    assert api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}).status_code == 202
    with session_factory.begin() as session:
        execution = session.scalar(select(Execution).where(Execution.adapter_id == adapter["id"]))
        assert execution is not None
        execution.status = "succeeded"
        execution.ended_at = datetime.now(UTC)
        admission.release_admission_once(session, execution)
        counter = session.get(AdapterExecutionAdmission, adapter["id"])
        assert counter is not None
        counter.outstanding_count = 9
        counter.outstanding_bytes = 99

    with session_factory() as session:
        report = admission.reconcile_admission(session, adapter_id=adapter["id"])
        assert report.adapters_checked == 1
        assert report.adapter_count_delta == -9
        assert report.adapter_bytes_delta == -99

    with session_factory() as session:
        counter = session.get(AdapterExecutionAdmission, adapter["id"])
        assert counter is not None
        assert (counter.outstanding_count, counter.outstanding_bytes) == (0, 0)
        global_counter = session.get(GlobalExecutionAdmission, "global")
        assert global_counter is not None
        assert (global_counter.outstanding_count, global_counter.outstanding_bytes) == (0, 0)


def test_full_reconcile_resumes_interleaved_terminal_rows_without_skipping_adapters(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded terminal page uses an Adapter-stable composite cursor."""
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "bounded-reconcile-worker")
    adapters = [create_adapter(api_client, name=f"bounded-reconcile-{index}") for index in range(2)]
    for adapter in adapters:
        save_version(api_client, adapter["id"])
        assert (
            api_client.patch(
                f"/api/adapters/{adapter['id']}",
                json={"runtime_worker_id": worker["id"]},
            ).status_code
            == 200
        )

    # Interleave global Execution IDs: adapter 1 gets IDs 1/3 and adapter 2
    # gets IDs 2/4.  A global LIMIT with a last-row cursor would skip ID 3.
    accepted_ids: list[int] = []
    for adapter in (adapters[0], adapters[1], adapters[0], adapters[1]):
        response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
        assert response.status_code == 202, response.text
        accepted_ids.append(response.json()["id"])

    with session_factory.begin() as session:
        executions = [session.get(Execution, execution_id) for execution_id in accepted_ids]
        assert all(execution is not None for execution in executions)
        for execution in executions:
            assert execution is not None
            execution.status = "succeeded"
            execution.ended_at = func.now()

    reports = []
    cursor: tuple[int | None, int | None] = (None, None)
    for _ in range(6):
        with session_factory() as session:
            report = admission.reconcile_admission(
                session,
                batch_size=2,
                after_adapter_id=cursor[0],
                after_execution_id=cursor[1],
            )
        reports.append(report)
        assert report.adapters_checked <= 2
        if report.complete:
            break
        cursor = (report.next_adapter_id, report.next_execution_id)
    else:
        pytest.fail("bounded reconciliation did not reach a complete cursor")

    assert reports[-1].complete is True
    assert [report.adapters_checked for report in reports] == [2, 1, 0]
    with session_factory() as session:
        rows = list(
            session.scalars(
                select(Execution).where(Execution.id.in_(accepted_ids)).order_by(Execution.id)
            )
        )
        assert [row.status for row in rows] == ["succeeded"] * 4
        assert all(row.admission_released_at is not None for row in rows)
        assert (
            session.scalar(
                select(func.count())
                .select_from(AdapterExecutionAdmission)
                .where(AdapterExecutionAdmission.outstanding_count != 0)
            )
            == 0
        )
        global_counter = session.get(GlobalExecutionAdmission, "global")
        assert global_counter is not None
        assert (global_counter.outstanding_count, global_counter.outstanding_bytes) == (0, 0)


@pytest.mark.parametrize("reconcile_scope", ("targeted", "full"))
def test_rabbit_cancel_and_reconcile_use_one_postgres_lock_order(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    reconcile_scope: str,
) -> None:
    """Cancellation and repair serialize without an Execution/counter cycle."""
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, f"lock-order-{reconcile_scope}-worker")
    adapter = create_adapter(api_client, name=f"lock-order-{reconcile_scope}")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker["id"]},
        ).status_code
        == 200
    )
    accepted = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert accepted.status_code == 202, accepted.text
    execution_id = accepted.json()["id"]

    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []

    def cancel() -> None:
        session = session_factory()
        try:
            session.execute(text("SET lock_timeout = '2s'"))
            barrier.wait(timeout=5)
            execution = execution_service.cancel_execution(session, execution_id)
            results.append(f"cancel:{execution.status}")
        except BaseException as exc:  # noqa: BLE001 - assert both DB workers finish
            errors.append(exc)
        finally:
            session.close()

    def reconcile() -> None:
        session = session_factory()
        try:
            session.execute(text("SET lock_timeout = '2s'"))
            barrier.wait(timeout=5)
            if reconcile_scope == "targeted":
                report = admission.reconcile_admission(session, adapter_id=adapter["id"])
            else:
                report = admission.reconcile_admission(session)
            results.append(f"reconcile:{report.adapters_checked}")
        except BaseException as exc:  # noqa: BLE001 - assert both DB workers finish
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=cancel), threading.Thread(target=reconcile)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=8)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(results) == ["cancel:cancelled", "reconcile:1"]

    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        assert execution.status == "cancelled"
        assert execution.admission_released_at is not None
        adapter_counter = session.get(AdapterExecutionAdmission, adapter["id"])
        global_counter = session.get(GlobalExecutionAdmission, "global")
        assert adapter_counter is not None
        assert (adapter_counter.outstanding_count, adapter_counter.outstanding_bytes) == (0, 0)
        assert global_counter is not None
        assert (global_counter.outstanding_count, global_counter.outstanding_bytes) == (0, 0)
        outbox_row = session.scalar(
            select(ExecutionOutbox).where(ExecutionOutbox.execution_id == execution_id)
        )
        assert outbox_row is not None
        assert outbox_row.status == "published"
        assert outbox_row.last_error_code == "execution_cancelled"
        assert outbox_row.lease_owner is None

    # Repeated terminal cancellation/repair is a no-op and cannot release a
    # second charge or resurrect the settled Outbox responsibility.
    with session_factory() as session:
        repeated = execution_service.cancel_execution(session, execution_id)
        assert repeated.status == "cancelled"
    with session_factory() as session:
        admission.reconcile_admission(session, adapter_id=adapter["id"])
        adapter_counter = session.get(AdapterExecutionAdmission, adapter["id"])
        global_counter = session.get(GlobalExecutionAdmission, "global")
        assert adapter_counter is not None
        assert global_counter is not None
        assert (adapter_counter.outstanding_count, global_counter.outstanding_count) == (0, 0)


@pytest.mark.parametrize("reconcile_scope", ("targeted", "full"))
@pytest.mark.parametrize("operation", ("cancel", "stop_delete"))
def test_reconcile_and_stop_paths_have_deterministic_canonical_lock_order(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    reconcile_scope: str,
    operation: str,
) -> None:
    """Force the old Execution-first cycle's critical interleaving."""
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(
        api_client, f"deterministic-lock-{reconcile_scope}-{operation}-worker"
    )
    adapter = create_adapter(api_client, name=f"deterministic-lock-{reconcile_scope}-{operation}")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker["id"]},
        ).status_code
        == 200
    )
    accepted = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert accepted.status_code == 202, accepted.text
    execution_id = accepted.json()["id"]

    scope_locked = threading.Event()
    release_scope = threading.Event()
    execution_lock_attempted = threading.Event()
    results: list[str] = []
    errors: list[BaseException] = []

    def reconcile() -> None:
        session = session_factory()
        try:
            session.execute(text("SET lock_timeout = '2s'"))
            original_scalar = session.scalar
            paused = False

            def scalar(statement, *args, **kwargs):
                nonlocal paused
                result = original_scalar(statement, *args, **kwargs)
                if not paused and "global_execution_admission" in str(statement):
                    paused = True
                    scope_locked.set()
                    if not release_scope.wait(5):
                        raise TimeoutError("reconcile scope release barrier timed out")
                return result

            session.scalar = scalar  # type: ignore[method-assign]
            if reconcile_scope == "targeted":
                report = admission.reconcile_admission(session, adapter_id=adapter["id"])
            else:
                report = admission.reconcile_admission(session)
            results.append(f"reconcile:{report.adapters_checked}")
        except BaseException as exc:  # noqa: BLE001 - assert both DB workers finish
            errors.append(exc)
        finally:
            session.close()

    original_lock_execution = execution_cancellation.lock_execution

    def observe_execution_lock(session: Session, current_execution_id: int):
        execution_lock_attempted.set()
        return original_lock_execution(session, current_execution_id)

    monkeypatch.setattr(execution_cancellation, "lock_execution", observe_execution_lock)
    reconcile_thread = threading.Thread(target=reconcile)
    reconcile_thread.start()
    assert scope_locked.wait(5), "reconcile did not reach its locked admission scope"

    def mutate() -> None:
        session = session_factory()
        try:
            session.execute(text("SET lock_timeout = '2s'"))
            if operation == "cancel":
                execution = execution_service.cancel_execution(session, execution_id)
                results.append(f"cancel:{execution.status}")
            else:
                result = adapter_service.delete_adapter(session, adapter["id"], stop=True)
                results.append(f"stop_delete:{result.waiting_for_worker}")
        except BaseException as exc:  # noqa: BLE001 - assert both DB workers finish
            errors.append(exc)
        finally:
            session.close()

    mutate_thread = threading.Thread(target=mutate)
    mutate_thread.start()
    # Under the former Execution-first implementation this event fires while
    # the mutator waits for the Adapter held by reconcile.  Reconcile would
    # then wait for the same Execution row: a real cycle, not a start barrier.
    assert not execution_lock_attempted.wait(0.5)
    release_scope.set()
    reconcile_thread.join(timeout=6)
    mutate_thread.join(timeout=6)

    assert not reconcile_thread.is_alive()
    assert not mutate_thread.is_alive()
    assert errors == []
    assert "reconcile:1" in results
    if operation == "cancel":
        assert "cancel:cancelled" in results
    else:
        assert "stop_delete:False" in results

    with session_factory() as session:
        global_counter = session.get(GlobalExecutionAdmission, "global")
        assert global_counter is not None
        if operation == "cancel":
            execution = session.get(Execution, execution_id)
            assert execution is not None and execution.status == "cancelled"
            assert execution.admission_released_at is not None
            adapter_counter = session.get(AdapterExecutionAdmission, adapter["id"])
            assert adapter_counter is not None
            assert (adapter_counter.outstanding_count, adapter_counter.outstanding_bytes) == (0, 0)
            assert (global_counter.outstanding_count, global_counter.outstanding_bytes) == (0, 0)
        else:
            assert session.get(Execution, execution_id) is None
            assert session.get(AdapterExecutionAdmission, adapter["id"]) is None
            assert (global_counter.outstanding_count, global_counter.outstanding_bytes) == (0, 0)


def test_postgres_adapter_last_count_and_bytes_allow_one_concurrent_reservation(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final Adapter count/bytes slot has one database winner."""
    monkeypatch.setattr(settings, "admission_adapter_max_count", 1)
    monkeypatch.setattr(settings, "admission_adapter_max_bytes", 1)
    monkeypatch.setattr(settings, "admission_global_max_count", 10)
    monkeypatch.setattr(settings, "admission_global_max_bytes", 10)
    adapter = create_adapter(api_client, name="adapter-capacity-race")
    barrier = threading.Barrier(2)

    def reserve() -> str:
        with session_factory() as session:
            session.execute(text("SET lock_timeout = '2s'"))
            barrier.wait(timeout=5)
            try:
                admission.reserve_admission(session, adapter["id"], 1)
                session.commit()
            except HTTPException as error:
                session.rollback()
                detail = error.detail if isinstance(error.detail, dict) else {}
                return str(detail.get("code", "unknown"))
            return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: reserve(), (1, 2)))

    assert sorted(outcomes) == ["accepted", "adapter_queue_full"]
    with session_factory() as session:
        adapter_counter = session.get(AdapterExecutionAdmission, adapter["id"])
        global_counter = session.get(GlobalExecutionAdmission, "global")
        assert adapter_counter is not None
        assert (adapter_counter.outstanding_count, adapter_counter.outstanding_bytes) == (1, 1)
        assert global_counter is not None
        assert (global_counter.outstanding_count, global_counter.outstanding_bytes) == (1, 1)


def test_postgres_global_last_count_and_bytes_allow_one_concurrent_reservation(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final Global count/bytes slot has one database winner across Adapters."""
    monkeypatch.setattr(settings, "admission_adapter_max_count", 10)
    monkeypatch.setattr(settings, "admission_adapter_max_bytes", 10)
    monkeypatch.setattr(settings, "admission_global_max_count", 1)
    monkeypatch.setattr(settings, "admission_global_max_bytes", 1)
    adapters = [
        create_adapter(api_client, name=f"global-capacity-race-{index}") for index in range(2)
    ]
    with session_factory() as session:
        session.add_all(
            [AdapterExecutionAdmission(adapter_id=adapter["id"]) for adapter in adapters]
        )
        session.commit()
    barrier = threading.Barrier(2)

    def reserve(adapter_id: int) -> str:
        with session_factory() as session:
            session.execute(text("SET lock_timeout = '2s'"))
            barrier.wait(timeout=5)
            try:
                admission.reserve_admission(session, adapter_id, 1)
                session.commit()
            except HTTPException as error:
                session.rollback()
                detail = error.detail if isinstance(error.detail, dict) else {}
                return str(detail.get("code", "unknown"))
            return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(reserve, (adapter["id"] for adapter in adapters)))

    assert sorted(outcomes) == ["accepted", "runtime_capacity_full"]
    with session_factory() as session:
        global_counter = session.get(GlobalExecutionAdmission, "global")
        assert global_counter is not None
        assert (global_counter.outstanding_count, global_counter.outstanding_bytes) == (1, 1)
        adapter_counters = list(
            session.scalars(
                select(AdapterExecutionAdmission).where(
                    AdapterExecutionAdmission.adapter_id.in_(adapter["id"] for adapter in adapters)
                )
            )
        )
        assert sorted(counter.outstanding_count for counter in adapter_counters) == [0, 1]
        assert sorted(counter.outstanding_bytes for counter in adapter_counters) == [0, 1]


def test_global_admission_rejects_a_second_singleton_key(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        session.add(GlobalExecutionAdmission(singleton_key="not-global"))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_retry_policy_is_closed_bounded_and_snapshotted(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    assert reliable_execution.default_retry_policy() == {
        "max_attempts": 3,
        "initial_backoff_seconds": 5.0,
        "multiplier": 2.0,
        "max_backoff_seconds": 300.0,
        "jitter_ratio": 0.2,
        "retryable_error_classes": ["platform_transient", "worker_lost"],
    }
    invalid_policies = (
        {**reliable_execution.DEFAULT_RETRY_POLICY, "max_attempts": 0},
        {**reliable_execution.DEFAULT_RETRY_POLICY, "initial_backoff_seconds": -1.0},
        {**reliable_execution.DEFAULT_RETRY_POLICY, "multiplier": 0.5},
        {**reliable_execution.DEFAULT_RETRY_POLICY, "jitter_ratio": 0.21},
        {**reliable_execution.DEFAULT_RETRY_POLICY, "initial_backoff_seconds": 301.0},
    )
    for policy in invalid_policies:
        with pytest.raises(ValueError):
            reliable_execution.validate_retry_policy(policy)
    monkeypatch.setattr(
        settings,
        "execution_retry_initial_backoff_seconds",
        5.0,
    )
    monkeypatch.setattr(settings, "execution_retry_max_backoff_seconds", 4.0)
    with pytest.raises(ValueError, match="must not exceed"):
        validate_deployment_configuration(settings)
    monkeypatch.setattr(settings, "execution_retry_initial_backoff_seconds", 5.0)
    monkeypatch.setattr(settings, "execution_retry_max_backoff_seconds", 300.0)

    worker = _register_reliable_worker(api_client, "retry-policy-worker")
    adapter = create_adapter(api_client, name="retry-policy-snapshot")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker["id"]},
        ).status_code
        == 200
    )
    first = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert first.status_code == 202, first.text
    first_id = first.json()["id"]
    monkeypatch.setattr(settings, "execution_retry_initial_backoff_seconds", 11.0)
    monkeypatch.setattr(settings, "execution_retry_max_backoff_seconds", 600.0)
    second = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert second.status_code == 202, second.text
    with session_factory() as session:
        rows = {
            row.id: row
            for row in session.scalars(
                select(Execution).where(Execution.id.in_((first_id, second.json()["id"])))
            ).all()
        }
        assert rows[first_id].retry_policy_snapshot["initial_backoff_seconds"] == 5.0
        assert rows[first_id].retry_policy_snapshot["max_backoff_seconds"] == 300.0
        assert rows[first_id].max_attempts_snapshot == 3
        assert rows[second.json()["id"]].retry_policy_snapshot["initial_backoff_seconds"] == 11.0
        assert rows[second.json()["id"]].retry_policy_snapshot["max_backoff_seconds"] == 600.0


def test_rabbit_execution_freezes_credential_binding_and_protects_credential(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "credential-snapshot-worker")
    adapter = create_adapter(api_client, name="credential-snapshot")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker["id"]},
        ).status_code
        == 200
    )
    old_credential = api_client.post(
        "/api/credentials",
        json={"name": "snapshot-old", "type": "secret", "fields": {"value": "old"}},
    ).json()
    new_credential = api_client.post(
        "/api/credentials",
        json={"name": "snapshot-new", "type": "secret", "fields": {"value": "new"}},
    ).json()
    binding_response = api_client.put(
        f"/api/adapters/{adapter['id']}/credential-bindings",
        json={
            "bindings": [
                {"env_key": "TOKEN", "credential_id": old_credential["id"], "field": "value"}
            ]
        },
    )
    assert binding_response.status_code == 200, binding_response.text

    first = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert first.status_code == 202, first.text
    first_id = first.json()["id"]
    with session_factory() as session:
        first_binding = session.scalar(
            select(AdapterCredentialBinding).where(
                AdapterCredentialBinding.adapter_id == adapter["id"],
                AdapterCredentialBinding.credential_id == old_credential["id"],
            )
        )
        assert first_binding is not None
        first_snapshot = session.scalars(
            select(ExecutionCredentialBindingSnapshot).where(
                ExecutionCredentialBindingSnapshot.execution_id == first_id
            )
        ).all()
        assert [
            (row.binding_id, row.credential_id, row.env_key, row.field) for row in first_snapshot
        ] == [(first_binding.id, old_credential["id"], "TOKEN", "value")]

    replacement = api_client.put(
        f"/api/adapters/{adapter['id']}/credential-bindings",
        json={
            "bindings": [
                {"env_key": "TOKEN", "credential_id": new_credential["id"], "field": "value"}
            ]
        },
    )
    assert replacement.status_code == 200, replacement.text
    blocked_delete = api_client.delete(f"/api/credentials/{old_credential['id']}")
    assert blocked_delete.status_code == 409, blocked_delete.text
    assert blocked_delete.json()["detail"]["code"] == "credential_in_use"

    second = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert second.status_code == 202, second.text
    second_id = second.json()["id"]
    with session_factory() as session:
        executions = {
            execution.id: execution
            for execution in session.scalars(
                select(Execution).where(Execution.id.in_((first_id, second_id)))
            ).all()
        }
        assert executions[first_id].credential_bindings_snapshot == [
            {
                "binding_id": first_binding.id,
                "credential_id": old_credential["id"],
                "env_key": "TOKEN",
                "field": "value",
            }
        ]
        assert (
            executions[second_id].credential_bindings_snapshot[0]["credential_id"]
            == (new_credential["id"])
        )
        assert session.get(Credential, old_credential["id"]) is not None


def test_rabbit_schedule_freezes_policy_for_old_and_new_executions(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "schedule-snapshot-worker")
    adapter = create_adapter(api_client, name="schedule-policy-snapshot", adapter_type="task")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"run_mode": "schedule", "runtime_worker_id": worker["id"]},
        ).status_code
        == 200
    )
    schedule_payload = {
        "enabled": True,
        "cron": "* * * * *",
        "timezone": "UTC",
        "input": {"scheduled": True},
        "misfire_policy": "coalesce_latest",
        "max_catchup_count": 2,
        "max_catchup_age_seconds": 600,
    }
    created = api_client.put(f"/api/adapters/{adapter['id']}/schedule", json=schedule_payload)
    assert created.status_code == 200, created.text
    now = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
    with session_factory.begin() as session:
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert schedule is not None
        schedule.next_run_at = now - timedelta(minutes=1)
    with session_factory() as session:
        assert scheduler_tick(session, now=now) == 1
    with session_factory() as session:
        rows = list(
            session.scalars(
                select(Execution)
                .where(Execution.adapter_id == adapter["id"], Execution.trigger == "schedule")
                .order_by(Execution.id)
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].schedule_policy_snapshot == {
            "misfire_policy": "coalesce_latest",
            "max_catchup_count": 2,
            "max_catchup_age_seconds": 600,
        }

    disabled = dict(schedule_payload)
    disabled["enabled"] = False
    assert (
        api_client.put(f"/api/adapters/{adapter['id']}/schedule", json=disabled).status_code == 200
    )
    changed = dict(schedule_payload)
    changed.update(
        {
            "misfire_policy": "queue_every_occurrence",
            "max_catchup_count": 7,
            "max_catchup_age_seconds": 1_200,
        }
    )
    assert (
        api_client.put(f"/api/adapters/{adapter['id']}/schedule", json=changed).status_code == 200
    )
    with session_factory.begin() as session:
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert schedule is not None
        schedule.next_run_at = now - timedelta(minutes=1)
    with session_factory() as session:
        assert scheduler_tick(session, now=now) == 1
    with session_factory() as session:
        rows = list(
            session.scalars(
                select(Execution)
                .where(Execution.adapter_id == adapter["id"], Execution.trigger == "schedule")
                .order_by(Execution.id)
            ).all()
        )
        assert len(rows) == 2
        assert rows[0].schedule_policy_snapshot == {
            "misfire_policy": "coalesce_latest",
            "max_catchup_count": 2,
            "max_catchup_age_seconds": 600,
        }
        assert rows[1].schedule_policy_snapshot == {
            "misfire_policy": "queue_every_occurrence",
            "max_catchup_count": 7,
            "max_catchup_age_seconds": 1_200,
        }


def test_schedule_outcome_validator_rebuilds_large_and_dst_ranges_and_rejects_tampering() -> None:
    def outcome_variant(
        base: ScheduleDispatchOutcome,
        **updates: object,
    ) -> ScheduleDispatchOutcome:
        values = {
            "schedule_id": base.schedule_id,
            "first_scheduled_for": base.first_scheduled_for,
            "last_scheduled_for": base.last_scheduled_for,
            "occurrence_count": base.occurrence_count,
            "outcome": base.outcome,
            "reason": base.reason,
            "cron_snapshot": base.cron_snapshot,
            "timezone_snapshot": base.timezone_snapshot,
            "execution_id": base.execution_id,
        }
        values.update(updates)
        return ScheduleDispatchOutcome(**values)

    first = datetime(2026, 1, 1, 0, 0, 17, tzinfo=UTC)
    window = _due_points(
        "* * * * *",
        "UTC",
        first,
        first + timedelta(minutes=SCHEDULE_AUDIT_PAGE_SIZE - 1),
    )
    large = ScheduleDispatchOutcome(
        schedule_id=1,
        first_scheduled_for=window.points[0],
        last_scheduled_for=window.points[-1],
        occurrence_count=len(window.points),
        outcome="expired",
        reason="catchup_age",
        cron_snapshot="* * * * *",
        timezone_snapshot="UTC",
    )
    validate_schedule_outcome(large)

    cron = "30 1 * * *"
    timezone = "America/New_York"
    dst_first = croniter(
        cron,
        datetime(2026, 10, 31, tzinfo=ZoneInfo(timezone)),
    ).get_next(datetime)
    dst_first = dst_first.astimezone(UTC)
    dst_window = _due_points(cron, timezone, dst_first, dst_first + timedelta(days=3))
    dst = outcome_variant(
        large,
        schedule_id=2,
        first_scheduled_for=dst_window.points[0],
        last_scheduled_for=dst_window.points[-1],
        occurrence_count=len(dst_window.points),
        cron_snapshot=cron,
        timezone_snapshot=timezone,
    )
    validate_schedule_outcome(dst)

    with pytest.raises(ScheduleOutcomeValidationError):
        validate_schedule_outcome(
            outcome_variant(
                large,
                last_scheduled_for=large.last_scheduled_for + timedelta(minutes=1),
            )
        )
    with pytest.raises(ScheduleOutcomeValidationError):
        validate_schedule_outcome(outcome_variant(large, occurrence_count=0))
    with pytest.raises(ScheduleOutcomeValidationError):
        validate_schedule_outcomes(
            [
                large,
                outcome_variant(large, first_scheduled_for=large.first_scheduled_for),
            ]
        )


def test_schedule_outcome_database_constraints_reject_invalid_ranges_and_references(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """PostgreSQL remains the final authority below the service validator."""
    worker = _register_reliable_worker(api_client, "schedule-outcome-db-worker")
    adapter = create_adapter(api_client, name="schedule-outcome-db", adapter_type="task")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"run_mode": "schedule", "runtime_worker_id": worker["id"]},
        ).status_code
        == 200
    )
    configured = api_client.put(
        f"/api/adapters/{adapter['id']}/schedule",
        json={"enabled": True, "cron": "* * * * *", "timezone": "UTC", "input": None},
    )
    assert configured.status_code == 200, configured.text
    with session_factory() as session:
        schedule_id = session.scalar(
            select(AdapterSchedule.id).where(AdapterSchedule.adapter_id == adapter["id"])
        )
    assert schedule_id is not None
    first = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)

    def outcome(**overrides: object) -> ScheduleDispatchOutcome:
        values: dict[str, object] = {
            "schedule_id": schedule_id,
            "first_scheduled_for": first,
            "last_scheduled_for": first,
            "occurrence_count": 1,
            "outcome": "skipped",
            "reason": "runtime_worker_invalid",
            "cron_snapshot": "* * * * *",
            "timezone_snapshot": "UTC",
        }
        values.update(overrides)
        return ScheduleDispatchOutcome(**values)

    with session_factory() as session:
        valid = outcome()
        session.add(valid)
        session.flush()

        rejected = (
            outcome(outcome="not-an-outcome"),
            outcome(occurrence_count=0),
            outcome(occurrence_count=SCHEDULE_AUDIT_PAGE_SIZE + 1),
            outcome(
                first_scheduled_for=first + timedelta(minutes=1),
                last_scheduled_for=first,
            ),
            outcome(schedule_id=9_999_999),
        )
        for row in rejected:
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(row)
                session.flush()


def test_execution_artifact_hold_database_constraints_and_delete_boundaries(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hold uniqueness, enum and both FK delete policies are real PostgreSQL facts."""
    _enable_rabbitmq_test(monkeypatch)
    worker = _register_reliable_worker(api_client, "artifact-hold-db-worker")
    adapter = create_adapter(api_client, name="artifact-hold-db")
    save_version(api_client, adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": worker["id"]},
        ).status_code
        == 200
    )
    accepted = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert accepted.status_code == 202, accepted.text
    execution_id = accepted.json()["id"]
    expires_at = datetime(2026, 1, 8, tzinfo=UTC)

    with session_factory() as session:
        reservation = ManagedInputUploadReservation(
            adapter_id=adapter["id"],
            upload_session_id="artifact-hold-db-reservation",
            reserved_bytes=1,
            status="CONSUMED",
            expires_at=expires_at,
            consumed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        session.add(reservation)
        session.flush()
        artifact = ManagedInputArtifact(
            adapter_id=adapter["id"],
            upload_session_id="artifact-hold-db-reservation",
            upload_reservation_id=reservation.id,
            original_filename="hold.txt",
            storage_key="artifact-hold-db-key",
            content_type="text/plain",
            size_bytes=1,
            status="READY",
            retention_mode="system_default",
            expires_at=expires_at,
        )
        session.add(artifact)
        session.flush()
        hold = ExecutionArtifactHold(
            execution_id=execution_id,
            artifact_id=artifact.id,
            reason="dead_letter_replay",
            expires_at=expires_at,
        )
        session.add(hold)
        session.flush()

        invalid_rows = (
            ExecutionArtifactHold(
                execution_id=execution_id,
                artifact_id=artifact.id,
                reason="unsupported_reason",
                expires_at=expires_at,
            ),
            ExecutionArtifactHold(
                execution_id=execution_id,
                artifact_id=artifact.id,
                reason="dead_letter_replay",
                expires_at=expires_at,
            ),
            ExecutionArtifactHold(
                execution_id=9_999_999,
                artifact_id=artifact.id,
                reason="dead_letter_replay",
                expires_at=expires_at,
            ),
            ExecutionArtifactHold(
                execution_id=execution_id,
                artifact_id=9_999_999,
                reason="dead_letter_replay",
                expires_at=expires_at,
            ),
        )
        for row in invalid_rows:
            with pytest.raises(IntegrityError), session.begin_nested():
                session.add(row)
                session.flush()

        with pytest.raises(IntegrityError), session.begin_nested():
            session.delete(artifact)
            session.flush()

        execution = session.get(Execution, execution_id)
        assert execution is not None
        execution.status = "cancelled"
        execution.ended_at = func.now()
        admission.release_admission_once(session, execution)
        outbox.settle_cancelled_outbox(session, execution.id)
        session.delete(execution)
        session.flush()
        assert (
            session.scalar(
                select(func.count())
                .select_from(ExecutionArtifactHold)
                .where(ExecutionArtifactHold.id == hold.id)
            )
            == 0
        )
        session.delete(artifact)
        session.flush()
