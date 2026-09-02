"""M5.4.1 Schedule integration with latest Revision and runtime lock."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from croniter import croniter
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, null, select, update
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import (
    AdapterExecutionAdmission,
    AdapterInputConfig,
    AdapterSchedule,
    Execution,
    ExecutionOutbox,
    GlobalExecutionAdmission,
    ScheduleDispatchOutcome,
    Worker,
)
from dlr.control.services import admission, outbox, rabbitmq, worker_availability
from dlr.control.services import schedule as schedule_service
from dlr.control.services.schedule import (
    SCHEDULE_AUDIT_PAGE_SIZE,
    _due_points,
    latest_due_point,
    next_run_after,
    scheduler_tick,
)
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


def enable_rabbitmq_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model a successful asynchronous capability probe for API unit tests."""
    monkeypatch.setattr(settings, "rabbitmq_execution_enabled", True)
    monkeypatch.setattr(settings, "rabbitmq_url", "amqp://dlr:test-password@rabbitmq:5672/%2F")
    monkeypatch.setattr(settings, "rabbitmq_management_url", "http://rabbitmq:15672")
    rabbitmq.mark_runtime_ready()


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


def test_due_points_non_aligned_cursor_paginates_without_overlap_or_omission() -> None:
    """A legacy cursor between cron minutes still has exact bounded pages."""
    cron = "* * * * *"
    since = datetime(2026, 1, 1, 0, 0, 17, tzinfo=UTC)
    now = since + timedelta(minutes=SCHEDULE_AUDIT_PAGE_SIZE + 5)

    first = _due_points(cron, "UTC", since, now)
    assert len(first.points) == SCHEDULE_AUDIT_PAGE_SIZE
    assert first.truncated is True
    assert first.next_point is not None

    second = _due_points(cron, "UTC", first.next_point, now)
    assert 0 < len(second.points) <= SCHEDULE_AUDIT_PAGE_SIZE
    assert second.points[0] == first.next_point
    assert set(first.points).isdisjoint(second.points)

    expected = [since]
    iterator = croniter(cron, since - timedelta(microseconds=1))
    while True:
        candidate = iterator.get_next(datetime).astimezone(UTC)
        if candidate > now:
            break
        if candidate > expected[-1]:
            expected.append(candidate)
    observed = list(first.points) + list(second.points)
    assert observed == expected


@pytest.mark.parametrize("existing_status", ["queued", "running", "retry_wait"])
def test_rabbit_coalesce_latest_accepts_latest_while_existing_execution_is_outstanding(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    existing_status: str,
) -> None:
    """Coalescing is per due-point batch, not a RabbitMQ busy gate."""
    enable_rabbitmq_test(monkeypatch)
    adapter, _, worker = setup_task(api_client, f"rabbit-coalesce-{existing_status}")
    with session_factory.begin() as session:
        session.execute(update(Worker).where(Worker.id == worker["id"]).values(protocol_version=3))
    assert (
        put_schedule(
            api_client,
            adapter["id"],
            misfire_policy="coalesce_latest",
            max_catchup_count=10,
        ).status_code
        == 200
    )
    set_cursor(session_factory, adapter["id"], BASE - timedelta(minutes=2))

    with session_factory() as session:
        assert scheduler_tick(session, now=BASE) == 1
    first = schedule_executions(session_factory, adapter["id"])[0]
    with session_factory.begin() as session:
        session.execute(
            update(Execution).where(Execution.id == first.id).values(status=existing_status)
        )
        session.execute(
            update(AdapterSchedule)
            .where(AdapterSchedule.adapter_id == adapter["id"])
            .values(next_run_at=first.scheduled_for + timedelta(minutes=1))
        )

    with session_factory() as session:
        assert scheduler_tick(session, now=BASE + timedelta(minutes=3)) == 1
    rows = schedule_executions(session_factory, adapter["id"])
    assert len(rows) == 2
    assert rows[1].scheduled_for is not None
    assert rows[1].scheduled_for > first.scheduled_for
    assert rows[1].status == "queued"
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ExecutionOutbox)
                .join(Execution, Execution.id == ExecutionOutbox.execution_id)
                .where(Execution.adapter_id == adapter["id"])
            )
            == 2
        )

    # Avoid carrying the reliable counter into the next test through the
    # shared singleton admission row.  A running row receives a cancellation
    # request rather than an immediate terminal transition in the API.
    with session_factory.begin() as session:
        for execution_id in (first.id, rows[1].id):
            execution = session.get(Execution, execution_id)
            assert execution is not None
            execution.status = "cancelled"
            execution.ended_at = func.now()
            admission.release_admission_once(session, execution)
            outbox.settle_cancelled_outbox(session, execution.id)


def test_rabbit_skip_while_busy_accepts_oldest_due_point_when_idle(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idle skip-while-busy evaluates a backlog in ascending plan order."""
    enable_rabbitmq_test(monkeypatch)
    adapter, _, worker = setup_task(api_client, "rabbit-skip-ascending")
    with session_factory.begin() as session:
        session.execute(update(Worker).where(Worker.id == worker["id"]).values(protocol_version=3))
    assert (
        put_schedule(
            api_client,
            adapter["id"],
            misfire_policy="skip_while_busy",
            max_catchup_count=10,
        ).status_code
        == 200
    )
    now = BASE.replace(minute=0, second=0, microsecond=0)
    first_due = now - timedelta(hours=2)
    set_cursor(session_factory, adapter["id"], first_due)

    with session_factory() as session:
        assert scheduler_tick(session, now=now) == 1

    rows = schedule_executions(session_factory, adapter["id"])
    assert len(rows) == 1
    assert rows[0].scheduled_for == first_due
    with session_factory() as session:
        outcomes = list(
            session.scalars(
                select(ScheduleDispatchOutcome)
                .join(AdapterSchedule, AdapterSchedule.id == ScheduleDispatchOutcome.schedule_id)
                .where(AdapterSchedule.adapter_id == adapter["id"])
                .order_by(ScheduleDispatchOutcome.first_scheduled_for)
            ).all()
        )
        assert [
            (outcome.outcome, outcome.reason, outcome.occurrence_count) for outcome in outcomes
        ] == [("enqueued", "accepted", 1), ("skipped", "adapter_busy", 2)]
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert schedule is not None
        assert schedule.last_processed_due_at == now
        assert schedule.next_run_at is not None and schedule.next_run_at > now


def test_rabbit_skip_while_busy_consumes_due_points_when_global_capacity_is_full(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global capacity pressure is an explicit consumed admission outcome."""
    enable_rabbitmq_test(monkeypatch)
    adapter, _, worker = setup_task(api_client, "rabbit-skip-global-full")
    with session_factory.begin() as session:
        session.execute(update(Worker).where(Worker.id == worker["id"]).values(protocol_version=3))
        counter = session.get(GlobalExecutionAdmission, "global")
        if counter is None:
            counter = GlobalExecutionAdmission(singleton_key="global")
            session.add(counter)
        counter.outstanding_count = settings.admission_global_max_count
    assert (
        put_schedule(
            api_client,
            adapter["id"],
            misfire_policy="skip_while_busy",
            max_catchup_count=10,
        ).status_code
        == 200
    )
    now = BASE.replace(minute=0, second=0, microsecond=0)
    first_due = now - timedelta(hours=2)
    set_cursor(session_factory, adapter["id"], first_due)

    with session_factory() as session:
        assert scheduler_tick(session, now=now) == 0
    assert schedule_executions(session_factory, adapter["id"]) == []
    with session_factory() as session:
        outcome = session.scalar(
            select(ScheduleDispatchOutcome)
            .join(AdapterSchedule, AdapterSchedule.id == ScheduleDispatchOutcome.schedule_id)
            .where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert outcome is not None
        assert (outcome.outcome, outcome.reason, outcome.occurrence_count) == (
            "skipped",
            "admission_full",
            3,
        )
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert schedule is not None
        assert schedule.last_processed_due_at == now
        assert schedule.next_run_at is not None and schedule.next_run_at > now
        assert schedule.last_blocked_reason == "runtime_capacity_full"


def test_rabbit_skip_while_busy_consumes_due_points_when_outbox_capacity_is_full(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outbox pressure is visible as skipped admission, not adapter busy."""
    enable_rabbitmq_test(monkeypatch)
    adapter, _, worker = setup_task(api_client, "rabbit-skip-outbox-full")
    with session_factory.begin() as session:
        session.execute(update(Worker).where(Worker.id == worker["id"]).values(protocol_version=3))
    assert (
        put_schedule(
            api_client,
            adapter["id"],
            misfire_policy="skip_while_busy",
            max_catchup_count=10,
        ).status_code
        == 200
    )
    other, _, other_worker = setup_task(api_client, "rabbit-skip-outbox-full-other")
    with session_factory.begin() as session:
        session.execute(
            update(Worker).where(Worker.id == other_worker["id"]).values(protocol_version=3)
        )
    existing = api_client.post(f"/api/adapters/{other['id']}/executions", json={})
    assert existing.status_code == 202, existing.text
    monkeypatch.setattr(settings, "outbox_max_pending_count", 1)
    monkeypatch.setattr(settings, "outbox_max_oldest_seconds", 604_800)
    now = BASE.replace(minute=0, second=0, microsecond=0)
    first_due = now - timedelta(hours=2)
    set_cursor(session_factory, adapter["id"], first_due)

    with session_factory() as session:
        assert scheduler_tick(session, now=now) == 0
    assert schedule_executions(session_factory, adapter["id"]) == []
    with session_factory() as session:
        outcome = session.scalar(
            select(ScheduleDispatchOutcome)
            .join(AdapterSchedule, AdapterSchedule.id == ScheduleDispatchOutcome.schedule_id)
            .where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert outcome is not None
        assert (outcome.outcome, outcome.reason, outcome.occurrence_count) == (
            "skipped",
            "admission_full",
            3,
        )
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert schedule is not None
        assert schedule.last_processed_due_at == now
        assert schedule.next_run_at is not None and schedule.next_run_at > now
        assert schedule.last_blocked_reason == "outbox_backlog_full"
        assert (
            session.scalar(
                select(func.count())
                .select_from(ExecutionOutbox)
                .join(Execution, Execution.id == ExecutionOutbox.execution_id)
                .where(Execution.adapter_id == other["id"], ExecutionOutbox.status == "pending")
            )
            == 1
        )


def test_rabbit_queue_every_occurrence_returns_all_created_executions(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queue-every tick reports the number of rows it actually created."""
    enable_rabbitmq_test(monkeypatch)
    adapter, _, worker = setup_task(api_client, "rabbit-queue-three")
    with session_factory.begin() as session:
        session.execute(update(Worker).where(Worker.id == worker["id"]).values(protocol_version=3))
    assert (
        put_schedule(
            api_client,
            adapter["id"],
            cron="* * * * *",
            misfire_policy="queue_every_occurrence",
            max_catchup_count=3,
        ).status_code
        == 200
    )
    now = BASE.replace(second=0, microsecond=0)
    first_due = now - timedelta(minutes=2)
    set_cursor(session_factory, adapter["id"], first_due)

    with session_factory() as session:
        assert scheduler_tick(session, now=now) == 3
    rows = schedule_executions(session_factory, adapter["id"])
    assert [row.scheduled_for for row in rows] == [
        first_due,
        first_due + timedelta(minutes=1),
        now,
    ]


def test_rabbit_queue_every_occurrence_stops_at_capacity_point(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queue-every leaves the first unaccepted cron point due."""
    enable_rabbitmq_test(monkeypatch)
    adapter, _, worker = setup_task(api_client, "rabbit-queue-capacity")
    with session_factory.begin() as session:
        session.execute(update(Worker).where(Worker.id == worker["id"]).values(protocol_version=3))
    monkeypatch.setattr(settings, "admission_adapter_max_count", 1)
    assert (
        put_schedule(
            api_client,
            adapter["id"],
            cron="* * * * *",
            misfire_policy="queue_every_occurrence",
            max_catchup_count=3,
        ).status_code
        == 200
    )
    now = BASE.replace(second=0, microsecond=0)
    first_due = now - timedelta(minutes=2)
    second_due = first_due + timedelta(minutes=1)
    set_cursor(session_factory, adapter["id"], first_due)

    with session_factory() as session:
        assert scheduler_tick(session, now=now) == 1
    assert [row.scheduled_for for row in schedule_executions(session_factory, adapter["id"])] == [
        first_due
    ]
    with session_factory() as session:
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert schedule is not None
        assert schedule.next_run_at == second_due
        assert schedule.last_processed_due_at == first_due
        assert schedule.last_blocked_reason == "adapter_queue_full"


def test_rabbit_queue_every_occurrence_input_invalid_then_capacity_keeps_second_point_due(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consumed invalid input cannot make a later capacity failure skip ahead."""
    enable_rabbitmq_test(monkeypatch)
    adapter, _, worker = setup_task(api_client, "rabbit-queue-invalid-capacity")
    with session_factory.begin() as session:
        session.execute(update(Worker).where(Worker.id == worker["id"]).values(protocol_version=3))
        session.add(
            AdapterExecutionAdmission(
                adapter_id=adapter["id"],
                outstanding_count=settings.admission_adapter_max_count,
            )
        )
    monkeypatch.setattr(settings, "admission_adapter_max_count", 1)
    assert (
        put_schedule(
            api_client,
            adapter["id"],
            cron="* * * * *",
            misfire_policy="queue_every_occurrence",
            max_catchup_count=3,
        ).status_code
        == 200
    )
    now = BASE.replace(second=0, microsecond=0)
    first_due = now - timedelta(minutes=2)
    second_due = first_due + timedelta(minutes=1)
    set_cursor(session_factory, adapter["id"], first_due)
    original_create = schedule_service._create_execution_locked

    def fail_first_then_create(*args: object, **kwargs: object):
        if kwargs.get("scheduled_for") == first_due:
            raise HTTPException(
                status_code=422,
                detail={"code": "input_invalid", "message": "invalid test input"},
            )
        return original_create(*args, **kwargs)

    monkeypatch.setattr(schedule_service, "_create_execution_locked", fail_first_then_create)
    with session_factory() as session:
        assert scheduler_tick(session, now=now) == 0
    assert schedule_executions(session_factory, adapter["id"]) == []
    with session_factory() as session:
        outcomes = list(
            session.scalars(
                select(ScheduleDispatchOutcome)
                .join(AdapterSchedule, AdapterSchedule.id == ScheduleDispatchOutcome.schedule_id)
                .where(AdapterSchedule.adapter_id == adapter["id"])
                .order_by(ScheduleDispatchOutcome.first_scheduled_for)
            ).all()
        )
        assert [
            (outcome.outcome, outcome.reason, outcome.occurrence_count) for outcome in outcomes
        ] == [("skipped", "input_invalid", 1)]
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert schedule is not None
        assert schedule.next_run_at == second_due
        assert schedule.last_processed_due_at == first_due
        assert schedule.last_blocked_reason == "adapter_queue_full"


def test_rabbit_queue_every_occurrence_records_duplicate_and_does_not_retry_it(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing terminal schedule point is consumed as a duplicate."""
    enable_rabbitmq_test(monkeypatch)
    adapter, _, worker = setup_task(api_client, "rabbit-queue-duplicate")
    with session_factory.begin() as session:
        session.execute(update(Worker).where(Worker.id == worker["id"]).values(protocol_version=3))
    assert (
        put_schedule(
            api_client,
            adapter["id"],
            cron="* * * * *",
            misfire_policy="queue_every_occurrence",
            max_catchup_count=3,
        ).status_code
        == 200
    )
    now = BASE.replace(second=0, microsecond=0)
    set_cursor(session_factory, adapter["id"], now)
    with session_factory() as session:
        assert scheduler_tick(session, now=now) == 1
    first = schedule_executions(session_factory, adapter["id"])[0]
    with session_factory.begin() as session:
        execution = session.get(Execution, first.id)
        assert execution is not None
        execution.status = "cancelled"
        execution.ended_at = func.now()
        admission.release_admission_once(session, execution)
        outbox.settle_cancelled_outbox(session, execution.id)
        session.execute(
            update(AdapterSchedule)
            .where(AdapterSchedule.adapter_id == adapter["id"])
            .values(next_run_at=now)
        )

    with session_factory() as session:
        assert scheduler_tick(session, now=now) == 0
        assert scheduler_tick(session, now=now) == 0
    with session_factory() as session:
        outcomes = list(
            session.scalars(
                select(ScheduleDispatchOutcome)
                .join(AdapterSchedule, AdapterSchedule.id == ScheduleDispatchOutcome.schedule_id)
                .where(AdapterSchedule.adapter_id == adapter["id"])
                .order_by(ScheduleDispatchOutcome.first_scheduled_for, ScheduleDispatchOutcome.id)
            ).all()
        )
        assert [
            (outcome.outcome, outcome.reason, outcome.occurrence_count) for outcome in outcomes
        ] == [("enqueued", "accepted", 1), ("skipped", "duplicate", 1)]
        assert (
            session.scalar(
                select(func.count())
                .select_from(Execution)
                .where(Execution.adapter_id == adapter["id"])
            )
            == 1
        )


def test_disable_only_cannot_mutate_schedule_policy_while_runtime_locked(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling an enabled Schedule is not a policy-edit escape hatch."""
    enable_rabbitmq_test(monkeypatch)
    adapter, _, worker = setup_task(api_client, "schedule-disable-policy-lock")
    with session_factory.begin() as session:
        session.execute(update(Worker).where(Worker.id == worker["id"]).values(protocol_version=3))
    configured = put_schedule(
        api_client,
        adapter["id"],
        misfire_policy="queue_every_occurrence",
        max_catchup_count=3,
        max_catchup_age_seconds=3_600,
    )
    assert configured.status_code == 200, configured.text
    queued = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert queued.status_code == 202, queued.text

    changed = put_schedule(
        api_client,
        adapter["id"],
        enabled=False,
        misfire_policy="skip_while_busy",
        max_catchup_count=4,
        max_catchup_age_seconds=7_200,
    )
    assert changed.status_code == 409, changed.text
    assert changed.json()["detail"]["code"] == "adapter_runtime_locked"
    current = get_schedule(api_client, adapter["id"])
    assert current.status_code == 200
    assert current.json()["enabled"] is True
    assert current.json()["misfire_policy"] == "queue_every_occurrence"
    assert current.json()["max_catchup_count"] == 3
    assert current.json()["max_catchup_age_seconds"] == 3_600


def test_rabbit_skip_while_busy_input_invalid_consumes_cursor_without_execution(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_rabbitmq_test(monkeypatch)
    adapter, _, worker = setup_task(api_client, "rabbit-skip-invalid")
    with session_factory.begin() as session:
        session.execute(update(Worker).where(Worker.id == worker["id"]).values(protocol_version=3))
    assert (
        put_schedule(
            api_client,
            adapter["id"],
            misfire_policy="skip_while_busy",
        ).status_code
        == 200
    )
    with session_factory.begin() as session:
        session.execute(
            update(AdapterInputConfig)
            .where(AdapterInputConfig.adapter_id == adapter["id"])
            .values(source_type="remote_files", json_value=null())
        )
    due = BASE - timedelta(minutes=1)
    set_cursor(session_factory, adapter["id"], due)
    expected_due = latest_due_point("0 * * * *", "UTC", due, BASE)

    with session_factory() as session:
        assert scheduler_tick(session, now=BASE) == 0
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Execution)
                .where(Execution.adapter_id == adapter["id"])
            )
            == 0
        )
        outcomes = list(
            session.scalars(
                select(ScheduleDispatchOutcome)
                .join(AdapterSchedule, AdapterSchedule.id == ScheduleDispatchOutcome.schedule_id)
                .where(AdapterSchedule.adapter_id == adapter["id"])
            ).all()
        )
        assert [
            (outcome.outcome, outcome.reason, outcome.occurrence_count) for outcome in outcomes
        ] == [("skipped", "input_invalid", 1)]
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert schedule is not None
        assert schedule.last_processed_due_at == expected_due
        assert schedule.next_run_at is not None and schedule.next_run_at > BASE
        assert schedule.last_blocked_reason == "input_invalid"


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (
            "coalesce_latest",
            [("coalesced", "coalesce_latest", 6), ("enqueued", "accepted", 1)],
        ),
        (
            "queue_every_occurrence",
            [
                ("expired", "catchup_limit", 5),
                ("enqueued", "accepted", 1),
                ("enqueued", "accepted", 1),
            ],
        ),
        ("skip_while_busy", [("skipped", "adapter_busy", 7)]),
    ],
)
def test_rabbit_schedule_backlog_over_max_plus_one_has_exact_outcome_coverage(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    policy: str,
    expected: list[tuple[str, str, int]],
) -> None:
    """A long-but-bounded age window is audited beyond max_catchup_count + 1."""
    enable_rabbitmq_test(monkeypatch)
    adapter, _, worker = setup_task(api_client, f"rabbit-backlog-{policy}")
    with session_factory.begin() as session:
        session.execute(update(Worker).where(Worker.id == worker["id"]).values(protocol_version=3))
    assert (
        put_schedule(
            api_client,
            adapter["id"],
            misfire_policy=policy,
            max_catchup_count=2,
            max_catchup_age_seconds=86_400,
        ).status_code
        == 200
    )
    now = BASE.replace(minute=0, second=0, microsecond=0)
    since = now - timedelta(hours=6)
    set_cursor(session_factory, adapter["id"], since)

    if policy == "skip_while_busy":
        busy = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
        assert busy.status_code == 202, busy.text

    with session_factory() as session:
        assert (
            scheduler_tick(session, now=now)
            == {
                "coalesce_latest": 1,
                "queue_every_occurrence": 2,
                "skip_while_busy": 0,
            }[policy]
        )

    with session_factory() as session:
        outcomes = list(
            session.scalars(
                select(ScheduleDispatchOutcome)
                .join(AdapterSchedule, AdapterSchedule.id == ScheduleDispatchOutcome.schedule_id)
                .where(AdapterSchedule.adapter_id == adapter["id"])
                .order_by(ScheduleDispatchOutcome.first_scheduled_for)
            ).all()
        )
        assert [
            (outcome.outcome, outcome.reason, outcome.occurrence_count) for outcome in outcomes
        ] == expected
        assert sum(outcome.occurrence_count for outcome in outcomes) == 7
        assert outcomes[0].first_scheduled_for == since
        assert outcomes[-1].last_scheduled_for == now
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert schedule is not None
        assert schedule.last_processed_due_at == now
        assert schedule.next_run_at is not None and schedule.next_run_at > now

    # Keep the global singleton admission row neutral for the following test.
    with session_factory.begin() as session:
        executions = list(
            session.scalars(
                select(Execution).where(
                    Execution.adapter_id == adapter["id"],
                    Execution.status.in_(("queued", "running", "retry_wait")),
                )
            ).all()
        )
        for execution in executions:
            execution.status = "cancelled"
            execution.ended_at = func.now()
            admission.release_admission_once(session, execution)
            outbox.settle_cancelled_outbox(session, execution.id)


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
    monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch.setattr(worker_availability, "current_time", lambda _session: BASE)
    recovered_at = BASE
    with session_factory.begin() as session:
        session.execute(
            update(Worker)
            .where(Worker.id == worker["id"])
            .values(last_heartbeat=recovered_at, status="online")
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


def test_structural_schedule_skip_uses_bounded_pages_without_overlap(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """An invalid target consumes a long due range one auditable page at a time."""
    adapter, _, worker = setup_task(api_client, "schedule-structural-pages")
    assert put_schedule(api_client, adapter["id"], cron="* * * * *").status_code == 200
    now = BASE.replace(second=0, microsecond=0)
    since = now - timedelta(minutes=SCHEDULE_AUDIT_PAGE_SIZE + 10)
    set_cursor(session_factory, adapter["id"], since)
    with session_factory.begin() as session:
        session.execute(
            update(Worker).where(Worker.id == worker["id"]).values(capabilities=["java"])
        )

    with session_factory() as session:
        assert scheduler_tick(session, now=now) == 0
    with session_factory() as session:
        first_page = list(
            session.scalars(
                select(ScheduleDispatchOutcome)
                .join(AdapterSchedule, AdapterSchedule.id == ScheduleDispatchOutcome.schedule_id)
                .where(AdapterSchedule.adapter_id == adapter["id"])
                .order_by(ScheduleDispatchOutcome.first_scheduled_for)
            ).all()
        )
        assert len(first_page) == 1
        assert first_page[0].outcome == "skipped"
        assert first_page[0].reason == "runtime_worker_invalid"
        assert first_page[0].occurrence_count == SCHEDULE_AUDIT_PAGE_SIZE
        assert first_page[0].first_scheduled_for == since
        assert first_page[0].last_scheduled_for < now
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert schedule is not None
        assert schedule.next_run_at == first_page[0].last_scheduled_for + timedelta(minutes=1)

    with session_factory() as session:
        assert scheduler_tick(session, now=now) == 0
    with session_factory() as session:
        pages = list(
            session.scalars(
                select(ScheduleDispatchOutcome)
                .join(AdapterSchedule, AdapterSchedule.id == ScheduleDispatchOutcome.schedule_id)
                .where(AdapterSchedule.adapter_id == adapter["id"])
                .order_by(ScheduleDispatchOutcome.first_scheduled_for)
            ).all()
        )
        assert len(pages) == 2
        assert pages[1].occurrence_count == 11
        assert pages[1].first_scheduled_for == pages[0].last_scheduled_for + timedelta(minutes=1)
        assert pages[1].last_scheduled_for <= now
        assert pages[0].last_scheduled_for < pages[1].first_scheduled_for
        assert sum(page.occurrence_count for page in pages) == SCHEDULE_AUDIT_PAGE_SIZE + 11
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert schedule is not None
        assert schedule.next_run_at == pages[1].last_scheduled_for + timedelta(minutes=1)


def test_rabbit_expired_backlog_page_is_exact_before_policy_processing(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first page may be wholly expired; the next page resumes exactly."""
    enable_rabbitmq_test(monkeypatch)
    adapter, _, worker = setup_task(api_client, "rabbit-expired-pages")
    with session_factory.begin() as session:
        session.execute(update(Worker).where(Worker.id == worker["id"]).values(protocol_version=3))
    assert (
        put_schedule(
            api_client,
            adapter["id"],
            cron="* * * * *",
            misfire_policy="coalesce_latest",
            max_catchup_count=2,
            max_catchup_age_seconds=60,
        ).status_code
        == 200
    )
    now = BASE.replace(second=0, microsecond=0)
    since = now - timedelta(minutes=SCHEDULE_AUDIT_PAGE_SIZE + 10)
    set_cursor(session_factory, adapter["id"], since)

    with session_factory() as session:
        assert scheduler_tick(session, now=now) == 0
    with session_factory() as session:
        first = list(
            session.scalars(
                select(ScheduleDispatchOutcome)
                .join(AdapterSchedule, AdapterSchedule.id == ScheduleDispatchOutcome.schedule_id)
                .where(AdapterSchedule.adapter_id == adapter["id"])
                .order_by(ScheduleDispatchOutcome.first_scheduled_for)
            ).all()
        )
        assert len(first) == 1
        assert (first[0].outcome, first[0].reason, first[0].occurrence_count) == (
            "expired",
            "catchup_age",
            SCHEDULE_AUDIT_PAGE_SIZE,
        )
        assert first[0].first_scheduled_for == since
        assert first[0].last_scheduled_for == since + timedelta(
            minutes=SCHEDULE_AUDIT_PAGE_SIZE - 1
        )
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert schedule is not None
        assert schedule.next_run_at == first[0].last_scheduled_for + timedelta(minutes=1)
        assert schedule.next_run_at < now

    with session_factory() as session:
        assert scheduler_tick(session, now=now) == 1
    with session_factory() as session:
        outcomes = list(
            session.scalars(
                select(ScheduleDispatchOutcome)
                .join(AdapterSchedule, AdapterSchedule.id == ScheduleDispatchOutcome.schedule_id)
                .where(AdapterSchedule.adapter_id == adapter["id"])
                .order_by(ScheduleDispatchOutcome.first_scheduled_for)
            ).all()
        )
        assert [
            (outcome.outcome, outcome.reason, outcome.occurrence_count) for outcome in outcomes
        ] == [
            ("expired", "catchup_age", SCHEDULE_AUDIT_PAGE_SIZE),
            ("expired", "catchup_age", 9),
            ("coalesced", "coalesce_latest", 1),
            ("enqueued", "accepted", 1),
        ]
        assert outcomes[1].first_scheduled_for == outcomes[0].last_scheduled_for + timedelta(
            minutes=1
        )
        assert outcomes[1].last_scheduled_for < outcomes[2].first_scheduled_for
        assert outcomes[2].last_scheduled_for < outcomes[3].first_scheduled_for
        assert sum(outcome.occurrence_count for outcome in outcomes) == (
            SCHEDULE_AUDIT_PAGE_SIZE + 11
        )
        assert len(schedule_executions(session_factory, adapter["id"])) == 1

    with session_factory.begin() as session:
        execution = session.scalar(
            select(Execution).where(
                Execution.adapter_id == adapter["id"],
                Execution.dispatch_backend == "rabbitmq",
            )
        )
        assert execution is not None
        execution.status = "cancelled"
        execution.ended_at = func.now()
        admission.release_admission_once(session, execution)
        outbox.settle_cancelled_outbox(session, execution.id)


def test_cron_arithmetic_remains_timezone_aware() -> None:
    spring = datetime(2026, 3, 8, 6, 59, tzinfo=UTC)
    next_point = next_run_after("30 2 * * *", "America/New_York", spring)
    assert next_point.tzinfo is not None
    due = latest_due_point("0 * * * *", "UTC", BASE - timedelta(hours=2), BASE)
    assert BASE - timedelta(hours=1) <= due <= BASE
