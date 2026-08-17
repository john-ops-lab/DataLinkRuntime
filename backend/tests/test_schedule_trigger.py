"""M5.4.1 Schedule integration with latest Revision and runtime lock."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import AdapterSchedule, Execution, Worker
from dlr.control.services.schedule import latest_due_point, next_run_after, scheduler_tick
from test_adapters import create_adapter, save_version
from test_workers import register_worker

BASE = datetime.now(UTC)


def put_schedule(client: TestClient, adapter_id: int, **overrides: object):
    payload: dict[str, object] = {
        "enabled": True,
        "cron": "0 * * * *",
        "timezone": "UTC",
        "input": None,
    }
    payload.update(overrides)
    return client.put(f"/api/adapters/{adapter_id}/schedule", json=payload)


def get_schedule(client: TestClient, adapter_id: int):
    return client.get(f"/api/adapters/{adapter_id}/schedule")


def setup_task(client: TestClient, name: str) -> tuple[dict, dict, dict]:
    worker = register_worker(client, name=f"{name}-worker")
    adapter = create_adapter(client, name=name, adapter_type="task")
    version = save_version(client, adapter["id"])
    response = client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"run_mode": "schedule"},
    )
    assert response.status_code == 200, response.text
    return response.json(), version, worker


def set_cursor(session_factory: sessionmaker[Session], adapter_id: int, at: datetime) -> None:
    with session_factory.begin() as session:
        session.execute(
            update(AdapterSchedule)
            .where(AdapterSchedule.adapter_id == adapter_id)
            .values(next_run_at=at)
        )


def schedule_executions(session_factory: sessionmaker[Session], adapter_id: int) -> list[Execution]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(Execution)
                .where(Execution.adapter_id == adapter_id, Execution.trigger == "schedule")
                .order_by(Execution.id)
            ).all()
        )


def finish_active(session_factory: sessionmaker[Session], adapter_id: int) -> None:
    with session_factory.begin() as session:
        session.execute(
            update(Execution)
            .where(
                Execution.adapter_id == adapter_id,
                Execution.status.in_(("pending", "running")),
            )
            .values(status="succeeded", ended_at=func.now())
        )


def test_schedule_is_task_only_and_get_before_configuration_is_404(
    api_client: TestClient,
) -> None:
    task = create_adapter(api_client, name="schedule-empty", adapter_type="task")
    assert (
        get_schedule(api_client, task["id"]).json()["detail"]["code"] == "schedule_not_configured"
    )
    webhook = create_adapter(api_client, name="schedule-wrong-type", adapter_type="webhook")
    response = put_schedule(api_client, webhook["id"])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_type_mismatch"

    response = put_schedule(api_client, task["id"])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "schedule_mode_required"


def test_schedule_validation_and_input_cap(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _, _ = setup_task(api_client, "schedule-validation")
    assert put_schedule(api_client, adapter["id"], cron="* * *").status_code == 422
    assert put_schedule(api_client, adapter["id"], timezone="Mars/Olympus").status_code == 422
    monkeypatch.setattr(settings, "execution_input_max_bytes", 32)
    too_large = put_schedule(api_client, adapter["id"], input="x" * 100)
    assert too_large.status_code == 413
    assert get_schedule(api_client, adapter["id"]).status_code == 404


def test_schedule_enable_requires_saved_revision_and_configured_worker(
    api_client: TestClient,
) -> None:
    worker = register_worker(api_client, name="schedule-prerequisite-worker")
    no_revision = create_adapter(api_client, name="schedule-no-revision", adapter_type="task")
    configured = api_client.patch(
        f"/api/adapters/{no_revision['id']}",
        json={"run_mode": "schedule", "runtime_worker_id": worker["id"]},
    )
    assert configured.status_code == 200, configured.text

    rejected = put_schedule(api_client, no_revision["id"])
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == {
        "code": "adapter_has_no_version",
        "message": "Save a Revision before enabling Schedule",
    }
    assert get_schedule(api_client, no_revision["id"]).status_code == 404
    assert api_client.get(f"/api/adapters/{no_revision['id']}").json()["runtime_locked"] is False

    no_worker = create_adapter(api_client, name="schedule-no-worker", adapter_type="task")
    save_version(api_client, no_worker["id"])
    configured = api_client.patch(
        f"/api/adapters/{no_worker['id']}",
        json={"run_mode": "schedule", "runtime_worker_id": None},
    )
    assert configured.status_code == 200, configured.text

    rejected = put_schedule(api_client, no_worker["id"])
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == {
        "code": "runtime_worker_required",
        "message": "Select a runtime Worker before enabling Schedule",
    }
    assert get_schedule(api_client, no_worker["id"]).status_code == 404
    assert api_client.get(f"/api/adapters/{no_worker['id']}").json()["runtime_locked"] is False


def test_enabled_schedule_locks_configuration_but_can_be_disabled(
    api_client: TestClient,
) -> None:
    adapter, _, _ = setup_task(api_client, "schedule-lock")
    created = put_schedule(
        api_client,
        adapter["id"],
        cron="*/5 * * * *",
        timezone="Asia/Shanghai",
        input={"kind": "full"},
    )
    assert created.status_code == 200, created.text
    assert api_client.get(f"/api/adapters/{adapter['id']}").json()["runtime_locked"] is True

    changed = put_schedule(
        api_client,
        adapter["id"],
        cron="*/10 * * * *",
        timezone="Asia/Shanghai",
        input={"kind": "full"},
    )
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "adapter_runtime_locked"

    disabled = put_schedule(
        api_client,
        adapter["id"],
        enabled=False,
        cron="*/5 * * * *",
        timezone="Asia/Shanghai",
        input={"kind": "full"},
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["next_run_at"] is None
    assert api_client.get(f"/api/adapters/{adapter['id']}").json()["runtime_locked"] is False


def test_due_schedule_uses_latest_revision_and_runtime_worker(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, version, worker = setup_task(api_client, "schedule-due")
    assert put_schedule(api_client, adapter["id"], input={"scheduled": True}).status_code == 200
    set_cursor(session_factory, adapter["id"], BASE - timedelta(minutes=1))

    with session_factory() as session:
        assert scheduler_tick(session, now=BASE) == 1
    rows = schedule_executions(session_factory, adapter["id"])
    assert len(rows) == 1
    assert rows[0].version_id == version["id"]
    assert rows[0].target_worker_id == worker["id"]
    assert rows[0].input == {"scheduled": True}


def test_scheduled_execution_payload_carries_adapter_timeout(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """M5.5.11: Task schedule runs share the Adapter-level execution timeout."""
    from test_workers import claim

    adapter, _, worker = setup_task(api_client, "schedule-timeout")
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"timeout_seconds": 2700},
        ).status_code
        == 200
    )
    assert put_schedule(api_client, adapter["id"]).status_code == 200
    set_cursor(session_factory, adapter["id"], BASE - timedelta(minutes=1))
    with session_factory() as session:
        assert scheduler_tick(session, now=BASE) == 1

    payload = claim(api_client, worker["id"]).json()
    assert payload["execution_timeout_seconds"] == 2700


def test_disabled_save_then_reenabled_schedule_runs_new_latest_revision(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, first, worker = setup_task(api_client, "schedule-revision")
    assert put_schedule(api_client, adapter["id"]).status_code == 200
    set_cursor(session_factory, adapter["id"], BASE - timedelta(minutes=1))
    with session_factory() as session:
        assert scheduler_tick(session, now=BASE) == 1
    finish_active(session_factory, adapter["id"])

    assert put_schedule(api_client, adapter["id"], enabled=False).status_code == 200
    second = save_version(api_client, adapter["id"], code="# revision 2\n")
    assert put_schedule(api_client, adapter["id"]).status_code == 200
    set_cursor(session_factory, adapter["id"], BASE + timedelta(minutes=59))
    with session_factory.begin() as session:
        session.execute(
            update(Worker)
            .where(Worker.id == worker["id"])
            .values(last_heartbeat=BASE + timedelta(hours=1), status="online")
        )
    with session_factory() as session:
        assert scheduler_tick(session, now=BASE + timedelta(hours=1)) == 1

    rows = schedule_executions(session_factory, adapter["id"])
    assert [row.version_id for row in rows] == [first["id"], second["id"]]


def test_active_manual_execution_holds_schedule_without_creating_second_run(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _, _ = setup_task(api_client, "schedule-busy")
    assert put_schedule(api_client, adapter["id"]).status_code == 200
    manual = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert manual.status_code == 202
    due = BASE - timedelta(minutes=1)
    set_cursor(session_factory, adapter["id"], due)
    with session_factory() as session:
        assert scheduler_tick(session, now=BASE) == 0
    assert schedule_executions(session_factory, adapter["id"]) == []
    assert (
        datetime.fromisoformat(get_schedule(api_client, adapter["id"]).json()["next_run_at"]) == due
    )


def test_disable_keeps_runtime_locked_until_active_manual_execution_finishes(
    api_client: TestClient,
) -> None:
    adapter, _, _ = setup_task(api_client, "schedule-disable-active")
    configured = put_schedule(api_client, adapter["id"])
    assert configured.status_code == 200, configured.text
    next_run_at = configured.json()["next_run_at"]

    manual = api_client.post(
        f"/api/adapters/{adapter['id']}/executions",
        json={"input": {"source": "manual"}},
    )
    assert manual.status_code == 202, manual.text
    unchanged = get_schedule(api_client, adapter["id"]).json()
    assert unchanged["enabled"] is True
    assert unchanged["next_run_at"] == next_run_at

    disabled = put_schedule(api_client, adapter["id"], enabled=False)
    assert disabled.status_code == 200, disabled.text
    state = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert state["runtime_locked"] is True
    assert state["running_execution_id"] == manual.json()["id"]

    save = api_client.post(
        f"/api/adapters/{adapter['id']}/versions",
        json={"code": "# must remain locked\n"},
    )
    assert save.status_code == 409
    assert save.json()["detail"]["code"] == "adapter_runtime_locked"
    mode = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"run_mode": "manual"},
    )
    assert mode.status_code == 409
    assert mode.json()["detail"]["code"] == "adapter_runtime_locked"

    cancelled = api_client.post(f"/api/executions/{manual.json()['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    state = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert state["runtime_locked"] is False


def test_active_schedule_execution_rejects_manual_run_once(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _, _ = setup_task(api_client, "schedule-run-once-busy")
    assert put_schedule(api_client, adapter["id"]).status_code == 200
    set_cursor(session_factory, adapter["id"], BASE - timedelta(minutes=1))
    with session_factory() as session:
        assert scheduler_tick(session, now=BASE) == 1

    manual = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})

    assert manual.status_code == 409
    assert manual.json()["detail"]["code"] == "adapter_busy"


def test_offline_worker_holds_due_point_then_recovers_once(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _, worker = setup_task(api_client, "schedule-offline")
    assert put_schedule(api_client, adapter["id"]).status_code == 200
    due = BASE - timedelta(minutes=1)
    set_cursor(session_factory, adapter["id"], due)
    with session_factory.begin() as session:
        session.execute(
            update(Worker)
            .where(Worker.id == worker["id"])
            .values(last_heartbeat=datetime(2000, 1, 1, tzinfo=UTC), status="online")
        )
    with session_factory() as session:
        assert scheduler_tick(session, now=BASE) == 0
    with session_factory.begin() as session:
        session.execute(
            update(Worker)
            .where(Worker.id == worker["id"])
            .values(last_heartbeat=BASE, status="online")
        )
    with session_factory() as session:
        assert scheduler_tick(session, now=BASE) == 1
    assert len(schedule_executions(session_factory, adapter["id"])) == 1


def test_concurrent_scheduler_ticks_create_one_execution(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _, _ = setup_task(api_client, "schedule-race")
    assert put_schedule(api_client, adapter["id"]).status_code == 200
    set_cursor(session_factory, adapter["id"], BASE - timedelta(minutes=1))
    barrier = threading.Barrier(2)
    created: list[int] = []

    def run_tick() -> None:
        barrier.wait(timeout=5)
        with session_factory() as session:
            created.append(scheduler_tick(session, now=BASE))

    threads = [threading.Thread(target=run_tick) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(created) == 1
    assert len(schedule_executions(session_factory, adapter["id"])) == 1


def test_soft_deleted_adapter_never_triggers(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _, _ = setup_task(api_client, "schedule-deleted")
    assert put_schedule(api_client, adapter["id"], enabled=False).status_code == 200
    assert api_client.delete(f"/api/adapters/{adapter['id']}").status_code == 204
    with session_factory.begin() as session:
        session.execute(
            update(AdapterSchedule)
            .where(AdapterSchedule.adapter_id == adapter["id"])
            .values(enabled=True, next_run_at=BASE - timedelta(minutes=1))
        )
    with session_factory() as session:
        assert scheduler_tick(session, now=BASE) == 0
    assert schedule_executions(session_factory, adapter["id"]) == []


def test_cron_arithmetic_remains_timezone_aware() -> None:
    spring = datetime(2026, 3, 8, 6, 59, tzinfo=UTC)
    next_point = next_run_after("30 2 * * *", "America/New_York", spring)
    assert next_point.tzinfo is not None
    due = latest_due_point("0 * * * *", "UTC", BASE - timedelta(hours=2), BASE)
    assert BASE - timedelta(hours=1) <= due <= BASE
