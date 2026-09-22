"""Issue #161 stable Control cache guard and reference contracts."""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.control.models import (
    Adapter,
    AdapterExecutionAdmission,
    AdapterSchedule,
    AdapterVersion,
    Execution,
    ExecutionArtifactHold,
    ExecutionAttempt,
    ExecutionInfrastructureIncident,
    ExecutionOutbox,
    Worker,
    WorkerCacheGuard,
    WorkerCleanupRequest,
)
from dlr.control.security import SUPERADMIN_PRINCIPAL
from dlr.control.services import attempt as attempt_service
from dlr.control.services import cache_governance
from dlr.control.services.incident_disposition import dispose_incident
from dlr.control.services.schedule import scheduler_tick
from dlr.worker.client import ControlClient
from test_issue127_b2_binding import create_artifact
from test_issue130_b2_runtime import (
    _dispatch,
    _enable_runtime,
    _execution,
    _rabbit_adapter,
    _ready_worker,
)

WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}


def _fixture(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> tuple[dict[str, object], dict[str, object], int]:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"{name}-worker")
    adapter = _rabbit_adapter(api_client, worker, f"{name}-adapter")
    with session_factory.begin() as session:
        row = session.get(Worker, int(worker["id"]))
        assert row is not None
        row.isolation_capabilities = {
            **row.isolation_capabilities,
            "cache_governance_v1": True,
        }
        version_id = session.scalar(
            select(Adapter.latest_version_id).where(Adapter.id == int(adapter["id"]))
        )
        assert version_id is not None
    return worker, adapter, int(version_id)


def _code(error: HTTPException) -> str | None:
    return error.detail.get("code") if isinstance(error.detail, dict) else None


def _force_guard_active(
    session_factory: sessionmaker[Session], *, worker_id: int, version_id: int
) -> None:
    with session_factory.begin() as session:
        guard = session.get(WorkerCacheGuard, (worker_id, version_id))
        assert guard is not None
        guard.generation += 1
        guard.operation_id = uuid.uuid4()
        guard.phase = "acquired"


def test_guard_first_blocks_future_execution_without_partial_admission(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, adapter, version_id = _fixture(api_client, session_factory, monkeypatch, "guard-first")
    operation_id = uuid.uuid4()
    acquired = api_client.post(
        f"/api/workers/{worker['id']}/cache/guards/acquire",
        json={
            "adapter_id": adapter["id"],
            "version_id": version_id,
            "operation_id": str(operation_id),
        },
        headers=WORKER_HEADERS,
    )
    assert acquired.status_code == 200, acquired.text
    assert acquired.json()["generation"] == 1
    checked = api_client.get(
        f"/api/workers/{worker['id']}/cache/guards/{operation_id}/check",
        headers=WORKER_HEADERS,
    )
    assert checked.status_code == 200
    listed = api_client.get(f"/api/workers/{worker['id']}/cache/guards", headers=WORKER_HEADERS)
    assert [item["operation_id"] for item in listed.json()["items"]] == [str(operation_id)]
    second_operation_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.add(
            WorkerCacheGuard(
                worker_id=int(worker["id"]),
                adapter_id=int(adapter["id"]),
                version_id=version_id + 1,
                generation=1,
                operation_id=second_operation_id,
                phase="acquired",
            )
        )
    first_page = api_client.get(
        f"/api/workers/{worker['id']}/cache/guards?limit=1", headers=WORKER_HEADERS
    )
    assert first_page.status_code == 200
    assert [item["operation_id"] for item in first_page.json()["items"]] == [str(operation_id)]
    assert first_page.json()["next_after_version_id"] == version_id
    second_page = api_client.get(
        f"/api/workers/{worker['id']}/cache/guards?limit=1&after_version_id={version_id}",
        headers=WORKER_HEADERS,
    )
    assert second_page.status_code == 200
    assert [item["operation_id"] for item in second_page.json()["items"]] == [
        str(second_operation_id)
    ]
    assert second_page.json()["next_after_version_id"] is None

    blocked = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "cache_reclamation_in_progress"
    with session_factory() as session:
        assert session.scalar(select(Execution.id)) is None
        admission = session.get(AdapterExecutionAdmission, int(adapter["id"]))
        assert admission is None or admission.outstanding_count == 0
    stale = api_client.post(
        f"/api/workers/{worker['id']}/cache/guards/{operation_id}/result",
        json={"generation": 2, "outcome": "aborted"},
        headers=WORKER_HEADERS,
    )
    assert stale.status_code == 409
    finished = api_client.post(
        f"/api/workers/{worker['id']}/cache/guards/{operation_id}/result",
        json={"generation": 1, "outcome": "aborted"},
        headers=WORKER_HEADERS,
    )
    assert finished.status_code == 200
    assert finished.json()["phase"] == "aborted"
    repeated = api_client.post(
        f"/api/workers/{worker['id']}/cache/guards/{operation_id}/result",
        json={"generation": 1, "outcome": "aborted"},
        headers=WORKER_HEADERS,
    )
    assert repeated.status_code == 200
    assert repeated.json()["phase"] == "aborted"
    next_operation_id = uuid.uuid4()
    next_acquired = api_client.post(
        f"/api/workers/{worker['id']}/cache/guards/acquire",
        json={
            "adapter_id": adapter["id"],
            "version_id": version_id,
            "operation_id": str(next_operation_id),
        },
        headers=WORKER_HEADERS,
    )
    assert next_acquired.status_code == 200
    assert next_acquired.json()["generation"] == 2
    replay_old = api_client.post(
        f"/api/workers/{worker['id']}/cache/guards/{operation_id}/result",
        json={"generation": 1, "outcome": "aborted"},
        headers=WORKER_HEADERS,
    )
    assert replay_old.status_code == 200
    current = api_client.get(
        f"/api/workers/{worker['id']}/cache/guards/{next_operation_id}/check",
        headers=WORKER_HEADERS,
    )
    assert current.status_code == 200
    assert current.json()["phase"] == "acquired"
    conflicting_old = api_client.post(
        f"/api/workers/{worker['id']}/cache/guards/{operation_id}/result",
        json={"generation": 1, "outcome": "completed"},
        headers=WORKER_HEADERS,
    )
    assert conflicting_old.status_code == 409
    for _ in range(2):
        completed = api_client.post(
            f"/api/workers/{worker['id']}/cache/guards/{next_operation_id}/result",
            json={"generation": 2, "outcome": "completed"},
            headers=WORKER_HEADERS,
        )
        assert completed.status_code == 200
        assert completed.json()["phase"] == "completed"


def test_schedule_guard_failure_keeps_due_cursor_and_creates_nothing(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, adapter, version_id = _fixture(api_client, session_factory, monkeypatch, "schedule")
    patched = api_client.patch(f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"})
    assert patched.status_code == 200, patched.text
    configured = api_client.put(
        f"/api/adapters/{adapter['id']}/schedule",
        json={
            "enabled": True,
            "cron": "* * * * *",
            "timezone": "UTC",
            "input": {},
            "misfire_policy": "skip_while_busy",
        },
    )
    assert configured.status_code == 200, configured.text
    due = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)
    with session_factory.begin() as session:
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == int(adapter["id"]))
        )
        assert schedule is not None
        schedule.next_run_at = due
    operation_id = uuid.uuid4()
    acquired = api_client.post(
        f"/api/workers/{worker['id']}/cache/guards/acquire",
        json={
            "adapter_id": adapter["id"],
            "version_id": version_id,
            "operation_id": str(operation_id),
        },
        headers=WORKER_HEADERS,
    )
    assert acquired.status_code == 200, acquired.text
    with session_factory() as session:
        assert scheduler_tick(session, now=due) == 0
    with session_factory() as session:
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == int(adapter["id"]))
        )
        assert schedule is not None
        assert schedule.next_run_at == due
        assert session.scalar(select(Execution.id)) is None


def test_reference_transaction_wins_real_postgres_guard_race(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, adapter, version_id = _fixture(
        api_client, session_factory, monkeypatch, "reference-first"
    )
    locked = threading.Event()
    release = threading.Event()

    def install_reference() -> None:
        with session_factory() as session:
            cache_governance.ensure_reference_allowed(
                session,
                worker_id=int(worker["id"]),
                adapter_id=int(adapter["id"]),
                version_id=version_id,
            )
            session.add(
                WorkerCleanupRequest(
                    worker_id=int(worker["id"]),
                    adapter_id=int(adapter["id"]),
                    status="pending",
                )
            )
            locked.set()
            assert release.wait(5)
            session.commit()

    def acquire() -> str | None:
        with session_factory() as session:
            try:
                cache_governance.acquire_guard(
                    session,
                    worker_id=int(worker["id"]),
                    adapter_id=int(adapter["id"]),
                    version_id=version_id,
                    operation_id=uuid.uuid4(),
                )
            except HTTPException as error:
                session.rollback()
                return _code(error)
        return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(install_reference)
        assert locked.wait(5)
        second = pool.submit(acquire)
        time.sleep(0.2)
        assert not second.done()
        release.set()
        first.result(timeout=5)
        assert second.result(timeout=5) == "cache_reference_active"


def test_reference_query_distinguishes_history_cleanup_and_open_incident(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, adapter, version_id = _fixture(
        api_client, session_factory, monkeypatch, "reference-facts"
    )
    execution = _execution(api_client, int(adapter["id"]))
    with session_factory.begin() as session:
        row = session.get(Execution, int(execution["id"]))
        assert row is not None
        row.status = "succeeded"
        row.worker_id = int(worker["id"])
        row.workspace_cleanup_status = "completed"
        facts = cache_governance.reference_facts(
            session,
            worker_id=int(worker["id"]),
            adapter_id=int(adapter["id"]),
            version_id=version_id,
        )
        assert facts.protected is False

        row.workspace_cleanup_status = "deferred"
        session.flush()
        facts = cache_governance.reference_facts(
            session,
            worker_id=int(worker["id"]),
            adapter_id=int(adapter["id"]),
            version_id=version_id,
        )
        assert "workspace_cleanup_incomplete" in facts.reasons

        row.workspace_cleanup_status = "completed"
        row.status = "dead_letter"
        incident = ExecutionInfrastructureIncident(
            execution_id=row.id,
            dispatch_generation=row.dispatch_generation,
            kind="delivery_limit",
            status="open",
        )
        session.add(incident)
        session.flush()
        artifact_id = create_artifact(
            session_factory, int(adapter["id"]), "replay.txt", status="READY"
        )
        session.add(
            ExecutionArtifactHold(
                execution_id=row.id,
                artifact_id=artifact_id,
                reason="dead_letter_replay",
                expires_at=datetime.now(UTC) + timedelta(days=1),
                held_bytes=8,
            )
        )
        session.flush()
        facts = cache_governance.reference_facts(
            session,
            worker_id=int(worker["id"]),
            adapter_id=int(adapter["id"]),
            version_id=version_id,
        )
        assert "incident_open" in facts.reasons
        assert "recovery_material_active" in facts.reasons


def test_missing_attempt_history_is_unknown_but_never_claimed_terminal_is_clean(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, adapter, version_id = _fixture(
        api_client, session_factory, monkeypatch, "missing-attempt-history"
    )
    execution = _execution(api_client, int(adapter["id"]))
    with session_factory.begin() as session:
        row = session.get(Execution, int(execution["id"]))
        assert row is not None
        row.status = "succeeded"
        row.worker_id = None
        row.workspace_cleanup_status = "completed"
        row.attempt_count = 1
        session.flush()
        facts = cache_governance.reference_facts(
            session,
            worker_id=int(worker["id"]),
            adapter_id=int(adapter["id"]),
            version_id=version_id,
        )
        assert "attempt_history_unknown" in facts.reasons
        assert "attempt_identity_unknown" in facts.reasons

        row.attempt_count = 0
        session.flush()
        facts = cache_governance.reference_facts(
            session,
            worker_id=int(worker["id"]),
            adapter_id=int(adapter["id"]),
            version_id=version_id,
        )
        assert facts.protected is False


def test_clean_history_is_filtered_before_reference_bound_but_late_active_is_seen(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, adapter, version_id = _fixture(
        api_client, session_factory, monkeypatch, "bounded-reference"
    )
    executions = [_execution(api_client, int(adapter["id"])) for _ in range(3)]
    with session_factory.begin() as session:
        for execution in executions:
            row = session.get(Execution, int(execution["id"]))
            assert row is not None
            row.status = "succeeded"
            row.workspace_cleanup_status = "completed"
        monkeypatch.setattr(cache_governance, "MAX_REFERENCE_RECORDS", 1)
        session.flush()
        facts = cache_governance.reference_facts(
            session,
            worker_id=int(worker["id"]),
            adapter_id=int(adapter["id"]),
            version_id=version_id,
        )
        assert facts.protected is False

        late = session.get(Execution, int(executions[-1]["id"]))
        assert late is not None
        late.status = "retry_wait"
        session.flush()
        facts = cache_governance.reference_facts(
            session,
            worker_id=int(worker["id"]),
            adapter_id=int(adapter["id"]),
            version_id=version_id,
        )
        assert facts.reasons == ("execution_retry_wait",)


def test_target_snapshot_and_current_worker_both_remain_responsible_when_offline(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, adapter, version_id = _fixture(
        api_client, session_factory, monkeypatch, "worker-migration"
    )
    second = _ready_worker(api_client, "worker-migration-new-worker")
    execution = _execution(api_client, int(adapter["id"]))
    with session_factory.begin() as session:
        old_worker = session.get(Worker, int(first["id"]))
        row = session.get(Execution, int(execution["id"]))
        assert old_worker is not None and row is not None
        old_worker.status = "offline"
        row.target_worker_id = int(second["id"])
        for worker_id in (int(first["id"]), int(second["id"])):
            facts = cache_governance.reference_facts(
                session,
                worker_id=worker_id,
                adapter_id=int(adapter["id"]),
                version_id=version_id,
            )
            assert "execution_queued" in facts.reasons


def test_terminal_attempt_deferred_cleanup_protects_but_completed_history_does_not(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, adapter, version_id = _fixture(
        api_client, session_factory, monkeypatch, "attempt-cleanup"
    )
    execution = _execution(api_client, int(adapter["id"]))
    claim = api_client.post(
        f"/api/workers/{worker['id']}/v3/claim",
        json=_dispatch(session_factory, int(execution["id"])),
        headers=WORKER_HEADERS,
    )
    assert claim.status_code == 200, claim.text
    attempt_id = int(claim.json()["attempt_id"])
    with session_factory.begin() as session:
        row = session.get(Execution, int(execution["id"]))
        attempt = session.get(ExecutionAttempt, attempt_id)
        assert row is not None and attempt is not None
        row.status = "succeeded"
        row.workspace_cleanup_status = "completed"
        attempt.status = "failed"
        attempt.ended_at = datetime.now(UTC)
        attempt.cleanup_summary = {"workspace_cleanup_status": "deferred"}
        facts = cache_governance.reference_facts(
            session,
            worker_id=int(worker["id"]),
            adapter_id=int(adapter["id"]),
            version_id=version_id,
        )
        assert "attempt_cleanup_incomplete" in facts.reasons
        attempt.cleanup_summary = {"workspace_cleanup_status": "completed"}
        session.flush()
        facts = cache_governance.reference_facts(
            session,
            worker_id=int(worker["id"]),
            adapter_id=int(adapter["id"]),
            version_id=version_id,
        )
        assert facts.protected is False


def test_claim_returns_retryable_pause_while_guard_is_active(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, adapter, version_id = _fixture(api_client, session_factory, monkeypatch, "claim")
    execution = _execution(api_client, int(adapter["id"]))
    dispatch = _dispatch(session_factory, int(execution["id"]))
    _force_guard_active(session_factory, worker_id=int(worker["id"]), version_id=version_id)
    response = api_client.post(
        f"/api/workers/{worker['id']}/v3/claim",
        json=dispatch,
        headers=WORKER_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "PAUSE_CONSUMER"
    assert response.json()["reason"] == "cache_reclamation_in_progress"
    with session_factory() as session:
        row = session.get(Execution, int(execution["id"]))
        assert row is not None and row.status == "queued" and row.attempt_count == 0


def test_retry_replay_and_incident_recovery_keep_state_while_guard_is_active(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, adapter, version_id = _fixture(api_client, session_factory, monkeypatch, "reactivation")
    retry_execution = _execution(api_client, int(adapter["id"]))
    due = datetime.now(UTC) - timedelta(seconds=1)
    with session_factory.begin() as session:
        row = session.get(Execution, int(retry_execution["id"]))
        assert row is not None
        row.status = "retry_wait"
        row.next_attempt_at = due
    _force_guard_active(session_factory, worker_id=int(worker["id"]), version_id=version_id)
    with session_factory() as session:
        assert attempt_service.retry_dispatcher_once(session, now=due) == 0
    with session_factory() as session:
        row = session.get(Execution, int(retry_execution["id"]))
        assert row is not None and row.status == "retry_wait"

    with session_factory.begin() as session:
        row = session.get(Execution, int(retry_execution["id"]))
        assert row is not None
        row.status = "dead_letter"
        row.next_attempt_at = None
        row.workspace_cleanup_status = "completed"
    replay = api_client.post(f"/api/executions/{retry_execution['id']}/replay")
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "cache_reclamation_in_progress"

    with session_factory.begin() as session:
        row = session.get(Execution, int(retry_execution["id"]))
        assert row is not None
        row.status = "queued"
        row.next_attempt_at = None
        outbox = session.scalar(
            select(ExecutionOutbox).where(ExecutionOutbox.execution_id == row.id)
        )
        assert outbox is not None
        incident = ExecutionInfrastructureIncident(
            execution_id=row.id,
            dispatch_generation=row.dispatch_generation,
            message_id=outbox.message_id,
            kind="delivery_limit",
            status="open",
        )
        session.add(incident)
        session.flush()
        incident_id = incident.id
    with session_factory() as session, pytest.raises(HTTPException) as caught:
        dispose_incident(
            session,
            execution_id=int(retry_execution["id"]),
            incident_id=incident_id,
            action="recover",
            expected_generation=1,
            idempotency_key=uuid.uuid4(),
            reason_code="capacity_repaired",
            principal=SUPERADMIN_PRINCIPAL,
        )
    assert _code(caught.value) == "cache_reclamation_in_progress"


def test_deleted_adapter_does_not_delete_stable_guard_anchor(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, adapter, version_id = _fixture(
        api_client, session_factory, monkeypatch, "deleted-anchor"
    )
    with session_factory.begin() as session:
        cache_governance.ensure_reference_allowed(
            session,
            worker_id=int(worker["id"]),
            adapter_id=int(adapter["id"]),
            version_id=version_id,
        )
    deleted = api_client.delete(f"/api/adapters/{adapter['id']}")
    assert deleted.status_code == 204, deleted.text
    with session_factory() as session:
        assert session.get(Adapter, int(adapter["id"])) is None
        assert session.get(AdapterVersion, version_id) is None
        guard = session.get(WorkerCacheGuard, (int(worker["id"]), version_id))
        assert guard is not None
        assert guard.adapter_id == int(adapter["id"])
        facts = cache_governance.reference_facts(
            session,
            worker_id=int(worker["id"]),
            adapter_id=int(adapter["id"]),
            version_id=version_id,
        )
        assert "adapter_cleanup_incomplete" in facts.reasons


def test_worker_reference_resolver_checks_attempt_responsibility(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, adapter, version_id = _fixture(api_client, session_factory, monkeypatch, "resolver")
    execution = _execution(api_client, int(adapter["id"]))
    resolved = api_client.post(
        f"/api/workers/{worker['id']}/cache/references/resolve",
        json={"execution_id": execution["id"], "attempt_id": None},
        headers=WORKER_HEADERS,
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json() == {
        "key": f"{adapter['id']}-{version_id}",
        "adapter_id": adapter["id"],
        "version_id": version_id,
    }


def test_cache_guard_worker_routes_require_worker_token(
    api_client: TestClient,
) -> None:
    response = api_client.get("/api/workers/1/cache/guards")
    assert response.status_code == 401


def test_worker_client_returns_one_bounded_cache_guard_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ControlClient("https://control.invalid", "token")
    paths: list[str] = []

    def fake_expect(method: str, path: str, payload: object = None, **_kwargs: object) -> bytes:
        assert method == "GET"
        assert payload is None
        paths.append(path)
        return b'{"items":[{"version_id":9}],"next_after_version_id":null}'

    monkeypatch.setattr(client, "_expect", fake_expect)
    assert client.list_cache_guard_page(3, after_version_id=7, limit=2) == (
        [{"version_id": 9}],
        None,
    )
    assert paths == ["/api/workers/3/cache/guards?limit=2&after_version_id=7"]
