"""Issue #223 core behavior through authenticated APIs and real PostgreSQL."""

import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from dlr.control.models import AdapterBatch, AdapterBatchItem, AdapterSchedule, Execution
from dlr.control.schemas.adapter_batch import BatchCreate
from dlr.control.security import SUPERADMIN_PRINCIPAL
from dlr.control.services import adapter_batch
from dlr.control.services.adapter import domain_error
from test_adapters import create_adapter, save_version
from test_issue219_management import setup_subscription
from test_schedule_trigger import put_schedule
from test_webhook_trigger import setup_webhook


def prepare(client, ids, action="start", *, batch_id=None):
    payload = {
        "id": str(batch_id or uuid.uuid4()),
        "action": action,
        "targets": [
            {
                "adapter_id": adapter_id,
                "expected_token": client.get(f"/api/adapters/{adapter_id}").json()["batch_token"],
            }
            for adapter_id in ids
        ],
    }
    response = client.post("/api/adapter-batches", json=payload)
    assert response.status_code == 200, response.text
    return response.json(), payload


def run(client, batch):
    response = client.post(f"/api/adapter-batches/{batch['id']}/execute")
    assert response.status_code == 200, response.text
    return response.json()


def scheduled_adapter(client, name="scheduled"):
    adapter = create_adapter(client, name=name)
    save_version(client, adapter["id"])
    response = client.patch(f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"})
    assert response.status_code == 200, response.text
    return response.json()


def test_prepare_is_read_only_and_uuid_detects_another_intent(api_client, session_factory):
    adapter = create_adapter(api_client)
    save_version(api_client, adapter["id"])
    batch, payload = prepare(api_client, [adapter["id"]])
    assert batch["items"][0]["action"] == "run_once"
    assert batch["items"][0]["status"] == "pending"
    again = api_client.post("/api/adapter-batches", json=payload)
    assert again.status_code == 200 and again.json() == batch
    changed = api_client.post("/api/adapter-batches", json={**payload, "action": "stop"})
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "batch_operation_conflict"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        assert session.scalar(select(func.count()).select_from(AdapterBatch)) == 1


@pytest.mark.parametrize(
    "targets",
    [
        [],
        [{"adapter_id": 1, "expected_token": "a" * 64}] * 2,
        [{"adapter_id": i, "expected_token": "a" * 64} for i in range(1, 102)],
    ],
)
def test_prepare_rejects_empty_duplicates_and_overlimit(api_client, targets, session_factory):
    response = api_client.post(
        "/api/adapter-batches",
        json={
            "id": str(uuid.uuid4()),
            "action": "start",
            "targets": targets,
        },
    )
    assert response.status_code == 422
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AdapterBatch)) == 0


def test_one_hundred_targets_are_all_reported_without_existence_leak(api_client):
    response = api_client.post(
        "/api/adapter-batches",
        json={
            "id": str(uuid.uuid4()),
            "action": "stop",
            "targets": [
                {"adapter_id": value, "expected_token": "a" * 64} for value in range(1, 101)
            ],
        },
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["adapter_id"] for item in items] == list(range(1, 101))
    assert all(item["error_code"] == "adapter_not_found" for item in items)
    assert all(item["adapter_name"] is None and item["action"] is None for item in items)
    assert all(item["executions"] == [] and item["subscription_id"] is None for item in items)


@pytest.mark.parametrize("adapter_ids", [[True], ["1"], [0], [1, 1], []])
def test_retry_ids_are_strict_positive_distinct_integers(api_client, adapter_ids):
    response = api_client.post(
        f"/api/adapter-batches/{uuid.uuid4()}/retry", json={"adapter_ids": adapter_ids}
    )
    assert response.status_code == 422


def test_mixed_start_keeps_single_semantics_and_success_replay(
    api_client, monkeypatch, session_factory
):
    manual = create_adapter(api_client, name="manual")
    save_version(api_client, manual["id"])
    scheduled = scheduled_adapter(api_client)
    assert put_schedule(api_client, scheduled["id"], enabled=False).status_code == 200
    hook, _, _, _, _ = setup_webhook(api_client, "hook", enabled=False)
    event, _, _ = setup_subscription(api_client, monkeypatch, name="events")
    missing = create_adapter(api_client, name="missing-code")
    batch, _ = prepare(
        api_client, [manual["id"], scheduled["id"], hook["id"], event["id"], missing["id"]]
    )
    result = run(api_client, batch)
    items = result["items"]
    assert [item["action"] for item in items] == [
        "run_once",
        "enable_schedule",
        "enable_webhook",
        "enable_subscription",
        "run_once",
    ]
    assert [item["status"] for item in items] == [
        "accepted",
        "succeeded",
        "succeeded",
        "accepted",
        "failed",
    ]
    assert items[-1]["error_code"] == "adapter_has_no_version"
    assert not items[-1]["retryable"]
    again = run(api_client, batch)
    assert again["items"][0]["execution_ids"] == items[0]["execution_ids"]
    with session_factory() as session:
        rows = list(session.scalars(select(Execution)))
        assert len(rows) == 1 and rows[0].adapter_id == manual["id"]
        assert rows[0].trigger == "manual"
        assert (
            session.scalar(
                select(AdapterSchedule.enabled).where(AdapterSchedule.adapter_id == scheduled["id"])
            )
            is True
        )


def test_retry_only_explicit_temporary_failure_and_success_kept(
    api_client, monkeypatch, session_factory
):
    good = create_adapter(api_client, name="good")
    bad = create_adapter(api_client, name="temporary-capacity")
    for adapter in [good, bad]:
        save_version(api_client, adapter["id"])
    batch, _ = prepare(api_client, [good["id"], bad["id"]])
    original = adapter_batch.execution._create_execution_locked

    def fail_one(session, adapter, **kwargs):
        if adapter.id == bad["id"]:
            raise domain_error(429, "runtime_capacity_full", "Capacity is temporarily full")
        return original(session, adapter, **kwargs)

    monkeypatch.setattr(adapter_batch.execution, "_create_execution_locked", fail_one)
    result = run(api_client, batch)
    assert [item["status"] for item in result["items"]] == ["accepted", "failed"]
    assert result["items"][1]["retryable"]
    original_id = result["items"][0]["execution_ids"]
    rejected = api_client.post(
        f"/api/adapter-batches/{batch['id']}/retry", json={"adapter_ids": [good["id"], bad["id"]]}
    )
    assert rejected.status_code == 409
    monkeypatch.setattr(adapter_batch.execution, "_create_execution_locked", original)
    retry = api_client.post(
        f"/api/adapter-batches/{batch['id']}/retry", json={"adapter_ids": [bad["id"]]}
    )
    assert retry.status_code == 200, retry.text
    assert [item["status"] for item in retry.json()["items"]] == ["accepted", "accepted"]
    assert retry.json()["items"][0]["execution_ids"] == original_id
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Execution)) == 2


def test_per_item_failure_rolls_back_mutation_and_receipt_is_atomic(
    api_client, monkeypatch, session_factory
):
    first = create_adapter(api_client, name="rejected")
    second = create_adapter(api_client, name="accepted")
    for adapter in [first, second]:
        save_version(api_client, adapter["id"])
    batch, _ = prepare(api_client, [first["id"], second["id"]])
    original = adapter_batch._apply

    def apply_then_reject(session, operation, item, principal):
        original(session, operation, item, principal)
        if item.adapter_id == first["id"]:
            raise domain_error(409, "adapter_busy", "Injected definite domain rejection")

    monkeypatch.setattr(adapter_batch, "_apply", apply_then_reject)
    result = run(api_client, batch)
    assert [item["status"] for item in result["items"]] == ["failed", "accepted"]
    with session_factory() as session:
        executions = list(session.scalars(select(Execution)))
        assert len(executions) == 1 and executions[0].adapter_id == second["id"]
        item = session.get(AdapterBatchItem, (uuid.UUID(batch["id"]), first["id"]))
        assert item.execution_ids == [] and item.status == "failed"


def test_stop_schedule_preserves_pending_execution_and_block_reason(api_client, session_factory):
    adapter = scheduled_adapter(api_client)
    assert put_schedule(api_client, adapter["id"], enabled=True).status_code == 200
    accepted = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}).json()
    with session_factory.begin() as session:
        row = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        row.last_blocked_reason = "input_invalid"
        row.last_blocked_detail = {"reason": "existing failure"}
    batch, _ = prepare(api_client, [adapter["id"]], "stop")
    item = run(api_client, batch)["items"][0]
    assert item["status"] == "succeeded" and item["action"] == "disable_schedule"
    with session_factory() as session:
        row = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert not row.enabled and row.next_run_at is None
        assert row.last_blocked_reason == "input_invalid"
        assert row.last_blocked_detail == {"reason": "existing failure"}
        assert session.get(Execution, accepted["id"]).status == "queued"


def test_token_ignores_name_and_scheduler_progress(api_client, session_factory):
    adapter = scheduled_adapter(api_client)
    assert put_schedule(api_client, adapter["id"], enabled=False).status_code == 200
    first = api_client.get(f"/api/adapters/{adapter['id']}").json()["batch_token"]
    assert (
        api_client.patch(f"/api/adapters/{adapter['id']}", json={"name": "renamed"}).status_code
        == 200
    )
    with session_factory.begin() as session:
        row = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        row.next_run_at = row.updated_at
        row.last_processed_due_at = row.updated_at
    assert api_client.get(f"/api/adapters/{adapter['id']}").json()["batch_token"] == first
    assert (
        put_schedule(api_client, adapter["id"], enabled=False, cron="30 * * * *").status_code == 200
    )
    assert api_client.get(f"/api/adapters/{adapter['id']}").json()["batch_token"] != first


def test_migration_preserves_business_rows_and_refuses_to_drop_receipts(test_engine, monkeypatch):
    name = "dlr_test_issue223_upgrade_" + uuid.uuid4().hex[:10]
    maintenance = create_engine(
        test_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with maintenance.connect() as connection:
        connection.execute(text(f"CREATE DATABASE {name}"))
    engine = create_engine(test_engine.url.set(database=name))
    config = Config()
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        command.upgrade(config, "0049_issue219_queue_sources")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO adapters (name,language,adapter_type) "
                    "VALUES ('retained-adapter','python','task')"
                )
            )
            before = connection.scalar(text("SELECT row_to_json(a) FROM adapters a"))
        command.upgrade(config, "0050_issue223_adapter_batches")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT row_to_json(a) FROM adapters a")) == before
            assert connection.scalar(text("SELECT count(*) FROM adapter_batches")) == 0
        command.downgrade(config, "0049_issue219_queue_sources")
        command.upgrade(config, "0050_issue223_adapter_batches")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO adapter_batches (id,owner_key,action,request_hash) "
                    "VALUES (:id,'superadmin','start',:hash)"
                ),
                {"id": uuid.uuid4(), "hash": "a" * 64},
            )
        with pytest.raises(RuntimeError, match="Preserve batch receipts"):
            command.downgrade(config, "0049_issue219_queue_sources")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM adapter_batches")) == 1
            assert connection.scalar(text("SELECT row_to_json(a) FROM adapters a")) == before
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0050_issue223_adapter_batches"
            )
    finally:
        engine.dispose()
        with maintenance.connect() as connection:
            connection.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        maintenance.dispose()


def test_accepted_event_start_stopped_by_single_action_does_not_poll_or_restart(
    api_client,
    monkeypatch,
):
    adapter, _, source = setup_subscription(api_client, monkeypatch, name="later-stopped")
    batch, _ = prepare(api_client, [adapter["id"]])
    assert run(api_client, batch)["items"][0]["status"] == "accepted"
    disabled = api_client.post(
        f"/api/event-subscriptions/{source['id']}/disable",
        json={"expected_config_version": source["config_version"]},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["connection_state"] == "stopped"
    activation_id = disabled.json()["activation_id"]
    observed = api_client.get(f"/api/adapter-batches/{batch['id']}").json()["items"][0]
    assert observed["status"] == "unknown"
    assert observed["error_code"] == "batch_result_unavailable"
    assert observed["subscription_state"] == "stopped"
    assert not observed["retryable"]
    assert run(api_client, batch)["items"][0] == observed
    current = api_client.get(f"/api/event-subscriptions/{source['id']}").json()
    assert current["desired_state"] == "disabled"
    assert current["activation_id"] == activation_id


def test_coordinator_uses_one_pool_connection_and_releases_advisory_lock(api_client, test_engine):
    adapter = create_adapter(api_client)
    save_version(api_client, adapter["id"])
    token = api_client.get(f"/api/adapters/{adapter['id']}").json()["batch_token"]
    # A second checked-out connection would timeout, exposing pool reservation
    # deadlocks rather than relying on default pool capacity to mask them.
    engine = create_engine(test_engine.url, pool_size=1, max_overflow=0, pool_timeout=0.2)
    try:
        with Session(engine, expire_on_commit=False) as session:
            session.execute(text("SELECT 1"))  # Existing authentication read transaction.
            batch = adapter_batch.prepare(
                session,
                BatchCreate(
                    id=uuid.uuid4(),
                    action="start",
                    targets=[{"adapter_id": adapter["id"], "expected_token": token}],
                ),
                SUPERADMIN_PRINCIPAL,
            )
            result = adapter_batch.execute(session, batch.id, SUPERADMIN_PRINCIPAL)
            assert result.items[0].status == "accepted"
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks "
                        "WHERE locktype='advisory' AND pid=pg_backend_pid()"
                    )
                )
                == 0
            )
    finally:
        engine.dispose()
