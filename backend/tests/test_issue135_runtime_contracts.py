"""Issue #135 runtime metadata and cancellation contracts."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.models import (
    AdapterExecutionAdmission,
    AdapterExecutionSlot,
    Execution,
    ExecutionAttempt,
    ExecutionOutbox,
    GlobalExecutionAdmission,
    Worker,
)
from dlr.control.schemas.reliable_runtime import AttemptPrepareFailedBody, AttemptResultBody
from dlr.control.services import attempt as attempt_service
from dlr.control.services import execution as execution_service
from test_issue130_b2_runtime import (
    _claim,
    _dispatch,
    _enable_runtime,
    _execution,
    _rabbit_adapter,
    _ready_worker,
)


def test_worker_metadata_matches_published_migration_objects(test_engine: Engine) -> None:
    """The ORM describes the Worker objects already published by migration 0031."""
    worker_table = Worker.__table__
    metadata_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in worker_table.constraints
        if constraint.name is not None and hasattr(constraint, "sqltext")
    }
    metadata_indexes = {
        index.name: [column.name for column in index.columns] for index in worker_table.indexes
    }

    schema = inspect(test_engine)
    database_checks = {
        constraint["name"]: constraint["sqltext"]
        for constraint in schema.get_check_constraints("workers")
    }
    database_indexes = {
        index["name"]: index["column_names"] for index in schema.get_indexes("workers")
    }

    expected_expression = "isolation_preflight_status IN ('unknown', 'passed', 'failed')"
    assert metadata_checks["ck_workers_isolation_preflight_status"] == expected_expression
    migrated_expression = database_checks["ck_workers_isolation_preflight_status"]
    assert "isolation_preflight_status" in migrated_expression
    assert all(value in migrated_expression for value in ("unknown", "passed", "failed"))
    expected_columns = ["protocol_version", "rabbitmq_execution_v3"]
    assert metadata_indexes["ix_workers_rabbitmq_execution_v3"] == expected_columns
    assert database_indexes["ix_workers_rabbitmq_execution_v3"] == expected_columns
    assert list(database_checks).count("ck_workers_isolation_preflight_status") == 1
    assert list(database_indexes).count("ix_workers_rabbitmq_execution_v3") == 1

    with test_engine.begin() as connection:
        worker_id = connection.scalar(
            Worker.__table__.insert()
            .values(
                name="issue135-existing-worker",
                status="offline",
                capabilities=["python"],
                isolation_preflight_status="passed",
                rabbitmq_execution_v3=True,
            )
            .returning(Worker.id)
        )
        preserved = connection.execute(
            select(
                Worker.name,
                Worker.status,
                Worker.isolation_preflight_status,
                Worker.rabbitmq_execution_v3,
            ).where(Worker.id == worker_id)
        ).one()
    assert preserved == ("issue135-existing-worker", "offline", "passed", True)


@pytest.mark.parametrize("initial_status", ["queued", "retry_wait"])
def test_unclaimed_cancellation_uses_canonical_code_and_preserves_history(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    initial_status: str,
) -> None:
    """Direct cancellation is canonical, idempotent, and leaves old terminal codes alone."""
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"issue135-{initial_status}-worker")
    adapter = _rabbit_adapter(api_client, worker, f"issue135-{initial_status}-adapter")
    execution = _execution(api_client, adapter["id"])
    dispatch = _dispatch(session_factory, execution["id"])
    if initial_status == "retry_wait":
        with session_factory.begin() as session:
            row = session.get(Execution, execution["id"])
            assert row is not None
            row.status = "retry_wait"

    cancelled = api_client.post(f"/api/executions/{execution['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["error_code"] == "execution_cancelled"
    assert cancelled.json()["last_error_code"] == "execution_cancelled"
    detail = api_client.get(f"/api/executions/{execution['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["error_code"] == "execution_cancelled"
    assert detail.json()["last_error_code"] == "execution_cancelled"

    repeated = api_client.post(f"/api/executions/{execution['id']}/cancel")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["error_code"] == "execution_cancelled"
    assert repeated.json()["last_error_code"] == "execution_cancelled"

    claim_after_cancel = _claim(session_factory, worker["id"], dispatch)
    assert claim_after_cancel.decision == "ACK_NOOP"
    assert claim_after_cancel.reason == "cancelled"
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        outbox_row = session.scalar(
            select(ExecutionOutbox).where(ExecutionOutbox.execution_id == execution["id"])
        )
        adapter_admission = session.get(AdapterExecutionAdmission, adapter["id"])
        global_admission = session.get(GlobalExecutionAdmission, "global")
        assert row is not None and row.admission_released_at is not None
        released_at = row.admission_released_at
        assert row.attempt_count == 0
        assert outbox_row is not None and outbox_row.last_error_code == "execution_cancelled"
        assert adapter_admission is not None and adapter_admission.outstanding_count == 0
        assert global_admission is not None and global_admission.outstanding_count == 0

    # A historical terminal row is observation-only; the normalization does
    # not rewrite its old machine code or release resources a second time.
    with session_factory.begin() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        row.error_code = "cancelled"
        row.last_error_code = "cancelled"
    historical = api_client.post(f"/api/executions/{execution['id']}/cancel")
    assert historical.status_code == 200, historical.text
    assert historical.json()["error_code"] == "cancelled"
    assert historical.json()["last_error_code"] == "cancelled"
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None and row.admission_released_at == released_at


def test_claim_observing_cancel_flag_uses_canonical_code_without_attempt(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued cancel flag converges before Claim without creating an Attempt or Slot."""
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue135-claim-flag-worker")
    adapter = _rabbit_adapter(api_client, worker, "issue135-claim-flag-adapter")
    execution = _execution(api_client, adapter["id"])
    dispatch = _dispatch(session_factory, execution["id"])
    with session_factory.begin() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        row.cancel_requested = True

    decision = _claim(session_factory, worker["id"], dispatch)
    assert decision.decision == "ACK_NOOP"
    assert decision.reason == "cancelled"
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        attempts = list(
            session.scalars(
                select(ExecutionAttempt).where(ExecutionAttempt.execution_id == execution["id"])
            )
        )
        slot = session.scalar(
            select(AdapterExecutionSlot).where(AdapterExecutionSlot.adapter_id == adapter["id"])
        )
        adapter_admission = session.get(AdapterExecutionAdmission, adapter["id"])
        global_admission = session.get(GlobalExecutionAdmission, "global")
        assert row is not None and row.status == "cancelled"
        assert row.error_code == "execution_cancelled"
        assert row.last_error_code == "execution_cancelled"
        assert row.admission_released_at is not None
        assert attempts == []
        assert slot is not None
        assert slot.active_attempt_id is None and slot.lease_expires_at is None
        assert adapter_admission is not None and adapter_admission.outstanding_count == 0
        assert global_admission is not None and global_admission.outstanding_count == 0


@pytest.mark.parametrize("first_terminal", ["cancel", "result"])
def test_running_cancel_result_orders_preserve_terminal_and_release_once(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    first_terminal: str,
) -> None:
    """The first authoritative terminal path wins without stale-fence or double release."""
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"issue135-{first_terminal}-worker")
    adapter = _rabbit_adapter(api_client, worker, f"issue135-{first_terminal}-adapter")
    execution = _execution(api_client, adapter["id"])
    claimed = _claim(session_factory, worker["id"], _dispatch(session_factory, execution["id"]))
    assert claimed.payload is not None and claimed.attempt_id is not None

    result_payload = AttemptResultBody.model_validate(
        {
            "attempt_id": claimed.attempt_id,
            "fencing_token": claimed.payload.fencing_token,
            "claim_token": claimed.payload.claim_token,
            "status": "succeeded",
            "workspace_cleanup_status": "completed",
        }
    )
    release_counts: dict[str, int] = {"admission": 0, "lease": 0, "slot": 0}
    original_admission_release = attempt_service.admission.release_admission_once
    original_lease_release = execution_service.release_execution_leases
    original_slot_release = attempt_service._release_slot_locked

    def count_admission(*args: Any, **kwargs: Any) -> bool:
        release_counts["admission"] += 1
        return original_admission_release(*args, **kwargs)

    def count_leases(*args: Any, **kwargs: Any) -> None:
        release_counts["lease"] += 1
        original_lease_release(*args, **kwargs)

    def count_slot(*args: Any, **kwargs: Any) -> bool:
        release_counts["slot"] += 1
        return original_slot_release(*args, **kwargs)

    monkeypatch.setattr(attempt_service.admission, "release_admission_once", count_admission)
    monkeypatch.setattr(execution_service, "release_execution_leases", count_leases)
    monkeypatch.setattr(attempt_service, "_release_slot_locked", count_slot)

    if first_terminal == "cancel":
        with session_factory() as session:
            requested = execution_service.cancel_execution(session, execution["id"])
            assert requested.status == "running" and requested.cancel_requested is True
        with session_factory() as session:
            result = attempt_service.finish_attempt(
                session, worker["id"], claimed.attempt_id, result_payload
            )
        assert result.reason == "terminal_recorded"
    else:
        with session_factory() as session:
            result = attempt_service.finish_attempt(
                session, worker["id"], claimed.attempt_id, result_payload
            )
        assert result.reason == "terminal_recorded"
        with session_factory() as session:
            observed = execution_service.cancel_execution(session, execution["id"])
            assert observed.status == "succeeded"

    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        attempt = session.get(ExecutionAttempt, claimed.attempt_id)
        slot = session.scalar(
            select(AdapterExecutionSlot).where(AdapterExecutionSlot.adapter_id == adapter["id"])
        )
        adapter_admission = session.get(AdapterExecutionAdmission, adapter["id"])
        global_admission = session.get(GlobalExecutionAdmission, "global")
        assert row is not None and attempt is not None and slot is not None
        assert attempt.fencing_token == claimed.payload.fencing_token
        assert slot.active_attempt_id is None and slot.lease_expires_at is None
        assert adapter_admission is not None and adapter_admission.outstanding_count == 0
        assert global_admission is not None and global_admission.outstanding_count == 0
        if first_terminal == "cancel":
            assert row.status == "cancelled"
            assert row.error_code == "execution_cancelled"
            assert row.last_error_code == "execution_cancelled"
            assert attempt.error_code == "execution_succeeded"
        else:
            assert row.status == "succeeded"
            assert row.error_code is None and row.last_error_code is None
            assert attempt.error_code == "execution_succeeded"

    assert release_counts == {"admission": 1, "lease": 1, "slot": 1}


@pytest.mark.parametrize("terminal_path", ["lease_recovery", "prepare_failed"])
def test_cancel_flag_preserves_non_cancel_attempt_error_code(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    terminal_path: str,
) -> None:
    """Cancellation normalizes the Execution without erasing the Attempt's own outcome."""
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"issue135-{terminal_path}-worker")
    adapter = _rabbit_adapter(api_client, worker, f"issue135-{terminal_path}-adapter")
    execution = _execution(api_client, adapter["id"])
    claimed = _claim(session_factory, worker["id"], _dispatch(session_factory, execution["id"]))
    assert claimed.payload is not None and claimed.attempt_id is not None
    with session_factory() as session:
        requested = execution_service.cancel_execution(session, execution["id"])
        assert requested.status == "running" and requested.cancel_requested is True

    if terminal_path == "lease_recovery":
        with session_factory.begin() as session:
            attempt = session.get(ExecutionAttempt, claimed.attempt_id)
            assert attempt is not None
            attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        with session_factory() as session:
            assert attempt_service.recover_expired_attempts(session, limit=10) == 1
        expected_status = "worker_lost"
        expected_code = "worker_lost"
    else:
        with session_factory() as session:
            decision = attempt_service.prepare_failed(
                session,
                worker["id"],
                claimed.attempt_id,
                AttemptPrepareFailedBody.model_validate(
                    {
                        "attempt_id": claimed.attempt_id,
                        "fencing_token": claimed.payload.fencing_token,
                        "claim_token": claimed.payload.claim_token,
                        "error_code": "dependency_preparation_failed",
                        "error_class": "platform_transient",
                    }
                ),
            )
        assert decision.reason == "terminal_recorded"
        expected_status = "failed"
        expected_code = "dependency_preparation_failed"

    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        attempt = session.get(ExecutionAttempt, claimed.attempt_id)
        assert row is not None and attempt is not None
        assert row.status == "cancelled"
        assert row.error_code == "execution_cancelled"
        assert row.last_error_code == "execution_cancelled"
        assert attempt.status == expected_status
        assert attempt.error_code == expected_code
