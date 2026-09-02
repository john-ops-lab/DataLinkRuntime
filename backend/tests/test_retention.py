"""M5.11 Wave B terminal Execution retention tests."""

import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import (
    AdapterExecutionAdmission,
    Execution,
    ExecutionIdempotencyRecord,
    ExecutionOutbox,
    GlobalExecutionAdmission,
)
from dlr.control.services import admission
from dlr.control.services.retention import (
    RetentionPolicy,
    _delete_batch,
    _terminal_query,
    cleanup_execution_retention,
)
from test_adapters import create_adapter, save_version


def _add_execution(
    session: Session,
    *,
    adapter_id: int,
    version_id: int,
    trigger: str,
    status: str,
    created_at: datetime,
    scheduled_for: datetime | None = None,
) -> None:
    session.add(
        Execution(
            adapter_id=adapter_id,
            version_id=version_id,
            trigger=trigger,
            status=status,
            input={"trigger": trigger, "status": status},
            stdout=f"stdout-{trigger}",
            stderr=f"stderr-{trigger}",
            error=f"error-{trigger}",
            created_at=created_at,
            scheduled_for=scheduled_for,
        )
    )


def _add_rabbit_terminal(
    session: Session,
    *,
    adapter_id: int,
    version_id: int,
    created_at: datetime,
    logical_input_bytes: int = 10,
    admission_released_at: datetime | None = None,
) -> Execution:
    execution = Execution(
        adapter_id=adapter_id,
        version_id=version_id,
        trigger="manual",
        status="succeeded",
        dispatch_backend="rabbitmq",
        dispatch_generation=1,
        logical_input_bytes=logical_input_bytes,
        resource_class="standard",
        input={"trigger": "manual"},
        stdout="",
        stderr="",
        error=None,
        created_at=created_at,
        admission_released_at=admission_released_at,
    )
    session.add(execution)
    session.flush()
    return execution


def test_cleanup_applies_per_trigger_counts_and_preserves_active_rows(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    adapters: dict[str, tuple[dict, dict]] = {}
    for trigger in ("webhook", "manual", "schedule"):
        adapter = create_adapter(api_client, name=f"retention-counts-{trigger}")
        adapters[trigger] = (adapter, save_version(api_client, adapter["id"]))
    now = datetime.now(UTC)
    monkeypatch.setattr(settings, "execution_retention_webhook_max_per_adapter", 2)
    monkeypatch.setattr(settings, "execution_retention_task_max_per_adapter", 2)
    monkeypatch.setattr(settings, "execution_retention_schedule_max_per_adapter", 2)
    monkeypatch.setattr(settings, "execution_retention_batch_size", 1)

    with session_factory.begin() as session:
        for trigger in ("webhook", "manual", "schedule"):
            adapter, version = adapters[trigger]
            for index in range(3):
                _add_execution(
                    session,
                    adapter_id=adapter["id"],
                    version_id=version["id"],
                    trigger=trigger,
                    status="succeeded",
                    created_at=now + timedelta(seconds=index),
                    scheduled_for=(
                        now + timedelta(seconds=index) if trigger == "schedule" else None
                    ),
                )
            if trigger == "manual":
                _add_execution(
                    session,
                    adapter_id=adapter["id"],
                    version_id=version["id"],
                    trigger=trigger,
                    status="pending",
                    created_at=now,
                )
            if trigger == "webhook":
                _add_execution(
                    session,
                    adapter_id=adapter["id"],
                    version_id=version["id"],
                    trigger=trigger,
                    status="running",
                    created_at=now,
                )

    with session_factory() as session:
        report = cleanup_execution_retention(session, now=now + timedelta(days=1))
        assert report.deleted == 3
        assert report.batches == 3

    with session_factory() as session:
        for trigger in ("webhook", "manual", "schedule"):
            adapter, _ = adapters[trigger]
            assert session.scalar(
                select(func.count())
                .select_from(Execution)
                .where(Execution.adapter_id == adapter["id"], Execution.trigger == trigger)
            ) == (3 if trigger in ("webhook", "manual") else 2)
        active = session.scalars(
            select(Execution).where(Execution.status.in_(("pending", "running")))
        ).all()
        assert len(active) == 2

    with session_factory() as session:
        assert cleanup_execution_retention(session, now=now + timedelta(days=1)).deleted == 0


def test_retention_summary_log_has_exact_fields_without_formatting_error(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    caplog,
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="dlr.control.retention"),
        session_factory() as session,
    ):
        report = cleanup_execution_retention(session, now=datetime.now(UTC))

    summaries = [
        record
        for record in caplog.records
        if record.name == "dlr.control.retention"
        and record.msg.startswith("retention cycle complete:")
    ]
    assert len(summaries) == 1
    assert summaries[0].getMessage() == (
        "retention cycle complete: "
        f"deleted={report.deleted} batches={report.batches} "
        f"failures={report.failures} elapsed_ms={report.elapsed_ms}"
    )
    assert not any(record.exc_info for record in summaries)


def test_cleanup_applies_age_cutoff_and_removes_all_execution_payload_fields(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    adapter = create_adapter(api_client, name="retention-age")
    version = save_version(api_client, adapter["id"])
    now = datetime.now(UTC)
    monkeypatch.setattr(settings, "execution_retention_webhook_days", 7)
    monkeypatch.setattr(settings, "execution_retention_task_days", 7)
    monkeypatch.setattr(settings, "execution_retention_schedule_days", 7)
    monkeypatch.setattr(settings, "execution_retention_webhook_max_per_adapter", 100)
    monkeypatch.setattr(settings, "execution_retention_task_max_per_adapter", 100)
    monkeypatch.setattr(settings, "execution_retention_schedule_max_per_adapter", 100)

    with session_factory.begin() as session:
        _add_execution(
            session,
            adapter_id=adapter["id"],
            version_id=version["id"],
            trigger="manual",
            status="failed",
            created_at=now - timedelta(days=8),
        )
        _add_execution(
            session,
            adapter_id=adapter["id"],
            version_id=version["id"],
            trigger="manual",
            status="succeeded",
            created_at=now,
        )

    with session_factory() as session:
        report = cleanup_execution_retention(session, now=now)
        assert report.deleted == 1
        rows = session.scalars(select(Execution).where(Execution.adapter_id == adapter["id"])).all()
        assert len(rows) == 1
        assert rows[0].status == "succeeded"


def test_retention_preserves_pending_outbox_and_rechecks_before_delete(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    adapter = create_adapter(api_client, name="retention-pending-outbox")
    version = save_version(api_client, adapter["id"])
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    policy = RetentionPolicy("manual", 7, 100)
    monkeypatch.setattr(settings, "execution_retention_task_days", policy.days)
    monkeypatch.setattr(
        settings,
        "execution_retention_task_max_per_adapter",
        policy.max_per_adapter,
    )

    with session_factory.begin() as session:
        _add_execution(
            session,
            adapter_id=adapter["id"],
            version_id=version["id"],
            trigger="manual",
            status="succeeded",
            created_at=old,
        )

    with session_factory() as session:
        execution = session.scalar(select(Execution).where(Execution.adapter_id == adapter["id"]))
        assert execution is not None
        execution_id = execution.id
        session.commit()

    # The first query must exclude an already pending responsibility.
    with session_factory.begin() as session:
        session.add(
            ExecutionOutbox(
                execution_id=execution_id,
                dispatch_generation=1,
                message_id=uuid4(),
                routing_key="worker.test",
                payload_json={"execution_id": execution_id},
                payload_bytes=20,
                available_at=old,
                created_at=old,
            )
        )
    with session_factory() as session:
        report = cleanup_execution_retention(session, now=now)
        assert report.deleted == 0
        assert session.get(Execution, execution_id) is not None

    with session_factory.begin() as session:
        session.execute(
            ExecutionOutbox.__table__.update()
            .where(ExecutionOutbox.execution_id == execution_id)
            .values(status="published")
        )

    # Recheck the selected IDs after another transaction creates pending work.
    with session_factory() as selector:
        selected = list(selector.scalars(_terminal_query(policy, adapter["id"])))
        assert selected == [execution_id]
        with session_factory.begin() as concurrent:
            concurrent.add(
                ExecutionOutbox(
                    execution_id=execution_id,
                    dispatch_generation=2,
                    message_id=uuid4(),
                    routing_key="worker.test",
                    payload_json={"execution_id": execution_id, "generation": 2},
                    payload_bytes=30,
                    available_at=old,
                    created_at=old,
                )
            )
        assert _delete_batch(selector, selected) == 0

    # Once all Outbox responsibilities are published, rowcount is the actual
    # deletion result and the terminal Execution may be removed.
    with session_factory.begin() as session:
        session.execute(
            ExecutionOutbox.__table__.update()
            .where(ExecutionOutbox.execution_id == execution_id)
            .values(status="published")
        )
    with session_factory() as session:
        assert _delete_batch(session, [execution_id]) == 1
        assert session.get(Execution, execution_id) is None


def test_rabbitmq_retention_requires_admission_release_and_reconciles_safely(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    """Retention must not erase the row needed to repair an Admission leak."""

    adapter = create_adapter(api_client, name="retention-rabbitmq-release")
    version = save_version(api_client, adapter["id"])
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    policy = RetentionPolicy("manual", 7, 100)
    monkeypatch.setattr(settings, "execution_retention_task_days", policy.days)
    monkeypatch.setattr(
        settings,
        "execution_retention_task_max_per_adapter",
        policy.max_per_adapter,
    )

    with session_factory.begin() as session:
        unreleased = _add_rabbit_terminal(
            session,
            adapter_id=adapter["id"],
            version_id=version["id"],
            created_at=old,
        )
        admission.reserve_admission(session, adapter["id"], 10)

        pending_outbox = _add_rabbit_terminal(
            session,
            adapter_id=adapter["id"],
            version_id=version["id"],
            created_at=old,
        )
        admission.reserve_admission(session, adapter["id"], 10)
        admission.release_admission_once(session, pending_outbox, now=now)
        session.add(
            ExecutionOutbox(
                execution_id=pending_outbox.id,
                dispatch_generation=1,
                message_id=uuid4(),
                routing_key="worker.test",
                payload_json={"execution_id": pending_outbox.id},
                payload_bytes=20,
                available_at=old,
                created_at=old,
            )
        )

        idempotent = _add_rabbit_terminal(
            session,
            adapter_id=adapter["id"],
            version_id=version["id"],
            created_at=old,
        )
        admission.reserve_admission(session, adapter["id"], 10)
        admission.release_admission_once(session, idempotent, now=now)
        session.add(
            ExecutionIdempotencyRecord(
                adapter_id=adapter["id"],
                key_hash=b"k" * 32,
                payload_hash=b"p" * 32,
                execution_id=idempotent.id,
                created_at=old,
                expires_at=now + timedelta(hours=1),
            )
        )

        released = _add_rabbit_terminal(
            session,
            adapter_id=adapter["id"],
            version_id=version["id"],
            created_at=old,
        )
        admission.reserve_admission(session, adapter["id"], 10)
        admission.release_admission_once(session, released, now=now)

    with session_factory() as session:
        report = cleanup_execution_retention(session, now=now)
        assert report.deleted == 1
        assert session.get(Execution, unreleased.id) is not None
        assert session.get(Execution, pending_outbox.id) is not None
        assert session.get(Execution, idempotent.id) is not None
        assert session.get(Execution, released.id) is None
        adapter_counter = session.get(AdapterExecutionAdmission, adapter["id"])
        global_counter = session.get(GlobalExecutionAdmission, "global")
        assert adapter_counter is not None
        assert global_counter is not None
        assert (adapter_counter.outstanding_count, adapter_counter.outstanding_bytes) == (1, 10)
        assert (global_counter.outstanding_count, global_counter.outstanding_bytes) == (1, 10)

    # A repair cycle records the missing release without touching Execution's
    # business terminal state; the next retention cycle may then remove it.
    with session_factory() as session:
        report = admission.reconcile_admission(session, adapter_id=adapter["id"])
        assert report.adapters_checked == 1
        repaired = session.get(Execution, unreleased.id)
        assert repaired is not None
        assert repaired.status == "succeeded"
        assert repaired.admission_released_at is not None

    with session_factory() as session:
        assert cleanup_execution_retention(session, now=now).deleted == 1
        assert session.get(Execution, unreleased.id) is None

    with session_factory() as session:
        # Repeated cycles are idempotent and do not hide a counter leak.
        assert cleanup_execution_retention(session, now=now).deleted == 0
        adapter_counter = session.get(AdapterExecutionAdmission, adapter["id"])
        global_counter = session.get(GlobalExecutionAdmission, "global")
        assert adapter_counter is not None
        assert global_counter is not None
        assert (adapter_counter.outstanding_count, adapter_counter.outstanding_bytes) == (0, 0)
        assert (global_counter.outstanding_count, global_counter.outstanding_bytes) == (0, 0)


def test_rabbitmq_retention_rechecks_release_before_delete(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch,
) -> None:
    adapter = create_adapter(api_client, name="retention-rabbitmq-recheck")
    version = save_version(api_client, adapter["id"])
    now = datetime.now(UTC)
    old = now - timedelta(days=8)
    policy = RetentionPolicy("manual", 7, 100)
    monkeypatch.setattr(settings, "execution_retention_task_days", policy.days)
    monkeypatch.setattr(
        settings,
        "execution_retention_task_max_per_adapter",
        policy.max_per_adapter,
    )

    with session_factory.begin() as session:
        execution = _add_rabbit_terminal(
            session,
            adapter_id=adapter["id"],
            version_id=version["id"],
            created_at=old,
            admission_released_at=now,
        )

    with session_factory() as selector:
        selected = list(selector.scalars(_terminal_query(policy, adapter["id"])))
        assert selected == [execution.id]
        with session_factory.begin() as concurrent:
            concurrent.execute(
                Execution.__table__.update()
                .where(Execution.id == execution.id)
                .values(admission_released_at=None)
            )
        assert _delete_batch(selector, selected) == 0

    with session_factory() as session:
        assert session.get(Execution, execution.id) is not None
        assert cleanup_execution_retention(session, now=now).deleted == 0
