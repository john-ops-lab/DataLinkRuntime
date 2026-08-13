"""M4.1 effective Worker availability contracts.

These tests keep the stored Worker state separate from the Control-side
effective status.  Heartbeat ages are constructed explicitly; no test waits
for wall-clock time to pass.
"""

import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.common.config import Settings, settings
from dlr.control.models import Execution, Worker
from dlr.control.services.worker_availability import (
    current_time,
    effective_status,
    is_effectively_online,
)
from test_adapters import create_adapter, save_version
from test_production_lifecycle import setup_publishable, start, wait_for_postgres_lock

WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}
FIXED_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
STALE_HEARTBEAT = datetime(2000, 1, 1, tzinfo=UTC)


def register_worker(
    client: TestClient,
    name: str,
    capabilities: list[str] | None = None,
) -> dict:
    response = client.post(
        "/api/workers/register",
        json={"name": name, "capabilities": capabilities or ["python"]},
        headers=WORKER_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()


def set_worker_state(
    session_factory: sessionmaker[Session],
    worker_id: int,
    *,
    status: str = "online",
    last_heartbeat: datetime = STALE_HEARTBEAT,
) -> None:
    with session_factory.begin() as session:
        worker = session.get(Worker, worker_id)
        assert worker is not None
        worker.status = status
        worker.last_heartbeat = last_heartbeat


def execution_count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        count = session.scalar(select(func.count()).select_from(Execution))
        assert count is not None
        return count


def worker_at(*, status: str, last_heartbeat: datetime) -> Worker:
    return Worker(
        name="availability-unit-worker",
        status=status,
        last_heartbeat=last_heartbeat,
        capabilities=["python"],
    )


@pytest.fixture()
def heartbeat_timeout(monkeypatch: pytest.MonkeyPatch) -> float:
    timeout = 30.0
    monkeypatch.setattr(settings, "worker_heartbeat_timeout_seconds", timeout)
    return timeout


def test_stored_online_with_fresh_heartbeat_is_effectively_online(
    heartbeat_timeout: float,
) -> None:
    worker = worker_at(
        status="online",
        last_heartbeat=FIXED_NOW - timedelta(seconds=heartbeat_timeout - 1),
    )

    assert is_effectively_online(worker, now=FIXED_NOW) is True
    assert effective_status(worker, now=FIXED_NOW) == "online"


def test_heartbeat_exactly_at_timeout_boundary_is_online(heartbeat_timeout: float) -> None:
    worker = worker_at(
        status="online",
        last_heartbeat=FIXED_NOW - timedelta(seconds=heartbeat_timeout),
    )

    assert is_effectively_online(worker, now=FIXED_NOW) is True
    assert effective_status(worker, now=FIXED_NOW) == "online"


def test_stored_online_with_stale_heartbeat_is_effectively_offline(
    heartbeat_timeout: float,
) -> None:
    worker = worker_at(
        status="online",
        last_heartbeat=FIXED_NOW - timedelta(seconds=heartbeat_timeout + 1),
    )

    assert is_effectively_online(worker, now=FIXED_NOW) is False
    assert effective_status(worker, now=FIXED_NOW) == "offline"


def test_stored_offline_with_fresh_heartbeat_stays_effectively_offline(
    heartbeat_timeout: float,
) -> None:
    worker = worker_at(
        status="offline",
        last_heartbeat=FIXED_NOW - timedelta(seconds=heartbeat_timeout - 1),
    )

    assert is_effectively_online(worker, now=FIXED_NOW) is False
    assert effective_status(worker, now=FIXED_NOW) == "offline"


def test_heartbeat_age_uses_absolute_time_across_dst_fold(heartbeat_timeout: float) -> None:
    new_york = ZoneInfo("America/New_York")
    worker = worker_at(
        status="online",
        # 01:59:40 EDT = 05:59:40 UTC, immediately before the clock repeats.
        last_heartbeat=datetime(2026, 11, 1, 1, 59, 40, tzinfo=new_york, fold=0),
    )
    # 01:00:20 EST = 06:00:20 UTC: actual heartbeat age is 40 seconds.
    now = datetime(2026, 11, 1, 1, 0, 20, tzinfo=new_york, fold=1)

    assert heartbeat_timeout == 30.0
    assert is_effectively_online(worker, now=now) is False


def test_current_time_is_captured_after_a_transaction_lock_wait(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A lock wait must not leave Test/Start using transaction-start time."""
    worker = register_worker(api_client, "availability-decision-clock")
    holder = session_factory()
    ready = threading.Event()
    state: dict[str, object] = {}

    def wait_for_lock_then_capture_time() -> None:
        try:
            with session_factory() as waiter:
                state["transaction_started"] = waiter.scalar(select(func.now()))
                state["backend_pid"] = waiter.scalar(select(func.pg_backend_pid()))
                ready.set()
                locked = waiter.scalar(
                    select(Worker).where(Worker.id == worker["id"]).with_for_update()
                )
                assert locked is not None
                state["decision_time"] = current_time(waiter)
        except BaseException as error:  # noqa: BLE001 - surfaced in the main test thread
            state["error"] = error
            ready.set()

    waiter_thread = threading.Thread(target=wait_for_lock_then_capture_time)
    try:
        holder.begin()
        assert holder.get(Worker, worker["id"], with_for_update=True) is not None
        waiter_thread.start()
        assert ready.wait(timeout=5), "waiter did not start its transaction"
        if "error" in state:
            raise AssertionError("waiter failed before entering the lock wait") from state["error"]
        backend_pid = state.get("backend_pid")
        assert isinstance(backend_pid, int)
        with session_factory() as monitor:
            wait_for_postgres_lock(monitor, backend_pid)
        holder.commit()
        waiter_thread.join(timeout=5)
    finally:
        holder.rollback()
        holder.close()
        waiter_thread.join(timeout=5)

    assert not waiter_thread.is_alive(), "waiter did not resume after the lock was released"
    if "error" in state:
        raise AssertionError("waiter failed after the lock was released") from state["error"]
    transaction_started = state.get("transaction_started")
    decision_time = state.get("decision_time")
    assert isinstance(transaction_started, datetime)
    assert isinstance(decision_time, datetime)
    assert decision_time > transaction_started


def test_worker_api_reports_stale_stored_online_as_offline_without_rewriting_storage(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker = register_worker(api_client, "availability-api-stale")
    set_worker_state(session_factory, worker["id"])

    response = api_client.get("/api/workers")

    assert response.status_code == 200
    listed = {item["id"]: item for item in response.json()}[worker["id"]]
    assert listed["status"] == "offline"
    assert datetime.fromisoformat(listed["last_heartbeat"]) == STALE_HEARTBEAT
    with session_factory() as session:
        stored = session.get(Worker, worker["id"])
        assert stored is not None
        assert stored.status == "online"
        assert stored.last_heartbeat == STALE_HEARTBEAT


def test_heartbeat_restores_stale_worker_to_effective_online(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker = register_worker(api_client, "availability-heartbeat-restore")
    set_worker_state(session_factory, worker["id"])
    assert {item["id"]: item for item in api_client.get("/api/workers").json()}[worker["id"]][
        "status"
    ] == "offline"

    heartbeat = api_client.post(
        f"/api/workers/{worker['id']}/heartbeat",
        headers=WORKER_HEADERS,
    )

    assert heartbeat.status_code == 204
    listed = {item["id"]: item for item in api_client.get("/api/workers").json()}[worker["id"]]
    assert listed["status"] == "online"
    with session_factory() as session:
        stored = session.get(Worker, worker["id"])
        assert stored is not None
        assert stored.status == "online"
        assert stored.last_heartbeat > STALE_HEARTBEAT


def test_configured_stale_worker_blocks_manual_test_without_execution(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter = create_adapter(api_client, name="availability-test-configured-stale")
    save_version(api_client, adapter["id"])
    worker = register_worker(api_client, "availability-test-configured-worker")
    configured = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"production_worker_id": worker["id"]},
    )
    assert configured.status_code == 200
    set_worker_state(session_factory, worker["id"])

    response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "worker_offline"
    assert execution_count(session_factory) == 0
    assert (
        api_client.get(f"/api/adapters/{adapter['id']}").json()["production_worker_id"]
        == worker["id"]
    )


def test_manual_test_auto_selection_ignores_stale_compatible_worker(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    stale = register_worker(api_client, "availability-test-auto-stale")
    set_worker_state(session_factory, stale["id"])
    fresh = register_worker(api_client, "availability-test-auto-fresh")
    adapter = create_adapter(api_client, name="availability-test-auto")
    save_version(api_client, adapter["id"])

    response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})

    assert response.status_code == 202, response.text
    assert response.json()["target_worker_id"] == fresh["id"]
    assert (
        api_client.get(f"/api/adapters/{adapter['id']}").json()["production_worker_id"]
        == fresh["id"]
    )


def test_manual_test_auto_selection_ignores_stale_worker_when_counting_capabilities(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    stale_compatible = register_worker(api_client, "availability-test-stale-compatible")
    set_worker_state(session_factory, stale_compatible["id"])
    register_worker(
        api_client,
        "availability-test-fresh-incompatible",
        capabilities=["javascript"],
    )
    adapter = create_adapter(api_client, name="availability-test-stale-capability")
    save_version(api_client, adapter["id"])

    response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "worker_capability_missing"
    assert execution_count(session_factory) == 0


def test_manual_test_without_effective_worker_is_offline_and_has_no_execution(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    stale = register_worker(api_client, "availability-test-none-stale")
    set_worker_state(session_factory, stale["id"])
    adapter = create_adapter(api_client, name="availability-test-none")
    save_version(api_client, adapter["id"])

    response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "worker_offline"
    assert execution_count(session_factory) == 0
    assert api_client.get(f"/api/adapters/{adapter['id']}").json()["production_worker_id"] is None


def test_manual_test_with_only_effective_incompatible_workers_reports_capability_missing(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    stale_compatible = register_worker(api_client, "availability-test-cap-stale")
    set_worker_state(session_factory, stale_compatible["id"])
    register_worker(
        api_client,
        "availability-test-cap-javascript",
        capabilities=["javascript"],
    )
    adapter = create_adapter(api_client, name="availability-test-capability")
    save_version(api_client, adapter["id"])

    response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "worker_capability_missing"
    assert execution_count(session_factory) == 0


def test_manual_test_with_multiple_effective_compatible_workers_requires_selection(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    register_worker(api_client, "availability-test-multi-a")
    register_worker(api_client, "availability-test-multi-b")
    adapter = create_adapter(api_client, name="availability-test-multi")
    save_version(api_client, adapter["id"])

    response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "production_worker_required"
    assert execution_count(session_factory) == 0


def test_configured_stale_worker_blocks_start_without_state_or_execution_changes(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _version, worker = setup_publishable(
        api_client,
        "availability-start-configured-stale",
        worker_name="availability-start-configured-worker",
    )
    set_worker_state(session_factory, worker["id"])
    before = api_client.get(f"/api/adapters/{adapter['id']}").json()
    before_count = execution_count(session_factory)

    response = start(api_client, adapter["id"])

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "worker_offline"
    after = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert execution_count(session_factory) == before_count
    for field in (
        "latest_version_id",
        "published_version_id",
        "production_worker_id",
        "production_state",
        "running_execution_id",
    ):
        assert after[field] == before[field]


def test_start_auto_selection_ignores_stale_worker(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _version, fresh = setup_publishable(
        api_client,
        "availability-start-auto",
        worker_name="availability-start-auto-fresh",
    )
    stale_compatible = register_worker(api_client, "availability-start-auto-stale")
    set_worker_state(session_factory, stale_compatible["id"])
    cleared = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"production_worker_id": None},
    )
    assert cleared.status_code == 200

    response = start(api_client, adapter["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["production_worker_id"] == fresh["id"]
    assert body["production_state"] == "running"
    fetched = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert fetched["production_worker_id"] == fresh["id"]
    assert fetched["production_state"] == "running"


def test_start_without_effective_worker_is_offline_and_has_no_side_effects(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _version, worker = setup_publishable(
        api_client,
        "availability-start-none",
        worker_name="availability-start-none-worker",
    )
    cleared = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"production_worker_id": None},
    )
    assert cleared.status_code == 200
    set_worker_state(session_factory, worker["id"])
    before = api_client.get(f"/api/adapters/{adapter['id']}").json()
    before_count = execution_count(session_factory)

    response = start(api_client, adapter["id"])

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "worker_offline"
    assert execution_count(session_factory) == before_count
    after = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert after["production_worker_id"] is None
    assert after["production_state"] == before["production_state"]
    assert after["published_version_id"] == before["published_version_id"]


def test_start_with_only_effective_incompatible_worker_reports_capability_missing(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _version, worker = setup_publishable(
        api_client,
        "availability-start-capability",
        worker_name="availability-start-capability-worker",
    )
    cleared = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"production_worker_id": None},
    )
    assert cleared.status_code == 200
    set_worker_state(session_factory, worker["id"])
    register_worker(
        api_client,
        "availability-start-capability-javascript",
        capabilities=["javascript"],
    )
    before_count = execution_count(session_factory)

    response = start(api_client, adapter["id"])

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "worker_capability_missing"
    assert execution_count(session_factory) == before_count
    fetched = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert fetched["production_worker_id"] is None
    assert fetched["production_state"] == "idle"


def test_start_with_multiple_effective_compatible_workers_requires_selection(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _version, first = setup_publishable(
        api_client,
        "availability-start-multi",
        worker_name="availability-start-multi-a",
    )
    second = register_worker(api_client, "availability-start-multi-b")
    assert second["id"] != first["id"]
    cleared = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"production_worker_id": None},
    )
    assert cleared.status_code == 200
    before_count = execution_count(session_factory)

    response = start(api_client, adapter["id"])

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "production_worker_required"
    assert execution_count(session_factory) == before_count
    fetched = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert fetched["production_worker_id"] is None
    assert fetched["production_state"] == "idle"


def test_worker_heartbeat_timeout_settings_default_and_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DLR_WORKER_HEARTBEAT_TIMEOUT_SECONDS", raising=False)
    assert Settings().worker_heartbeat_timeout_seconds == 30.0

    monkeypatch.setenv("DLR_WORKER_HEARTBEAT_TIMEOUT_SECONDS", "47.5")
    assert Settings().worker_heartbeat_timeout_seconds == 47.5


@pytest.mark.parametrize("value", ["0", "-0.1", "-1", "inf", "-inf", "nan"])
def test_worker_heartbeat_timeout_settings_reject_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("DLR_WORKER_HEARTBEAT_TIMEOUT_SECONDS", value)

    with pytest.raises(ValidationError):
        Settings()


def test_large_finite_heartbeat_timeout_does_not_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "worker_heartbeat_timeout_seconds", 1e300)
    worker = worker_at(status="online", last_heartbeat=STALE_HEARTBEAT)

    assert is_effectively_online(worker, now=FIXED_NOW) is True


def test_adapter_settings_can_prebind_stale_or_stored_offline_worker(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    stale = register_worker(api_client, "availability-prebind-stale")
    offline = register_worker(api_client, "availability-prebind-offline")
    set_worker_state(session_factory, stale["id"])
    set_worker_state(
        session_factory,
        offline["id"],
        status="offline",
        last_heartbeat=FIXED_NOW,
    )
    adapter = create_adapter(api_client, name="availability-prebind")

    stale_response = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"production_worker_id": stale["id"]},
    )
    offline_response = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"production_worker_id": offline["id"]},
    )

    assert stale_response.status_code == 200
    assert stale_response.json()["production_worker_id"] == stale["id"]
    assert offline_response.status_code == 200
    assert offline_response.json()["production_worker_id"] == offline["id"]
