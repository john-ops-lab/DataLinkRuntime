"""Single-runtime protocol, schema and concurrent Slot invariants."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import Settings
from dlr.control.models import (
    AdapterExecutionAdmission,
    AdapterExecutionSlot,
    Execution,
    ExecutionAttempt,
    ExecutionOutbox,
    GlobalExecutionAdmission,
    Worker,
)
from dlr.control.schemas.reliable_runtime import AttemptResultBody, ClaimDecision
from dlr.control.schemas.worker import REQUIRED_ISOLATION_CAPABILITIES
from dlr.control.services import attempt as attempt_service
from dlr.control.services import rabbitmq
from test_adapters import create_adapter, save_version

WORKER_HEADERS = {"Authorization": "Bearer test-worker-token"}
ISOLATION_PASS = {name: True for name in REQUIRED_ISOLATION_CAPABILITIES}


def test_unified_runtime_has_no_dispatch_or_cutover_switches() -> None:
    removed = {
        "rabbitmq_execution_enabled",
        "rabbitmq_execution_canary_enabled",
        "min_worker_protocol_version",
        "legacy_execution_claim_enabled",
        "cutover_backup_restore_gate_passed",
        "cutover_sandbox_gate_passed",
        "cutover_slot_gate_passed",
    }
    assert removed.isdisjoint(Settings.model_fields)


@pytest.mark.parametrize("protocol_version", [1, 2, 4])
def test_worker_registration_rejects_other_protocols(
    api_client: TestClient,
    protocol_version: int,
) -> None:
    response = api_client.post(
        "/api/workers/register",
        json={
            "name": "obsolete-worker",
            "capabilities": ["python"],
            "protocol_version": protocol_version,
        },
        headers=WORKER_HEADERS,
    )
    assert response.status_code == 422


def test_private_cgroup_namespace_is_required_for_execution_readiness(
    api_client: TestClient,
) -> None:
    capabilities = dict(ISOLATION_PASS)
    del capabilities["cgroup_namespace_private"]
    response = api_client.post(
        "/api/workers/register",
        json={
            "name": "host-cgroup-worker",
            "capabilities": ["python"],
            "protocol_version": 3,
            "isolation_capabilities": capabilities,
        },
        headers=WORKER_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["rabbitmq_execution_v3"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("dispatch_backend", "legacy"),
        ("dispatch_generation", 0),
        ("status", "pending"),
        ("status", "failed"),
        ("status", "timeout"),
    ],
)
def test_execution_database_rejects_retired_runtime_states(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    field: str,
    value: object,
) -> None:
    worker = _register_worker(api_client, "schema-worker")
    rabbitmq.mark_runtime_ready()
    adapter = _adapter(api_client, worker["id"], "schema-adapter")
    execution = _execution(api_client, adapter["id"], "schema-execution")
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        setattr(row, field, value)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_worker_database_rejects_legacy_protocol(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker = _register_worker(api_client, "schema-protocol-worker")
    with session_factory() as session:
        row = session.get(Worker, worker["id"])
        assert row is not None
        row.protocol_version = 2
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/admin/reliable-runtime/inventory"),
        ("get", "/api/admin/reliable-runtime/cutover/preflight"),
        ("post", "/api/admin/reliable-runtime/migration/dry-run"),
        ("post", "/api/admin/reliable-runtime/migration/legacy-pending"),
        ("post", "/api/admin/reliable-runtime/migration/legacy-running-drain"),
        ("post", "/api/admin/reliable-runtime/cutover/retire-legacy-index"),
        ("post", "/api/adapters/1/executions/canary"),
        ("post", "/api/workers/1/claim"),
    ],
)
def test_retired_rollout_endpoints_are_absent(
    api_client: TestClient,
    method: str,
    path: str,
) -> None:
    assert api_client.request(method, path, json={}).status_code == 404


def test_slots_are_sole_concurrency_authority_on_fresh_schema(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker = _register_worker(api_client, "unified-slot-worker")
    rabbitmq.mark_runtime_ready()
    assert not _index_present(session_factory)
    _exercise_slot_claim_result_and_recovery(api_client, session_factory, worker["id"], "unified")


def _register_worker(
    client: TestClient,
    name: str,
    *,
    protocol_version: int = 3,
) -> dict[str, Any]:
    response = client.post(
        "/api/workers/register",
        json={
            "name": name,
            "capabilities": ["python"],
            "protocol_version": protocol_version,
            "isolation_capabilities": ISOLATION_PASS if protocol_version == 3 else {},
        },
        headers=WORKER_HEADERS,
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def _index_present(session_factory: sessionmaker[Session]) -> bool:
    with session_factory() as session:
        return bool(
            session.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_indexes "
                    "WHERE schemaname = current_schema() "
                    "AND indexname = 'uq_executions_active_adapter')"
                )
            )
        )


def _adapter(client: TestClient, worker_id: int, name: str) -> dict[str, Any]:
    adapter = create_adapter(client, name=name)
    save_version(client, adapter["id"])
    response = client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"runtime_worker_id": worker_id},
    )
    assert response.status_code == 200, response.text
    return adapter


def _execution(client: TestClient, adapter_id: int, marker: str) -> dict[str, Any]:
    response = client.post(
        f"/api/adapters/{adapter_id}/executions",
        json={"input": {"marker": marker}},
    )
    assert response.status_code == 202, response.text
    body = cast(dict[str, Any], response.json())
    assert body["dispatch_backend"] == "rabbitmq"
    assert body["status"] == "queued"
    return body


def _dispatch(
    session_factory: sessionmaker[Session],
    execution_id: int,
) -> dict[str, Any]:
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        row = session.scalar(
            select(ExecutionOutbox).where(
                ExecutionOutbox.execution_id == execution_id,
                ExecutionOutbox.dispatch_generation == execution.dispatch_generation,
            )
        )
        assert row is not None
        return cast(dict[str, Any], dict(row.payload_json))


def _claim(
    session_factory: sessionmaker[Session],
    worker_id: int,
    dispatch: dict[str, Any],
) -> ClaimDecision:
    with session_factory() as session:
        return attempt_service.claim_dispatch(session, worker_id, dispatch)


def _finish(
    session_factory: sessionmaker[Session],
    worker_id: int,
    decision: ClaimDecision,
) -> None:
    assert decision.attempt_id is not None and decision.payload is not None
    with session_factory() as session:
        result = attempt_service.finish_attempt(
            session,
            worker_id,
            decision.attempt_id,
            AttemptResultBody.model_validate(
                {
                    "attempt_id": decision.attempt_id,
                    "fencing_token": decision.payload.fencing_token,
                    "claim_token": decision.payload.claim_token,
                    "status": "succeeded",
                    "workspace_cleanup_status": "completed",
                }
            ),
        )
    assert result.reason == "terminal_recorded"


def _exercise_slot_claim_result_and_recovery(
    client: TestClient,
    session_factory: sessionmaker[Session],
    worker_id: int,
    marker: str,
) -> None:
    same_adapter = _adapter(client, worker_id, f"b3-{marker}-same")
    other_adapter = _adapter(client, worker_id, f"b3-{marker}-other")
    same_first = _execution(client, same_adapter["id"], f"{marker}-same-1")
    same_second = _execution(client, same_adapter["id"], f"{marker}-same-2")
    other = _execution(client, other_adapter["id"], f"{marker}-other")
    dispatches = {
        same_first["id"]: _dispatch(session_factory, same_first["id"]),
        same_second["id"]: _dispatch(session_factory, same_second["id"]),
        other["id"]: _dispatch(session_factory, other["id"]),
    }
    start = threading.Barrier(3)

    def concurrent_claim(execution_id: int) -> tuple[int, ClaimDecision]:
        start.wait(timeout=10)
        return execution_id, _claim(session_factory, worker_id, dispatches[execution_id])

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = dict(pool.map(concurrent_claim, dispatches))

    same_decisions = [results[same_first["id"]], results[same_second["id"]]]
    assert sorted(decision.decision for decision in same_decisions) == ["DEFER", "EXECUTE"]
    assert (
        next(decision for decision in same_decisions if decision.decision == "DEFER").reason
        == "adapter_slot_busy"
    )
    assert results[other["id"]].decision == "EXECUTE"

    same_active = next(decision for decision in same_decisions if decision.decision == "EXECUTE")
    same_deferred_id = next(
        execution_id
        for execution_id in (same_first["id"], same_second["id"])
        if results[execution_id].decision == "DEFER"
    )

    recovered = results[other["id"]]
    assert recovered.attempt_id is not None
    expired_at = datetime.now(UTC) - timedelta(minutes=1)
    with session_factory.begin() as session:
        attempt = session.get(ExecutionAttempt, recovered.attempt_id)
        slot = session.get(AdapterExecutionSlot, (other_adapter["id"], 0))
        assert attempt is not None and slot is not None
        attempt.lease_expires_at = expired_at
        slot.lease_expires_at = expired_at
    with session_factory() as session:
        assert attempt_service.recover_expired_attempts(session, limit=10) == 1
    with session_factory() as session:
        assert (
            attempt_service.retry_dispatcher_once(
                session,
                limit=10,
                now=datetime.now(UTC) + timedelta(hours=1),
            )
            == 1
        )
    retried = _claim(session_factory, worker_id, _dispatch(session_factory, other["id"]))
    assert retried.decision == "EXECUTE"
    _finish(session_factory, worker_id, retried)

    _finish(session_factory, worker_id, same_active)
    deferred = _claim(
        session_factory,
        worker_id,
        dispatches[same_deferred_id],
    )
    assert deferred.decision == "EXECUTE"
    _finish(session_factory, worker_id, deferred)

    with session_factory() as session:
        active_attempts = int(
            session.scalar(
                select(func.count(ExecutionAttempt.id)).where(
                    ExecutionAttempt.adapter_id.in_((same_adapter["id"], other_adapter["id"])),
                    ExecutionAttempt.status.in_(attempt_service.ACTIVE_ATTEMPT_STATUSES),
                )
            )
            or 0
        )
        slots = list(
            session.scalars(
                select(AdapterExecutionSlot).where(
                    AdapterExecutionSlot.adapter_id.in_((same_adapter["id"], other_adapter["id"]))
                )
            )
        )
        adapter_admission = list(
            session.scalars(
                select(AdapterExecutionAdmission).where(
                    AdapterExecutionAdmission.adapter_id.in_(
                        (same_adapter["id"], other_adapter["id"])
                    )
                )
            )
        )
        global_admission = session.get(GlobalExecutionAdmission, "global")
        assert active_attempts == 0
        assert slots and all(slot.active_attempt_id is None for slot in slots)
        assert adapter_admission and all(
            row.outstanding_count == 0 and row.outstanding_bytes == 0 for row in adapter_admission
        )
        assert global_admission is not None
        assert global_admission.outstanding_count == 0
        assert global_admission.outstanding_bytes == 0


@pytest.mark.parametrize(
    ("messages_ready", "messages_unacknowledged"),
    ((True, 0), (-1, 0), (0, False), (0, -1), (None, 0), (0, "1")),
)
def test_infrastructure_dlq_observation_rejects_invalid_counters(
    monkeypatch: pytest.MonkeyPatch,
    messages_ready: object,
    messages_unacknowledged: object,
) -> None:
    monkeypatch.setattr(
        rabbitmq,
        "_fetch_queue_details",
        lambda _queue: {
            "messages_ready": messages_ready,
            "messages_unacknowledged": messages_unacknowledged,
        },
    )
    monkeypatch.setattr(rabbitmq, "_assert_queue_policy", lambda *_args: None)
    with pytest.raises(rabbitmq.RabbitMQTopologyError) as error:
        rabbitmq.infrastructure_dlq_observation()
    assert error.value.code == "topology_unavailable"
