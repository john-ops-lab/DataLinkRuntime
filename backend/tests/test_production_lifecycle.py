"""M3.2 + M5.1 tests: Adapter production lifecycle.

Covers publish gate, Start/Stop, production version locking (M5.1),
Worker locking during production, trigger value space expansion and
the active-production unique constraint.

M5.1 changes Start to open the production entry and lock the production
version without creating an Execution. Stop clears the locked version.
"""

import threading
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.control.models import Adapter, Execution
from dlr.control.models.platform import AdapterCredentialBinding, Credential
from dlr.control.schemas.adapter import AdapterUpdate
from dlr.control.services import adapter as adapter_service
from dlr.control.services import worker as worker_service
from test_adapters import create_adapter, pass_publish_gate, save_version
from test_executions import create_execution
from test_workers import claim, register_worker

WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}


def create_production_execution(
    session_factory: sessionmaker[Session],
    adapter_id: int,
    version_id: int,
    target_worker_id: int,
    status: str = "pending",
) -> int:
    """Insert a production-class Execution directly in the DB.

    M5.1: Start no longer creates an Execution. Tests that need an active
    production Execution (e.g. Stop with cancel) use this helper.
    """
    with session_factory() as session:
        execution = Execution(
            adapter_id=adapter_id,
            version_id=version_id,
            trigger="production",
            status=status,
            target_worker_id=target_worker_id,
            input={},
        )
        session.add(execution)
        session.commit()
        return execution.id


PUBLISH = "/api/adapters/{adapter_id}/versions/{version_id}/publish"
START = "/api/adapters/{adapter_id}/production/start"
STOP = "/api/adapters/{adapter_id}/production/stop"


def publish(client: TestClient, adapter_id: int, version_id: int):
    return client.post(PUBLISH.format(adapter_id=adapter_id, version_id=version_id))


def gate(client: TestClient, adapter_id: int, version_id: int):
    return client.get(f"/api/adapters/{adapter_id}/versions/{version_id}/publish-gate")


def start(client: TestClient, adapter_id: int):
    return client.post(START.format(adapter_id=adapter_id))


def stop(client: TestClient, adapter_id: int, mode: str = "wait"):
    return client.post(STOP.format(adapter_id=adapter_id), json={"mode": mode})


def finish_execution(
    client: TestClient, worker_id: int, execution_id: int, status: str = "succeeded"
):
    return client.post(
        f"/api/workers/{worker_id}/executions/{execution_id}/result",
        json={"status": status},
        headers=WORKER_HEADERS,
    )


def setup_publishable(
    client: TestClient, name: str, worker_name: str = "prod-worker"
) -> tuple[dict, dict, dict]:
    """Adapter + version + satisfied gate + published pointer."""
    adapter = create_adapter(client, name=name)
    version = save_version(client, adapter["id"])
    worker = pass_publish_gate(client, adapter["id"], version["id"], worker_name=worker_name)
    response = publish(client, adapter["id"], version["id"])
    assert response.status_code == 200, response.text
    return adapter, version, worker


def wait_for_postgres_lock(session: Session, backend_pid: int) -> None:
    """Wait until one racer is blocked on a PostgreSQL lock, without sleep."""
    deadline = time.monotonic() + 5
    statement = text(
        "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :backend_pid"
    )
    while time.monotonic() < deadline:
        if session.scalar(statement, {"backend_pid": backend_pid}) is True:
            return
    raise AssertionError("concurrent database session did not enter a lock wait")


# --- production Worker configuration (PATCH) ------------------------------------


def test_patch_production_worker_set_clear_and_validate(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="pw-config")
    worker = register_worker(api_client, name="pw-1")

    set_response = api_client.patch(
        f"/api/adapters/{adapter['id']}", json={"production_worker_id": worker["id"]}
    )
    assert set_response.status_code == 200
    assert set_response.json()["production_worker_id"] == worker["id"]

    # Omitting the field leaves it unchanged.
    untouched = api_client.patch(f"/api/adapters/{adapter['id']}", json={"description": "x"})
    assert untouched.json()["production_worker_id"] == worker["id"]

    # Explicit null clears it.
    cleared = api_client.patch(
        f"/api/adapters/{adapter['id']}", json={"production_worker_id": None}
    )
    assert cleared.json()["production_worker_id"] is None

    unknown = api_client.patch(
        f"/api/adapters/{adapter['id']}", json={"production_worker_id": 999999}
    )
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "worker_not_found"


# --- publish gate ----------------------------------------------------------------


def test_publish_gate_reasons(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="gate-reasons")
    version = save_version(api_client, adapter["id"])

    # No production Worker configured.
    body = gate(api_client, adapter["id"], version["id"]).json()
    assert body["allowed"] is False
    assert body["reason"] == "no_production_worker"

    worker = register_worker(api_client, name="gate-w1")
    api_client.patch(f"/api/adapters/{adapter['id']}", json={"production_worker_id": worker["id"]})

    # Configured but never tested on that Worker.
    body = gate(api_client, adapter["id"], version["id"]).json()
    assert body["allowed"] is False
    assert body["reason"] == "not_tested_on_production_worker"

    # A failed test run locks the gate and is reported as last_test.
    execution = create_execution(api_client, adapter["id"], {"version_id": version["id"]})
    assert execution["target_worker_id"] == worker["id"], "tests target the production Worker"
    assert claim(api_client, worker["id"]).status_code == 200
    api_client.post(
        f"/api/workers/{worker['id']}/executions/{execution['id']}/result",
        json={"status": "failed", "error": "boom"},
        headers=WORKER_HEADERS,
    )
    body = gate(api_client, adapter["id"], version["id"]).json()
    assert body["allowed"] is False
    assert body["reason"] == "last_test_not_succeeded"
    assert body["last_test"]["execution_id"] == execution["id"]
    assert body["last_test"]["status"] == "failed"


def test_publish_gate_satisfied_by_succeeded_test(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="gate-ok")
    version = save_version(api_client, adapter["id"])
    pass_publish_gate(api_client, adapter["id"], version["id"], worker_name="gate-ok-worker")

    body = gate(api_client, adapter["id"], version["id"]).json()
    assert body["allowed"] is True
    assert body["reason"] is None
    assert body["last_test"]["status"] == "succeeded"


def test_publish_gate_requires_test_on_current_production_worker(
    api_client: TestClient,
) -> None:
    """Switching the production Worker invalidates an older gate (Issue §23)."""
    adapter = create_adapter(api_client, name="gate-switch")
    version = save_version(api_client, adapter["id"])
    pass_publish_gate(api_client, adapter["id"], version["id"], worker_name="gate-old")
    assert gate(api_client, adapter["id"], version["id"]).json()["allowed"] is True

    new_worker = register_worker(api_client, name="gate-new")
    api_client.patch(
        f"/api/adapters/{adapter['id']}", json={"production_worker_id": new_worker["id"]}
    )
    body = gate(api_client, adapter["id"], version["id"]).json()
    assert body["allowed"] is False, "tests ran on the previous Worker only"
    assert body["reason"] == "not_tested_on_production_worker"


def test_publish_enforces_gate(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="gate-enforce")
    version = save_version(api_client, adapter["id"])
    response = publish(api_client, adapter["id"], version["id"])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "publish_gate_locked"


# --- Start / Stop ------------------------------------------------------------------


def test_start_opens_entry_locks_version(api_client: TestClient) -> None:
    """M5.1: Start returns an Adapter, locks the production version, no Execution."""
    adapter, version, worker = setup_publishable(api_client, "prod-start")

    response = start(api_client, adapter["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    # Start now returns an AdapterResponse (not an ExecutionResponse).
    assert body["production_state"] == "running"
    assert body["production_version_id"] == version["id"]
    assert body["production_version_seq"] == version["seq"]
    assert body["published_version_id"] == version["id"]
    assert body["published_version_seq"] == version["seq"]
    assert body["running_execution_id"] is None
    assert body["running_version_id"] is None

    # No production Execution was created by Start (only the manual test run
    # from pass_publish_gate exists).
    history = api_client.get(f"/api/adapters/{adapter['id']}/executions").json()
    assert all(item["trigger"] == "manual" for item in history["items"])

    # A second Start is rejected while the production entry is running.
    again = start(api_client, adapter["id"])
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "production_already_running"


def test_start_preconditions(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="prod-precond")
    save_version(api_client, adapter["id"])

    # Nothing published yet.
    response = start(api_client, adapter["id"])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_not_published"

    # Published but the production Worker is offline.
    worker = register_worker(api_client, name="prod-offline")
    api_client.patch(f"/api/adapters/{adapter['id']}", json={"production_worker_id": worker["id"]})
    execution = create_execution(api_client, adapter["id"])
    assert claim(api_client, worker["id"]).status_code == 200
    api_client.post(
        f"/api/workers/{worker['id']}/executions/{execution['id']}/result",
        json={"status": "succeeded"},
        headers=WORKER_HEADERS,
    )
    assert publish(api_client, adapter["id"], execution["version_id"]).status_code == 200
    api_client.post(f"/api/workers/{worker['id']}/offline", headers=WORKER_HEADERS)
    response = start(api_client, adapter["id"])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "worker_offline"


def test_test_run_rejects_configured_offline_worker_without_persisting_execution(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter = create_adapter(api_client, name="test-offline")
    save_version(api_client, adapter["id"])
    worker = register_worker(api_client, name="test-offline-worker")
    configured = api_client.patch(
        f"/api/adapters/{adapter['id']}", json={"production_worker_id": worker["id"]}
    )
    assert configured.status_code == 200
    offline = api_client.post(f"/api/workers/{worker['id']}/offline", headers=WORKER_HEADERS)
    assert offline.status_code == 204

    response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "worker_offline"
    with session_factory() as session:
        assert session.scalars(select(Execution)).all() == []


def test_start_revalidates_published_version_after_production_worker_switch(
    api_client: TestClient,
) -> None:
    adapter, version, worker_01 = setup_publishable(
        api_client, "prod-worker-switch", worker_name="worker-01"
    )
    worker_02 = register_worker(api_client, name="worker-02")
    switched = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"production_worker_id": worker_02["id"]},
    )
    assert switched.status_code == 200

    blocked = start(api_client, adapter["id"])
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "production_test_required"
    history = api_client.get(f"/api/adapters/{adapter['id']}/executions").json()["items"]
    assert all(item["trigger"] == "manual" for item in history)
    assert all(item["worker_id"] == worker_01["id"] for item in history)

    retest = create_execution(api_client, adapter["id"], {"version_id": version["id"]})
    assert retest["target_worker_id"] == worker_02["id"]
    assert claim(api_client, worker_02["id"]).status_code == 200
    assert finish_execution(api_client, worker_02["id"], retest["id"]).status_code == 200

    started = start(api_client, adapter["id"])
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["production_version_id"] == version["id"]
    assert body["production_worker_id"] == worker_02["id"]


def test_start_defaults_to_single_online_worker(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="prod-default")
    version = save_version(api_client, adapter["id"])
    worker = register_worker(api_client, name="prod-only")
    api_client.patch(f"/api/adapters/{adapter['id']}", json={"production_worker_id": worker["id"]})
    execution = create_execution(api_client, adapter["id"], {"version_id": version["id"]})
    assert claim(api_client, worker["id"]).status_code == 200
    api_client.post(
        f"/api/workers/{worker['id']}/executions/{execution['id']}/result",
        json={"status": "succeeded"},
        headers=WORKER_HEADERS,
    )
    # Publish while the gate is satisfied, then clear the pointer: Start
    # must adopt the only online Worker again and write it back.
    assert publish(api_client, adapter["id"], version["id"]).status_code == 200
    api_client.patch(f"/api/adapters/{adapter['id']}", json={"production_worker_id": None})

    response = start(api_client, adapter["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["production_worker_id"] == worker["id"], "adoption is written back"
    assert body["production_version_id"] == version["id"]


def test_stop_wait_keeps_active_execution(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker = setup_publishable(api_client, "prod-stop-wait")
    start(api_client, adapter["id"])
    exec_id = create_production_execution(
        session_factory, adapter["id"], version["id"], worker["id"]
    )

    response = stop(api_client, adapter["id"], mode="wait")
    assert response.status_code == 200
    assert response.json()["production_state"] == "stopped"
    assert response.json()["running_execution_id"] == exec_id
    assert response.json()["last_production_execution_status"] == "pending"

    fetched = api_client.get(f"/api/executions/{exec_id}").json()
    assert fetched["status"] == "pending", "wait mode lets the run finish naturally"

    # Still blocked while the Execution is active.
    again = start(api_client, adapter["id"])
    assert again.status_code == 409

    # Terminate now cancels the pending Execution and unblocks Start.
    terminated = stop(api_client, adapter["id"], mode="terminate")
    assert terminated.status_code == 200
    fetched = api_client.get(f"/api/executions/{exec_id}").json()
    assert fetched["status"] == "cancelled"
    assert fetched["ended_at"] is not None

    restarted = start(api_client, adapter["id"])
    assert restarted.status_code == 200


def test_stop_terminate_flags_running_execution(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker = setup_publishable(api_client, "prod-stop-term")
    start(api_client, adapter["id"])
    exec_id = create_production_execution(
        session_factory, adapter["id"], version["id"], worker["id"]
    )

    # A non-target Worker can never claim it; the target Worker can.
    intruder = register_worker(api_client, name="prod-intruder")
    assert claim(api_client, intruder["id"]).status_code == 204
    claimed = claim(api_client, worker["id"])
    assert claimed.status_code == 200
    assert claimed.json()["execution_id"] == exec_id

    response = stop(api_client, adapter["id"], mode="terminate")
    assert response.status_code == 200
    fetched = api_client.get(f"/api/executions/{exec_id}").json()
    assert fetched["status"] == "running", "the Worker decides the terminal state"
    assert fetched["cancel_requested"] is True

    # cancel_requested is transitional: the old run is still active and a
    # new Start must remain blocked until the Worker reports cancelled.
    blocked = start(api_client, adapter["id"])
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "production_already_running"
    cancelled = finish_execution(api_client, worker["id"], exec_id, status="cancelled")
    assert cancelled.status_code == 200
    assert start(api_client, adapter["id"]).status_code == 200


def test_stop_terminate_serializes_with_claim_when_claim_wins(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """A claim holding the row lock wins; Stop observes running and requests cancel."""
    adapter, version, worker = setup_publishable(api_client, "prod-claim-wins")
    start(api_client, adapter["id"])
    exec_id = create_production_execution(
        session_factory, adapter["id"], version["id"], worker["id"]
    )

    claim_has_lock = threading.Event()
    stop_connection_ready = threading.Event()
    claim_payloads: list[object] = []
    stop_backend_pids: list[int] = []
    errors: list[BaseException] = []

    def claim_racer() -> None:
        with session_factory() as session:
            original_commit = session.commit

            def coordinated_commit() -> None:
                # try_claim has already selected the Execution FOR UPDATE.
                claim_has_lock.set()
                if not stop_connection_ready.wait(timeout=5):
                    raise AssertionError("Stop racer did not open its database connection")
                wait_for_postgres_lock(session, stop_backend_pids[0])
                original_commit()

            session.commit = coordinated_commit  # type: ignore[method-assign]
            try:
                claim_payloads.append(worker_service.try_claim(session, worker["id"]))
            except BaseException as exc:  # noqa: BLE001 - reported in the test thread
                errors.append(exc)

    def stop_racer() -> None:
        with session_factory() as session:
            try:
                backend_pid = session.scalar(select(func.pg_backend_pid()))
                assert isinstance(backend_pid, int)
                stop_backend_pids.append(backend_pid)
                stop_connection_ready.set()
                adapter_service.stop_production(session, adapter["id"], "terminate")
            except BaseException as exc:  # noqa: BLE001 - reported in the test thread
                errors.append(exc)

    claim_thread = threading.Thread(target=claim_racer)
    stop_thread = threading.Thread(target=stop_racer)
    claim_thread.start()
    assert claim_has_lock.wait(timeout=5), "claim did not acquire the Execution row lock"
    stop_thread.start()
    claim_thread.join(timeout=10)
    stop_thread.join(timeout=10)

    assert not claim_thread.is_alive()
    assert not stop_thread.is_alive()
    assert errors == []
    assert len(claim_payloads) == 1
    payload = claim_payloads[0]
    assert payload is not None
    assert payload.execution_id == exec_id
    with session_factory() as session:
        row = session.get(Execution, exec_id)
        assert row is not None
        assert row.status == "running"
        assert row.worker_id == worker["id"]
        assert row.cancel_requested is True


def test_stop_terminate_serializes_with_claim_when_stop_wins(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """Stop holding the row lock cancels pending before claim can see it."""
    adapter, version, worker = setup_publishable(api_client, "prod-stop-wins")
    start(api_client, adapter["id"])
    exec_id = create_production_execution(
        session_factory, adapter["id"], version["id"], worker["id"]
    )

    stop_has_lock = threading.Event()
    allow_stop_commit = threading.Event()
    errors: list[BaseException] = []

    def stop_racer() -> None:
        with session_factory() as session:
            original_commit = session.commit

            def coordinated_commit() -> None:
                # stop_production has already selected the Execution FOR UPDATE.
                stop_has_lock.set()
                if not allow_stop_commit.wait(timeout=5):
                    raise AssertionError("claim racer did not release Stop commit")
                original_commit()

            session.commit = coordinated_commit  # type: ignore[method-assign]
            try:
                adapter_service.stop_production(session, adapter["id"], "terminate")
            except BaseException as exc:  # noqa: BLE001 - reported in the test thread
                errors.append(exc)

    stop_thread = threading.Thread(target=stop_racer)
    stop_thread.start()
    assert stop_has_lock.wait(timeout=5), "Stop did not acquire the Execution row lock"
    try:
        with session_factory() as session:
            claim_payload = worker_service.try_claim(session, worker["id"])
    finally:
        allow_stop_commit.set()
    stop_thread.join(timeout=10)

    assert not stop_thread.is_alive()
    assert errors == []
    assert claim_payload is None
    with session_factory() as session:
        row = session.get(Execution, exec_id)
        assert row is not None
        assert row.status == "cancelled"
        assert row.worker_id is None
        assert row.cancel_requested is False
        assert row.ended_at is not None


# --- Unpublish -----------------------------------------------------------------------


def test_unpublish_requires_stop_and_clears_pointer(api_client: TestClient) -> None:
    adapter, version, _worker = setup_publishable(api_client, "prod-unpublish")
    start(api_client, adapter["id"])

    blocked = api_client.post(f"/api/adapters/{adapter['id']}/unpublish")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "production_running"

    stop(api_client, adapter["id"], mode="terminate")
    response = api_client.post(f"/api/adapters/{adapter['id']}/unpublish")
    assert response.status_code == 200
    assert response.json()["published_version_id"] is None

    # Idempotent when already unpublished.
    again = api_client.post(f"/api/adapters/{adapter['id']}/unpublish")
    assert again.status_code == 200
    assert again.json()["published_version_id"] is None

    # Start is impossible without a published version.
    assert start(api_client, adapter["id"]).status_code == 409
    assert version["id"] is not None


def test_unpublish_and_archive_require_stop_after_production_execution_succeeds(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """A started-but-idle production entry remains protected until Stop."""
    adapter, version, worker = setup_publishable(api_client, "prod-idle-protected")
    start(api_client, adapter["id"])
    exec_id = create_production_execution(
        session_factory, adapter["id"], version["id"], worker["id"]
    )
    assert claim(api_client, worker["id"]).status_code == 200
    assert finish_execution(api_client, worker["id"], exec_id).status_code == 200

    fetched = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert fetched["production_state"] == "running"
    assert fetched["running_execution_id"] is None
    assert fetched["production_version_id"] == version["id"]

    unpublish = api_client.post(f"/api/adapters/{adapter['id']}/unpublish")
    assert unpublish.status_code == 409
    assert unpublish.json()["detail"]["code"] == "production_running"
    archive = api_client.post(f"/api/adapters/{adapter['id']}/archive")
    assert archive.status_code == 409
    assert archive.json()["detail"]["code"] == "production_running"

    assert stop(api_client, adapter["id"]).status_code == 200
    assert api_client.post(f"/api/adapters/{adapter['id']}/unpublish").status_code == 200
    assert api_client.post(f"/api/adapters/{adapter['id']}/archive").status_code == 200


def test_publish_new_version_keeps_running_version_until_stop_then_start(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """Running=v2 -> Publish v3 keeps v2; v3 needs an explicit safe restart."""
    adapter, running_version, worker = setup_publishable(api_client, "prod-target-change")
    next_version = save_version(api_client, adapter["id"])
    pass_publish_gate(
        api_client,
        adapter["id"],
        next_version["id"],
        worker_name=worker["name"],
    )

    start(api_client, adapter["id"])
    exec_id = create_production_execution(
        session_factory, adapter["id"], running_version["id"], worker["id"]
    )
    assert claim(api_client, worker["id"]).status_code == 200

    published = publish(api_client, adapter["id"], next_version["id"])
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["published_version_id"] == next_version["id"]
    assert body["published_version_seq"] == next_version["seq"]
    assert body["running_version_id"] == running_version["id"]
    assert body["running_version_seq"] == running_version["seq"]
    assert body["running_execution_id"] == exec_id
    assert body["production_version_id"] == running_version["id"]
    old_run = api_client.get(f"/api/executions/{exec_id}").json()
    assert old_run["status"] == "running"
    assert old_run["version_id"] == running_version["id"]

    # Publish is not Start. The open production entry blocks v3 both before
    # Stop and while Stop(wait) is still waiting for v2 to end.
    assert start(api_client, adapter["id"]).status_code == 409
    waiting = stop(api_client, adapter["id"], mode="wait")
    assert waiting.json()["running_execution_id"] == exec_id
    assert start(api_client, adapter["id"]).status_code == 409

    assert finish_execution(api_client, worker["id"], exec_id).status_code == 200
    restarted = start(api_client, adapter["id"])
    assert restarted.status_code == 200, restarted.text
    assert restarted.json()["production_version_id"] == next_version["id"]


def test_started_idle_exposes_succeeded_last_production_and_requires_stop(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker = setup_publishable(api_client, "prod-idle-success")
    start(api_client, adapter["id"])
    exec_id = create_production_execution(
        session_factory, adapter["id"], version["id"], worker["id"]
    )
    assert claim(api_client, worker["id"]).status_code == 200
    assert finish_execution(api_client, worker["id"], exec_id).status_code == 200

    fetched = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert fetched["production_state"] == "running", "the production entry stays open"
    assert fetched["running_execution_id"] is None, "there is no active child process"
    assert fetched["running_version_id"] is None
    assert fetched["production_version_id"] == version["id"], "M5.1: version stays locked"
    assert fetched["last_production_execution_id"] == exec_id
    assert fetched["last_production_execution_status"] == "succeeded"
    assert fetched["last_production_version_id"] == version["id"]
    assert fetched["last_production_version_seq"] == version["seq"]

    listed = api_client.get("/api/adapters").json()
    listed_adapter = next(item for item in listed if item["id"] == adapter["id"])
    assert listed_adapter["last_production_execution_status"] == "succeeded"
    assert listed_adapter["published_version_seq"] == version["seq"]
    assert listed_adapter["last_production_version_seq"] == version["seq"]

    # An idle production entry is still started. A new run requires the
    # administrator to explicitly Stop first.
    blocked = start(api_client, adapter["id"])
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "production_already_running"
    assert stop(api_client, adapter["id"]).status_code == 200
    assert start(api_client, adapter["id"]).status_code == 200


@pytest.mark.parametrize("status", ["failed", "timeout"])
def test_adapter_response_exposes_failed_last_production_execution(
    api_client: TestClient, status: str, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker = setup_publishable(api_client, f"prod-last-{status}")
    start(api_client, adapter["id"])
    exec_id = create_production_execution(
        session_factory, adapter["id"], version["id"], worker["id"]
    )
    assert claim(api_client, worker["id"]).status_code == 200
    assert finish_execution(api_client, worker["id"], exec_id, status).status_code == 200

    fetched = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert fetched["production_state"] == "running"
    assert fetched["running_execution_id"] is None
    assert fetched["production_version_id"] == version["id"]
    assert fetched["last_production_execution_id"] == exec_id
    assert fetched["last_production_execution_status"] == status
    assert fetched["last_production_version_id"] == version["id"]


# --- Archive / Restore ------------------------------------------------------------------


def test_archived_adapter_is_read_only(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="prod-archive")
    version = save_version(api_client, adapter["id"])

    archived = api_client.post(f"/api/adapters/{adapter['id']}/archive")
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None

    save = api_client.post(
        f"/api/adapters/{adapter['id']}/versions",
        json={"code": "def handle(context, input):\n    return 1\n"},
    )
    assert save.status_code == 409
    assert save.json()["detail"]["code"] == "adapter_archived"
    assert create_execution_response(api_client, adapter["id"]) == "adapter_archived"
    assert publish(api_client, adapter["id"], version["id"]).status_code == 409
    assert start(api_client, adapter["id"]).status_code == 409

    restored = api_client.post(f"/api/adapters/{adapter['id']}/restore")
    assert restored.status_code == 200
    assert restored.json()["archived_at"] is None
    ok = api_client.post(
        f"/api/adapters/{adapter['id']}/versions",
        json={"code": "def handle(context, input):\n    return 2\n"},
    )
    assert ok.status_code == 201


def create_execution_response(client: TestClient, adapter_id: int) -> str:
    response = client.post(f"/api/adapters/{adapter_id}/executions", json={})
    assert response.status_code == 409
    return response.json()["detail"]["code"]


def test_archive_blocked_while_production_active(api_client: TestClient) -> None:
    adapter, _version, _worker = setup_publishable(api_client, "prod-archive-block")
    start(api_client, adapter["id"])

    blocked = api_client.post(f"/api/adapters/{adapter['id']}/archive")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "production_running"

    # M5.1: production_version_id is set while running.
    fetched = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert fetched["production_version_id"] is not None

    stop(api_client, adapter["id"], mode="terminate")

    # M5.1: Stop clears production_version_id.
    fetched = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert fetched["production_version_id"] is None

    assert api_client.post(f"/api/adapters/{adapter['id']}/archive").status_code == 200


# --- Clone -------------------------------------------------------------------------------


def test_clone_copies_working_copy_and_bindings(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter = create_adapter(api_client, name="clone-source", description="src")
    v1 = save_version(api_client, adapter["id"], code="# v1")
    v2 = save_version(
        api_client, adapter["id"], code="# v2", requirements="requests\n", runtime_config={"a": 1}
    )
    with session_factory() as session:
        credential = Credential(name="clone-cred", type="secret", ciphertext="encrypted")
        session.add(credential)
        session.flush()
        session.add(
            AdapterCredentialBinding(
                adapter_id=adapter["id"],
                env_key="DB_PASSWORD",
                credential_id=credential.id,
                field="value",
            )
        )
        session.commit()
        credential_id = credential.id

    response = api_client.post(f"/api/adapters/{adapter['id']}/clone", json={"name": "clone-copy"})
    assert response.status_code == 201, response.text
    clone = response.json()
    assert clone["description"] == "src"
    assert clone["language"] == "python"
    assert clone["published_version_id"] is None
    assert clone["production_state"] == "idle"
    assert clone["production_worker_id"] is None
    assert clone["production_version_id"] is None
    assert clone["archived_at"] is None

    versions = api_client.get(f"/api/adapters/{clone['id']}/versions").json()
    assert len(versions) == 1
    assert versions[0]["seq"] == 1
    detail = api_client.get(f"/api/adapters/{clone['id']}/versions/{versions[0]['id']}").json()
    assert detail["code"] == v2["code"], "the working copy (latest) becomes v1"
    assert detail["requirements"] == "requests\n"
    assert detail["runtime_config"] == {"a": 1}
    assert v1["id"] != versions[0]["id"]

    with session_factory() as session:
        bindings = session.scalars(
            select(AdapterCredentialBinding).where(
                AdapterCredentialBinding.adapter_id == clone["id"]
            )
        ).all()
    assert len(bindings) == 1
    assert bindings[0].env_key == "DB_PASSWORD"
    assert bindings[0].credential_id == credential_id
    assert bindings[0].field == "value"

    conflict = api_client.post(f"/api/adapters/{adapter['id']}/clone", json={"name": "clone-copy"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "adapter_name_conflict"


# --- test-run scheduling -------------------------------------------------------------------


def test_test_run_rejected_without_production_worker_on_multi_worker_setup(
    api_client: TestClient,
) -> None:
    adapter = create_adapter(api_client, name="test-multi")
    save_version(api_client, adapter["id"])
    register_worker(api_client, name="multi-a")
    register_worker(api_client, name="multi-b")

    response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "production_worker_required"


def test_single_online_worker_adopts_test_runs(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="test-single")
    save_version(api_client, adapter["id"])
    worker = register_worker(api_client, name="single-a")

    execution = create_execution(api_client, adapter["id"])
    assert execution["target_worker_id"] == worker["id"]
    fetched = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert fetched["production_worker_id"] == worker["id"], "adoption is written back"


def test_only_active_production_execution_counts(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """The DB unique index forbids a second active production-class Execution."""
    adapter, version, worker = setup_publishable(api_client, "prod-unique")
    start(api_client, adapter["id"])

    # M5.1: Start no longer creates an Execution; insert one manually.
    with session_factory() as session:
        session.add(
            Execution(
                adapter_id=adapter["id"],
                version_id=version["id"],
                trigger="production",
                status="pending",
                target_worker_id=worker["id"],
                input={},
            )
        )
        session.commit()

    # A second active production-class Execution must conflict.
    with pytest.raises(IntegrityError), session_factory() as session:
        session.add(
            Execution(
                adapter_id=adapter["id"],
                version_id=version["id"],
                trigger="schedule",
                status="pending",
                target_worker_id=worker["id"],
                input={},
            )
        )
        session.commit()


# --- M5.1 new tests ---------------------------------------------------------------


def test_start_does_not_create_execution(api_client: TestClient) -> None:
    """M5.1: Start succeeds but no production Executions exist afterwards."""
    adapter, _version, _worker = setup_publishable(api_client, "m51-no-exec")
    response = start(api_client, adapter["id"])
    assert response.status_code == 200

    # Only manual test runs from the publish gate exist.
    history = api_client.get(f"/api/adapters/{adapter['id']}/executions").json()
    assert all(item["trigger"] == "manual" for item in history["items"])


def test_start_locks_published_version(api_client: TestClient) -> None:
    """M5.1: production_version_id == published_version_id after Start."""
    adapter, version, _worker = setup_publishable(api_client, "m51-lock-ver")
    response = start(api_client, adapter["id"])
    assert response.status_code == 200
    body = response.json()
    assert body["production_version_id"] == version["id"]
    assert body["production_version_id"] == body["published_version_id"]


def test_publish_during_running_does_not_change_production_version(
    api_client: TestClient,
) -> None:
    """Publishing a new version while running must not change production_version_id."""
    adapter, v1, worker = setup_publishable(api_client, "m51-publish-run")
    start(api_client, adapter["id"])
    before = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert before["production_version_id"] == v1["id"]

    v2 = save_version(api_client, adapter["id"])
    pass_publish_gate(api_client, adapter["id"], v2["id"], worker_name=worker["name"])
    assert publish(api_client, adapter["id"], v2["id"]).status_code == 200

    after = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert after["production_version_id"] == v1["id"], "production version unchanged"
    assert after["published_version_id"] == v2["id"], "published version updated"


def test_stop_then_start_locks_new_version(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """Stop -> Start after a new publish locks the new version."""
    adapter, v1, worker = setup_publishable(api_client, "m51-stop-start")
    start(api_client, adapter["id"])
    exec_id = create_production_execution(session_factory, adapter["id"], v1["id"], worker["id"])
    assert claim(api_client, worker["id"]).status_code == 200
    assert finish_execution(api_client, worker["id"], exec_id).status_code == 200

    v2 = save_version(api_client, adapter["id"])
    pass_publish_gate(api_client, adapter["id"], v2["id"], worker_name=worker["name"])
    assert publish(api_client, adapter["id"], v2["id"]).status_code == 200

    assert stop(api_client, adapter["id"]).status_code == 200
    stopped = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert stopped["production_version_id"] is None

    restarted = start(api_client, adapter["id"])
    assert restarted.status_code == 200
    started_body = restarted.json()
    assert started_body["production_version_id"] == v2["id"]


def test_running_worker_id_change_blocked(api_client: TestClient) -> None:
    """Changing the production Worker while running returns 409."""
    adapter, _version, _worker = setup_publishable(api_client, "m51-worker-lock")
    start(api_client, adapter["id"])

    new_worker = register_worker(api_client, name="m51-new-worker")
    response = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"production_worker_id": new_worker["id"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "production_running"

    # Same value (no actual change) is accepted.
    same = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"production_worker_id": _worker["id"]},
    )
    assert same.status_code == 200


def test_stop_clears_production_version_id(api_client: TestClient) -> None:
    """Stop sets production_version_id to null."""
    adapter, _version, _worker = setup_publishable(api_client, "m51-stop-clear")
    start(api_client, adapter["id"])
    before = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert before["production_version_id"] is not None

    assert stop(api_client, adapter["id"]).status_code == 200
    after = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert after["production_version_id"] is None


def test_db_accepts_schedule_webhook_triggers(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """The DB check constraint accepts schedule and webhook trigger values."""
    adapter = create_adapter(api_client, name="m51-triggers")
    version = save_version(api_client, adapter["id"])
    worker = register_worker(api_client, name="m51-tw")

    for trigger_value in ("production", "schedule", "webhook", "manual"):
        with session_factory() as session:
            session.add(
                Execution(
                    adapter_id=adapter["id"],
                    version_id=version["id"],
                    trigger=trigger_value,
                    status="succeeded",
                    target_worker_id=worker["id"],
                    input={},
                )
            )
            session.commit()

    with session_factory() as session:
        rows = session.scalars(select(Execution)).all()
        assert len(rows) == 4


def test_unique_constraint_covers_all_production_triggers(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """production/schedule/webhook cannot have simultaneous active Executions."""
    adapter, version, worker = setup_publishable(api_client, "m51-uq-triggers")
    start(api_client, adapter["id"])

    with session_factory() as session:
        session.add(
            Execution(
                adapter_id=adapter["id"],
                version_id=version["id"],
                trigger="schedule",
                status="pending",
                target_worker_id=worker["id"],
                input={},
            )
        )
        session.commit()

    # A webhook Execution must now conflict with the active schedule one.
    with pytest.raises(IntegrityError), session_factory() as session:
        session.add(
            Execution(
                adapter_id=adapter["id"],
                version_id=version["id"],
                trigger="webhook",
                status="running",
                target_worker_id=worker["id"],
                input={},
            )
        )
        session.commit()


def test_manual_execution_not_constrained(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """Manual Executions are never constrained by the production unique index."""
    adapter, version, worker = setup_publishable(api_client, "m51-manual-ok")
    start(api_client, adapter["id"])

    with session_factory() as session:
        session.add(
            Execution(
                adapter_id=adapter["id"],
                version_id=version["id"],
                trigger="production",
                status="pending",
                target_worker_id=worker["id"],
                input={},
            )
        )
        session.commit()

    # A manual Execution coexists with the active production one.
    with session_factory() as session:
        session.add(
            Execution(
                adapter_id=adapter["id"],
                version_id=version["id"],
                trigger="manual",
                status="pending",
                target_worker_id=worker["id"],
                input={},
            )
        )
        session.commit()

    with session_factory() as session:
        rows = session.scalars(select(Execution)).all()
        # 1 manual test run from setup_publishable + 1 production + 1 manual
        assert len(rows) == 3
        assert sum(1 for r in rows if r.trigger == "manual") == 2
        assert sum(1 for r in rows if r.trigger == "production") == 1


# --- M5.1: Start / Worker PATCH Adapter row-lock serialization ---------------------


def test_start_wins_adapter_lock_and_worker_patch_rechecks_running(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """Start holds the Adapter row lock: a concurrent Worker PATCH must queue on
    the same lock and then see production_state=running (409), so the Worker can
    never be swapped underneath an already-started production entry.
    """
    adapter, version, worker = setup_publishable(api_client, "m51-lock-start-wins")
    intruder = register_worker(api_client, name="m51-lock-intruder")
    adapter_id = adapter["id"]

    start_has_lock = threading.Event()
    allow_start_commit = threading.Event()
    patch_ready = threading.Event()
    patch_backend_pids: list[int] = []
    patch_codes: list[str] = []
    errors: list[BaseException] = []

    def start_racer() -> None:
        with session_factory() as session:
            original_commit = session.commit

            def coordinated_commit() -> None:
                # start_production holds the Adapter row lock at commit time.
                start_has_lock.set()
                if not allow_start_commit.wait(timeout=5):
                    raise AssertionError("Worker PATCH racer did not release Start commit")
                original_commit()

            session.commit = coordinated_commit  # type: ignore[method-assign]
            try:
                adapter_service.start_production(session, adapter_id)
            except BaseException as exc:  # noqa: BLE001 - reported in the test thread
                errors.append(exc)

    def patch_racer() -> None:
        with session_factory() as session:
            try:
                backend_pid = session.scalar(select(func.pg_backend_pid()))
                assert isinstance(backend_pid, int)
                patch_backend_pids.append(backend_pid)
                patch_ready.set()
                adapter_service.update_adapter(
                    session,
                    adapter_id,
                    AdapterUpdate(production_worker_id=intruder["id"]),
                )
            except HTTPException as exc:
                patch_codes.append(exc.detail["code"])
            except BaseException as exc:  # noqa: BLE001 - reported in the test thread
                errors.append(exc)

    start_thread = threading.Thread(target=start_racer)
    patch_thread = threading.Thread(target=patch_racer)
    start_thread.start()
    assert start_has_lock.wait(timeout=5), "Start did not acquire the Adapter row lock"
    patch_thread.start()
    try:
        assert patch_ready.wait(timeout=5), "Worker PATCH did not open its transaction"
        with session_factory() as monitor:
            # PATCH must queue on the very same Adapter row lock Start holds.
            wait_for_postgres_lock(monitor, patch_backend_pids[0])
    finally:
        allow_start_commit.set()
    start_thread.join(timeout=10)
    patch_thread.join(timeout=10)

    assert not start_thread.is_alive()
    assert not patch_thread.is_alive()
    assert errors == []
    assert patch_codes == ["production_running"]
    with session_factory() as session:
        stored = session.get(Adapter, adapter_id)
        assert stored is not None
        assert stored.production_state == "running"
        assert stored.production_worker_id == worker["id"]
        assert stored.production_version_id == version["id"]


def test_worker_patch_wins_adapter_lock_and_start_revalidates_gate(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """PATCH holds the Adapter row lock: Start must queue on the same lock and
    then re-evaluate every gate against the new Worker, never starting with a
    version that was never tested on that Worker.
    """
    adapter, _version, _worker = setup_publishable(api_client, "m51-lock-patch-wins")
    intruder = register_worker(api_client, name="m51-lock-patch-intruder")
    adapter_id = adapter["id"]

    patch_has_lock = threading.Event()
    allow_patch_commit = threading.Event()
    start_ready = threading.Event()
    start_backend_pids: list[int] = []
    start_codes: list[str] = []
    errors: list[BaseException] = []

    def patch_racer() -> None:
        with session_factory() as session:
            original_commit = session.commit

            def coordinated_commit() -> None:
                # update_adapter holds the Adapter row lock at commit time.
                patch_has_lock.set()
                if not allow_patch_commit.wait(timeout=5):
                    raise AssertionError("Start racer did not release PATCH commit")
                original_commit()

            session.commit = coordinated_commit  # type: ignore[method-assign]
            try:
                adapter_service.update_adapter(
                    session,
                    adapter_id,
                    AdapterUpdate(production_worker_id=intruder["id"]),
                )
            except BaseException as exc:  # noqa: BLE001 - reported in the test thread
                errors.append(exc)

    def start_racer() -> None:
        with session_factory() as session:
            try:
                backend_pid = session.scalar(select(func.pg_backend_pid()))
                assert isinstance(backend_pid, int)
                start_backend_pids.append(backend_pid)
                start_ready.set()
                adapter_service.start_production(session, adapter_id)
            except HTTPException as exc:
                start_codes.append(exc.detail["code"])
            except BaseException as exc:  # noqa: BLE001 - reported in the test thread
                errors.append(exc)

    patch_thread = threading.Thread(target=patch_racer)
    start_thread = threading.Thread(target=start_racer)
    patch_thread.start()
    assert patch_has_lock.wait(timeout=5), "PATCH did not acquire the Adapter row lock"
    start_thread.start()
    try:
        assert start_ready.wait(timeout=5), "Start did not open its transaction"
        with session_factory() as monitor:
            # Start must queue on the very same Adapter row lock PATCH holds.
            wait_for_postgres_lock(monitor, start_backend_pids[0])
    finally:
        allow_patch_commit.set()
    patch_thread.join(timeout=10)
    start_thread.join(timeout=10)

    assert not patch_thread.is_alive()
    assert not start_thread.is_alive()
    assert errors == []
    assert start_codes == ["production_test_required"], (
        "Start must re-validate the gate against the Worker PATCH installed"
    )
    with session_factory() as session:
        stored = session.get(Adapter, adapter_id)
        assert stored is not None
        assert stored.production_state != "running"
        assert stored.production_worker_id == intruder["id"]
        assert stored.production_version_id is None
