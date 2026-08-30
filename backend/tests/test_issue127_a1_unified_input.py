"""Issue #127 A1 red/green contract tests for unified Task input execution."""

import logging
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import AdapterInputConfig, AdapterSchedule, Execution
from dlr.control.schemas.execution import ExecutionCreate
from dlr.control.services import execution as execution_service
from dlr.control.services.execution import compact_json_bytes
from dlr.control.services.schedule import latest_due_point, scheduler_tick
from test_adapters import create_adapter, save_version
from test_workers import register_worker

BASE = datetime.now(UTC)


def put_schedule(client: TestClient, adapter_id: int, **overrides: object):
    payload: dict[str, object] = {
        "enabled": True,
        "cron": "* * * * *",
        "timezone": "UTC",
    }
    payload.update(overrides)
    return client.put(f"/api/adapters/{adapter_id}/schedule", json=payload)


def setup_task(client: TestClient, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    worker = register_worker(client, name=f"{name}-worker")
    adapter = create_adapter(client, name=name)
    save_version(client, adapter["id"])
    return adapter, worker


def save_input(client: TestClient, adapter_id: int, value: Any) -> dict[str, Any]:
    response = client.put(
        f"/api/adapters/{adapter_id}/input-config",
        json={"expected_revision": 1, "source_type": "json", "json_value": value},
    )
    assert response.status_code == 200, response.text
    return response.json()


def set_cursor(session_factory: sessionmaker[Session], adapter_id: int, at: datetime) -> None:
    with session_factory.begin() as session:
        session.execute(
            update(AdapterSchedule)
            .where(AdapterSchedule.adapter_id == adapter_id)
            .values(next_run_at=at)
        )


def _integrity_error(constraint_name: str) -> IntegrityError:
    return IntegrityError(
        "INSERT INTO executions ...",
        {},
        SimpleNamespace(diag=SimpleNamespace(constraint_name=constraint_name)),
    )


def schedule_state(session_factory: sessionmaker[Session], adapter_id: int) -> tuple[Any, ...]:
    with session_factory() as session:
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter_id)
        )
        assert schedule is not None
        return (
            schedule.enabled,
            schedule.cron,
            schedule.timezone,
            schedule.input,
            schedule.next_run_at,
            schedule.last_processed_due_at,
        )


@pytest.mark.parametrize(
    ("source_type", "value"),
    [
        ("none", None),
        ("json", {}),
        ("json", [1, "two"]),
        ("json", "scalar"),
        ("json", 7),
        ("json", True),
        ("json", None),
    ],
)
def test_manual_and_scheduler_use_one_saved_input_snapshot(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    source_type: str,
    value: Any,
) -> None:
    manual_adapter, _ = setup_task(api_client, f"a1-resolver-manual-{source_type}-{str(value)[:4]}")
    revision = 1
    if source_type == "json":
        saved = save_input(api_client, manual_adapter["id"], value)
        revision = saved["revision"]

    manual = api_client.post(f"/api/adapters/{manual_adapter['id']}/executions", json={})
    assert manual.status_code == 202, manual.text
    manual_body = manual.json()
    assert manual_body["input"] is value or manual_body["input"] == value
    assert manual_body["input_source_type"] == source_type
    assert manual_body["input_config_revision"] == revision
    assert manual_body["input_snapshot"] == {
        "source_type": source_type,
        "revision": revision,
    }

    api_client.post(f"/api/executions/{manual_body['id']}/cancel")
    schedule_adapter, _ = setup_task(
        api_client, f"a1-resolver-schedule-{source_type}-{str(value)[:4]}"
    )
    if source_type == "json":
        save_input(api_client, schedule_adapter["id"], value)
    mode = api_client.patch(
        f"/api/adapters/{schedule_adapter['id']}", json={"run_mode": "schedule"}
    )
    assert mode.status_code == 200, mode.text
    configured = (
        put_schedule(api_client, schedule_adapter["id"], enabled=True, input=value)
        if source_type == "json"
        else put_schedule(api_client, schedule_adapter["id"], enabled=True)
    )
    assert configured.status_code == 200, configured.text
    set_cursor(session_factory, schedule_adapter["id"], BASE - timedelta(minutes=1))
    with session_factory() as session:
        assert scheduler_tick(session, now=BASE) == 1
        scheduled = session.scalar(
            select(Execution).where(
                Execution.adapter_id == schedule_adapter["id"],
                Execution.trigger == "schedule",
            )
        )
        assert scheduled is not None
        assert scheduled.input == value
        assert scheduled.input_source_type == source_type
        assert scheduled.input_config_revision == revision
        assert scheduled.input_snapshot == {
            "source_type": source_type,
            "revision": revision,
        }


def test_schedule_run_now_uses_saved_input_and_does_not_move_cursor(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _ = setup_task(api_client, "a1-run-now-cursor")
    saved = save_input(api_client, adapter["id"], {"saved": "value"})
    mode = api_client.patch(f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"})
    assert mode.status_code == 200, mode.text
    configured = put_schedule(
        api_client,
        adapter["id"],
        cron="*/5 * * * *",
        timezone="America/New_York",
        input={"saved": "value"},
    )
    assert configured.status_code == 200, configured.text
    before = schedule_state(session_factory, adapter["id"])

    response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["trigger"] == "manual"
    assert body["input"] == {"saved": "value"}
    assert body["input_config_revision"] == saved["revision"]
    assert schedule_state(session_factory, adapter["id"]) == before


def test_manual_execution_maps_only_the_known_active_unique_constraint(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _ = setup_task(api_client, "a1-integrity-boundary")

    def fail_with_unknown(*_args: object, **_kwargs: object) -> None:
        raise _integrity_error("unrelated_data_integrity_constraint")

    monkeypatch.setattr(execution_service, "_create_execution_locked", fail_with_unknown)
    with session_factory() as session, pytest.raises(IntegrityError):
        execution_service.create_execution(session, adapter["id"], ExecutionCreate())

    def fail_with_active(*_args: object, **_kwargs: object) -> None:
        raise _integrity_error("uq_executions_active_adapter")

    monkeypatch.setattr(execution_service, "_create_execution_locked", fail_with_active)
    with session_factory() as session, pytest.raises(HTTPException) as caught:
        execution_service.create_execution(session, adapter["id"], ExecutionCreate())
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "adapter_busy"


def test_legacy_override_is_one_shot_logged_and_disabled_rejects_null(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter, _ = setup_task(api_client, "a1-legacy-compat")
    saved = save_input(api_client, adapter["id"], {"saved": True})

    with caplog.at_level(logging.INFO, logger="dlr.control.execution"):
        override = api_client.post(
            f"/api/adapters/{adapter['id']}/executions",
            json={"input": {"legacy": "EXAMPLE_ONLY"}},
        )
    assert override.status_code == 202, override.text
    assert override.json()["input"] == {"legacy": "EXAMPLE_ONLY"}
    assert override.json()["input_snapshot"] == {
        "source_type": "json",
        "revision": saved["revision"],
        "legacy_override": True,
    }
    assert any("legacy_input_compat" in record.getMessage() for record in caplog.records)
    api_client.post(f"/api/executions/{override.json()['id']}/cancel")
    assert (
        api_client.get(f"/api/adapters/{adapter['id']}/input-config").json()["revision"]
        == saved["revision"]
    )

    monkeypatch.setattr(settings, "legacy_input_compat_enabled", False, raising=False)
    rejected = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={"input": None})
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "execution_input_override_not_supported"
    omitted = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert omitted.status_code == 202, omitted.text
    assert omitted.json()["input"] == {"saved": True}
    api_client.post(f"/api/executions/{omitted.json()['id']}/cancel")

    monkeypatch.setattr(settings, "legacy_input_compat_enabled", True, raising=False)
    restored = api_client.post(
        f"/api/adapters/{adapter['id']}/executions",
        json={"input": {"restored": "EXAMPLE_ONLY"}},
    )
    assert restored.status_code == 202, restored.text
    assert restored.json()["input"] == {"restored": "EXAMPLE_ONLY"}


def test_json_input_exact_size_limit_is_accepted(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _ = setup_task(api_client, "a1-json-exact-limit")
    value = {"exact": "boundary"}
    monkeypatch.setattr(settings, "execution_input_max_bytes", len(compact_json_bytes(value)))
    saved = save_input(api_client, adapter["id"], value)
    response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert response.status_code == 202, response.text
    assert response.json()["input"] == value
    assert response.json()["input_config_revision"] == saved["revision"]


def test_schedule_put_and_input_config_mirror_without_revision_fork(
    api_client: TestClient,
) -> None:
    adapter, _ = setup_task(api_client, "a1-schedule-mirror")
    mode = api_client.patch(f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"})
    assert mode.status_code == 200, mode.text

    legacy = put_schedule(api_client, adapter["id"], enabled=False, input={"legacy": 1})
    assert legacy.status_code == 200, legacy.text
    current = api_client.get(f"/api/adapters/{adapter['id']}/input-config").json()
    assert current["source_type"] == "json"
    assert current["json_value"] == {"legacy": 1}
    assert current["revision"] == 2

    new_value = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": current["revision"],
            "source_type": "json",
            "json_value": {"new": 2},
        },
    )
    assert new_value.status_code == 200, new_value.text
    schedule = api_client.get(f"/api/adapters/{adapter['id']}/schedule").json()
    assert schedule["input"] == {"new": 2}

    stale = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={"expected_revision": current["revision"], "source_type": "json", "json_value": {}},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "input_config_revision_conflict"
    assert api_client.get(f"/api/adapters/{adapter['id']}/schedule").json()["input"] == {"new": 2}


def test_invalid_schedule_consumes_due_point_and_persists_block(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _ = setup_task(api_client, "a1-invalid-cursor")
    mode = api_client.patch(f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"})
    assert mode.status_code == 200, mode.text
    configured = put_schedule(api_client, adapter["id"])
    assert configured.status_code == 200, configured.text
    due = BASE - timedelta(minutes=1)
    expected_due = latest_due_point("* * * * *", "UTC", due, BASE)
    set_cursor(session_factory, adapter["id"], due)
    with session_factory.begin() as session:
        config = session.get(AdapterInputConfig, adapter["id"])
        assert config is not None
        config.source_type = "managed_files"
        config.json_value = None

    with session_factory() as session:
        assert scheduler_tick(session, now=BASE) == 0
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert schedule is not None
        assert schedule.last_blocked_reason == "input_invalid"
        assert schedule.last_blocked_detail == {"reason": "managed_files_empty"}
        assert schedule.last_processed_due_at == expected_due
        assert schedule.next_run_at is not None and schedule.next_run_at > BASE

    with session_factory() as session:
        assert scheduler_tick(session, now=BASE) == 0
    assert schedule_state(session_factory, adapter["id"])[4] > BASE


def test_schedule_persists_source_unavailable_code_without_mislabeling(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _ = setup_task(api_client, "a1-source-unavailable-code")
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"}
        ).status_code
        == 200
    )
    assert put_schedule(api_client, adapter["id"]).status_code == 200
    set_cursor(session_factory, adapter["id"], BASE - timedelta(minutes=1))
    with session_factory.begin() as session:
        config = session.get(AdapterInputConfig, adapter["id"])
        assert config is not None
        config.source_type = "remote_files"

    with session_factory() as session:
        assert scheduler_tick(session, now=BASE) == 0
    with session_factory() as session:
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert schedule is not None
        assert schedule.last_blocked_reason == "input_invalid"
        assert schedule.last_blocked_detail == {"code": "input_source_not_available"}


def test_schedule_persists_oversized_code_and_structured_limit(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _ = setup_task(api_client, "a1-oversized-code")
    save_input(api_client, adapter["id"], {"payload": "large-after-policy-change"})
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"}
        ).status_code
        == 200
    )
    assert put_schedule(api_client, adapter["id"]).status_code == 200
    set_cursor(session_factory, adapter["id"], BASE - timedelta(minutes=1))
    monkeypatch.setattr(settings, "execution_input_max_bytes", 8)

    with session_factory() as session:
        assert scheduler_tick(session, now=BASE) == 0
    with session_factory() as session:
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert schedule is not None
        assert schedule.last_blocked_reason == "input_invalid"
        assert schedule.last_blocked_detail == {
            "code": "execution_input_too_large",
            "max_bytes": 8,
        }


def test_invalid_schedule_multi_scheduler_consumes_once(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _ = setup_task(api_client, "a1-invalid-multi-scheduler")
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"}
        ).status_code
        == 200
    )
    assert put_schedule(api_client, adapter["id"]).status_code == 200
    due = BASE - timedelta(minutes=1)
    expected_due = latest_due_point("* * * * *", "UTC", due, BASE)
    set_cursor(session_factory, adapter["id"], due)
    with session_factory.begin() as session:
        config = session.get(AdapterInputConfig, adapter["id"])
        assert config is not None
        config.source_type = "managed_files"

    barrier = threading.Barrier(2)
    results: list[int] = []

    def tick() -> None:
        barrier.wait(timeout=5)
        with session_factory() as session:
            results.append(scheduler_tick(session, now=BASE))

    threads = [threading.Thread(target=tick) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert results == [0, 0]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        schedule = session.scalar(select(AdapterSchedule))
        assert schedule is not None and schedule.last_processed_due_at == expected_due
        assert schedule.next_run_at is not None and schedule.next_run_at > BASE


def test_invalid_saved_input_blocks_schedule_enable_without_side_effect(
    api_client: TestClient,
) -> None:
    adapter, _ = setup_task(api_client, "a1-invalid-enable")
    saved = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert saved.status_code == 200, saved.text
    mode = api_client.patch(f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"})
    assert mode.status_code == 200, mode.text
    rejected = put_schedule(api_client, adapter["id"], enabled=True)
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "input_invalid"
    assert api_client.get(f"/api/adapters/{adapter['id']}/schedule").status_code == 404


@pytest.mark.parametrize(
    ("source_type", "json_value", "retention"),
    [
        ("none", None, {"mode": "system_default", "seconds": None}),
        ("json", {"clone": [1, 2]}, {"mode": "system_default", "seconds": None}),
        ("managed_files", None, {"mode": "custom", "seconds": 7200}),
    ],
)
def test_clone_copies_input_config_but_not_file_selection(
    api_client: TestClient,
    source_type: str,
    json_value: Any,
    retention: dict[str, Any],
) -> None:
    source, _ = setup_task(api_client, f"a1-clone-{source_type}")
    if source_type == "none":
        saved = api_client.get(f"/api/adapters/{source['id']}/input-config").json()
    elif source_type == "json":
        saved = save_input(api_client, source["id"], json_value)
    else:
        saved_response = api_client.put(
            f"/api/adapters/{source['id']}/input-config",
            json={
                "expected_revision": 1,
                "source_type": source_type,
                "artifact_ids": [],
                "retention": retention,
            },
        )
        assert saved_response.status_code == 200, saved_response.text
        saved = saved_response.json()

    cloned = api_client.post(
        f"/api/adapters/{source['id']}/clone", json={"name": f"{source_type}-copy"}
    )
    assert cloned.status_code == 201, cloned.text
    clone_config = api_client.get(f"/api/adapters/{cloned.json()['id']}/input-config")
    assert clone_config.status_code == 200, clone_config.text
    body = clone_config.json()
    assert body["source_type"] == source_type
    assert body["json_value"] == json_value
    assert body["retention"] == saved["retention"]
    assert body["revision"] == 1
    if source_type == "managed_files":
        assert body["artifacts"] == []
