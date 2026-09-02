"""Deterministic Batch 2 attempt, native defer, migration and failure tests."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pika
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings, validate_deployment_configuration
from dlr.control.models import (
    AdapterExecutionAdmission,
    AdapterExecutionSlot,
    Execution,
    ExecutionArtifactHold,
    ExecutionAttempt,
    ExecutionInfrastructureIncident,
    ExecutionOutbox,
    GlobalExecutionAdmission,
    ManagedInputArtifact,
)
from dlr.control.schemas.reliable_runtime import (
    AttemptRenewBody,
    AttemptResultBody,
    V3TaskPayload,
)
from dlr.control.services import attempt as attempt_service
from dlr.control.services import infrastructure_dlq, rabbitmq, reliable_execution
from dlr.control.services.dispatch import DISPATCH_EXCHANGE, worker_routing_key
from dlr.worker.client import ClientError, ControlUnavailableError
from dlr.worker.consumer import ConsumerConfig, V3Consumer
from test_adapters import create_adapter, save_version
from test_issue127_b2_binding import create_artifact

ISOLATION_PASS = {
    "cgroup_v2": True,
    "mount_namespace": True,
    "pid_namespace": True,
    "memory_hard_limit": True,
    "pids_hard_limit": True,
    "tmpfs_hard_limit": True,
    "bounded_output": True,
    "preflight_passed": True,
}
WORKER_HEADERS = {"Authorization": "Bearer test-worker-token"}


def _enable_canary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rabbitmq_execution_enabled", False)
    monkeypatch.setattr(settings, "rabbitmq_execution_canary_enabled", True)
    monkeypatch.setattr(settings, "rabbitmq_url", "amqp://dlr:test-password@rabbitmq:5672")
    monkeypatch.setattr(settings, "rabbitmq_vhost", "/")
    monkeypatch.setattr(settings, "rabbitmq_management_url", "http://rabbitmq:15672")
    rabbitmq.mark_runtime_ready()


def _ready_worker(
    client: TestClient,
    name: str,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/api/workers/register",
        json={
            "name": name,
            "capabilities": capabilities or ["python"],
            "protocol_version": 3,
            "isolation_capabilities": ISOLATION_PASS,
        },
        headers=WORKER_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rabbitmq_execution_v3"] is True
    return body


def _rabbit_adapter(
    client: TestClient,
    worker: dict[str, Any],
    name: str,
    *,
    language: str = "python",
) -> dict[str, Any]:
    adapter = create_adapter(client, name=name, language=language)
    save_version(client, adapter["id"])
    response = client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"runtime_worker_id": worker["id"]},
    )
    assert response.status_code == 200, response.text
    return adapter


def _canary_execution(
    client: TestClient,
    adapter_id: int,
    *,
    input_value: Any = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if input_value is not None:
        payload["input"] = input_value
    response = client.post(f"/api/adapters/{adapter_id}/executions/canary", json=payload)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["dispatch_backend"] == "rabbitmq"
    assert body["status"] == "queued"
    return body


def _dispatch(session_factory: sessionmaker[Session], execution_id: int) -> dict[str, Any]:
    with session_factory() as session:
        row = session.scalar(
            select(ExecutionOutbox).where(ExecutionOutbox.execution_id == execution_id)
        )
        assert row is not None
        return dict(row.payload_json)


def _claim(session_factory: sessionmaker[Session], worker_id: int, dispatch: dict[str, Any]) -> Any:
    with session_factory() as session:
        return attempt_service.claim_dispatch(session, worker_id, dispatch)


class _NativeDeferChannel:
    def __init__(self) -> None:
        self.nacks: list[dict[str, Any]] = []
        self.publishes = 0
        self.acks = 0
        self.stop_consuming_calls = 0
        self.qos: dict[str, Any] = {}

    def basic_nack(self, **kwargs: Any) -> None:
        self.nacks.append(kwargs)

    def basic_publish(self, **_: Any) -> None:
        self.publishes += 1

    def basic_ack(self, **_: Any) -> None:
        self.acks += 1

    def stop_consuming(self) -> None:
        self.stop_consuming_calls += 1

    def basic_qos(self, **kwargs: Any) -> None:
        self.qos = kwargs

    def basic_consume(self, **_: Any) -> None:
        return


class _ImmediateCallbackConnection:
    def add_callback_threadsafe(self, callback: Any) -> None:
        callback()


class _OneShotConsumerConnection:
    def __init__(self, owner: V3Consumer) -> None:
        self.owner = owner
        self.channel_instance = _NativeDeferChannel()
        self.is_open = True

    def channel(self) -> _NativeDeferChannel:
        return self.channel_instance

    def process_data_events(self, *, time_limit: float) -> None:
        assert time_limit == 1.0
        self.owner.request_stop()

    def close(self) -> None:
        self.is_open = False


def _valid_consumer_payload(*, execution_id: int = 13, attempt_id: int = 41) -> dict[str, Any]:
    return {
        "execution_id": execution_id,
        "attempt_id": attempt_id,
        "attempt_no": 1,
        "fencing_token": 7,
        "lease_expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        "lease_seconds": 30,
        "renew_seconds": 5,
        "claim_token": "claim-token",
        "cleanup_token": "cleanup-token",
        "adapter_id": 19,
        "version_id": 23,
        "language": "python",
        "code": "print('ok')",
        "requirements": "",
        "runtime_config": {},
        "input": None,
        "execution_timeout_seconds": 10,
        "input_source_type": "none",
        "input_snapshot": {"source_type": "none"},
        "resource_profile": {
            "schema_version": 1,
            "resource_class": "small",
            "backend": "cgroup_v2",
            "cpu_cores": 1.0,
            "memory_bytes": 1,
            "pids": 1,
            "tmp_bytes": 1,
            "nofile": 1,
            "execution_timeout_seconds": 10,
            "claim_timeout_seconds": 30,
            "recovery_grace_seconds": 60,
            "workspace_cleanup_attempt_timeout_seconds": 1,
            "workspace_cleanup_total_timeout_seconds": 2,
            "stream_max_bytes": 1,
            "output_max_bytes": 1,
            "output_preview_max_bytes": 1,
        },
    }


def test_v3_consumer_slots_one_bounds_prefetch_pool_and_saturation() -> None:
    """A single local slot yields one broker delivery and pauses on saturation."""

    connection_holder: dict[str, _OneShotConsumerConnection] = {}

    def make_connection() -> _OneShotConsumerConnection:
        connection = _OneShotConsumerConnection(consumer)
        connection_holder["connection"] = connection
        return connection

    consumer = V3Consumer(
        ConsumerConfig(
            worker_id=7,
            queue="dlr.worker.7.q",
            execution_slots=1,
            runtime_root=Path("/tmp/dlr-b2-runtime"),
            attempt_journal_root=Path("/tmp/dlr-b2-journal"),
        ),
        object(),  # type: ignore[arg-type]
        connection_factory=make_connection,
        runtime_settings=SimpleNamespace(),
    )
    try:
        consumer.run()
        channel = connection_holder["connection"].channel_instance
        assert channel.qos == {"prefetch_count": 1, "global_qos": False}
        assert channel.stop_consuming_calls == 0
        assert consumer._pool._max_workers == 1

        assert consumer._slots.acquire(blocking=False)
        try:
            consumer._on_delivery(
                _ImmediateCallbackConnection(),
                channel,
                SimpleNamespace(delivery_tag=99),
                None,
                b"{}",
            )
        finally:
            consumer._slots.release()
        assert channel.stop_consuming_calls == 1
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_v3_consumer_local_slot_defers_second_delivery_until_first_finishes(
    tmp_path: Path,
) -> None:
    """The local slot gate prevents Claim while one execution is still running."""

    payload = _valid_consumer_payload()
    first_claimed = threading.Event()
    first_runner_started = threading.Event()
    release_first_runner = threading.Event()
    first_completed = threading.Event()
    second_claimed = threading.Event()
    second_completed = threading.Event()

    class SlotClient:
        def __init__(self) -> None:
            self.claim_calls = 0
            self.result_calls = 0

        def claim_v3(self, _worker_id: int, _dispatch: Mapping[str, Any]) -> dict[str, Any]:
            self.claim_calls += 1
            if self.claim_calls == 1:
                first_claimed.set()
            elif self.claim_calls == 2:
                second_claimed.set()
            return {"decision": "EXECUTE", "payload": payload}

        def start_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            _body: Mapping[str, Any],
        ) -> dict[str, Any]:
            return {}

        def renew_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            _body: Mapping[str, Any],
        ) -> dict[str, Any]:
            return {}

        def result_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            _body: Mapping[str, Any],
        ) -> dict[str, Any]:
            self.result_calls += 1
            if self.result_calls == 1:
                first_completed.set()
            else:
                second_completed.set()
            return {"decision": "ACK_NOOP"}

    client = SlotClient()
    run_calls = 0

    def runner(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal run_calls
        run_calls += 1
        if run_calls == 1:
            first_runner_started.set()
            release_first_runner.wait(timeout=10)
        return {"status": "succeeded"}

    consumer = V3Consumer(
        ConsumerConfig(
            worker_id=7,
            queue="dlr.worker.7.q",
            execution_slots=1,
            runtime_root=tmp_path / "runtime",
            attempt_journal_root=tmp_path / "journal",
        ),
        client,  # type: ignore[arg-type]
        connection_factory=lambda: object(),  # type: ignore[return-value]
        runtime_settings=SimpleNamespace(),
        runner=runner,
    )
    connection = _ImmediateCallbackConnection()
    channel = _NativeDeferChannel()
    body = json.dumps({"message_id": "slot-test"}).encode()
    try:
        consumer._on_delivery(
            connection,
            channel,
            SimpleNamespace(delivery_tag=1),
            None,
            body,
        )
        assert first_claimed.wait(timeout=10)
        assert first_runner_started.wait(timeout=10)

        consumer._on_delivery(
            connection,
            channel,
            SimpleNamespace(delivery_tag=2),
            None,
            body,
        )
        assert client.claim_calls == 1
        assert channel.stop_consuming_calls == 1

        release_first_runner.set()
        assert first_completed.wait(timeout=10)
        slot_deadline = time.monotonic() + 10
        while not consumer._slots.acquire(blocking=False):
            assert time.monotonic() < slot_deadline
            time.sleep(0.01)
        consumer._slots.release()

        consumer._on_delivery(
            connection,
            channel,
            SimpleNamespace(delivery_tag=3),
            None,
            body,
        )
        assert second_claimed.wait(timeout=10)
        assert client.claim_calls == 2
        assert second_completed.wait(timeout=10)
    finally:
        release_first_runner.set()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_defer_uses_native_quorum_return_without_republish_or_ack() -> None:
    """A DEFER returns the original delivery; Rabbit owns delayed retry."""

    consumer = V3Consumer(
        ConsumerConfig(
            worker_id=7,
            queue="dlr.worker.7.q",
            execution_slots=1,
            runtime_root=Path("/tmp/dlr-b2-runtime"),
            attempt_journal_root=Path("/tmp/dlr-b2-journal"),
        ),
        object(),  # type: ignore[arg-type]
        connection_factory=lambda: object(),  # type: ignore[return-value]
        runtime_settings=SimpleNamespace(),
    )
    channel = _NativeDeferChannel()
    try:
        consumer._defer(_ImmediateCallbackConnection(), channel, 41)  # type: ignore[arg-type]
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)

    assert channel.nacks == [{"delivery_tag": 41, "requeue": True}]
    assert channel.publishes == 0
    assert channel.acks == 0


@pytest.mark.parametrize(
    "failure",
    [
        ControlUnavailableError("control partition"),
        ClientError(401, "worker token rejected"),
    ],
)
def test_control_or_auth_failure_pauses_consumer_without_hot_loop(
    failure: Exception,
) -> None:
    """A single delivery failure pauses the channel and leaves it unacked."""

    class FailingClient:
        def claim_v3(self, _worker_id: int, _payload: Mapping[str, Any]) -> dict[str, Any]:
            raise failure

    consumer = V3Consumer(
        ConsumerConfig(
            worker_id=7,
            queue="dlr.worker.7.q",
            execution_slots=1,
            runtime_root=Path("/tmp/dlr-b2-runtime"),
            attempt_journal_root=Path("/tmp/dlr-b2-journal"),
        ),
        FailingClient(),  # type: ignore[arg-type]
        connection_factory=lambda: object(),  # type: ignore[return-value]
        runtime_settings=SimpleNamespace(),
    )
    channel = _NativeDeferChannel()
    try:
        assert consumer._slots.acquire(blocking=False)
        consumer._handle_delivery(
            _ImmediateCallbackConnection(),
            channel,
            delivery_tag=41,
            body=b"{}",
        )
        assert consumer._pause.is_set()
        assert channel.stop_consuming_calls == 1
        assert channel.nacks == []
        assert channel.acks == 0
        assert channel.publishes == 0
        assert consumer._slots.acquire(blocking=False)
        consumer._slots.release()
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_invalid_v3_payload_stays_unacked_when_prepare_failure_cannot_be_reported() -> None:
    """A malformed Claim is not ACKed until Control records its failure."""

    class FailingPrepareClient:
        def prepare_failed_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            _payload: Mapping[str, Any],
        ) -> dict[str, Any]:
            raise ControlUnavailableError("control partition")

    consumer = V3Consumer(
        ConsumerConfig(
            worker_id=7,
            queue="dlr.worker.7.q",
            execution_slots=1,
            runtime_root=Path("/tmp/dlr-b2-runtime"),
            attempt_journal_root=Path("/tmp/dlr-b2-journal"),
        ),
        FailingPrepareClient(),  # type: ignore[arg-type]
        connection_factory=lambda: object(),  # type: ignore[return-value]
        runtime_settings=SimpleNamespace(),
    )
    channel = _NativeDeferChannel()
    decision = {
        "decision": "EXECUTE",
        "payload": {
            "attempt_id": 41,
            "fencing_token": 7,
            "claim_token": "claim-token",
            "cleanup_token": "cleanup-token",
            "execution_id": 13,
            "attempt_no": 1,
            # ResourceProfile is closed and requires the isolation contract.
            "resource_profile": {},
        },
    }
    try:
        assert consumer._slots.acquire(blocking=False)
        consumer._prepare_execute(
            _ImmediateCallbackConnection(),
            channel,
            delivery_tag=41,
            decision=decision,
        )
        assert consumer._pause.is_set()
        assert channel.stop_consuming_calls == 1
        assert channel.nacks == []
        assert channel.acks == 0
    finally:
        consumer._slots.release()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_v3_result_cleanup_removes_journal_after_control_accepts_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A journal is retained for recovery until the terminal result is accepted."""

    class SuccessfulClient:
        def start_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            _payload: Mapping[str, Any],
        ) -> dict[str, Any]:
            return {}

        def result_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            _payload: Mapping[str, Any],
        ) -> dict[str, Any]:
            return {"decision": "ACK_NOOP"}

    removed: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        "dlr.worker.consumer.workspace.remove_attempt_journal",
        lambda root, attempt_id: removed.append((root, attempt_id)) or True,
    )
    payload = V3TaskPayload.model_validate(
        {
            "execution_id": 13,
            "attempt_id": 41,
            "attempt_no": 1,
            "fencing_token": 7,
            "lease_expires_at": datetime.now(UTC) + timedelta(minutes=1),
            "lease_seconds": 30,
            "renew_seconds": 5,
            "claim_token": "claim-token",
            "cleanup_token": "cleanup-token",
            "adapter_id": 19,
            "version_id": 23,
            "language": "python",
            "code": "print('ok')",
            "requirements": "",
            "runtime_config": {},
            "input": None,
            "execution_timeout_seconds": 10,
            "input_source_type": "none",
            "input_snapshot": {"source_type": "none"},
            "resource_profile": {
                "schema_version": 1,
                "resource_class": "small",
                "backend": "cgroup_v2",
                "cpu_cores": 1.0,
                "memory_bytes": 1,
                "pids": 1,
                "tmp_bytes": 1,
                "nofile": 1,
                "execution_timeout_seconds": 10,
                "claim_timeout_seconds": 30,
                "recovery_grace_seconds": 60,
                "workspace_cleanup_attempt_timeout_seconds": 1,
                "workspace_cleanup_total_timeout_seconds": 2,
                "stream_max_bytes": 1,
                "output_max_bytes": 1,
                "output_preview_max_bytes": 1,
            },
        }
    )
    consumer = V3Consumer(
        ConsumerConfig(
            worker_id=7,
            queue="dlr.worker.7.q",
            execution_slots=1,
            runtime_root=Path("/tmp/dlr-b2-runtime"),
            attempt_journal_root=Path("/tmp/dlr-b2-journal"),
        ),
        SuccessfulClient(),  # type: ignore[arg-type]
        connection_factory=lambda: object(),  # type: ignore[return-value]
        runtime_settings=SimpleNamespace(),
        runner=lambda *_args, **_kwargs: {"status": "succeeded"},
    )
    try:
        assert consumer._slots.acquire(blocking=False)
        consumer._run_attempt(payload)
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)
    assert removed == [(Path("/tmp/dlr-b2-journal"), 41)]


@pytest.mark.parametrize("retry_type", ["all", "returned"])
def test_quorum_delayed_retry_policy_matches_native_return_modes(
    monkeypatch: pytest.MonkeyPatch,
    retry_type: str,
) -> None:
    monkeypatch.setattr(settings, "rabbitmq_delayed_retry_type", retry_type)
    arguments = rabbitmq.work_queue_arguments()
    assert arguments["x-queue-type"] == "quorum"
    assert arguments["x-overflow"] == "reject-publish"
    assert arguments["x-dead-letter-strategy"] == "at-least-once"
    assert arguments["x-delayed-retry-type"] == retry_type
    assert arguments["x-delayed-retry-min"] > 0
    assert arguments["x-delayed-retry-max"] >= arguments["x-delayed-retry-min"]


@pytest.mark.parametrize(
    ("retry_base", "retry_max", "expected_min", "expected_max"),
    [
        (0.0001, 0.0002, 1, 1),
        (1.275, 4.275, 1_275, 4_275),
        (300.0, 3_600.0, 300_000, 3_600_000),
    ],
)
def test_quorum_delayed_retry_bounds_derive_integer_milliseconds(
    monkeypatch: pytest.MonkeyPatch,
    retry_base: float,
    retry_max: float,
    expected_min: int,
    expected_max: int,
) -> None:
    monkeypatch.setattr(settings, "rabbitmq_retry_base_seconds", retry_base)
    monkeypatch.setattr(settings, "rabbitmq_retry_max_seconds", retry_max)

    arguments = rabbitmq.work_queue_arguments()

    assert arguments["x-delayed-retry-min"] == expected_min
    assert arguments["x-delayed-retry-max"] == expected_max


def test_quorum_delayed_retry_bounds_reject_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "rabbitmq_retry_base_seconds", 10.0)
    monkeypatch.setattr(settings, "rabbitmq_retry_max_seconds", 1.0)
    with pytest.raises(ValueError, match="DLR_RABBITMQ_RETRY_BASE_SECONDS"):
        rabbitmq.work_queue_arguments()

    monkeypatch.setattr(settings, "rabbitmq_retry_base_seconds", float("inf"))
    with pytest.raises(ValueError, match="finite positive"):
        rabbitmq.work_queue_arguments()


def test_retry_not_due_duplicate_is_ack_noop_and_waits_for_dispatcher(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_canary(monkeypatch)
    worker = _ready_worker(api_client, "b2-retry-not-due-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-retry-not-due-adapter")
    execution = _canary_execution(api_client, adapter["id"])
    dispatch = _dispatch(session_factory, execution["id"])
    retry_at = datetime.now(UTC) + timedelta(hours=1)

    with session_factory.begin() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        row.status = "retry_wait"
        row.next_attempt_at = retry_at

    decision = _claim(session_factory, worker["id"], dispatch)

    assert decision.decision == "ACK_NOOP"
    assert decision.reason == "retry_not_due"
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        attempts = list(
            session.scalars(
                select(ExecutionAttempt).where(ExecutionAttempt.execution_id == execution["id"])
            )
        )
        outbox_rows = list(
            session.scalars(
                select(ExecutionOutbox).where(ExecutionOutbox.execution_id == execution["id"])
            )
        )
        assert row is not None and row.status == "retry_wait"
        assert row.next_attempt_at is not None
        assert len(attempts) == 0
        assert len(outbox_rows) == 1


def test_same_adapter_slot_blocks_second_claim_during_concurrent_long_claim(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first claim holds Slot 0 while a concurrent second claim waits."""
    _enable_canary(monkeypatch)
    worker = _ready_worker(api_client, "b2-slot-concurrency-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-slot-concurrency-adapter")
    first_execution = _canary_execution(api_client, adapter["id"], input_value={"run": 1})
    second_execution = _canary_execution(api_client, adapter["id"], input_value={"run": 2})
    first_dispatch = _dispatch(session_factory, first_execution["id"])
    second_dispatch = _dispatch(session_factory, second_execution["id"])

    first_claim_entered = threading.Event()
    release_first_claim = threading.Event()
    build_lock = threading.Lock()
    build_calls = 0
    original_build = attempt_service._build_v3_payload

    def block_first_claim(*args: Any, **kwargs: Any) -> Any:
        nonlocal build_calls
        with build_lock:
            build_calls += 1
            first_call = build_calls == 1
        if first_call:
            first_claim_entered.set()
            assert release_first_claim.wait(timeout=10)
        return original_build(*args, **kwargs)

    monkeypatch.setattr(attempt_service, "_build_v3_payload", block_first_claim)
    first_result: dict[str, Any] = {}
    second_result: dict[str, Any] = {}

    def claim_first() -> None:
        try:
            first_result["decision"] = _claim(session_factory, worker["id"], first_dispatch)
        except BaseException as error:  # pragma: no cover - surfaced below
            first_result["error"] = error

    def claim_second() -> None:
        try:
            second_result["decision"] = _claim(session_factory, worker["id"], second_dispatch)
        except BaseException as error:  # pragma: no cover - surfaced below
            second_result["error"] = error

    first_thread = threading.Thread(target=claim_first, name="b2-first-claim")
    second_thread = threading.Thread(target=claim_second, name="b2-second-claim")
    first_thread.start()
    assert first_claim_entered.wait(timeout=10)
    second_thread.start()
    release_first_claim.set()
    first_thread.join(timeout=10)
    second_thread.join(timeout=10)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert "error" not in first_result, first_result.get("error")
    assert "error" not in second_result, second_result.get("error")
    first_decision = first_result["decision"]
    second_decision = second_result["decision"]
    assert first_decision.decision == "EXECUTE"
    assert second_decision.decision == "DEFER"
    assert second_decision.reason == "adapter_slot_busy"

    with session_factory() as session:
        attempts = list(
            session.scalars(
                select(ExecutionAttempt)
                .where(ExecutionAttempt.adapter_id == adapter["id"])
                .order_by(ExecutionAttempt.id)
            )
        )
        slot = session.scalar(
            select(AdapterExecutionSlot).where(
                AdapterExecutionSlot.adapter_id == adapter["id"],
                AdapterExecutionSlot.slot_no == 0,
            )
        )
        assert len(attempts) == 1
        assert slot is not None and slot.active_attempt_id == attempts[0].id


@pytest.mark.parametrize("edge", ["lower", "upper"])
def test_retry_jitter_persists_deterministic_db_bounds(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    edge: str,
) -> None:
    _enable_canary(monkeypatch)
    worker = _ready_worker(api_client, f"b2-jitter-{edge}-worker")
    adapter = _rabbit_adapter(api_client, worker, f"b2-jitter-{edge}-adapter")
    execution = _canary_execution(api_client, adapter["id"])
    dispatch = _dispatch(session_factory, execution["id"])
    fixed_now = datetime.now(UTC).replace(microsecond=0)
    policy = {
        "max_attempts": 3,
        "initial_backoff_seconds": 10.0,
        "multiplier": 2.0,
        "max_backoff_seconds": 60.0,
        "jitter_ratio": 0.2,
        "retryable_error_classes": ["platform_transient", "worker_lost"],
    }
    with session_factory.begin() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        row.retry_policy_snapshot = policy

    monkeypatch.setattr(
        attempt_service,
        "database_now",
        lambda _session: fixed_now,
    )
    claimed = _claim(session_factory, worker["id"], dispatch)
    assert claimed.payload is not None and claimed.attempt_id is not None
    lower = 8.0
    upper = 12.0
    monkeypatch.setattr(
        attempt_service.random,
        "uniform",
        lambda _lower, _upper: lower if edge == "lower" else upper,
    )

    with session_factory() as session:
        result = attempt_service.finish_attempt(
            session,
            worker["id"],
            claimed.attempt_id,
            AttemptResultBody.model_validate(
                {
                    "attempt_id": claimed.attempt_id,
                    "fencing_token": claimed.payload.fencing_token,
                    "claim_token": claimed.payload.claim_token,
                    "status": "failed",
                    "error_code": "control_transient",
                    "error_class": "platform_transient",
                }
            ),
        )
        assert result.decision == "ACK_NOOP"

    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None and row.status == "retry_wait"
        assert row.next_attempt_at == fixed_now + timedelta(
            seconds=lower if edge == "lower" else upper
        )


@pytest.mark.skipif(
    os.environ.get("DLR_B2_REAL_RABBIT") != "1",
    reason="real RabbitMQ 4.3.5 evidence requires DLR_B2_REAL_RABBIT=1",
)
@pytest.mark.parametrize("retry_type", ["all", "returned"])
def test_real_rabbit_delayed_busy_allows_other_adapter_progress(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    retry_type: str,
) -> None:
    """Native delayed requeue keeps busy Adapter A from starving Adapter B."""

    real_url = os.environ.get("DLR_B2_REAL_RABBIT_URL")
    management_url = os.environ.get("DLR_B2_REAL_RABBIT_MANAGEMENT_URL")
    if not real_url or not management_url:
        pytest.fail("DLR_B2_REAL_RABBIT_URL and DLR_B2_REAL_RABBIT_MANAGEMENT_URL are required")
    _enable_canary(monkeypatch)
    monkeypatch.setattr(settings, "rabbitmq_url", real_url)
    monkeypatch.setattr(settings, "rabbitmq_management_url", management_url)
    monkeypatch.setattr(settings, "rabbitmq_delayed_retry_type", retry_type)
    worker = _ready_worker(api_client, "b2-real-delayed-worker")
    adapter_a = _rabbit_adapter(api_client, worker, "b2-real-delayed-adapter-a")
    adapter_b = _rabbit_adapter(api_client, worker, "b2-real-delayed-adapter-b")
    connection = rabbitmq.connect()
    channel = connection.channel()
    queue_name: str | None = None
    try:
        capabilities = rabbitmq.verify_runtime_capabilities(connection)
        assert capabilities.version == rabbitmq.RABBITMQ_BASELINE_VERSION
        rabbitmq.mark_runtime_ready(worker_count=1, worker_ids=[worker["id"]])

        names = rabbitmq.bootstrap_topology(channel, worker["id"])
        queue_name = names.queue
        rabbitmq.inspect_topology_policies(worker["id"])
        assert rabbitmq.work_queue_arguments()["x-delayed-retry-type"] == retry_type
        queue_state = channel.queue_declare(queue=queue_name, passive=True).method
        assert queue_state.message_count == 0

        busy_a = _canary_execution(api_client, adapter_a["id"], input_value={"run": "busy"})
        delayed_a = _canary_execution(api_client, adapter_a["id"], input_value={"run": "delayed"})
        execution_b = _canary_execution(
            api_client, adapter_b["id"], input_value={"run": "progress"}
        )
        dispatch_busy_a = _dispatch(session_factory, busy_a["id"])
        dispatch_delayed_a = _dispatch(session_factory, delayed_a["id"])
        dispatch_b = _dispatch(session_factory, execution_b["id"])

        busy_claim = _claim(session_factory, worker["id"], dispatch_busy_a)
        assert busy_claim.decision == "EXECUTE"
        assert busy_claim.attempt_id is not None

        channel.confirm_delivery()
        for dispatch in (dispatch_delayed_a, dispatch_b):
            channel.basic_publish(
                exchange=DISPATCH_EXCHANGE,
                routing_key=worker_routing_key(worker["id"]),
                body=json.dumps(dispatch, separators=(",", ":")).encode(),
                properties=pika.BasicProperties(
                    delivery_mode=pika.DeliveryMode.Persistent,
                    message_id=str(dispatch["message_id"]),
                    content_type="application/json",
                ),
                mandatory=True,
            )

        first_method, _first_properties, first_body = channel.basic_get(
            queue=queue_name, auto_ack=False
        )
        first_deadline = time.monotonic() + 2
        while first_method is None and time.monotonic() < first_deadline:
            time.sleep(0.01)
            first_method, _first_properties, first_body = channel.basic_get(
                queue=queue_name, auto_ack=False
            )
        assert first_method is not None and first_body is not None
        first_dispatch = json.loads(first_body)
        assert first_dispatch["execution_id"] == delayed_a["id"]
        delayed_decision = _claim(session_factory, worker["id"], first_dispatch)
        assert delayed_decision.decision == "DEFER"
        assert delayed_decision.reason == "adapter_slot_busy"
        channel.basic_nack(delivery_tag=first_method.delivery_tag, requeue=True)

        second_method, _second_properties, second_body = channel.basic_get(
            queue=queue_name, auto_ack=False
        )
        second_deadline = time.monotonic() + 2
        while second_method is None and time.monotonic() < second_deadline:
            time.sleep(0.01)
            second_method, _second_properties, second_body = channel.basic_get(
                queue=queue_name, auto_ack=False
            )
        assert second_method is not None and second_body is not None
        second_dispatch = json.loads(second_body)
        assert second_dispatch["execution_id"] == execution_b["id"]
        b_claim = _claim(session_factory, worker["id"], second_dispatch)
        assert b_claim.decision == "EXECUTE"
        assert b_claim.attempt_id is not None and b_claim.payload is not None
        with session_factory() as session:
            result = attempt_service.finish_attempt(
                session,
                worker["id"],
                b_claim.attempt_id,
                AttemptResultBody.model_validate(
                    {
                        "attempt_id": b_claim.attempt_id,
                        "fencing_token": b_claim.payload.fencing_token,
                        "claim_token": b_claim.payload.claim_token,
                        "status": "succeeded",
                    }
                ),
            )
            assert result.decision == "ACK_NOOP"
        channel.basic_ack(delivery_tag=second_method.delivery_tag)

        # The native retry minimum is derived from the existing one-second
        # retry setting.  A busy message must not spin back into the queue
        # while another Adapter's ready message is allowed to make progress.
        retry_min_ms = rabbitmq.work_queue_arguments()["x-delayed-retry-min"]
        assert isinstance(retry_min_ms, int) and retry_min_ms >= 1_000
        hot_loop_deadline = time.monotonic() + min(retry_min_ms / 1_000 / 2, 0.5)
        while time.monotonic() < hot_loop_deadline:
            method, _properties, body = channel.basic_get(queue=queue_name, auto_ack=False)
            if method is None:
                time.sleep(0.01)
                continue
            assert body is not None
            assert json.loads(body)["execution_id"] != delayed_a["id"]
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
            pytest.fail("busy Adapter A delivery was redelivered before native delay elapsed")

        with session_factory() as session:
            delayed_row = session.get(Execution, delayed_a["id"])
            busy_row = session.get(Execution, busy_a["id"])
            b_row = session.get(Execution, execution_b["id"])
            assert delayed_row is not None and delayed_row.attempt_count == 0
            assert busy_row is not None and busy_row.attempt_count == 1
            assert b_row is not None and b_row.status == "succeeded"
            assert b_row.attempt_count == 1
    finally:
        if channel.is_open and queue_name is not None:
            channel.queue_delete(queue=queue_name)
        if channel.is_open:
            channel.close()
        if connection.is_open:
            connection.close()


def test_invalid_delayed_retry_mode_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rabbitmq_delayed_retry_type", "failed")
    with pytest.raises(ValueError, match="DLR_RABBITMQ_DELAYED_RETRY_TYPE"):
        validate_deployment_configuration(settings)


def test_claim_duplicate_lease_recovery_and_stale_result_are_fenced(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_canary(monkeypatch)
    worker = _ready_worker(api_client, "b2-failure-matrix-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-failure-matrix-adapter")
    execution = _canary_execution(api_client, adapter["id"])
    dispatch = _dispatch(session_factory, execution["id"])

    first = _claim(session_factory, worker["id"], dispatch)
    assert first.decision == "EXECUTE"
    assert first.payload is not None
    assert first.attempt_id is not None
    payload = first.payload

    duplicate = _claim(session_factory, worker["id"], dispatch)
    assert duplicate.decision == "ACK_NOOP"
    assert duplicate.reason == "execution_not_queued"

    with session_factory.begin() as session:
        session.execute(
            update(ExecutionAttempt)
            .where(ExecutionAttempt.id == first.attempt_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    with session_factory() as session:
        assert attempt_service.recover_expired_attempts(session, limit=10) == 1

    with session_factory() as session:
        recovered = session.get(ExecutionAttempt, first.attempt_id)
        current = session.get(Execution, execution["id"])
        slot = session.scalar(
            select(AdapterExecutionSlot).where(
                AdapterExecutionSlot.adapter_id == adapter["id"],
                AdapterExecutionSlot.slot_no == 0,
            )
        )
        assert recovered is not None and recovered.status == "worker_lost"
        assert current is not None and current.status == "retry_wait"
        assert slot is not None and slot.active_attempt_id is None

    with session_factory() as session:
        assert (
            attempt_service.retry_dispatcher_once(
                session,
                now=datetime.now(UTC) + timedelta(days=1),
            )
            == 1
        )

    with session_factory() as session:
        stale = attempt_service.finish_attempt(
            session,
            worker["id"],
            first.attempt_id,
            AttemptResultBody.model_validate(
                {
                    "attempt_id": first.attempt_id,
                    "fencing_token": payload.fencing_token,
                    "claim_token": payload.claim_token,
                    "status": "succeeded",
                }
            ),
        )
        assert stale.decision == "ACK_NOOP"
        assert stale.reason == "already_terminal"
        current = session.get(Execution, execution["id"])
        assert current is not None and current.status == "queued"
        assert current.dispatch_generation == 2


def test_attempt_actions_reject_wrong_token_and_stale_fence(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_canary(monkeypatch)
    worker = _ready_worker(api_client, "b2-fence-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-fence-adapter")
    execution = _canary_execution(api_client, adapter["id"])
    dispatch = _dispatch(session_factory, execution["id"])
    claimed = _claim(session_factory, worker["id"], dispatch)
    assert claimed.payload is not None and claimed.attempt_id is not None
    payload = claimed.payload

    with session_factory() as session:
        with pytest.raises(HTTPException) as wrong_token:
            attempt_service.renew_attempt(
                session,
                worker["id"],
                claimed.attempt_id,
                AttemptRenewBody.model_validate(
                    {
                        "attempt_id": claimed.attempt_id,
                        "fencing_token": payload.fencing_token,
                        "claim_token": "wrong-token",
                    }
                ),
            )
        assert wrong_token.value.detail["code"] == "attempt_token_invalid"

    with session_factory() as session:
        with pytest.raises(HTTPException) as stale_fence:
            attempt_service.renew_attempt(
                session,
                worker["id"],
                claimed.attempt_id,
                AttemptRenewBody.model_validate(
                    {
                        "attempt_id": claimed.attempt_id,
                        "fencing_token": payload.fencing_token + 1,
                        "claim_token": payload.claim_token,
                    }
                ),
            )
        assert stale_fence.value.detail["code"] == "attempt_stale_fence"


def test_business_dead_letter_replay_releases_admission_and_keeps_generation_audit(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_canary(monkeypatch)
    worker = _ready_worker(api_client, "b2-dead-letter-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-dead-letter-adapter")
    execution = _canary_execution(api_client, adapter["id"], input_value={"case": "business"})
    dispatch = _dispatch(session_factory, execution["id"])
    claimed = _claim(session_factory, worker["id"], dispatch)
    assert claimed.payload is not None and claimed.attempt_id is not None

    with session_factory.begin() as session:
        current = session.get(Execution, execution["id"])
        assert current is not None
        policy = reliable_execution.default_retry_policy()
        policy["max_attempts"] = 1
        current.retry_policy_snapshot = policy
        current.max_attempts_snapshot = 1

    with session_factory() as session:
        result = attempt_service.finish_attempt(
            session,
            worker["id"],
            claimed.attempt_id,
            AttemptResultBody.model_validate(
                {
                    "attempt_id": claimed.attempt_id,
                    "fencing_token": claimed.payload.fencing_token,
                    "claim_token": claimed.payload.claim_token,
                    "status": "failed",
                    "error_code": "adapter_business_error",
                    "error_class": "business_error",
                    "error": "adapter rejected input",
                }
            ),
        )
        assert result.decision == "ACK_NOOP"

    with session_factory() as session:
        current = session.get(Execution, execution["id"])
        adapter_counter = session.get(AdapterExecutionAdmission, adapter["id"])
        global_counter = session.get(GlobalExecutionAdmission, "global")
        outbox_rows = list(
            session.scalars(
                select(ExecutionOutbox).where(ExecutionOutbox.execution_id == execution["id"])
            )
        )
        assert current is not None and current.status == "dead_letter"
        assert current.admission_released_at is not None
        assert adapter_counter is not None and adapter_counter.outstanding_count == 0
        assert global_counter is not None and global_counter.outstanding_count == 0
        assert len(outbox_rows) == 1

    replay = api_client.post(f"/api/executions/{execution['id']}/replay")
    assert replay.status_code == 200, replay.text
    replay_body = replay.json()
    assert replay_body["replay_of_execution_id"] == execution["id"]
    with session_factory() as session:
        replayed = session.get(Execution, replay_body["execution_id"])
        assert replayed is not None
        assert replayed.dispatch_backend == "rabbitmq"
        assert replayed.status == "queued"
        assert replayed.replay_of_execution_id == execution["id"]


def test_infrastructure_dlq_mismatch_is_visible_manual_review_not_business_dead_letter(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_canary(monkeypatch)
    worker = _ready_worker(api_client, "b2-infra-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-infra-adapter")
    execution = _canary_execution(api_client, adapter["id"])
    dispatch = _dispatch(session_factory, execution["id"])
    dispatch["language"] = "javascript"

    with session_factory() as session:
        result = infrastructure_dlq.reconcile_message(
            session,
            json.dumps(dispatch).encode(),
            headers={"x-death": [{"reason": "rejected"}]},
        )
        assert result.action == "manual_review"
        incident = session.get(ExecutionInfrastructureIncident, result.incident_id)
        current = session.get(Execution, execution["id"])
        assert incident is not None and incident.status == "open"
        assert current is not None and current.status == "queued"


def test_dark_launch_inventory_and_pending_migration_converge_without_cutover(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_canary(monkeypatch)
    worker = _ready_worker(api_client, "b2-migration-worker")
    adapter = _rabbit_adapter(client=api_client, worker=worker, name="b2-migration-adapter")
    legacy = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert legacy.status_code == 202, legacy.text
    legacy_body = legacy.json()
    assert legacy_body["dispatch_backend"] == "legacy"
    assert legacy_body["status"] == "pending"

    inventory_response = api_client.get("/api/admin/reliable-runtime/inventory")
    assert inventory_response.status_code == 200, inventory_response.text
    facts = inventory_response.json()
    assert facts["dark_launch"]["rabbitmq_production_ingress_enabled"] is False
    assert facts["dark_launch"]["ordinary_new_traffic_backend"] == "legacy"
    assert facts["dark_launch"]["old_active_index_present"] is True
    assert facts["dark_launch"]["legacy_claim_enabled"] is True
    assert facts["sandbox_readiness"]["sandbox_gate"] == "not_run"
    assert facts["sandbox_readiness"]["cutover_ready"] is False

    dry_run = api_client.post("/api/admin/reliable-runtime/migration/dry-run")
    assert dry_run.status_code == 200, dry_run.text
    assert dry_run.json()["dry_run"]["would_convert_pending"] == 1

    migrated = api_client.post(
        "/api/admin/reliable-runtime/migration/legacy-pending",
        json={"limit": 1},
    )
    assert migrated.status_code == 200, migrated.text
    assert migrated.json()["converted"] == 1
    with session_factory() as session:
        converted = session.get(Execution, legacy_body["id"])
        outbox_rows = list(
            session.scalars(
                select(ExecutionOutbox).where(ExecutionOutbox.execution_id == legacy_body["id"])
            )
        )
        assert converted is not None and converted.dispatch_backend == "rabbitmq"
        assert converted.status == "queued"
        assert len(outbox_rows) == 1

    repeated = api_client.post(
        "/api/admin/reliable-runtime/migration/legacy-pending",
        json={"limit": 1},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["converted"] == 0
    assert repeated.json()["legacy_pending_remaining"] == 0


def test_legacy_running_drain_is_an_explicit_boundary(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_canary(monkeypatch)
    worker = _ready_worker(api_client, "b2-drain-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-drain-adapter")
    legacy = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert legacy.status_code == 202, legacy.text
    with session_factory.begin() as session:
        row = session.get(Execution, legacy.json()["id"])
        assert row is not None
        row.status = "running"
        row.worker_id = worker["id"]

    blocked = api_client.post("/api/admin/reliable-runtime/migration/legacy-running-drain")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "legacy_running_not_drained"

    with session_factory.begin() as session:
        row = session.get(Execution, legacy.json()["id"])
        assert row is not None
        row.status = "succeeded"
    drained = api_client.post("/api/admin/reliable-runtime/migration/legacy-running-drain")
    assert drained.status_code == 200, drained.text
    assert drained.json() == {"status": "drained", "legacy_running": 0}


@pytest.mark.parametrize("language", ["python", "javascript", "java"])
def test_three_language_canary_claim_and_terminal_cleanup(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    language: str,
) -> None:
    _enable_canary(monkeypatch)
    worker = _ready_worker(api_client, f"b2-canary-{language}", [language])
    adapter = _rabbit_adapter(
        api_client, worker, f"b2-canary-adapter-{language}", language=language
    )
    execution = _canary_execution(api_client, adapter["id"], input_value={"language": language})
    dispatch = _dispatch(session_factory, execution["id"])
    claimed = _claim(session_factory, worker["id"], dispatch)
    assert claimed.decision == "EXECUTE"
    assert claimed.payload is not None and claimed.attempt_id is not None
    with session_factory() as session:
        terminal = attempt_service.finish_attempt(
            session,
            worker["id"],
            claimed.attempt_id,
            AttemptResultBody.model_validate(
                {
                    "attempt_id": claimed.attempt_id,
                    "fencing_token": claimed.payload.fencing_token,
                    "claim_token": claimed.payload.claim_token,
                    "status": "succeeded",
                    "output": {"ok": True, "language": language},
                }
            ),
        )
        assert terminal.decision == "ACK_NOOP"
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None and row.status == "succeeded"


def test_managed_file_dead_letter_hold_is_bounded_and_replayable(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_canary(monkeypatch)
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    worker = _ready_worker(api_client, "b2-managed-canary-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-managed-canary-adapter")
    artifact_id = create_artifact(session_factory, adapter["id"], "b2-input.txt", status="READY")
    configured = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [artifact_id],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert configured.status_code == 200, configured.text
    execution = _canary_execution(api_client, adapter["id"])
    dispatch = _dispatch(session_factory, execution["id"])
    claimed = _claim(session_factory, worker["id"], dispatch)
    assert claimed.payload is not None and claimed.attempt_id is not None
    with session_factory() as session:
        current = session.get(Execution, execution["id"])
        assert current is not None
        policy = reliable_execution.default_retry_policy()
        policy["max_attempts"] = 1
        current.retry_policy_snapshot = policy
        current.max_attempts_snapshot = 1
    with session_factory() as session:
        attempt_service.finish_attempt(
            session,
            worker["id"],
            claimed.attempt_id,
            AttemptResultBody.model_validate(
                {
                    "attempt_id": claimed.attempt_id,
                    "fencing_token": claimed.payload.fencing_token,
                    "claim_token": claimed.payload.claim_token,
                    "status": "failed",
                    "error_code": "input_business_error",
                    "error_class": "business_error",
                }
            ),
        )
    with session_factory() as session:
        hold = session.scalar(
            select(ExecutionArtifactHold).where(
                ExecutionArtifactHold.execution_id == execution["id"]
            )
        )
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert hold is not None and hold.held_bytes == 8
        assert artifact is not None and artifact.size_bytes == hold.held_bytes
        assert attempt_service.held_backlog(session) == (1, 8)
