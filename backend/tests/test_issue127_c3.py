"""Issue #127 C3 stale Execution and cleanup receipt contract tests."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.common.config import settings
from dlr.control.models import (
    Execution,
    ExecutionInputArtifactLease,
    ManagedInputArtifact,
    ManagedInputUploadReservation,
    Worker,
)
from dlr.control.services import execution_reconciler
from test_adapters import create_adapter, save_version

WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}
FIXED_NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)


def _register_worker(client: TestClient, name: str, *, protocol_version: int = 2) -> dict[str, Any]:
    response = client.post(
        "/api/workers/register",
        json={
            "name": name,
            "capabilities": ["python"],
            "protocol_version": protocol_version,
        },
        headers=WORKER_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create_execution(client: TestClient, adapter_id: int) -> dict[str, Any]:
    response = client.post(f"/api/adapters/{adapter_id}/executions", json={})
    assert response.status_code == 202, response.text
    return response.json()


def _claim(client: TestClient, worker_id: int) -> Any:
    return client.post(
        f"/api/workers/{worker_id}/tasks/claim",
        params={"wait_seconds": 0},
        headers=WORKER_HEADERS,
    )


def _create_staged_artifact(
    session_factory: sessionmaker[Session], adapter_id: int, filename: str
) -> int:
    with session_factory.begin() as session:
        reservation = ManagedInputUploadReservation(
            adapter_id=adapter_id,
            upload_session_id=f"c3-session-{adapter_id}-{filename}",
            reserved_bytes=0,
            status="CONSUMED",
            expires_at=FIXED_NOW + timedelta(days=1),
            consumed_at=FIXED_NOW,
        )
        session.add(reservation)
        session.flush()
        artifact = ManagedInputArtifact(
            adapter_id=adapter_id,
            created_by_user_id=None,
            upload_session_id=reservation.upload_session_id,
            upload_reservation_id=reservation.id,
            original_filename=filename,
            storage_key=f"{adapter_id:016x}{reservation.id:048x}",
            content_type="text/plain",
            size_bytes=8,
            sha256="a" * 64,
            status="STAGED",
            retention_mode="system_default",
            expires_at=FIXED_NOW + timedelta(hours=1),
            created_at=FIXED_NOW,
        )
        session.add(artifact)
        session.flush()
        return int(artifact.id)


def _bind_artifact(client: TestClient, adapter_id: int, artifact_id: int) -> None:
    response = client.put(
        f"/api/adapters/{adapter_id}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [artifact_id],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert response.status_code == 200, response.text


def _mark_stale(
    session_factory: sessionmaker[Session], execution_id: int, *, running: bool = False
) -> None:
    with session_factory.begin() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        execution.claim_deadline_at = FIXED_NOW - timedelta(minutes=5)
        if running:
            execution.started_at = FIXED_NOW - timedelta(minutes=10)
            execution.execution_deadline_at = FIXED_NOW - timedelta(minutes=5)


def test_c3_red_high_risk_stale_paths_are_not_silent(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The C0 malformed pending row must converge with structured facts."""
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    worker = _register_worker(api_client, "c3-red-observable-worker")
    adapter = create_adapter(api_client, name="c3-red-observable")
    save_version(api_client, adapter["id"])
    artifact_id = _create_staged_artifact(session_factory, adapter["id"], "missing-lease.txt")
    _bind_artifact(api_client, adapter["id"], artifact_id)
    execution = _create_execution(api_client, adapter["id"])
    execution_id = execution["id"]

    with session_factory.begin() as session:
        removed = (
            session.query(ExecutionInputArtifactLease).filter_by(execution_id=execution_id).delete()
        )
        assert removed == 1
    _mark_stale(session_factory, execution_id)

    with session_factory() as session:
        report = execution_reconciler.reconcile_stale_executions(session, now=FIXED_NOW)
        assert report.reconciled == 1

    with session_factory() as session:
        row = session.get(Execution, execution_id)
        assert row is not None
        assert row.status == "failed"
        assert row.error_code == "worker_unavailable"
        assert row.error is not None
        assert row.workspace_cleanup_status == "completed"
        assert row.workspace_cleanup_error_code is None
        assert row.ended_at == FIXED_NOW
        assert (
            session.scalar(
                select(ExecutionInputArtifactLease).where(
                    ExecutionInputArtifactLease.execution_id == execution_id
                )
            )
            is None
        )

    with session_factory() as session:
        report = execution_reconciler.reconcile_stale_executions(session, now=FIXED_NOW)
        assert report.reconciled == 0
    assert _claim(api_client, worker["id"]).status_code == 204


def test_c3_stale_pending_releases_a_live_input_lease(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    adapter = create_adapter(api_client, name="c3-pending-live-lease")
    save_version(api_client, adapter["id"])
    artifact_id = _create_staged_artifact(session_factory, adapter["id"], "pending-live.txt")
    _bind_artifact(api_client, adapter["id"], artifact_id)
    execution = _create_execution(api_client, adapter["id"])
    execution_id = execution["id"]
    _mark_stale(session_factory, execution_id)

    with session_factory() as session:
        assert (
            session.scalar(
                select(ExecutionInputArtifactLease).where(
                    ExecutionInputArtifactLease.execution_id == execution_id
                )
            )
            is not None
        )
        report = execution_reconciler.reconcile_stale_executions(session, now=FIXED_NOW)
        assert report.pending_failed == 1

    with session_factory() as session:
        row = session.get(Execution, execution_id)
        assert row is not None and row.status == "failed"
        assert (
            session.scalar(
                select(ExecutionInputArtifactLease).where(
                    ExecutionInputArtifactLease.execution_id == execution_id
                )
            )
            is None
        )


@pytest.mark.parametrize(
    ("worker_status", "expected_status", "expected_error_code"),
    [
        ("online", "timeout", None),
        ("offline", "failed", "worker_lost"),
    ],
)
def test_c3_running_stale_uses_effective_worker_health_and_does_not_rerun(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    worker_status: str,
    expected_status: str,
    expected_error_code: str | None,
) -> None:
    worker = _register_worker(api_client, f"c3-running-{worker_status}")
    adapter = create_adapter(api_client, name=f"c3-running-{worker_status}")
    save_version(api_client, adapter["id"])
    execution = _create_execution(api_client, adapter["id"])
    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text
    _mark_stale(session_factory, execution["id"], running=True)

    with session_factory.begin() as session:
        row = session.get(Execution, execution["id"])
        registered = session.get(Worker, worker["id"])
        assert row is not None and registered is not None
        registered.status = worker_status
        registered.last_heartbeat = FIXED_NOW

    with session_factory() as session:
        report = execution_reconciler.reconcile_stale_executions(session, now=FIXED_NOW)
        assert report.reconciled == 1

    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        assert row.status == expected_status
        assert row.error_code == expected_error_code
        assert row.workspace_cleanup_status == "deferred"
        assert row.workspace_cleanup_error_code == "workspace_cleanup_unknown"
        assert row.ended_at == FIXED_NOW
        assert (
            session.scalar(
                select(ExecutionInputArtifactLease.execution_id).where(
                    ExecutionInputArtifactLease.execution_id == execution["id"]
                )
            )
            is None
        )
    assert _claim(api_client, worker["id"]).status_code == 204


def test_c3_late_result_and_non_owner_cannot_change_stale_terminal(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    owner = _register_worker(api_client, "c3-late-owner")
    intruder = _register_worker(api_client, "c3-late-intruder")
    adapter = create_adapter(api_client, name="c3-late-result")
    save_version(api_client, adapter["id"])
    execution = _create_execution(api_client, adapter["id"])
    claimed = _claim(api_client, owner["id"])
    assert claimed.status_code == 200, claimed.text
    _mark_stale(session_factory, execution["id"], running=True)

    with session_factory() as session:
        execution_reconciler.reconcile_stale_executions(session, now=FIXED_NOW)
    before = api_client.get(f"/api/executions/{execution['id']}").json()

    late = api_client.post(
        f"/api/workers/{owner['id']}/executions/{execution['id']}/result",
        json={"status": "succeeded", "output": {"late": True}},
        headers={**WORKER_HEADERS, "X-DLR-Claim-Token": claimed.json()["claim_token"]},
    )
    assert late.status_code == 200, late.text
    after = late.json()
    for key in ("status", "output", "error", "error_code", "ended_at", "workspace_cleanup_status"):
        assert after[key] == before[key]

    non_owner_result = api_client.post(
        f"/api/workers/{intruder['id']}/executions/{execution['id']}/result",
        json={"status": "succeeded"},
        headers=WORKER_HEADERS,
    )
    assert non_owner_result.status_code == 409
    assert non_owner_result.json()["detail"]["code"] == "execution_not_owned"
    non_owner_progress = api_client.post(
        f"/api/workers/{intruder['id']}/executions/{execution['id']}/progress",
        json={"stdout_chunk": "late"},
        headers=WORKER_HEADERS,
    )
    assert non_owner_progress.status_code == 409
    assert non_owner_progress.json()["detail"]["code"] == "execution_not_owned"


def test_c3_cleanup_receipt_is_idempotent_and_preserves_business_facts(
    api_client: TestClient,
) -> None:
    worker = _register_worker(api_client, "c3-receipt-worker")
    adapter = create_adapter(api_client, name="c3-receipt")
    save_version(api_client, adapter["id"])
    execution = _create_execution(api_client, adapter["id"])
    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text
    path = f"/api/workers/{worker['id']}/executions/{execution['id']}/cleanup-receipt"
    result = api_client.post(
        f"/api/workers/{worker['id']}/executions/{execution['id']}/result",
        json={
            "status": "succeeded",
            "output": {"answer": 42},
            "error": "business detail",
            "error_code": "business_code",
            "workspace_cleanup_status": "deferred",
            "workspace_cleanup_error_code": "workspace_cleanup_failed",
        },
        headers={**WORKER_HEADERS, "X-DLR-Claim-Token": claimed.json()["claim_token"]},
    )
    assert result.status_code == 200, result.text
    business_before = {
        key: result.json()[key]
        for key in ("status", "output", "error", "error_code", "ended_at", "duration_ms")
    }

    receipt = api_client.post(
        path,
        json={"status": "completed"},
        headers={**WORKER_HEADERS, "X-DLR-Cleanup-Token": claimed.json()["cleanup_token"]},
    )
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["workspace_cleanup_status"] == "completed"
    for key, value in business_before.items():
        assert receipt.json()[key] == value

    repeated = api_client.post(
        path,
        json={"status": "completed"},
        headers={**WORKER_HEADERS, "X-DLR-Cleanup-Token": claimed.json()["cleanup_token"]},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == receipt.json()


def test_c3_cleanup_receipt_rejects_non_terminal_transition(
    api_client: TestClient,
) -> None:
    worker = _register_worker(api_client, "c3-receipt-invalid-worker")
    adapter = create_adapter(api_client, name="c3-receipt-invalid")
    save_version(api_client, adapter["id"])
    execution = _create_execution(api_client, adapter["id"])
    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text
    response = api_client.post(
        f"/api/workers/{worker['id']}/executions/{execution['id']}/cleanup-receipt",
        json={"status": "completed"},
        headers={**WORKER_HEADERS, "X-DLR-Cleanup-Token": claimed.json()["cleanup_token"]},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "workspace_cleanup_transition_invalid"


def test_c3_reconciler_serializes_multiple_controls(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker = _register_worker(api_client, "c3-concurrency-worker")
    adapter = create_adapter(api_client, name="c3-concurrency")
    save_version(api_client, adapter["id"])
    execution = _create_execution(api_client, adapter["id"])
    _mark_stale(session_factory, execution["id"])

    barrier = threading.Barrier(2)
    reports: list[Any] = []
    errors: list[BaseException] = []

    def reconcile() -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=5)
            reports.append(execution_reconciler.reconcile_stale_executions(session, now=FIXED_NOW))
        except BaseException as exc:  # noqa: BLE001 - collect both worker outcomes
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=reconcile) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(reports) == 2
    assert sum(report.reconciled for report in reports) == 1
    assert _claim(api_client, worker["id"]).status_code == 204


def test_c3_reconciler_db_failure_rolls_back_terminal_and_lease(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    adapter = create_adapter(api_client, name="c3-atomic-failure")
    save_version(api_client, adapter["id"])
    artifact_id = _create_staged_artifact(session_factory, adapter["id"], "atomic.txt")
    _bind_artifact(api_client, adapter["id"], artifact_id)
    execution = _create_execution(api_client, adapter["id"])
    execution_id = execution["id"]
    _mark_stale(session_factory, execution_id)

    with session_factory() as session:

        def fail_after_flush() -> None:
            session.flush()
            raise RuntimeError("injected commit failure")

        monkeypatch.setattr(session, "commit", fail_after_flush)
        with pytest.raises(RuntimeError, match="injected commit failure"):
            execution_reconciler.reconcile_stale_executions(session, now=FIXED_NOW)

    with session_factory() as session:
        row = session.get(Execution, execution_id)
        assert row is not None and row.status == "pending"
        assert (
            session.scalar(
                select(ExecutionInputArtifactLease).where(
                    ExecutionInputArtifactLease.execution_id == execution_id
                )
            )
            is not None
        )


def test_c3_reconciler_loop_propagates_cancellation_without_leaking_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    async def fake_to_thread(function: Any, *args: Any, **kwargs: Any) -> None:
        calls.append((function, args, kwargs))

    async def cancel_on_sleep(_: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(execution_reconciler, "_asyncio_to_thread", fake_to_thread)
    monkeypatch.setattr(execution_reconciler, "_asyncio_sleep", cancel_on_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(execution_reconciler.stale_execution_reconciler_loop())
    assert len(calls) == 1
    assert calls[0][0] is execution_reconciler._reconcile_tick
