"""M5.11 Wave B terminal Execution retention tests."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import Execution
from dlr.control.services.retention import cleanup_execution_retention
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
