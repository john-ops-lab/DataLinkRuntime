"""Worker claim deadline eligibility and reconciler race tests."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.common.config import settings
from dlr.control.models import Execution
from dlr.control.services import execution_reconciler
from dlr.control.services import worker as worker_service
from test_adapters import create_adapter, save_version

WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}


def _register_worker(
    client: TestClient,
    name: str,
    *,
    protocol_version: int = 1,
) -> dict[str, Any]:
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


def _create_pending_execution(client: TestClient, name: str) -> tuple[dict[str, Any], int]:
    adapter = create_adapter(client, name=name)
    save_version(client, adapter["id"])
    response = client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert response.status_code == 202, response.text
    return adapter, int(response.json()["id"])


def _claim(client: TestClient, worker_id: int) -> Any:
    return client.post(
        f"/api/workers/{worker_id}/tasks/claim",
        params={"wait_seconds": 0},
        headers=WORKER_HEADERS,
    )


def test_explicitly_expired_execution_is_not_claimed_and_reconciles(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker = _register_worker(api_client, "deadline-explicit-worker")
    _adapter, execution_id = _create_pending_execution(api_client, "deadline-explicit")
    with session_factory.begin() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        execution.claim_deadline_at = datetime.now(UTC) - timedelta(minutes=1)

    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 204, claimed.text
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        assert execution.status == "pending"
        assert execution.worker_id is None
        assert execution.started_at is None
        assert execution.claim_token_hash is None
        assert execution.cleanup_receipt_token_hash is None

    with session_factory() as session:
        report = execution_reconciler.reconcile_stale_executions(session)
    assert report.pending_failed == 1
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        assert execution.status == "failed"
        assert execution.error_code == "worker_unavailable"
        assert execution.workspace_cleanup_status == "completed"


def test_recent_legacy_null_deadline_remains_claimable(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker = _register_worker(api_client, "deadline-null-recent-worker")
    _adapter, execution_id = _create_pending_execution(api_client, "deadline-null-recent")
    with session_factory.begin() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        execution.claim_deadline_at = None
        execution.created_at = datetime.now(UTC) - timedelta(seconds=1)

    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["execution_id"] == execution_id


def test_old_legacy_null_deadline_is_not_claimed(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker = _register_worker(api_client, "deadline-null-old-worker")
    _adapter, execution_id = _create_pending_execution(api_client, "deadline-null-old")
    with session_factory.begin() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        execution.claim_deadline_at = None
        execution.created_at = datetime.now(UTC) - timedelta(
            seconds=settings.execution_claim_timeout_seconds + 60
        )

    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 204, claimed.text
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        assert execution.status == "pending"
        assert execution.worker_id is None


def test_post_lock_deadline_equality_skips_to_later_valid_execution(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _register_worker(api_client, "deadline-boundary-worker")
    _first_adapter, first_id = _create_pending_execution(api_client, "deadline-boundary-first")
    _second_adapter, second_id = _create_pending_execution(api_client, "deadline-boundary-second")
    decision_time = datetime(2099, 1, 1, tzinfo=UTC)
    with session_factory.begin() as session:
        first = session.get(Execution, first_id)
        second = session.get(Execution, second_id)
        assert first is not None and second is not None
        first.claim_deadline_at = decision_time
        second.claim_deadline_at = decision_time + timedelta(minutes=1)

    monkeypatch.setattr(
        worker_service,
        "_database_now",
        lambda _session: decision_time,
        raising=False,
    )
    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["execution_id"] == second_id
    with session_factory() as session:
        first = session.get(Execution, first_id)
        second = session.get(Execution, second_id)
        assert first is not None and second is not None
        assert first.status == "pending"
        assert first.worker_id is None
        assert second.status == "running"
        assert second.worker_id == worker["id"]


def test_expired_v2_only_row_does_not_trigger_v1_incompatible_error(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker = _register_worker(api_client, "deadline-incompatible-worker")
    _adapter, execution_id = _create_pending_execution(api_client, "deadline-incompatible")
    with session_factory.begin() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        execution.input_source_type = "managed_files"
        execution.input_snapshot = {
            "source_type": "managed_files",
            "revision": 1,
            "artifacts": [],
        }
        execution.claim_deadline_at = datetime.now(UTC) - timedelta(minutes=1)

    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 204, claimed.text
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        assert execution.status == "pending"
        assert execution.worker_id is None


def test_expired_managed_files_row_without_lease_does_not_report_lease_409(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker = _register_worker(
        api_client,
        "deadline-expired-missing-lease-worker",
        protocol_version=2,
    )
    _adapter, execution_id = _create_pending_execution(
        api_client,
        "deadline-expired-missing-lease",
    )
    with session_factory.begin() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        execution.input_source_type = "managed_files"
        execution.input_snapshot = {
            "source_type": "managed_files",
            "revision": 1,
            "artifacts": [{"original_filename": "missing.txt"}],
        }
        execution.claim_deadline_at = datetime.now(UTC) - timedelta(minutes=1)

    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 204, claimed.text
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        assert execution.status == "pending"
        assert execution.worker_id is None


def test_claim_first_lock_rejects_deadline_equality_then_reconciles_once(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _register_worker(api_client, "deadline-claim-first-worker")
    _adapter, execution_id = _create_pending_execution(api_client, "deadline-claim-first")
    decision_time = datetime(2099, 1, 1, tzinfo=UTC)
    with session_factory.begin() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        execution.claim_deadline_at = decision_time

    claim_locked = threading.Event()
    release_claim = threading.Event()
    claim_results: list[Any] = []
    claim_errors: list[BaseException] = []

    def pause_after_claim_lock(_session: Session) -> datetime:
        claim_locked.set()
        if not release_claim.wait(timeout=5):
            raise TimeoutError("claim lock was not released")
        return decision_time

    monkeypatch.setattr(worker_service, "_database_now", pause_after_claim_lock)

    def claim() -> None:
        with session_factory() as session:
            try:
                claim_results.append(worker_service.try_claim(session, worker["id"]))
            except BaseException as error:  # noqa: BLE001 - assert thread outcome below
                claim_errors.append(error)

    claim_thread = threading.Thread(target=claim)
    claim_thread.start()
    assert claim_locked.wait(timeout=5)
    try:
        with session_factory() as session:
            skipped = execution_reconciler.reconcile_stale_executions(
                session,
                now=decision_time,
            )
    finally:
        release_claim.set()
    claim_thread.join(timeout=10)

    assert not claim_thread.is_alive()
    assert claim_errors == []
    assert claim_results == [None]
    assert skipped.reconciled == 0
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        assert execution.status == "pending"
        assert execution.worker_id is None
        assert execution.started_at is None
        assert execution.claim_token_hash is None
        assert execution.cleanup_receipt_token_hash is None

    with session_factory() as session:
        report = execution_reconciler.reconcile_stale_executions(
            session,
            now=decision_time,
        )
    assert report.pending_failed == 1
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        assert execution.status == "failed"
        assert execution.error_code == "worker_unavailable"
        assert execution.worker_id is None
        assert execution.started_at is None
        assert execution.claim_token_hash is None
        assert execution.cleanup_receipt_token_hash is None


def test_reconciler_first_lock_makes_claim_skip_then_converges(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker = _register_worker(api_client, "deadline-reconciler-first-worker")
    _adapter, execution_id = _create_pending_execution(api_client, "deadline-reconciler-first")
    decision_time = datetime(2099, 1, 1, tzinfo=UTC)
    with session_factory.begin() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        execution.claim_deadline_at = decision_time

    reconciler_locked = threading.Event()
    release_reconciler = threading.Event()
    reports: list[Any] = []
    reconcile_errors: list[BaseException] = []

    def reconcile() -> None:
        with session_factory() as session:
            connection = session.connection()

            def pause_after_reconciler_lock(
                _connection: Any,
                _cursor: Any,
                statement: str,
                _parameters: Any,
                _context: Any,
                _executemany: bool,
            ) -> None:
                normalized = " ".join(statement.upper().split())
                if (
                    "FROM EXECUTIONS" in normalized
                    and "CLAIM_DEADLINE_AT" in normalized
                    and "FOR UPDATE SKIP LOCKED" in normalized
                ):
                    reconciler_locked.set()
                    if not release_reconciler.wait(timeout=5):
                        raise TimeoutError("reconciler lock was not released")

            event.listen(connection, "after_cursor_execute", pause_after_reconciler_lock)
            try:
                reports.append(
                    execution_reconciler.reconcile_stale_executions(
                        session,
                        now=decision_time,
                    )
                )
            except BaseException as error:  # noqa: BLE001 - assert thread outcome below
                reconcile_errors.append(error)
            finally:
                event.remove(connection, "after_cursor_execute", pause_after_reconciler_lock)

    reconcile_thread = threading.Thread(target=reconcile)
    reconcile_thread.start()
    assert reconciler_locked.wait(timeout=5)
    try:
        with session_factory() as session:
            assert worker_service.try_claim(session, worker["id"]) is None
    finally:
        release_reconciler.set()
    reconcile_thread.join(timeout=10)

    assert not reconcile_thread.is_alive()
    assert reconcile_errors == []
    assert len(reports) == 1
    assert reports[0].pending_failed == 1
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        assert execution.status == "failed"
        assert execution.error_code == "worker_unavailable"
        assert execution.worker_id is None
        assert execution.started_at is None
