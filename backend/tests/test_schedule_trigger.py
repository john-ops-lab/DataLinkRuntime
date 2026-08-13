"""M5.2 tests: Schedule Trigger (Cron + Timezone + single latest catch-up).

Covers the Schedule configuration API contract, timezone-aware planned-point
arithmetic (including DST boundaries), the unified production gate evaluated
at every due point, the catch-up contract (transient failures keep the point
due; recovery creates only the latest planned point) and the concurrency
defenses: unified Adapter -> Schedule lock order, per-row single
transactions with SKIP LOCKED, and the partial unique indexes — exercised
against real PostgreSQL with concurrent sessions.

Scheduler time is controlled explicitly: ticks run with a fixed ``now`` so
downtime / offline / busy windows are deterministic.
"""

import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.models import Adapter, AdapterSchedule, Execution, Worker
from dlr.control.schemas.schedule import ScheduleUpsert
from dlr.control.services.adapter import start_production
from dlr.control.services.schedule import (
    ScheduleTickResult,
    latest_due_point,
    next_run_after,
    process_due_schedule,
    scheduler_tick,
    upsert_schedule,
)
from test_adapters import create_adapter, save_version
from test_executions import create_execution
from test_production_lifecycle import (
    WORKER_HEADERS,
    create_production_execution,
    publish,
    setup_publishable,
    start,
    stop,
)
from test_workers import claim, register_worker

# Fixed scheduler clock for tick tests; real Worker heartbeats (registered
# "today") are in the future of this clock, which still counts online.
BASE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


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


def set_cursor(session_factory: sessionmaker[Session], adapter_id: int, next_run_at: datetime):
    """Move the stored cursor directly (simulates elapsed time)."""
    with session_factory() as session:
        session.execute(
            update(AdapterSchedule)
            .where(AdapterSchedule.adapter_id == adapter_id)
            .values(next_run_at=next_run_at)
        )
        session.commit()


def tick(session_factory: sessionmaker[Session], now: datetime) -> int:
    with session_factory() as session:
        return scheduler_tick(session, now=now)


def schedule_executions(session_factory: sessionmaker[Session], adapter_id: int) -> list[Execution]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(Execution)
                .where(
                    Execution.adapter_id == adapter_id,
                    Execution.trigger == "schedule",
                )
                .order_by(Execution.id)
            ).all()
        )


def running_production(client: TestClient, name: str) -> tuple[dict, dict, dict]:
    """Published Adapter with an opened production entry (Start done)."""
    adapter, version, worker = setup_publishable(client, name=name)
    assert start(client, adapter["id"]).status_code == 200
    return adapter, version, worker


def cursor_of(client: TestClient, adapter_id: int) -> datetime:
    body = get_schedule(client, adapter_id).json()
    assert body["next_run_at"] is not None
    return datetime.fromisoformat(body["next_run_at"])


def set_worker_heartbeat(
    session_factory: sessionmaker[Session], worker_id: int, at: datetime
) -> None:
    with session_factory() as session:
        session.execute(
            update(Worker).where(Worker.id == worker_id).values(last_heartbeat=at, status="online")
        )
        session.commit()


def resolve_active_executions(session_factory: sessionmaker[Session], adapter_id: int) -> None:
    """Mark every active Execution of the Adapter succeeded (direct DB)."""
    with session_factory() as session:
        session.execute(
            update(Execution)
            .where(
                Execution.adapter_id == adapter_id,
                Execution.status.in_(("pending", "running")),
            )
            .values(status="succeeded")
        )
        session.commit()


def run_in_threads(tasks: list[Callable[[], None]]) -> list[Exception]:
    """Run the tasks concurrently (real PostgreSQL sessions) and collect errors."""
    errors: list[Exception] = []

    def wrapper(task: Callable[[], None]) -> None:
        try:
            task()
        except Exception as exc:  # noqa: BLE001 -- test harness collects all
            errors.append(exc)

    threads = [threading.Thread(target=wrapper, args=(task,)) for task in tasks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


# --- Schedule configuration API --------------------------------------------


def test_get_schedule_before_configuration_is_stable_404(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="sched-404")
    response = get_schedule(api_client, adapter["id"])
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "schedule_not_configured"


def test_schedule_endpoints_reject_unknown_adapter(api_client: TestClient) -> None:
    assert get_schedule(api_client, 999999).status_code == 404
    assert put_schedule(api_client, 999999).status_code == 404


def test_put_creates_and_returns_full_configuration(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="sched-create")
    response = put_schedule(
        api_client,
        adapter["id"],
        cron="  0  */2  *  *  * ",
        timezone="Asia/Shanghai",
        input={"full_sync": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body.keys()) == {
        "adapter_id",
        "enabled",
        "cron",
        "timezone",
        "input",
        "next_run_at",
        "updated_at",
    }
    assert body["adapter_id"] == adapter["id"]
    assert body["enabled"] is True
    assert body["cron"] == "0 */2 * * *", "cron is normalized to single spaces"
    assert body["timezone"] == "Asia/Shanghai"
    assert body["input"] == {"full_sync": True}
    assert body["next_run_at"] is not None

    stored = get_schedule(api_client, adapter["id"]).json()
    assert stored["cron"] == "0 */2 * * *"
    assert stored["input"] == {"full_sync": True}


def test_put_invalid_cron_rejected_and_not_persisted(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="sched-cron")
    for bad in ["* * * *", "* * * * * *", "@hourly", "61 * * * *", "a b c d e"]:
        response = put_schedule(api_client, adapter["id"], cron=bad)
        assert response.status_code == 422, bad
        assert response.json()["detail"]["code"] == "schedule_invalid_cron", bad
    assert get_schedule(api_client, adapter["id"]).status_code == 404, "zero persistence"


def test_put_invalid_timezone_rejected_and_not_persisted(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="sched-tz")
    for bad in ["Not/AZone", "EST+8", ""]:
        response = put_schedule(api_client, adapter["id"], timezone=bad)
        assert response.status_code == 422, bad
        assert response.json()["detail"]["code"] == "schedule_invalid_timezone", bad
    assert get_schedule(api_client, adapter["id"]).status_code == 404, "zero persistence"


def test_put_oversized_input_rejected_with_zero_persistence(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="sched-big-input")
    oversized = {"blob": "x" * (512 * 1024 + 1)}
    response = put_schedule(api_client, adapter["id"], input=oversized)
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "execution_input_too_large"
    assert get_schedule(api_client, adapter["id"]).status_code == 404, "zero persistence"


def test_next_run_at_semantics_create_update_disable_enable(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter = create_adapter(api_client, name="sched-cursor")

    # Create: re-based to the next future planned point of the cron.
    created = put_schedule(api_client, adapter["id"], cron="0 * * * *").json()
    expected = next_run_after("0 * * * *", "UTC", datetime.fromisoformat(created["updated_at"]))
    assert datetime.fromisoformat(created["next_run_at"]) == expected

    # Update: always re-based to the next future point of the NEW cron.
    updated = put_schedule(api_client, adapter["id"], cron="30 * * * *").json()
    expected = next_run_after("30 * * * *", "UTC", datetime.fromisoformat(updated["updated_at"]))
    assert datetime.fromisoformat(updated["next_run_at"]) == expected

    # Disable: cursor becomes null and no Execution is ever created.
    disabled = put_schedule(api_client, adapter["id"], cron="30 * * * *", enabled=False).json()
    assert disabled["enabled"] is False
    assert disabled["next_run_at"] is None

    # Enable: re-based to the next future point, never an immediate catch-up.
    enabled = put_schedule(api_client, adapter["id"], cron="30 * * * *", enabled=True).json()
    assert enabled["next_run_at"] is not None
    assert tick(session_factory, datetime.now(UTC)) == 0


# --- Planned-point arithmetic (DST boundaries) ------------------------------


def test_timezone_arithmetic_uses_configured_zone() -> None:
    # 02:00 UTC = 10:00 in Shanghai: today's 09:00 already passed.
    result = next_run_after("0 9 * * *", "Asia/Shanghai", datetime(2026, 8, 1, 2, 0, tzinfo=UTC))
    assert result == datetime(2026, 8, 2, 1, 0, tzinfo=UTC), "09:00 CST = 01:00 UTC"


def test_dst_spring_forward_skipped_time_fires_at_boundary() -> None:
    # America/New_York 2026-03-08: 02:30 never exists (02:00 -> 03:00).
    result = next_run_after(
        "30 2 * * *", "America/New_York", datetime(2026, 3, 8, 6, 0, tzinfo=UTC)
    )
    assert result == datetime(2026, 3, 8, 7, 0, tzinfo=UTC), "fires at the 03:00 EDT boundary"


def test_dst_fall_back_ambiguous_time_fires_twice() -> None:
    # America/New_York 2026-11-01: 01:30 occurs twice (EDT then EST).
    first = next_run_after(
        "30 1 * * *", "America/New_York", datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    )
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC), "01:30 EDT"
    second = next_run_after("30 1 * * *", "America/New_York", first)
    assert second == datetime(2026, 11, 1, 6, 30, tzinfo=UTC), "01:30 EST"


def test_latest_due_point_falls_back_to_cursor_on_exact_minute() -> None:
    since = BASE
    assert latest_due_point("0 * * * *", "UTC", since, BASE) == since
    assert latest_due_point("0 * * * *", "UTC", since, BASE + timedelta(seconds=30)) == BASE
    assert latest_due_point(
        "0 * * * *", "UTC", since, BASE + timedelta(hours=3, minutes=20)
    ) == BASE + timedelta(hours=3)


# --- Scheduler tick: the unified production gate ----------------------------


def test_due_point_creates_execution_locked_to_production_version_and_worker(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker = running_production(api_client, "sched-fire")
    assert put_schedule(api_client, adapter["id"], input={"mode": "full"}).status_code == 200

    # Publish a NEWER version without Stop/Start: the Schedule must stay
    # locked to the production version (M5.1 pointer semantics). The newer
    # version first passes the publish gate with one succeeded test run.
    newer = save_version(api_client, adapter["id"])
    test_run = create_execution(api_client, adapter["id"], {"version_id": newer["id"]})
    assert claim(api_client, worker["id"]).status_code == 200
    finished = api_client.post(
        f"/api/workers/{worker['id']}/executions/{test_run['id']}/result",
        json={"status": "succeeded"},
        headers=WORKER_HEADERS,
    )
    assert finished.status_code == 200, finished.text
    assert publish(api_client, adapter["id"], newer["id"]).status_code == 200

    due = BASE
    set_cursor(session_factory, adapter["id"], due)
    assert tick(session_factory, due + timedelta(seconds=30)) == 1

    runs = schedule_executions(session_factory, adapter["id"])
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "pending"
    assert run.version_id == version["id"], "locked production version, not the newer publish"
    assert run.target_worker_id == worker["id"], "fixed production Worker"
    assert run.scheduled_for == due
    assert run.input == {"mode": "full"}

    # Cursor advanced past the point: the same tick window never replays.
    assert tick(session_factory, due + timedelta(seconds=40)) == 0
    assert len(schedule_executions(session_factory, adapter["id"])) == 1

    # The M5.1 unified active-unique index and the transient busy rule are
    # the final defenses: while the created Execution stays active, a forced
    # cursor rewind keeps the point due but creates nothing (no queueing).
    set_cursor(session_factory, adapter["id"], due)
    assert tick(session_factory, due + timedelta(seconds=50)) == 0
    assert len(schedule_executions(session_factory, adapter["id"])) == 1
    assert cursor_of(api_client, adapter["id"]) == due, "busy keeps the point due"

    # Once the active Execution finishes, the scheduled_for unique index
    # still protects the same planned point: the lost race consumes the
    # point (cursor advances) without creating a duplicate.
    resolve_active_executions(session_factory, adapter["id"])
    assert tick(session_factory, due + timedelta(minutes=2)) == 0
    assert len(schedule_executions(session_factory, adapter["id"])) == 1
    assert cursor_of(api_client, adapter["id"]) == due + timedelta(hours=1)


def test_stopped_production_never_triggers(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, _, _ = running_production(api_client, "sched-stopped")
    assert put_schedule(api_client, adapter["id"]).status_code == 200
    assert stop(api_client, adapter["id"]).status_code == 200

    set_cursor(session_factory, adapter["id"], BASE)
    assert tick(session_factory, BASE + timedelta(seconds=30)) == 0
    assert schedule_executions(session_factory, adapter["id"]) == []


def test_stop_start_rebases_cursor_and_skips_missed_window(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, _, _ = running_production(api_client, "sched-rebase")
    assert put_schedule(api_client, adapter["id"]).status_code == 200
    assert stop(api_client, adapter["id"]).status_code == 200

    # Points pass while the production entry is closed.
    set_cursor(session_factory, adapter["id"], BASE)
    assert start(api_client, adapter["id"]).status_code == 200

    # Start re-based the cursor to the next FUTURE point: the missed window
    # is skipped entirely.
    body = get_schedule(api_client, adapter["id"]).json()
    cursor = datetime.fromisoformat(body["next_run_at"])
    assert cursor > datetime.now(UTC)
    assert tick(session_factory, BASE + timedelta(hours=5)) == 0
    assert schedule_executions(session_factory, adapter["id"]) == []


def test_downtime_catches_up_only_the_latest_point(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, _, _ = running_production(api_client, "sched-downtime")
    assert put_schedule(api_client, adapter["id"]).status_code == 200

    # Control was down for 4.5 hourly periods; on recovery at most the
    # latest planned point is created.
    set_cursor(session_factory, adapter["id"], BASE - timedelta(hours=4))
    assert tick(session_factory, BASE + timedelta(minutes=30)) == 1

    runs = schedule_executions(session_factory, adapter["id"])
    assert [run.scheduled_for for run in runs] == [BASE], "latest point only, never replayed"
    body = get_schedule(api_client, adapter["id"]).json()
    assert datetime.fromisoformat(body["next_run_at"]) == BASE + timedelta(hours=1)


def test_worker_offline_keeps_due_and_catches_up_immediately(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker = running_production(api_client, "sched-offline")
    assert put_schedule(api_client, adapter["id"]).status_code == 200

    set_worker_heartbeat(session_factory, worker["id"], BASE - timedelta(hours=1))

    set_cursor(session_factory, adapter["id"], BASE)
    assert tick(session_factory, BASE + timedelta(seconds=30)) == 0, "offline: never queued"
    assert schedule_executions(session_factory, adapter["id"]) == []
    assert cursor_of(api_client, adapter["id"]) == BASE, "offline keeps the point due"

    # The heartbeat recovers BEFORE the next cron point: the held due point
    # is caught up immediately — the original planned point, not the next.
    set_worker_heartbeat(session_factory, worker["id"], BASE + timedelta(minutes=9, seconds=55))
    assert tick(session_factory, BASE + timedelta(minutes=10)) == 1

    runs = schedule_executions(session_factory, adapter["id"])
    assert [run.scheduled_for for run in runs] == [BASE]
    assert runs[0].version_id == version["id"]
    assert cursor_of(api_client, adapter["id"]) == BASE + timedelta(hours=1)


def test_worker_offline_across_many_points_recovers_latest_only(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, _, worker = running_production(api_client, "sched-offline-many")
    assert put_schedule(api_client, adapter["id"]).status_code == 200

    set_worker_heartbeat(session_factory, worker["id"], BASE - timedelta(hours=1))
    set_cursor(session_factory, adapter["id"], BASE)

    # Several hourly points pass while the Worker stays offline: nothing is
    # queued and the cursor keeps the window due.
    assert tick(session_factory, BASE + timedelta(seconds=30)) == 0
    assert tick(session_factory, BASE + timedelta(hours=2, minutes=30)) == 0
    assert schedule_executions(session_factory, adapter["id"]) == []
    assert cursor_of(api_client, adapter["id"]) == BASE

    # Recovery: exactly the latest planned point of the missed window.
    set_worker_heartbeat(
        session_factory, worker["id"], BASE + timedelta(hours=2, minutes=44, seconds=55)
    )
    assert tick(session_factory, BASE + timedelta(hours=2, minutes=45)) == 1

    runs = schedule_executions(session_factory, adapter["id"])
    assert [run.scheduled_for for run in runs] == [BASE + timedelta(hours=2)]
    assert cursor_of(api_client, adapter["id"]) == BASE + timedelta(hours=3)


def test_busy_keeps_due_and_catches_up_after_finish(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker = running_production(api_client, "sched-busy")
    assert put_schedule(api_client, adapter["id"]).status_code == 200

    create_production_execution(
        session_factory, adapter["id"], version["id"], worker["id"], status="running"
    )

    set_cursor(session_factory, adapter["id"], BASE)
    assert tick(session_factory, BASE + timedelta(seconds=30)) == 0, "busy: no second Execution"
    assert schedule_executions(session_factory, adapter["id"]) == []
    assert cursor_of(api_client, adapter["id"]) == BASE, "busy keeps the point due"

    # The active Execution finishes before the next cron point: the held
    # due point is caught up immediately.
    resolve_active_executions(session_factory, adapter["id"])
    assert tick(session_factory, BASE + timedelta(seconds=40)) == 1
    runs = schedule_executions(session_factory, adapter["id"])
    assert [run.scheduled_for for run in runs] == [BASE]


def test_busy_across_many_points_catches_up_latest_only(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker = running_production(api_client, "sched-busy-many")
    assert put_schedule(api_client, adapter["id"]).status_code == 200

    create_production_execution(
        session_factory, adapter["id"], version["id"], worker["id"], status="running"
    )
    set_cursor(session_factory, adapter["id"], BASE)

    assert tick(session_factory, BASE + timedelta(hours=2, minutes=30)) == 0
    assert cursor_of(api_client, adapter["id"]) == BASE, "busy keeps the window due"

    resolve_active_executions(session_factory, adapter["id"])
    assert tick(session_factory, BASE + timedelta(hours=2, minutes=40)) == 1
    runs = schedule_executions(session_factory, adapter["id"])
    assert [run.scheduled_for for run in runs] == [BASE + timedelta(hours=2)]


def test_scheduled_for_unique_constraint(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker = running_production(api_client, "sched-unique")
    with session_factory() as session:
        session.add(
            Execution(
                adapter_id=adapter["id"],
                version_id=version["id"],
                trigger="schedule",
                status="pending",
                target_worker_id=worker["id"],
                input=None,
                scheduled_for=BASE,
            )
        )
        session.commit()
        session.add(
            Execution(
                adapter_id=adapter["id"],
                version_id=version["id"],
                trigger="schedule",
                status="pending",
                target_worker_id=worker["id"],
                input=None,
                scheduled_for=BASE,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    # A different planned point of the same Adapter does not conflict; the
    # stored row is terminal so the M5.1 active-unique index stays clear.
    with session_factory() as session:
        session.add(
            Execution(
                adapter_id=adapter["id"],
                version_id=version["id"],
                trigger="schedule",
                status="succeeded",
                target_worker_id=worker["id"],
                input=None,
                scheduled_for=BASE + timedelta(hours=1),
            )
        )
        session.commit()


def test_concurrent_schedulers_partition_due_rows(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, _, _ = running_production(api_client, "sched-race")
    assert put_schedule(api_client, adapter["id"]).status_code == 200
    set_cursor(session_factory, adapter["id"], BASE)

    # One scheduler holds the row locks (platform order: Adapter first);
    # a concurrent tick must skip the row (SKIP LOCKED) instead of blocking
    # or duplicating, and the holder creates the single Execution.
    holder = session_factory()
    try:
        locked_adapter = holder.scalar(
            select(Adapter).where(Adapter.id == adapter["id"]).with_for_update()
        )
        locked = holder.scalar(
            select(AdapterSchedule)
            .where(AdapterSchedule.adapter_id == adapter["id"])
            .with_for_update()
        )
        assert locked_adapter is not None and locked is not None
        assert tick(session_factory, BASE + timedelta(seconds=30)) == 0
        outcome = process_due_schedule(
            holder, locked_adapter, locked, now=BASE + timedelta(seconds=30)
        )
        assert outcome is ScheduleTickResult.CREATED
        holder.commit()
    finally:
        holder.close()

    assert len(schedule_executions(session_factory, adapter["id"])) == 1


def test_cron_edit_rebases_and_never_replays_old_cron(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, _, _ = running_production(api_client, "sched-edit")
    assert put_schedule(api_client, adapter["id"], cron="0 * * * *").status_code == 200

    set_cursor(session_factory, adapter["id"], BASE)
    assert tick(session_factory, BASE + timedelta(seconds=30)) == 1

    # Edit to a daily cron: the cursor re-bases to the new cron's next
    # future point, so the old hourly points are never replayed.
    assert put_schedule(api_client, adapter["id"], cron="0 9 * * *").status_code == 200
    body = get_schedule(api_client, adapter["id"]).json()
    cursor = datetime.fromisoformat(body["next_run_at"])
    assert cursor > datetime.now(UTC)
    assert cursor.hour == 9 and cursor.minute == 0

    assert tick(session_factory, BASE + timedelta(hours=3)) == 0
    assert len(schedule_executions(session_factory, adapter["id"])) == 1


def test_archived_adapter_never_triggers(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, _, _ = running_production(api_client, "sched-archived")
    assert put_schedule(api_client, adapter["id"]).status_code == 200

    # Direct marker keeps production_state=running so the archived check is
    # the only failing gate condition.
    with session_factory() as session:
        session.execute(update(Adapter).where(Adapter.id == adapter["id"]).values(archived_at=BASE))
        session.commit()

    # Archived is a structural gate failure: the point is consumed (cursor
    # advances), never queued and never executed.
    set_cursor(session_factory, adapter["id"], BASE)
    assert tick(session_factory, BASE + timedelta(seconds=30)) == 0
    assert schedule_executions(session_factory, adapter["id"]) == []
    assert cursor_of(api_client, adapter["id"]) == BASE + timedelta(hours=1)


def test_put_schedule_rejected_on_archived_adapter(api_client: TestClient) -> None:
    adapter, _, _ = running_production(api_client, "sched-arch-put")
    assert put_schedule(api_client, adapter["id"]).status_code == 200
    assert stop(api_client, adapter["id"]).status_code == 200
    assert api_client.post(f"/api/adapters/{adapter['id']}/archive").status_code == 200

    # Archived Adapters are read-only: PUT is rejected and persists nothing.
    response = put_schedule(api_client, adapter["id"], cron="30 * * * *")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_archived"

    # GET keeps viewing the stored configuration.
    stored = get_schedule(api_client, adapter["id"])
    assert stored.status_code == 200
    assert stored.json()["cron"] == "0 * * * *"


# --- Real PostgreSQL concurrency: lock order + transaction boundaries ------


def test_scheduler_vs_start_never_deadlocks_and_stays_consistent(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker = running_production(api_client, "sched-lock-start")
    assert put_schedule(api_client, adapter["id"]).status_code == 200
    locked_production_version = version["id"]

    created_counts: list[int] = []
    for _ in range(6):
        # Every round starts from a closed entry with exactly one due point.
        assert stop(api_client, adapter["id"]).status_code == 200
        resolve_active_executions(session_factory, adapter["id"])
        set_cursor(session_factory, adapter["id"], BASE)

        barrier = threading.Barrier(2)

        def run_tick(_barrier: threading.Barrier = barrier) -> None:
            _barrier.wait()
            with session_factory() as session:
                created_counts.append(scheduler_tick(session, now=BASE + timedelta(seconds=30)))

        def run_start(_barrier: threading.Barrier = barrier) -> None:
            _barrier.wait()
            with session_factory() as session:
                try:
                    start_production(session, adapter["id"])
                except HTTPException as error:
                    # A business race (e.g. the concurrent tick created the
                    # active Execution first) is acceptable; a deadlock is
                    # not, and surfaces as a database error instead.
                    assert error.status_code == 409, error.detail

        errors = run_in_threads([run_tick, run_start])
        assert errors == [], f"scheduler vs Start must never deadlock: {errors}"

    # Final consistency: the entry can be opened, the enabled cursor is
    # never NULL, and every created Execution stayed locked to the
    # production version/worker with unique planned points.
    resolve_active_executions(session_factory, adapter["id"])
    if api_client.get(f"/api/adapters/{adapter['id']}").json()["production_state"] != "running":
        assert start(api_client, adapter["id"]).status_code == 200
    assert get_schedule(api_client, adapter["id"]).json()["next_run_at"] is not None
    runs = schedule_executions(session_factory, adapter["id"])
    assert sum(created_counts) == len(runs)
    assert all(run.version_id == locked_production_version for run in runs)
    assert all(run.target_worker_id == worker["id"] for run in runs)
    planned = [run.scheduled_for for run in runs]
    assert len(planned) == len(set(planned)), "no duplicate planned points"


def test_scheduler_vs_schedule_put_never_deadlocks_and_stays_consistent(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker = running_production(api_client, "sched-lock-put")
    assert put_schedule(api_client, adapter["id"]).status_code == 200

    for round_index in range(6):
        resolve_active_executions(session_factory, adapter["id"])
        set_cursor(session_factory, adapter["id"], BASE)

        barrier = threading.Barrier(2)

        def run_tick(_barrier: threading.Barrier = barrier) -> None:
            _barrier.wait()
            with session_factory() as session:
                scheduler_tick(session, now=BASE + timedelta(seconds=30))

        def run_put(_barrier: threading.Barrier = barrier, _round: int = round_index) -> None:
            _barrier.wait()
            with session_factory() as session:
                upsert_schedule(
                    session,
                    adapter["id"],
                    ScheduleUpsert(
                        enabled=True,
                        cron="0 * * * *",
                        timezone="UTC",
                        input={"round": _round},
                    ),
                )

        errors = run_in_threads([run_tick, run_put])
        assert errors == [], f"scheduler vs Schedule PUT must never deadlock: {errors}"

    # Final consistency: configuration, locked production version and the
    # created Executions all agree; planned points are unique.
    body = get_schedule(api_client, adapter["id"]).json()
    assert body["enabled"] is True and body["next_run_at"] is not None
    final = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert final["production_state"] == "running"
    assert final["production_version_id"] == version["id"]
    runs = schedule_executions(session_factory, adapter["id"])
    assert all(run.version_id == version["id"] for run in runs)
    assert all(run.target_worker_id == worker["id"] for run in runs)
    planned = [run.scheduled_for for run in runs]
    assert len(planned) == len(set(planned)), "no duplicate planned points"


def test_two_controls_partition_multiple_due_schedules(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapters = [running_production(api_client, f"sched-part-{index}") for index in range(3)]
    for adapter, _, _ in adapters:
        assert put_schedule(api_client, adapter["id"]).status_code == 200
        set_cursor(session_factory, adapter["id"], BASE)

    # Two real concurrent sessions (two Control instances) tick the same
    # due window: every row is processed exactly once, by exactly one of
    # them — no duplicates, nothing skipped, nothing processed early.
    results: list[int] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def run_tick(_barrier: threading.Barrier = barrier) -> None:
        _barrier.wait()
        with session_factory() as session:
            created = scheduler_tick(session, now=BASE + timedelta(seconds=30))
        with results_lock:
            results.append(created)

    errors = run_in_threads([run_tick, run_tick])
    assert errors == [], f"concurrent ticks must not fail: {errors}"
    assert sum(results) == 3, f"exactly one Execution per due Schedule: {results}"

    for adapter, _, _ in adapters:
        runs = schedule_executions(session_factory, adapter["id"])
        assert len(runs) == 1, "no duplicates and nothing skipped"
        assert runs[0].scheduled_for == BASE
        assert cursor_of(api_client, adapter["id"]) == BASE + timedelta(hours=1)


def test_manual_test_run_unaffected_by_schedule(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter = create_adapter(api_client, name="sched-manual")
    save_version(api_client, adapter["id"])
    register_worker(api_client, name="manual-worker")
    assert put_schedule(api_client, adapter["id"]).status_code == 200

    execution = create_execution(api_client, adapter["id"])
    assert execution["trigger"] == "manual"
    assert execution["scheduled_for"] is None
    assert execution["status"] == "pending"

    with session_factory() as session:
        manual = session.scalar(select(Execution).where(Execution.id == execution["id"]))
        assert manual is not None and manual.scheduled_for is None
