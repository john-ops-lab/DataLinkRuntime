"""Deterministic Batch 2 attempt, native defer, migration and failure tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pika
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings, validate_deployment_configuration
from dlr.control.models import (
    Adapter,
    AdapterExecutionAdmission,
    AdapterExecutionSlot,
    Execution,
    ExecutionArtifactHold,
    ExecutionAttempt,
    ExecutionInfrastructureIncident,
    ExecutionInputArtifactLease,
    ExecutionOutbox,
    GlobalExecutionAdmission,
    ManagedInputArtifact,
)
from dlr.control.schemas.reliable_runtime import (
    AttemptProgressBody,
    AttemptRenewBody,
    AttemptResultBody,
    AttemptStartBody,
    V3TaskPayload,
)
from dlr.control.services import attempt as attempt_service
from dlr.control.services import execution as execution_service
from dlr.control.services import (
    execution_cancellation,
    infrastructure_dlq,
    rabbitmq,
    reliable_execution,
)
from dlr.control.services.artifact_store import LocalFileArtifactStore
from dlr.control.services.dispatch import DISPATCH_EXCHANGE, worker_routing_key
from dlr.worker import executor, workspace
from dlr.worker.client import ClientError, ControlUnavailableError
from dlr.worker.consumer import ConsumerConfig, V3Consumer
from test_adapters import create_adapter, save_version
from test_issue127_b2_binding import create_artifact
from worker_runtime_support import (
    install_test_sandbox,
    unit_resource_envelope,
    unit_sandbox_config,
)


@pytest.fixture(autouse=True)
def _unit_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    install_test_sandbox(monkeypatch)


ISOLATION_PASS = {
    "cgroup_v2": True,
    "cgroup_namespace_private": True,
    "mount_namespace": True,
    "pid_namespace": True,
    "memory_hard_limit": True,
    "pids_hard_limit": True,
    "tmpfs_hard_limit": True,
    "bounded_output": True,
    "preflight_passed": True,
    "resource_envelope_verified": True,
    "cpu_hard_limit": True,
    "swap_hard_limit": True,
    "nofile_hard_limit": True,
    "no_new_privileges": True,
    "cgroup_kill": True,
    "adapter_control_plane_hidden": True,
    "adapter_mount_blocked": True,
    "sandbox_cleanup": True,
}
WORKER_HEADERS = {"Authorization": "Bearer test-worker-token"}


def _enable_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
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
    code: str | None = None,
) -> dict[str, Any]:
    adapter = create_adapter(client, name=name, language=language)
    if code is None:
        save_version(client, adapter["id"])
    else:
        save_version(client, adapter["id"], code=code)
    response = client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"runtime_worker_id": worker["id"]},
    )
    assert response.status_code == 200, response.text
    return adapter


def _execution(
    client: TestClient,
    adapter_id: int,
    *,
    input_value: Any = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if input_value is not None:
        payload["input"] = input_value
    response = client.post(f"/api/adapters/{adapter_id}/executions", json=payload)
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


def _dispatch_generation(
    session_factory: sessionmaker[Session], execution_id: int, generation: int
) -> dict[str, Any]:
    with session_factory() as session:
        row = session.scalar(
            select(ExecutionOutbox)
            .where(ExecutionOutbox.execution_id == execution_id)
            .where(ExecutionOutbox.dispatch_generation == generation)
        )
        assert row is not None
        return dict(row.payload_json)


def _claim(session_factory: sessionmaker[Session], worker_id: int, dispatch: dict[str, Any]) -> Any:
    with session_factory() as session:
        return attempt_service.claim_dispatch(session, worker_id, dispatch)


class _V3ServiceClient:
    """Test transport that drives the real Control services and download API."""

    def __init__(
        self,
        api_client: TestClient,
        session_factory: sessionmaker[Session],
        *,
        fail_first_cleanup_receipt: bool = False,
    ) -> None:
        self.api_client = api_client
        self.session_factory = session_factory
        self.fail_first_cleanup_receipt = fail_first_cleanup_receipt
        self.claimed_payload: dict[str, Any] | None = None
        self.result_bodies: list[dict[str, Any]] = []
        self.result_event = threading.Event()
        self.cleanup_receipt_attempted = threading.Event()
        self.cleanup_receipt_calls: list[tuple[int, str]] = []
        self.download_claim_tokens: list[str] = []

    def claim_v3(self, worker_id: int, dispatch: Mapping[str, Any]) -> dict[str, Any]:
        with self.session_factory() as session:
            decision = attempt_service.claim_dispatch(session, worker_id, dispatch)
        body = decision.model_dump(mode="json")
        payload = body.get("payload")
        if isinstance(payload, Mapping):
            self.claimed_payload = dict(payload)
        return body

    def start_attempt(
        self, worker_id: int, attempt_id: int, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            decision = attempt_service.start_attempt(
                session,
                worker_id,
                attempt_id,
                AttemptStartBody.model_validate(body),
            )
        return decision.model_dump(mode="json")

    def renew_attempt(
        self, worker_id: int, attempt_id: int, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            decision = attempt_service.renew_attempt(
                session,
                worker_id,
                attempt_id,
                AttemptRenewBody.model_validate(body),
            )
        return decision.model_dump(mode="json")

    def progress_attempt(
        self, worker_id: int, attempt_id: int, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            decision = attempt_service.progress_attempt(
                session,
                worker_id,
                attempt_id,
                AttemptProgressBody.model_validate(body),
            )
        return decision.model_dump(mode="json")

    def result_attempt(
        self, worker_id: int, attempt_id: int, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        self.result_bodies.append(dict(body))
        try:
            with self.session_factory() as session:
                decision = attempt_service.finish_attempt(
                    session,
                    worker_id,
                    attempt_id,
                    AttemptResultBody.model_validate(body),
                )
            return decision.model_dump(mode="json")
        finally:
            self.result_event.set()

    def download_input_artifact(
        self,
        worker_id: int,
        execution_id: int,
        artifact_id: int,
        *,
        claim_token: str,
        destination: Any,
    ) -> int:
        self.download_claim_tokens.append(claim_token)
        response = self.api_client.get(
            f"/api/workers/{worker_id}/executions/{execution_id}"
            f"/input-artifacts/{artifact_id}/content",
            headers={**WORKER_HEADERS, "X-DLR-Claim-Token": claim_token},
        )
        if response.status_code != 200:
            raise ClientError(response.status_code, response.text)
        destination.write(response.content)
        return len(response.content)

    def report_cleanup_receipt(
        self, _worker_id: int, execution_id: int, *, cleanup_token: str
    ) -> dict[str, Any]:
        self.cleanup_receipt_attempted.set()
        self.cleanup_receipt_calls.append((execution_id, cleanup_token))
        if self.fail_first_cleanup_receipt:
            self.fail_first_cleanup_receipt = False
            raise ControlUnavailableError("simulated restart before cleanup receipt")
        response = self.api_client.post(
            f"/api/workers/executions/{execution_id}/workspace-cleanup",
            json={"status": "completed"},
            headers={**WORKER_HEADERS, "X-DLR-Cleanup-Token": cleanup_token},
        )
        if response.status_code != 200:
            raise ClientError(response.status_code, response.text)
        return response.json()


class _NativeDeferChannel:
    def __init__(self) -> None:
        self.nacks: list[dict[str, Any]] = []
        self.publishes = 0
        self.acks = 0
        self.stop_consuming_calls = 0
        self.qos: dict[str, Any] = {}
        self.consume_callback: Any | None = None

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

    def basic_consume(self, **kwargs: Any) -> None:
        self.consume_callback = kwargs["on_message_callback"]


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
        "recovery_grace_seconds_snapshot": 60,
        "workspace_cleanup_attempt_timeout_seconds_snapshot": 1,
        "workspace_cleanup_total_timeout_seconds_snapshot": 2,
        "input_source_type": "none",
        "input_snapshot": {"source_type": "none"},
        "resource_profile": {
            "schema_version": 1,
            "resource_class": "small",
            "backend": "cgroup_v2",
            "cpu_cores": 1.0,
            "memory_bytes": 16 * 1024 * 1024,
            "pids": 16,
            "tmp_bytes": 1024 * 1024,
            "nofile": 64,
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
        runtime_settings=SimpleNamespace(
            sandbox_config=unit_sandbox_config(), resource_envelope=unit_resource_envelope()
        ),
    )
    try:
        consumer.run()
        channel = connection_holder["connection"].channel_instance
        assert channel.qos == {"prefetch_count": 1, "global_qos": False}
        assert channel.consume_callback is not None
        assert channel.consume_callback.args == (connection_holder["connection"],)
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
            return {"decision": "ACK_NOOP", "reason": "started"}

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
        runtime_settings=SimpleNamespace(
            sandbox_config=unit_sandbox_config(), resource_envelope=unit_resource_envelope()
        ),
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
        runtime_settings=SimpleNamespace(
            sandbox_config=unit_sandbox_config(), resource_envelope=unit_resource_envelope()
        ),
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
        runtime_settings=SimpleNamespace(
            sandbox_config=unit_sandbox_config(), resource_envelope=unit_resource_envelope()
        ),
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
        runtime_settings=SimpleNamespace(
            sandbox_config=unit_sandbox_config(), resource_envelope=unit_resource_envelope()
        ),
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
        def __init__(self) -> None:
            self.cleanup_receipts: list[tuple[int, str]] = []

        def start_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            _payload: Mapping[str, Any],
        ) -> dict[str, Any]:
            return {"decision": "ACK_NOOP", "reason": "started"}

        def result_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            _payload: Mapping[str, Any],
        ) -> dict[str, Any]:
            return {"decision": "ACK_NOOP"}

        def report_cleanup_receipt(
            self,
            _worker_id: int,
            execution_id: int,
            *,
            cleanup_token: str,
        ) -> dict[str, Any]:
            self.cleanup_receipts.append((execution_id, cleanup_token))
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
    client = SuccessfulClient()
    consumer = V3Consumer(
        ConsumerConfig(
            worker_id=7,
            queue="dlr.worker.7.q",
            execution_slots=1,
            runtime_root=Path("/tmp/dlr-b2-runtime"),
            attempt_journal_root=Path("/tmp/dlr-b2-journal"),
        ),
        client,  # type: ignore[arg-type]
        connection_factory=lambda: object(),  # type: ignore[return-value]
        runtime_settings=SimpleNamespace(
            sandbox_config=unit_sandbox_config(), resource_envelope=unit_resource_envelope()
        ),
        runner=lambda *_args, **_kwargs: {
            "status": "succeeded",
            "workspace_cleanup_status": "completed",
        },
    )
    try:
        assert consumer._slots.acquire(blocking=False)
        consumer._run_attempt(payload)
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)
    assert removed == [(Path("/tmp/dlr-b2-journal"), 41)]
    assert client.cleanup_receipts == [(13, "cleanup-token")]


@pytest.mark.parametrize("cancel_channel", ["renew", "progress"])
def test_v3_consumer_reports_cancel_after_control_ack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cancel_channel: str
) -> None:
    """A cancel acknowledgement on either poll path permits the cancelled report."""

    payload_data = _valid_consumer_payload()
    payload_data.update({"lease_seconds": 10, "renew_seconds": 1})
    payload = V3TaskPayload.model_validate(payload_data)
    renewal_seen = threading.Event()
    progress_seen = threading.Event()

    class CancelClient:
        def __init__(self) -> None:
            self.result_body: dict[str, Any] | None = None

        def start_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            _body: Mapping[str, Any],
        ) -> dict[str, Any]:
            return {"decision": "ACK_NOOP", "reason": "started"}

        def renew_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            _body: Mapping[str, Any],
        ) -> dict[str, Any]:
            renewal_seen.set()
            if cancel_channel != "renew":
                return {
                    "decision": "ACK_NOOP",
                    "reason": "renewed",
                    "attempt_id": payload.attempt_id,
                    "cancel_requested": False,
                }
            return {
                "decision": "ACK_NOOP",
                "reason": "cancel_requested",
                "attempt_id": payload.attempt_id,
                "cancel_requested": True,
            }

        def progress_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            _body: Mapping[str, Any],
        ) -> dict[str, Any]:
            progress_seen.set()
            if cancel_channel == "progress":
                return {
                    "decision": "ACK_NOOP",
                    "reason": "cancel_requested",
                    "attempt_id": payload.attempt_id,
                    "cancel_requested": True,
                }
            return {
                "decision": "ACK_NOOP",
                "reason": "progressed",
                "attempt_id": payload.attempt_id,
                "cancel_requested": False,
            }

        def result_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            body: Mapping[str, Any],
        ) -> dict[str, Any]:
            self.result_body = dict(body)
            return {"decision": "ACK_NOOP"}

        def report_cleanup_receipt(
            self,
            _worker_id: int,
            _execution_id: int,
            *,
            cleanup_token: str,
        ) -> dict[str, Any]:
            assert cleanup_token == payload.cleanup_token
            return {"decision": "ACK_NOOP"}

    removed: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        "dlr.worker.consumer.workspace.remove_attempt_journal",
        lambda root, attempt_id: removed.append((root, attempt_id)) or True,
    )
    client = CancelClient()

    def runner(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        progress = kwargs["progress_callback"]
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if progress("", ""):
                return {
                    "status": "succeeded",
                    "workspace_cleanup_status": "completed",
                }
            time.sleep(0.05)
        raise AssertionError("cancellation was not observed")

    consumer = V3Consumer(
        ConsumerConfig(
            worker_id=7,
            queue="dlr.worker.test.q",
            execution_slots=1,
            runtime_root=tmp_path / "runtime",
            attempt_journal_root=tmp_path / "journal",
        ),
        client,  # type: ignore[arg-type]
        connection_factory=lambda: object(),  # type: ignore[return-value]
        runtime_settings=SimpleNamespace(
            sandbox_config=unit_sandbox_config(), resource_envelope=unit_resource_envelope()
        ),
        runner=runner,
    )
    try:
        assert consumer._slots.acquire(blocking=False)
        consumer._run_attempt(payload)
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)

    if cancel_channel == "renew":
        assert renewal_seen.is_set()
    else:
        assert progress_seen.is_set()
    assert client.result_body is not None
    assert client.result_body["status"] == "cancelled"
    assert client.result_body["error_code"] == "execution_cancelled"
    assert removed == [(tmp_path / "journal", payload.attempt_id)]


@pytest.mark.parametrize("transition", ["cancel", "lease_recovery"])
def test_v3_start_boundary_fails_closed_after_cancel_or_recovery(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transition: str,
) -> None:
    """A Claim never starts an Adapter after Control withdraws run authority."""

    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"b2-start-boundary-{transition}-worker")
    adapter = _rabbit_adapter(api_client, worker, f"b2-start-boundary-{transition}-adapter")
    execution = _execution(api_client, adapter["id"])
    dispatch = _dispatch(session_factory, execution["id"])
    claimed = _claim(session_factory, worker["id"], dispatch)
    assert claimed.payload is not None and claimed.attempt_id is not None

    if transition == "cancel":
        with session_factory() as session:
            cancelled = execution_service.cancel_execution(session, execution["id"])
            assert cancelled.status == "running"
            assert cancelled.cancel_requested is True
    else:
        with session_factory.begin() as session:
            session.execute(
                update(ExecutionAttempt)
                .where(ExecutionAttempt.id == claimed.attempt_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        with session_factory() as session:
            assert attempt_service.recover_expired_attempts(session, limit=10) == 1

    class StartClient:
        def __init__(self) -> None:
            self.response: dict[str, Any] | None = None

        def start_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            body: Mapping[str, Any],
        ) -> dict[str, Any]:
            with session_factory() as session:
                decision = attempt_service.start_attempt(
                    session,
                    worker["id"],
                    claimed.attempt_id,
                    AttemptStartBody.model_validate(body),
                )
            self.response = decision.model_dump(mode="json")
            return self.response

    client = StartClient()
    runner_calls = 0

    def runner(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal runner_calls
        runner_calls += 1
        return {"status": "succeeded"}

    consumer = V3Consumer(
        ConsumerConfig(
            worker_id=worker["id"],
            queue="dlr.worker.test.q",
            execution_slots=1,
            runtime_root=tmp_path / "runtime",
            attempt_journal_root=tmp_path / "journal",
        ),
        client,  # type: ignore[arg-type]
        connection_factory=lambda: object(),  # type: ignore[return-value]
        runtime_settings=SimpleNamespace(
            sandbox_config=unit_sandbox_config(), resource_envelope=unit_resource_envelope()
        ),
        runner=runner,
    )
    try:
        assert consumer._slots.acquire(blocking=False)
        consumer._run_attempt(claimed.payload)
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)

    assert runner_calls == 0
    assert client.response is not None
    assert client.response["decision"] == "ACK_NOOP"
    assert client.response["reason"] in {"cancel_requested", "already_terminal"}
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        attempt = session.get(ExecutionAttempt, claimed.attempt_id)
        assert row is not None and attempt is not None
        if transition == "cancel":
            assert row.status == "cancelled"
            assert attempt.status == "cancelled"
            assert row.admission_released_at is not None
        else:
            assert row.status == "retry_wait"
            assert attempt.status == "worker_lost"


@pytest.mark.parametrize("lost_via", ["renew", "progress"])
def test_v3_consumer_stops_runner_after_terminal_renew_or_progress_response(
    tmp_path: Path,
    lost_via: str,
) -> None:
    """A terminal Control response stops the local runner before Result."""

    payload_data = _valid_consumer_payload()
    payload_data.update({"lease_seconds": 10, "renew_seconds": 1})
    payload = V3TaskPayload.model_validate(payload_data)
    runner_started = threading.Event()
    runner_stopped = threading.Event()
    renew_called = threading.Event()
    renew_response_allowed = threading.Event()
    renew_response_returned = threading.Event()
    terminal_response = {
        "decision": "ACK_NOOP",
        "reason": "already_terminal",
        "attempt_id": payload.attempt_id,
        "cancel_requested": False,
    }
    renewed_response = {
        "decision": "ACK_NOOP",
        "reason": "renewed",
        "attempt_id": payload.attempt_id,
        "cancel_requested": False,
    }
    progressed_response = {
        "decision": "ACK_NOOP",
        "reason": "progressed",
        "attempt_id": payload.attempt_id,
        "cancel_requested": False,
    }

    class AuthorityClient:
        def __init__(self) -> None:
            self.renew_calls = 0
            self.progress_calls = 0
            self.result_calls = 0

        def start_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            _body: Mapping[str, Any],
        ) -> dict[str, Any]:
            return {
                "decision": "ACK_NOOP",
                "reason": "started",
                "attempt_id": payload.attempt_id,
                "cancel_requested": False,
            }

        def renew_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            _body: Mapping[str, Any],
        ) -> dict[str, Any]:
            self.renew_calls += 1
            renew_called.set()
            if lost_via == "renew":
                if not renew_response_allowed.wait(timeout=3):
                    raise AssertionError("test did not release the first renew response")
                response = terminal_response
            else:
                response = renewed_response
            renew_response_returned.set()
            return response

        def progress_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            _body: Mapping[str, Any],
        ) -> dict[str, Any]:
            self.progress_calls += 1
            return terminal_response if lost_via == "progress" else progressed_response

        def result_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            _body: Mapping[str, Any],
        ) -> dict[str, Any]:
            self.result_calls += 1
            return {"decision": "ACK_NOOP", "reason": "already_terminal"}

    client = AuthorityClient()

    def runner(
        _payload: Mapping[str, Any],
        _settings: Any,
        *,
        progress_callback: Any,
        input_downloader: Any,
    ) -> dict[str, Any]:
        del input_downloader
        runner_started.set()
        if lost_via == "renew":
            assert renew_called.wait(timeout=3)
            renew_response_allowed.set()
            assert renew_response_returned.wait(timeout=3)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if progress_callback("", ""):
                runner_stopped.set()
                return {"status": "succeeded"}
            time.sleep(0.01)
        pytest.fail("runner did not observe lost Control authority")

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
        runtime_settings=SimpleNamespace(
            sandbox_config=unit_sandbox_config(), resource_envelope=unit_resource_envelope()
        ),
        runner=runner,
    )
    try:
        assert consumer._slots.acquire(blocking=False)
        consumer._run_attempt(payload)
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)

    assert runner_started.is_set()
    assert runner_stopped.is_set()
    assert client.result_calls == 0
    if lost_via == "renew":
        assert client.renew_calls >= 1
        assert renew_called.is_set()
        assert renew_response_returned.is_set()
        assert client.progress_calls == 0
    else:
        assert client.renew_calls == 0
        assert client.progress_calls == 1


@pytest.mark.parametrize(
    ("control_available", "expected_ack", "expected_pause"),
    [(True, 1, False), (False, 0, True)],
    ids=["prepare-failed-ack", "control-unavailable-pause"],
)
def test_v3_attempt_journal_failure_never_starts_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    control_available: bool,
    expected_ack: int,
    expected_pause: bool,
) -> None:
    """A failed durable hand-off is ACKed only after Control records it."""

    class PrepareClient:
        def __init__(self) -> None:
            self.prepare_failed_calls = 0

        def prepare_failed_attempt(
            self,
            _worker_id: int,
            _attempt_id: int,
            _body: Mapping[str, Any],
        ) -> dict[str, Any]:
            self.prepare_failed_calls += 1
            if not control_available:
                raise ControlUnavailableError("control partition")
            return {"decision": "ACK_NOOP", "reason": "terminal_recorded"}

    def fail_journal(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("journal disk full")

    monkeypatch.setattr(workspace, "write_attempt_journal", fail_journal)
    client = PrepareClient()
    runner_calls = 0

    def runner(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal runner_calls
        runner_calls += 1
        return {"status": "succeeded"}

    consumer = V3Consumer(
        ConsumerConfig(
            worker_id=7,
            queue="dlr.worker.test.q",
            execution_slots=1,
            runtime_root=tmp_path / "runtime",
            attempt_journal_root=tmp_path / "journal",
        ),
        client,  # type: ignore[arg-type]
        connection_factory=lambda: object(),  # type: ignore[return-value]
        runtime_settings=SimpleNamespace(
            sandbox_config=unit_sandbox_config(), resource_envelope=unit_resource_envelope()
        ),
        runner=runner,
    )
    channel = _NativeDeferChannel()
    try:
        assert consumer._slots.acquire(blocking=False)
        assert (
            consumer._prepare_execute(
                _ImmediateCallbackConnection(),
                channel,
                delivery_tag=41,
                decision={"decision": "EXECUTE", "payload": _valid_consumer_payload()},
            )
            is False
        )
    finally:
        consumer._slots.release()
        consumer._pool.shutdown(wait=True, cancel_futures=True)

    assert client.prepare_failed_calls == 1
    assert runner_calls == 0
    assert channel.acks == expected_ack
    assert channel.nacks == []
    assert consumer._pause.is_set() is expected_pause


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
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "b2-retry-not-due-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-retry-not-due-adapter")
    execution = _execution(api_client, adapter["id"])
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
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "b2-slot-concurrency-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-slot-concurrency-adapter")
    first_execution = _execution(api_client, adapter["id"], input_value={"run": 1})
    second_execution = _execution(api_client, adapter["id"], input_value={"run": 2})
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


def test_claim_and_cancel_share_adapter_first_lock_order(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation waits at Adapter instead of deadlocking with Claim at Execution."""

    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "b2-claim-cancel-lock-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-claim-cancel-lock-adapter")
    execution = _execution(api_client, adapter["id"])
    dispatch = _dispatch(session_factory, execution["id"])

    claim_has_admission_scope = threading.Event()
    release_claim = threading.Event()
    cancel_entered_admission_scope = threading.Event()
    cancel_reached_execution = threading.Event()
    original_scope_lock = attempt_service.admission.lock_admission_scope
    original_execution_lock = execution_cancellation.lock_execution

    def pause_claim_after_scope_lock(session: Session, adapter_id: int) -> Any:
        if threading.current_thread().name == "b2-cancel-lock-order":
            cancel_entered_admission_scope.set()
        scope = original_scope_lock(session, adapter_id)
        if threading.current_thread().name == "b2-claim-lock-order":
            claim_has_admission_scope.set()
            assert release_claim.wait(timeout=10)
        return scope

    def observe_cancel_execution_lock(session: Session, execution_id: int) -> Execution | None:
        if threading.current_thread().name == "b2-cancel-lock-order":
            cancel_reached_execution.set()
        return original_execution_lock(session, execution_id)

    monkeypatch.setattr(
        attempt_service.admission,
        "lock_admission_scope",
        pause_claim_after_scope_lock,
    )
    monkeypatch.setattr(
        execution_cancellation,
        "lock_execution",
        observe_cancel_execution_lock,
    )
    results: dict[str, Any] = {}

    def claim() -> None:
        try:
            results["claim"] = _claim(session_factory, worker["id"], dispatch)
        except BaseException as error:  # pragma: no cover - surfaced below
            results["claim_error"] = error

    def cancel() -> None:
        try:
            with session_factory() as session:
                results["cancel"] = execution_service.cancel_execution(session, execution["id"])
        except BaseException as error:  # pragma: no cover - surfaced below
            results["cancel_error"] = error

    claim_thread = threading.Thread(target=claim, name="b2-claim-lock-order")
    cancel_thread = threading.Thread(target=cancel, name="b2-cancel-lock-order")
    claim_thread.start()
    assert claim_has_admission_scope.wait(timeout=10)
    cancel_thread.start()
    assert cancel_entered_admission_scope.wait(timeout=10)
    assert not cancel_reached_execution.is_set()
    release_claim.set()
    claim_thread.join(timeout=10)
    cancel_thread.join(timeout=10)

    assert not claim_thread.is_alive()
    assert not cancel_thread.is_alive()
    assert "claim_error" not in results, results.get("claim_error")
    assert "cancel_error" not in results, results.get("cancel_error")
    assert results["claim"].decision == "EXECUTE"
    assert results["cancel"].cancel_requested is True


def test_claim_blocked_on_adapter_does_not_lock_execution(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove Adapter is the first blocking row lock without scheduler timing."""

    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "b2-claim-nowait-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-claim-nowait-adapter")
    execution = _execution(api_client, adapter["id"])
    dispatch = _dispatch(session_factory, execution["id"])
    engine = session_factory.kw["bind"]
    adapter_lock_attempted = threading.Event()

    def observe_adapter_lock(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            threading.current_thread().name == "b2-claim-nowait"
            and "FROM adapters" in statement
            and "FOR UPDATE" in statement
        ):
            adapter_lock_attempted.set()

    event.listen(engine, "before_cursor_execute", observe_adapter_lock)
    result: dict[str, Any] = {}

    def claim() -> None:
        try:
            result["claim"] = _claim(session_factory, worker["id"], dispatch)
        except BaseException as error:  # pragma: no cover - surfaced below
            result["error"] = error

    thread = threading.Thread(target=claim, name="b2-claim-nowait")
    try:
        with session_factory() as blocker:
            assert (
                blocker.scalar(select(Adapter).where(Adapter.id == adapter["id"]).with_for_update())
                is not None
            )
            thread.start()
            assert adapter_lock_attempted.wait(timeout=10)
            with session_factory() as probe:
                assert (
                    probe.scalar(
                        select(Execution)
                        .where(Execution.id == execution["id"])
                        .with_for_update(nowait=True)
                    )
                    is not None
                )
            blocker.rollback()
        thread.join(timeout=10)
    finally:
        event.remove(engine, "before_cursor_execute", observe_adapter_lock)
        if thread.is_alive():
            thread.join(timeout=10)

    assert not thread.is_alive()
    assert "error" not in result, result.get("error")
    assert result["claim"].decision == "EXECUTE"


def test_retry_dispatch_and_cancel_share_adapter_first_lock_order(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry dispatch holds Adapter before Execution, matching cancellation."""

    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "b2-retry-cancel-lock-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-retry-cancel-lock-adapter")
    execution = _execution(api_client, adapter["id"])
    due_at = datetime.now(UTC) - timedelta(seconds=1)
    with session_factory.begin() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        row.status = "retry_wait"
        row.next_attempt_at = due_at

    retry_has_adapter = threading.Event()
    release_retry = threading.Event()
    cancel_entered_admission_scope = threading.Event()
    cancel_reached_execution = threading.Event()
    original_scope_lock = attempt_service.admission.lock_admission_scope
    original_execution_lock = execution_cancellation.lock_execution

    def observe_cancel_scope_lock(session: Session, adapter_id: int) -> Any:
        if threading.current_thread().name == "b2-retry-cancel":
            cancel_entered_admission_scope.set()
        return original_scope_lock(session, adapter_id)

    def observe_cancel_execution_lock(session: Session, execution_id: int) -> Execution | None:
        if threading.current_thread().name == "b2-retry-cancel":
            cancel_reached_execution.set()
        return original_execution_lock(session, execution_id)

    monkeypatch.setattr(
        attempt_service.admission,
        "lock_admission_scope",
        observe_cancel_scope_lock,
    )
    monkeypatch.setattr(
        execution_cancellation,
        "lock_execution",
        observe_cancel_execution_lock,
    )
    results: dict[str, Any] = {}

    def retry() -> None:
        try:
            with session_factory() as session:
                original_scalar = session.scalar

                def pause_after_adapter_lock(statement: Any, *args: Any, **kwargs: Any) -> Any:
                    value = original_scalar(statement, *args, **kwargs)
                    sql = str(statement)
                    if "FROM adapters" in sql and "FOR UPDATE" in sql:
                        retry_has_adapter.set()
                        assert release_retry.wait(timeout=10)
                    return value

                session.scalar = pause_after_adapter_lock  # type: ignore[method-assign]
                results["retry"] = attempt_service.retry_dispatcher_once(
                    session,
                    now=datetime.now(UTC),
                )
        except BaseException as error:  # pragma: no cover - surfaced below
            results["retry_error"] = error

    def cancel() -> None:
        try:
            with session_factory() as session:
                results["cancel"] = execution_service.cancel_execution(session, execution["id"])
        except BaseException as error:  # pragma: no cover - surfaced below
            results["cancel_error"] = error

    retry_thread = threading.Thread(target=retry, name="b2-retry-lock-order")
    cancel_thread = threading.Thread(target=cancel, name="b2-retry-cancel")
    retry_thread.start()
    assert retry_has_adapter.wait(timeout=10)
    cancel_thread.start()
    assert cancel_entered_admission_scope.wait(timeout=10)
    assert not cancel_reached_execution.is_set()
    release_retry.set()
    retry_thread.join(timeout=10)
    cancel_thread.join(timeout=10)

    assert not retry_thread.is_alive()
    assert not cancel_thread.is_alive()
    assert "retry_error" not in results, results.get("retry_error")
    assert "cancel_error" not in results, results.get("cancel_error")
    assert results["retry"] == 1
    assert results["cancel"].status == "cancelled"


def test_retry_blocked_on_adapter_does_not_lock_execution(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove Retry also waits at Adapter before touching Execution."""

    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "b2-retry-nowait-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-retry-nowait-adapter")
    execution = _execution(api_client, adapter["id"])
    due_at = datetime.now(UTC) - timedelta(seconds=1)
    with session_factory.begin() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        row.status = "retry_wait"
        row.next_attempt_at = due_at

    engine = session_factory.kw["bind"]
    adapter_lock_attempted = threading.Event()

    def observe_adapter_lock(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            threading.current_thread().name == "b2-retry-nowait"
            and "FROM adapters" in statement
            and "FOR UPDATE" in statement
        ):
            adapter_lock_attempted.set()

    event.listen(engine, "before_cursor_execute", observe_adapter_lock)
    result: dict[str, Any] = {}

    def retry() -> None:
        try:
            with session_factory() as session:
                result["retry"] = attempt_service.retry_dispatcher_once(
                    session,
                    now=datetime.now(UTC),
                )
        except BaseException as error:  # pragma: no cover - surfaced below
            result["error"] = error

    thread = threading.Thread(target=retry, name="b2-retry-nowait")
    try:
        with session_factory() as blocker:
            assert (
                blocker.scalar(select(Adapter).where(Adapter.id == adapter["id"]).with_for_update())
                is not None
            )
            thread.start()
            assert adapter_lock_attempted.wait(timeout=10)
            with session_factory() as probe:
                assert (
                    probe.scalar(
                        select(Execution)
                        .where(Execution.id == execution["id"])
                        .with_for_update(nowait=True)
                    )
                    is not None
                )
            blocker.rollback()
        thread.join(timeout=10)
    finally:
        event.remove(engine, "before_cursor_execute", observe_adapter_lock)
        if thread.is_alive():
            thread.join(timeout=10)

    assert not thread.is_alive()
    assert "error" not in result, result.get("error")
    assert result["retry"] == 0


def test_concurrent_cancel_and_success_result_release_once(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent cancellation and Result converge to one terminal release."""

    _enable_runtime(monkeypatch)
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    worker = _ready_worker(api_client, "b2-terminal-race-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-terminal-race-adapter")
    artifact_id = create_artifact(session_factory, adapter["id"], "b2-race.txt", status="READY")
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
    execution = _execution(api_client, adapter["id"])
    dispatch = _dispatch(session_factory, execution["id"])
    claimed = _claim(session_factory, worker["id"], dispatch)
    assert claimed.payload is not None and claimed.attempt_id is not None

    release_lock = threading.Lock()
    admission_releases = 0
    slot_releases = 0
    execution_lease_releases = 0
    original_admission_release = attempt_service.admission.release_admission_once
    original_slot_release = attempt_service._release_slot_locked
    original_execution_lease_release = execution_service.release_execution_leases

    def count_admission_release(*args: Any, **kwargs: Any) -> bool:
        nonlocal admission_releases
        with release_lock:
            admission_releases += 1
        return original_admission_release(*args, **kwargs)

    def count_slot_release(*args: Any, **kwargs: Any) -> bool:
        nonlocal slot_releases
        with release_lock:
            slot_releases += 1
        return original_slot_release(*args, **kwargs)

    def count_execution_lease_release(*args: Any, **kwargs: Any) -> None:
        nonlocal execution_lease_releases
        with release_lock:
            execution_lease_releases += 1
        original_execution_lease_release(*args, **kwargs)

    monkeypatch.setattr(
        attempt_service.admission, "release_admission_once", count_admission_release
    )
    monkeypatch.setattr(attempt_service, "_release_slot_locked", count_slot_release)
    monkeypatch.setattr(
        execution_service,
        "release_execution_leases",
        count_execution_lease_release,
    )

    race_gate = threading.Barrier(2)

    def cancel() -> str:
        race_gate.wait(timeout=10)
        with session_factory() as session:
            return execution_service.cancel_execution(session, execution["id"]).status

    def succeed() -> str:
        race_gate.wait(timeout=10)
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
                        "status": "succeeded",
                    }
                ),
            )
            return result.reason

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancel_future = pool.submit(cancel)
        result_future = pool.submit(succeed)
        cancel_status = cancel_future.result(timeout=15)
        result_reason = result_future.result(timeout=15)

    assert cancel_status in {"running", "succeeded"}
    assert result_reason in {"terminal_recorded", "already_terminal"}
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        attempts = list(
            session.scalars(
                select(ExecutionAttempt)
                .where(ExecutionAttempt.execution_id == execution["id"])
                .order_by(ExecutionAttempt.attempt_no)
            )
        )
        slot = session.scalar(
            select(AdapterExecutionSlot).where(
                AdapterExecutionSlot.adapter_id == adapter["id"],
                AdapterExecutionSlot.slot_no == 0,
            )
        )
        adapter_counter = session.get(AdapterExecutionAdmission, adapter["id"])
        global_counter = session.get(GlobalExecutionAdmission, "global")
        lease = session.scalar(
            select(ExecutionInputArtifactLease).where(
                ExecutionInputArtifactLease.execution_id == execution["id"]
            )
        )
        assert row is not None and slot is not None
        assert row.status in {"cancelled", "succeeded"}
        assert row.admission_released_at is not None
        assert len(attempts) == 1
        assert attempts[0].status in {
            "cancelled",
            "succeeded",
        }
        assert slot.active_attempt_id is None
        assert slot.lease_expires_at is None
        assert adapter_counter is not None and adapter_counter.outstanding_count == 0
        assert global_counter is not None and global_counter.outstanding_count == 0
        assert lease is None

    assert admission_releases == 1
    assert slot_releases == 1
    assert execution_lease_releases == 1


@pytest.mark.parametrize("edge", ["lower", "upper"])
def test_retry_jitter_persists_deterministic_db_bounds(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    edge: str,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"b2-jitter-{edge}-worker")
    adapter = _rabbit_adapter(api_client, worker, f"b2-jitter-{edge}-adapter")
    execution = _execution(api_client, adapter["id"])
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
    _enable_runtime(monkeypatch)
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

        busy_a = _execution(api_client, adapter_a["id"], input_value={"run": "busy"})
        delayed_a = _execution(api_client, adapter_a["id"], input_value={"run": "delayed"})
        execution_b = _execution(api_client, adapter_b["id"], input_value={"run": "progress"})
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
    tmp_path: Path,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "b2-failure-matrix-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-failure-matrix-adapter")
    execution = _execution(api_client, adapter["id"])
    dispatch = _dispatch(session_factory, execution["id"])

    first = _claim(session_factory, worker["id"], dispatch)
    assert first.decision == "EXECUTE"
    assert first.payload is not None
    assert first.attempt_id is not None
    payload = first.payload
    workspace.write_attempt_journal(
        tmp_path / "attempt-journal",
        execution_id=payload.execution_id,
        attempt_id=payload.attempt_id,
        attempt_no=payload.attempt_no,
        fencing_token=payload.fencing_token,
        lease_expires_at=payload.lease_expires_at.isoformat(),
        workspace=workspace.workspace_path(tmp_path / "runtime", payload.execution_id),
        claim_token=payload.claim_token,
        cleanup_token=payload.cleanup_token,
    )

    duplicate = _claim(session_factory, worker["id"], dispatch)
    assert duplicate.decision == "ACK_NOOP"
    assert duplicate.reason == "execution_not_queued"
    with session_factory() as session:
        attempts = list(
            session.scalars(
                select(ExecutionAttempt).where(ExecutionAttempt.execution_id == execution["id"])
            )
        )
        assert len(attempts) == 1
        assert [item for item in attempts if item.status in {"claimed", "running"}] == attempts
    assert workspace.attempt_journal_path(
        tmp_path / "attempt-journal", payload.attempt_id
    ).is_file()

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
    assert workspace.attempt_journal_path(
        tmp_path / "attempt-journal", payload.attempt_id
    ).is_file()

    redelivered_after_recovery = _claim(session_factory, worker["id"], dispatch)
    assert redelivered_after_recovery.decision == "ACK_NOOP"
    assert redelivered_after_recovery.reason == "retry_not_due"

    with session_factory() as session:
        assert (
            attempt_service.retry_dispatcher_once(
                session,
                now=datetime.now(UTC) + timedelta(days=1),
            )
            == 1
        )
    with session_factory() as session:
        assert (
            attempt_service.retry_dispatcher_once(
                session,
                now=datetime.now(UTC) + timedelta(days=1),
            )
            == 0
        )
        next_outbox = list(
            session.scalars(
                select(ExecutionOutbox)
                .where(ExecutionOutbox.execution_id == execution["id"])
                .where(ExecutionOutbox.dispatch_generation == 2)
            )
        )
        assert len(next_outbox) == 1

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
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "b2-fence-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-fence-adapter")
    execution = _execution(api_client, adapter["id"])
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
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "b2-dead-letter-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-dead-letter-adapter")
    execution = _execution(api_client, adapter["id"], input_value={"case": "business"})
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


def test_replay_waits_for_adapter_before_locking_execution(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked Replay must not hold Execution and form an AB-BA cycle."""

    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "b2-replay-lock-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-replay-lock-adapter")
    execution = _execution(api_client, adapter["id"])
    ended_at = datetime.now(UTC)
    with session_factory.begin() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        row.status = "dead_letter"
        row.ended_at = ended_at
        attempt_service.admission.release_admission_once(session, row, now=ended_at)

    engine = session_factory.kw["bind"]
    adapter_lock_attempted = threading.Event()

    def observe_adapter_lock(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            threading.current_thread().name == "b2-replay-lock-order"
            and "FROM adapters" in statement
            and "FOR UPDATE" in statement
        ):
            adapter_lock_attempted.set()

    event.listen(engine, "before_cursor_execute", observe_adapter_lock)
    result: dict[str, Any] = {}

    def replay() -> None:
        try:
            with session_factory() as session:
                result["replay"] = attempt_service.replay_execution(session, execution["id"])
        except BaseException as error:  # pragma: no cover - surfaced below
            result["error"] = error

    replay_thread = threading.Thread(target=replay, name="b2-replay-lock-order")
    try:
        with session_factory() as blocker:
            locked_adapter = blocker.scalar(
                select(Adapter).where(Adapter.id == adapter["id"]).with_for_update()
            )
            assert locked_adapter is not None
            replay_thread.start()
            assert adapter_lock_attempted.wait(timeout=10)

            # NOWAIT succeeds only if Replay has not taken the Execution lock
            # while it is blocked behind the canonical Adapter-first lock.
            with session_factory() as probe:
                locked_execution = probe.scalar(
                    select(Execution)
                    .where(Execution.id == execution["id"])
                    .with_for_update(nowait=True)
                )
                assert locked_execution is not None
            blocker.rollback()
        replay_thread.join(timeout=10)
    finally:
        event.remove(engine, "before_cursor_execute", observe_adapter_lock)
        if replay_thread.is_alive():
            replay_thread.join(timeout=10)

    assert not replay_thread.is_alive()
    assert "error" not in result, result.get("error")
    assert result["replay"].replay_of_execution_id == execution["id"]


def test_infrastructure_dlq_mismatch_is_visible_manual_review_not_business_dead_letter(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "b2-infra-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-infra-adapter")
    execution = _execution(api_client, adapter["id"])
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


@pytest.mark.parametrize(
    ("module", "loop_name", "tick_name"),
    [
        (attempt_service, "attempt_reconciler_loop", "_attempt_reconcile_tick"),
        (infrastructure_dlq, "infrastructure_dlq_loop", "_infrastructure_dlq_tick"),
    ],
)
def test_blocking_reconcilers_delegate_each_tick_to_a_thread(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    loop_name: str,
    tick_name: str,
) -> None:
    delegated: list[Any] = []

    async def fake_to_thread(function: Any, *args: Any, **kwargs: Any) -> None:
        delegated.append((function, args, kwargs))
        raise asyncio.CancelledError

    monkeypatch.setattr(module, "_asyncio_to_thread", fake_to_thread)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(getattr(module, loop_name)())

    assert delegated == [(getattr(module, tick_name), (), {})]


@pytest.mark.parametrize("language", ["python", "javascript", "java"])
def test_three_language_claim_and_terminal_cleanup(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    language: str,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"b2-canary-{language}", [language])
    adapter = _rabbit_adapter(
        api_client, worker, f"b2-canary-adapter-{language}", language=language
    )
    execution = _execution(api_client, adapter["id"], input_value={"language": language})
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
    _enable_runtime(monkeypatch)
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
    execution = _execution(api_client, adapter["id"])
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


_REAL_LANGUAGE_ADAPTERS = {
    "python": (
        "def handle(context, input):\n"
        "    return {'language': 'python', 'input': input, "
        "'input_files': len(context.input_files)}\n"
    ),
    "javascript": (
        "export async function handle(context, input) {\n"
        "  return {language: 'javascript', input, input_files: context.inputFiles.length};\n"
        "}\n"
    ),
    "java": (
        "import java.util.LinkedHashMap;\n"
        "import java.util.Map;\n"
        "public class Adapter {\n"
        "  public Object handle(Context context, Object input) {\n"
        "    Map<String, Object> result = new LinkedHashMap<>();\n"
        '    result.put("language", "java");\n'
        '    result.put("input", input);\n'
        '    result.put("input_files", context.inputFiles.size());\n'
        "    return result;\n"
        "  }\n"
        "}\n"
    ),
}


@pytest.mark.parametrize(
    ("language", "input_value"),
    [
        ("python", None),
        ("javascript", {"source": "json", "language": "javascript"}),
        ("java", {"source": "json", "language": "java"}),
    ],
    ids=["python-none", "javascript-json", "java-json"],
)
def test_v3_consumer_executes_real_language_adapter_and_records_terminal_result(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    language: str,
    input_value: Any,
) -> None:
    """The v3 Consumer invokes each real local language runtime end to end."""

    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"b2-part2-{language}-worker", [language])
    adapter = _rabbit_adapter(
        api_client,
        worker,
        f"b2-part2-{language}-adapter",
        language=language,
        code=_REAL_LANGUAGE_ADAPTERS[language],
    )
    execution = _execution(api_client, adapter["id"], input_value=input_value)
    dispatch = _dispatch(session_factory, execution["id"])
    client = _V3ServiceClient(api_client, session_factory)
    runtime_root = tmp_path / "runtime"
    cleanup_root = tmp_path / "cleanup-journal"
    consumer = V3Consumer(
        ConsumerConfig(
            worker_id=worker["id"],
            queue=f"dlr.worker.{worker['id']}.q",
            execution_slots=1,
            runtime_root=runtime_root,
            attempt_journal_root=tmp_path / "attempt-journal",
        ),
        client,  # type: ignore[arg-type]
        connection_factory=lambda: object(),  # type: ignore[return-value]
        runtime_settings=executor.RuntimeSettings(
            sandbox_config=unit_sandbox_config(),
            resource_envelope=unit_resource_envelope(),
            runtime_root=runtime_root,
            execution_timeout_seconds=30,
            dep_install_timeout_seconds=120,
            workspace_cleanup_journal_root=cleanup_root,
        ),
    )
    channel = _NativeDeferChannel()
    try:
        consumer._on_delivery(
            _ImmediateCallbackConnection(),
            channel,
            SimpleNamespace(delivery_tag=1),
            None,
            json.dumps(dispatch).encode("utf-8"),
        )
        assert client.result_event.wait(timeout=120), f"{language} Result was not reported"
        consumer._pool.shutdown(wait=True, cancel_futures=True)
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)

    assert client.claimed_payload is not None
    assert client.claimed_payload["protocol_version"] == 3
    assert client.claimed_payload["claim_token"]
    assert client.claimed_payload["cleanup_token"]
    assert client.claimed_payload["input_source_type"] == (
        "none" if input_value is None else "json"
    )
    assert len(client.result_bodies) == 1
    result_body = client.result_bodies[0]
    assert result_body["status"] == "succeeded"
    assert result_body["workspace_cleanup_status"] == "completed"
    assert result_body["output"] == {
        "language": language,
        "input": input_value,
        "input_files": 0,
    }
    assert len(client.cleanup_receipt_calls) == 1
    assert channel.acks == 1
    assert channel.nacks == []
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        attempt = session.scalar(
            select(ExecutionAttempt).where(ExecutionAttempt.execution_id == execution["id"])
        )
        assert row is not None and row.status == "succeeded"
        assert row.workspace_cleanup_status == "completed"
        assert attempt is not None and attempt.status == "succeeded"
    assert not workspace.journal_path(
        cleanup_root,
        execution["id"],
        attempt_id=client.claimed_payload["attempt_id"],
    ).exists()


def test_v3_consumer_managed_files_download_manifest_and_restart_cleanup_recovery(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """v3 proves Claim auth, Control streaming, materialization and recovery."""

    _enable_runtime(monkeypatch)
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    store_root = tmp_path / "artifact-store"
    monkeypatch.setattr(settings, "artifact_store_root", str(store_root))
    content = b"managed-v3"
    store = LocalFileArtifactStore(store_root)
    storage_key = store.new_storage_key()
    with store.put_part(storage_key) as part:
        part.write(content)
    store.commit(storage_key)

    worker = _ready_worker(api_client, "b2-part2-managed-worker", ["python"])
    adapter = _rabbit_adapter(
        api_client,
        worker,
        "b2-part2-managed-adapter",
        code=(
            "def handle(context, input):\n"
            "    item = context.input_files[0]\n"
            "    return {'source': input, 'filename': item.original_name, "
            "'content': item.path.read_text(encoding='utf-8'), "
            "'sha256': item.sha256, 'input_files': len(context.input_files)}\n"
        ),
    )
    artifact_id = create_artifact(session_factory, adapter["id"], "managed-v3.txt", status="READY")
    with session_factory.begin() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None
        artifact.storage_key = storage_key
        artifact.size_bytes = len(content)
        artifact.sha256 = hashlib.sha256(content).hexdigest()
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
    execution = _execution(api_client, adapter["id"])
    dispatch = _dispatch(session_factory, execution["id"])
    client = _V3ServiceClient(
        api_client,
        session_factory,
        fail_first_cleanup_receipt=True,
    )
    runtime_root = tmp_path / "runtime"
    cleanup_root = tmp_path / "cleanup-journal"
    consumer = V3Consumer(
        ConsumerConfig(
            worker_id=worker["id"],
            queue=f"dlr.worker.{worker['id']}.q",
            execution_slots=1,
            runtime_root=runtime_root,
            attempt_journal_root=tmp_path / "attempt-journal",
        ),
        client,  # type: ignore[arg-type]
        connection_factory=lambda: object(),  # type: ignore[return-value]
        runtime_settings=executor.RuntimeSettings(
            sandbox_config=unit_sandbox_config(),
            resource_envelope=unit_resource_envelope(),
            runtime_root=runtime_root,
            execution_timeout_seconds=30,
            dep_install_timeout_seconds=120,
            workspace_cleanup_journal_root=cleanup_root,
        ),
    )
    channel = _NativeDeferChannel()
    try:
        consumer._on_delivery(
            _ImmediateCallbackConnection(),
            channel,
            SimpleNamespace(delivery_tag=1),
            None,
            json.dumps(dispatch).encode("utf-8"),
        )
        assert client.result_event.wait(timeout=120), "managed-file Result was not reported"
        assert client.cleanup_receipt_attempted.wait(timeout=30)
        consumer._pool.shutdown(wait=True, cancel_futures=True)
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)

    assert client.claimed_payload is not None
    claimed_payload = client.claimed_payload
    assert claimed_payload["input_source_type"] == "managed_files"
    assert len(claimed_payload["input_files"]) == 1
    assert claimed_payload["input_files"][0]["id"] == artifact_id
    assert claimed_payload["claim_token"]
    assert client.download_claim_tokens == [claimed_payload["claim_token"]]
    assert len(client.result_bodies) == 1
    assert client.result_bodies[0]["status"] == "succeeded"
    assert client.result_bodies[0]["workspace_cleanup_status"] == "completed"
    assert len(client.cleanup_receipt_calls) == 1
    cleanup_journal = workspace.journal_path(
        cleanup_root,
        execution["id"],
        attempt_id=claimed_payload["attempt_id"],
    )
    assert cleanup_journal.exists(), "cleanup journal must survive the lost receipt"
    assert not workspace.workspace_path(
        runtime_root,
        execution["id"],
        attempt_id=claimed_payload["attempt_id"],
    ).exists()

    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        assert row.status == "succeeded"
        assert row.output == {
            "source": None,
            "filename": "managed-v3.txt",
            "content": content.decode(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "input_files": 1,
        }
        assert row.workspace_cleanup_status == "completed"

    recovered = workspace.recover_cleanup_journals(
        cleanup_root,
        runtime_root,
        report_cleanup=lambda execution_id, cleanup_token: bool(
            client.report_cleanup_receipt(worker["id"], execution_id, cleanup_token=cleanup_token)
        ),
        scan_timeout_seconds=5,
        retry_backoff_seconds=0,
    )
    assert recovered == {"inspected": 1, "completed": 1, "deferred": 0, "retained": 0}
    assert len(client.cleanup_receipt_calls) == 2
    assert not cleanup_journal.exists()
    assert not workspace.workspace_path(
        runtime_root,
        execution["id"],
        attempt_id=claimed_payload["attempt_id"],
    ).exists()


def test_cleanup_journals_are_attempt_scoped(
    tmp_path: Path,
) -> None:
    """A deferred v3 Attempt can recover beside another Attempt's journal."""

    runtime_root = tmp_path / "runtime"
    journal_root = tmp_path / "cleanup-journal"
    planned_first = workspace.workspace_path(runtime_root, 17, attempt_id=101)
    planned_second = workspace.workspace_path(runtime_root, 17, attempt_id=102)
    first_journal = workspace.write_cleanup_journal(
        journal_root,
        17,
        planned_first,
        "cleanup-token-first",
        protocol_version=3,
        attempt_id=101,
    )
    second_journal = workspace.write_cleanup_journal(
        journal_root,
        17,
        planned_second,
        "cleanup-token-second",
        protocol_version=3,
        attempt_id=102,
    )
    first_layout = workspace.create_workspace(runtime_root, 17, attempt_id=101)
    second_layout = workspace.create_workspace(runtime_root, 17, attempt_id=102)
    workspace.prepare_input_files(first_layout, [], None)
    workspace.prepare_input_files(second_layout, [], None)

    assert first_journal != second_journal
    assert first_journal.is_file() and second_journal.is_file()
    assert json.loads(first_journal.read_text(encoding="utf-8"))["attempt_id"] == 101
    assert json.loads(second_journal.read_text(encoding="utf-8"))["attempt_id"] == 102
    receipts: list[tuple[int, str]] = []
    recovered = workspace.recover_cleanup_journals(
        journal_root,
        runtime_root,
        report_cleanup=lambda execution_id, token: receipts.append((execution_id, token)) or True,
        retry_backoff_seconds=0,
    )

    assert recovered == {"inspected": 2, "completed": 2, "deferred": 0, "retained": 0}
    assert receipts == [(17, "cleanup-token-first"), (17, "cleanup-token-second")]
    assert not first_journal.exists() and not second_journal.exists()
    assert not first_layout.root.exists() and not second_layout.root.exists()


def test_v3_retry_keeps_old_cleanup_receipt_from_completing_next_attempt(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A lost receipt leaves the old journal recoverable without blocking Claim 2."""

    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "b2-cleanup-retry-worker")
    adapter = _rabbit_adapter(api_client, worker, "b2-cleanup-retry-adapter")
    execution = _execution(api_client, adapter["id"])
    first_dispatch = _dispatch(session_factory, execution["id"])
    first = _claim(session_factory, worker["id"], first_dispatch)
    assert first.decision == "EXECUTE"
    assert first.payload is not None and first.attempt_id is not None
    first_payload = first.payload
    runtime_root = tmp_path / "runtime"
    cleanup_root = tmp_path / "cleanup-journal"
    first_journal = workspace.write_cleanup_journal(
        cleanup_root,
        execution["id"],
        workspace.workspace_path(
            runtime_root, execution["id"], attempt_id=first_payload.attempt_id
        ),
        first_payload.cleanup_token,
        protocol_version=3,
        attempt_id=first_payload.attempt_id,
    )

    with session_factory.begin() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        policy = reliable_execution.default_retry_policy()
        policy["max_attempts"] = 3
        row.retry_policy_snapshot = policy
        row.max_attempts_snapshot = 3
    with session_factory() as session:
        terminal = attempt_service.finish_attempt(
            session,
            worker["id"],
            first.attempt_id,
            AttemptResultBody.model_validate(
                {
                    "attempt_id": first.attempt_id,
                    "fencing_token": first_payload.fencing_token,
                    "claim_token": first_payload.claim_token,
                    "status": "failed",
                    "error_code": "temporary_adapter_failure",
                    "error_class": "platform_transient",
                    "workspace_cleanup_status": "completed",
                }
            ),
        )
        assert terminal.decision == "ACK_NOOP"

    client = _V3ServiceClient(
        api_client,
        session_factory,
        fail_first_cleanup_receipt=True,
    )
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None and row.status == "retry_wait"
        assert row.workspace_cleanup_status == "completed"
    with pytest.raises(ControlUnavailableError):
        client.report_cleanup_receipt(
            worker["id"], execution["id"], cleanup_token=first_payload.cleanup_token
        )
    assert first_journal.exists(), "transport loss must retain the old recovery journal"

    with session_factory() as session:
        assert (
            attempt_service.retry_dispatcher_once(
                session,
                now=datetime.now(UTC) + timedelta(days=1),
            )
            == 1
        )
    second_dispatch = _dispatch_generation(session_factory, execution["id"], 2)
    second = _claim(session_factory, worker["id"], second_dispatch)
    assert second.decision == "EXECUTE"
    assert second.payload is not None and second.attempt_id is not None
    second_payload = second.payload
    assert second_payload.attempt_id != first_payload.attempt_id
    assert second_payload.cleanup_token != first_payload.cleanup_token
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        assert row.status == "running"
        assert row.workspace_cleanup_status == "pending"
        assert row.cleanup_receipt_token_hash is None

    second_journal = workspace.write_cleanup_journal(
        cleanup_root,
        execution["id"],
        workspace.workspace_path(
            runtime_root, execution["id"], attempt_id=second_payload.attempt_id
        ),
        second_payload.cleanup_token,
        protocol_version=3,
        attempt_id=second_payload.attempt_id,
    )
    assert first_journal != second_journal
    assert first_journal.exists() and second_journal.exists()

    # The old receipt is valid for old local cleanup, but leaves the new
    # Attempt's pending Execution cleanup state untouched.
    accepted_old = client.report_cleanup_receipt(
        worker["id"], execution["id"], cleanup_token=first_payload.cleanup_token
    )
    assert accepted_old["workspace_cleanup_status"] == "pending"
    assert second_journal.exists(), "an old receipt must not remove the new journal"
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        attempts = list(
            session.scalars(
                select(ExecutionAttempt)
                .where(ExecutionAttempt.execution_id == execution["id"])
                .order_by(ExecutionAttempt.attempt_no)
            )
        )
        assert row is not None and row.workspace_cleanup_status == "pending"
        assert len(attempts) == 2
        assert attempts[0].cleanup_summary == {"workspace_cleanup_status": "completed"}
    assert workspace.remove_cleanup_journal(
        cleanup_root, execution["id"], attempt_id=first_payload.attempt_id
    )
    assert not first_journal.exists()

    with session_factory() as session:
        terminal = attempt_service.finish_attempt(
            session,
            worker["id"],
            second.attempt_id,
            AttemptResultBody.model_validate(
                {
                    "attempt_id": second.attempt_id,
                    "fencing_token": second_payload.fencing_token,
                    "claim_token": second_payload.claim_token,
                    "status": "succeeded",
                    "workspace_cleanup_status": "deferred",
                    "workspace_cleanup_error_code": "workspace_cleanup_failed",
                }
            ),
        )
        assert terminal.decision == "ACK_NOOP"
    client.report_cleanup_receipt(
        worker["id"], execution["id"], cleanup_token=first_payload.cleanup_token
    )
    assert second_journal.exists(), "an old token must not complete new Attempt cleanup"
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None and row.status == "succeeded"
        assert row.workspace_cleanup_status == "deferred"
    accepted_new = client.report_cleanup_receipt(
        worker["id"], execution["id"], cleanup_token=second_payload.cleanup_token
    )
    assert accepted_new["workspace_cleanup_status"] == "completed"
    assert workspace.remove_cleanup_journal(
        cleanup_root, execution["id"], attempt_id=second_payload.attempt_id
    )
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None and row.workspace_cleanup_status == "completed"
        attempts = list(
            session.scalars(
                select(ExecutionAttempt)
                .where(ExecutionAttempt.execution_id == execution["id"])
                .order_by(ExecutionAttempt.attempt_no)
            )
        )
        assert attempts[1].cleanup_summary == {"workspace_cleanup_status": "completed"}


@pytest.mark.parametrize("final_status", ["succeeded", "dead_letter", "cancelled"])
def test_v3_final_states_accept_cleanup_receipt_and_converge(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    final_status: str,
) -> None:
    """Every v3 terminal business state has an idempotent cleanup boundary."""

    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"b2-final-cleanup-{final_status}-worker")
    adapter = _rabbit_adapter(api_client, worker, f"b2-final-cleanup-{final_status}-adapter")
    execution = _execution(api_client, adapter["id"])
    if final_status == "dead_letter":
        with session_factory.begin() as session:
            row = session.get(Execution, execution["id"])
            assert row is not None
            policy = reliable_execution.default_retry_policy()
            policy["max_attempts"] = 1
            row.retry_policy_snapshot = policy
            row.max_attempts_snapshot = 1
    dispatch = _dispatch(session_factory, execution["id"])
    claimed = _claim(session_factory, worker["id"], dispatch)
    assert claimed.payload is not None and claimed.attempt_id is not None
    payload = claimed.payload
    cleanup_root = tmp_path / "cleanup-journal"
    journal = workspace.write_cleanup_journal(
        cleanup_root,
        execution["id"],
        workspace.workspace_path(
            tmp_path / "runtime", execution["id"], attempt_id=payload.attempt_id
        ),
        payload.cleanup_token,
        protocol_version=3,
        attempt_id=payload.attempt_id,
    )
    body: dict[str, Any] = {
        "attempt_id": claimed.attempt_id,
        "fencing_token": payload.fencing_token,
        "claim_token": payload.claim_token,
        "status": "failed" if final_status == "dead_letter" else final_status,
        "workspace_cleanup_status": "deferred",
        "workspace_cleanup_error_code": "workspace_cleanup_failed",
    }
    if final_status == "dead_letter":
        body.update({"error_code": "business_failure", "error_class": "business_error"})
    with session_factory() as session:
        terminal = attempt_service.finish_attempt(
            session,
            worker["id"],
            claimed.attempt_id,
            AttemptResultBody.model_validate(body),
        )
        assert terminal.decision == "ACK_NOOP"
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None and row.status == final_status
        assert row.workspace_cleanup_status == "deferred"

    response = api_client.post(
        f"/api/workers/executions/{execution['id']}/workspace-cleanup",
        json={"status": "completed"},
        headers={**WORKER_HEADERS, "X-DLR-Cleanup-Token": payload.cleanup_token},
    )
    assert response.status_code == 200, response.text
    assert response.json()["workspace_cleanup_status"] == "completed"
    assert workspace.remove_cleanup_journal(
        cleanup_root, execution["id"], attempt_id=payload.attempt_id
    )
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        attempt = session.get(ExecutionAttempt, claimed.attempt_id)
        assert row is not None and row.status == final_status
        assert row.workspace_cleanup_status == "completed"
        assert attempt is not None
        assert attempt.cleanup_summary == {"workspace_cleanup_status": "completed"}
    assert not journal.exists()
