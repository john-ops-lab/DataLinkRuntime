"""Issue #134 bounded, restart-safe Attempt reconciliation contracts."""

from __future__ import annotations

import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, select, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.common.platform_logging import _RedactingFormatter
from dlr.control.models import (
    Adapter,
    AdapterExecutionAdmission,
    AdapterExecutionSlot,
    Execution,
    ExecutionArtifactHold,
    ExecutionAttempt,
    GlobalExecutionAdmission,
    RuntimeReconciliationCursor,
)
from dlr.control.schemas.reliable_runtime import AttemptRenewBody, AttemptResultBody
from dlr.control.services import attempt as attempt_service
from test_issue127_b2_binding import create_artifact
from test_issue130_b2_runtime import (
    _claim,
    _dispatch,
    _enable_runtime,
    _execution,
    _rabbit_adapter,
    _ready_worker,
)
from test_unified_runtime_migration import _isolated_schema, _upgrade


def _active_attempt(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    worker: dict[str, Any],
    name: str,
    *,
    expired: bool = True,
    invalid_snapshot: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    adapter = _rabbit_adapter(api_client, worker, f"issue134-{name}-adapter")
    execution = _execution(api_client, adapter["id"])
    claimed = _claim(session_factory, worker["id"], _dispatch(session_factory, execution["id"]))
    assert claimed.payload is not None and claimed.attempt_id is not None
    with session_factory.begin() as session:
        row = session.get(Execution, execution["id"])
        attempt = session.get(ExecutionAttempt, claimed.attempt_id)
        assert row is not None and attempt is not None
        if expired:
            attempt.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        if invalid_snapshot == "retry_policy_invalid":
            row.retry_policy_snapshot = {"private_marker": "must-not-be-logged"}
        elif invalid_snapshot == "resource_profile_invalid":
            row.resource_profile_snapshot = {"cpu_cores": 0}
    return adapter, execution, claimed.attempt_id


def test_reconciliation_migration_fresh_and_upgrade_preserve_state() -> None:
    for suffix, revision in (
        ("issue134_fresh", "head"),
        ("issue134_upgrade", "0038_issue138_languages"),
    ):
        with _isolated_schema(suffix, revision) as (engine, database):
            worker_id: int | None = None
            if revision != "head":
                with engine.begin() as connection:
                    worker_id = connection.scalar(
                        text(
                            "INSERT INTO workers (name, status, capabilities) "
                            "VALUES ('issue134-existing-worker', 'offline', '[\"python\"]'::jsonb) "
                            "RETURNING id"
                        )
                    )
                _upgrade(database, "head")

            schema = inspect(engine)
            assert "runtime_reconciliation_cursors" in schema.get_table_names()
            indexes = {item["name"]: item for item in schema.get_indexes("execution_attempts")}
            active_index = indexes["ix_execution_attempts_active_id"]
            assert active_index["column_names"] == ["id"]
            assert "status" in str(active_index["dialect_options"]["postgresql_where"])
            checks = {
                item["name"]
                for item in schema.get_check_constraints("runtime_reconciliation_cursors")
            }
            assert "ck_runtime_reconciliation_cursors_bounds" in checks
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                    "0044_issue161_cache_admin"
                )
                cursor = connection.execute(
                    text("SELECT name, after_id, upper_id FROM runtime_reconciliation_cursors")
                ).one()
                assert tuple(cursor) == ("expired_attempts", 0, 0)
                if worker_id is not None:
                    assert (
                        connection.scalar(
                            text("SELECT name FROM workers WHERE id = :id"), {"id": worker_id}
                        )
                        == "issue134-existing-worker"
                    )


def test_cursor_is_bounded_persistent_and_wraps_after_deleted_tail(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    with session_factory() as session:
        empty = attempt_service.reserve_recovery_candidates(session, limit=100_000)
    assert empty.attempt_ids == ()
    assert (empty.after_id, empty.upper_id) == (0, 0)

    # The immutable reservation is captured before commit. A caller using
    # SQLAlchemy's default expire_on_commit=True must not refresh the shared
    # cursor after another reconciler can acquire and advance it.
    cursor_queries: list[str] = []

    def observe_cursor(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if "FROM runtime_reconciliation_cursors" in statement:
            cursor_queries.append(statement)

    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", observe_cursor)
    try:
        expiring_factory = sessionmaker(bind=engine, expire_on_commit=True)
        with expiring_factory() as expiring_session:
            expiring = attempt_service.reserve_recovery_candidates(expiring_session, limit=1)
    finally:
        event.remove(engine, "before_cursor_execute", observe_cursor)
    assert (expiring.after_id, expiring.upper_id) == (0, 0)
    assert len(cursor_queries) == 1

    worker = _ready_worker(api_client, "issue134-cursor-worker")
    attempt_ids = [
        _active_attempt(
            api_client,
            session_factory,
            worker,
            f"cursor-{index}",
            expired=False,
        )[2]
        for index in range(3)
    ]
    with session_factory() as session:
        first = attempt_service.reserve_recovery_candidates(session, limit=2)
    assert first.attempt_ids == tuple(attempt_ids[:2])
    assert (first.after_id, first.upper_id, first.wrapped) == (
        attempt_ids[1],
        attempt_ids[2],
        False,
    )

    # A process restart reads the committed cursor. If the unreserved tail is
    # deleted, the next reservation performs at most two bounded candidate
    # queries and starts a fixed new cycle.
    with session_factory.begin() as session:
        tail = session.get(ExecutionAttempt, attempt_ids[2])
        assert tail is not None
        session.delete(tail)
    statements: list[str] = []

    def observe(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if "execution_attempts.id >" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        with session_factory() as restarted_session:
            restarted = attempt_service.reserve_recovery_candidates(restarted_session, limit=2)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert restarted.attempt_ids == tuple(attempt_ids[:2])
    assert restarted.wrapped is True
    assert len(statements) <= 2
    assert all("LIMIT" in statement for statement in statements)

    # Reserving without processing simulates a crash. The segment is skipped
    # until the next finite cycle, then becomes visible again.
    with session_factory() as session:
        crashed = attempt_service.reserve_recovery_candidates(session, limit=1)
    with session_factory() as session:
        following = attempt_service.reserve_recovery_candidates(session, limit=1)
    with session_factory() as session:
        wrapped = attempt_service.reserve_recovery_candidates(session, limit=1)
    assert crashed.attempt_ids != following.attempt_ids
    assert wrapped.attempt_ids == crashed.attempt_ids
    assert wrapped.wrapped is True


@pytest.mark.parametrize(("bad_count", "batch_size"), [(1, 1), (2, 2), (3, 2)])
def test_poison_rows_do_not_starve_later_expired_attempts(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    bad_count: int,
    batch_size: int,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"issue134-poison-{bad_count}-{batch_size}-worker")
    bad_rows = [
        _active_attempt(
            api_client,
            session_factory,
            worker,
            f"poison-{bad_count}-{batch_size}-{index}",
            invalid_snapshot="retry_policy_invalid",
        )
        for index in range(bad_count)
    ]
    good_adapter, good_execution, good_attempt_id = _active_attempt(
        api_client,
        session_factory,
        worker,
        f"poison-{bad_count}-{batch_size}-good",
    )
    with session_factory() as session:
        bad_lease_expiries = {
            attempt_id: session.get(ExecutionAttempt, attempt_id).lease_expires_at  # type: ignore[union-attr]
            for _adapter, _execution_row, attempt_id in bad_rows
        }
    candidate_count = bad_count + 1
    bound = 2 * (math.ceil(candidate_count / batch_size) + 1)
    recovered = 0
    calls = 0
    with caplog.at_level(logging.WARNING, logger="dlr.control.attempt"):
        while recovered == 0 and calls < bound:
            with session_factory() as session:
                recovered += attempt_service.recover_expired_attempts(session, limit=batch_size)
            calls += 1
    assert recovered == 1
    assert calls <= bound
    assert "must-not-be-logged" not in caplog.text
    assert (
        sum(
            message.startswith("attempt reconciliation row skipped:") for message in caplog.messages
        )
        >= bad_count
    )
    skipped = [
        record
        for record in caplog.records
        if record.getMessage().startswith("attempt reconciliation row skipped:")
    ]
    rendered = _RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    ).format(skipped[0])
    first_execution_id = bad_rows[0][1]["id"]
    first_attempt_id = bad_rows[0][2]
    for field in (
        f"attempt_id={first_attempt_id}",
        f"execution_id={first_execution_id}",
        "error_code=retry_policy_invalid",
        f"cursor_upper_id={good_attempt_id}",
        "wrapped=False",
        "index=1",
        f"count={batch_size}",
        "error_count=",
    ):
        assert field in rendered
    assert "must-not-be-logged" not in rendered

    with session_factory() as session:
        good_attempt = session.get(ExecutionAttempt, good_attempt_id)
        good_row = session.get(Execution, good_execution["id"])
        good_slot = session.scalar(
            select(AdapterExecutionSlot).where(
                AdapterExecutionSlot.adapter_id == good_adapter["id"]
            )
        )
        assert good_attempt is not None and good_attempt.status == "worker_lost"
        assert good_row is not None and good_row.status == "retry_wait"
        assert good_slot is not None and good_slot.active_attempt_id is None
        for adapter, execution, attempt_id in bad_rows:
            bad_attempt = session.get(ExecutionAttempt, attempt_id)
            bad_execution = session.get(Execution, execution["id"])
            bad_slot = session.scalar(
                select(AdapterExecutionSlot).where(AdapterExecutionSlot.adapter_id == adapter["id"])
            )
            adapter_admission = session.get(AdapterExecutionAdmission, adapter["id"])
            assert bad_attempt is not None and bad_attempt.status in {"claimed", "running"}
            assert bad_attempt.lease_expires_at == bad_lease_expiries[attempt_id]
            assert bad_attempt.ended_at is None and bad_attempt.error_code is None
            assert bad_execution is not None and bad_execution.status == "running"
            assert bad_execution.admission_released_at is None
            assert bad_slot is not None and bad_slot.active_attempt_id == attempt_id
            assert adapter_admission is not None and adapter_admission.outstanding_count == 1
        global_admission = session.get(GlobalExecutionAdmission, "global")
        assert global_admission is not None
        assert global_admission.outstanding_count == bad_count + 1


def test_fixed_upper_id_ignores_new_arrivals_until_next_cycle(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue134-upper-worker")
    _active_attempt(
        api_client,
        session_factory,
        worker,
        "upper-poison",
        invalid_snapshot="retry_policy_invalid",
    )
    _adapter, good_execution, good_attempt_id = _active_attempt(
        api_client, session_factory, worker, "upper-good"
    )
    with session_factory() as session:
        assert attempt_service.recover_expired_attempts(session, limit=1) == 0
    new_attempt_ids = [
        _active_attempt(api_client, session_factory, worker, f"upper-new-{index}")[2]
        for index in range(2)
    ]
    with session_factory() as session:
        assert attempt_service.recover_expired_attempts(session, limit=1) == 1
        cursor = session.get(RuntimeReconciliationCursor, "expired_attempts")
        assert cursor is not None and cursor.upper_id == good_attempt_id
    with session_factory() as session:
        good_attempt = session.get(ExecutionAttempt, good_attempt_id)
        good_row = session.get(Execution, good_execution["id"])
        assert good_attempt is not None and good_attempt.status == "worker_lost"
        assert good_row is not None and good_row.status == "retry_wait"
        assert all(
            session.get(ExecutionAttempt, attempt_id).status in {"claimed", "running"}  # type: ignore[union-attr]
            for attempt_id in new_attempt_ids
        )


@pytest.mark.parametrize("invalid_snapshot", ["retry_policy_invalid", "resource_profile_invalid"])
def test_only_closed_snapshot_errors_are_isolated(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    invalid_snapshot: str,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"issue134-{invalid_snapshot}-worker")
    _adapter, execution, attempt_id = _active_attempt(
        api_client,
        session_factory,
        worker,
        invalid_snapshot,
        invalid_snapshot=invalid_snapshot,
    )
    with session_factory() as session:
        assert attempt_service.recover_expired_attempts(session, limit=10) == 0
    with session_factory() as session:
        attempt = session.get(ExecutionAttempt, attempt_id)
        row = session.get(Execution, execution["id"])
        assert attempt is not None and attempt.status in {"claimed", "running"}
        assert row is not None and row.status == "running"
        assert row.admission_released_at is None


def test_two_reconcilers_converge_one_terminal_and_one_release(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue134-concurrent-worker")
    adapter, execution, attempt_id = _active_attempt(
        api_client, session_factory, worker, "concurrent"
    )
    barrier = threading.Barrier(2)

    def reconcile() -> int:
        with session_factory() as session:
            barrier.wait(timeout=5)
            return attempt_service.recover_expired_attempts(session, limit=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _value: reconcile(), (1, 2)))
    assert sum(results) == 1
    with session_factory() as session:
        attempt = session.get(ExecutionAttempt, attempt_id)
        row = session.get(Execution, execution["id"])
        slot = session.scalar(
            select(AdapterExecutionSlot).where(AdapterExecutionSlot.adapter_id == adapter["id"])
        )
        adapter_admission = session.get(AdapterExecutionAdmission, adapter["id"])
        global_admission = session.get(GlobalExecutionAdmission, "global")
        assert attempt is not None and attempt.status == "worker_lost"
        assert row is not None and row.status == "retry_wait"
        assert slot is not None and slot.active_attempt_id is None
        assert adapter_admission is not None and adapter_admission.outstanding_count == 1
        assert global_admission is not None and global_admission.outstanding_count == 1


@pytest.mark.parametrize("lock_target", ["cursor", "adapter", "attempt"])
def test_reconciliation_lock_wait_is_local_bounded_and_recoverable(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    lock_target: str,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"issue134-lock-{lock_target}-worker")
    adapter, execution, attempt_id = _active_attempt(
        api_client, session_factory, worker, f"lock-{lock_target}"
    )
    locked = threading.Event()
    release = threading.Event()

    def hold_row_lock() -> None:
        with session_factory() as holder:
            if lock_target == "cursor":
                statement = select(RuntimeReconciliationCursor).where(
                    RuntimeReconciliationCursor.name == "expired_attempts"
                )
            elif lock_target == "adapter":
                statement = select(Adapter).where(Adapter.id == adapter["id"])
            else:
                statement = select(ExecutionAttempt).where(ExecutionAttempt.id == attempt_id)
            assert holder.scalar(statement.with_for_update()) is not None
            locked.set()
            assert release.wait(timeout=10)
            holder.rollback()

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold_row_lock)
        assert locked.wait(timeout=5)
        try:
            with session_factory() as blocked:
                baseline_timeout = blocked.scalar(text("SHOW lock_timeout"))
                started = time.monotonic()
                with pytest.raises(OperationalError) as captured:
                    attempt_service.recover_expired_attempts(blocked, limit=1)
                elapsed = time.monotonic() - started
                assert getattr(captured.value.orig, "sqlstate", None) == "55P03"
                assert 0.5 <= elapsed < 5
                assert blocked.scalar(text("SHOW lock_timeout")) == baseline_timeout

            with session_factory() as session:
                attempt = session.get(ExecutionAttempt, attempt_id)
                row = session.get(Execution, execution["id"])
                slot = session.scalar(
                    select(AdapterExecutionSlot).where(
                        AdapterExecutionSlot.adapter_id == adapter["id"]
                    )
                )
                assert attempt is not None and attempt.status in {"claimed", "running"}
                assert row is not None and row.status == "running"
                assert slot is not None and slot.active_attempt_id == attempt_id
        finally:
            release.set()
        holder.result(timeout=5)

    with session_factory() as session:
        assert attempt_service.recover_expired_attempts(session, limit=1) == 1
    with session_factory() as session:
        attempt = session.get(ExecutionAttempt, attempt_id)
        row = session.get(Execution, execution["id"])
        assert attempt is not None and attempt.status == "worker_lost"
        assert row is not None and row.status == "retry_wait"


@pytest.mark.parametrize("competing_action", ["renew", "result"])
def test_renew_or_result_racing_recovery_has_one_authoritative_winner(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    competing_action: str,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"issue134-race-{competing_action}-worker")
    adapter = _rabbit_adapter(api_client, worker, f"issue134-race-{competing_action}-adapter")
    execution = _execution(api_client, adapter["id"])
    claimed = _claim(
        session_factory,
        worker["id"],
        _dispatch(session_factory, execution["id"]),
    )
    assert claimed.payload is not None and claimed.attempt_id is not None
    attempt_id = claimed.attempt_id
    original_expiry = claimed.payload.lease_expires_at
    monkeypatch.setattr(settings, "attempt_lease_seconds", settings.attempt_lease_seconds * 10)
    barrier = threading.Barrier(2)
    outcomes: dict[str, Any] = {}

    def recover() -> None:
        barrier.wait(timeout=5)
        with session_factory() as session:
            outcomes["recover"] = attempt_service.recover_expired_attempts(
                session,
                limit=10,
                now=original_expiry + timedelta(seconds=1),
            )

    def act() -> None:
        barrier.wait(timeout=5)
        with session_factory() as session:
            if competing_action == "renew":
                outcomes["action"] = attempt_service.renew_attempt(
                    session,
                    worker["id"],
                    attempt_id,
                    AttemptRenewBody(
                        attempt_id=attempt_id,
                        fencing_token=claimed.payload.fencing_token,
                        claim_token=claimed.payload.claim_token,
                    ),
                ).reason
            else:
                outcomes["action"] = attempt_service.finish_attempt(
                    session,
                    worker["id"],
                    attempt_id,
                    AttemptResultBody(
                        attempt_id=attempt_id,
                        fencing_token=claimed.payload.fencing_token,
                        claim_token=claimed.payload.claim_token,
                        status="succeeded",
                        workspace_cleanup_status="completed",
                    ),
                ).reason

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(recover), pool.submit(act)]
        for future in futures:
            future.result(timeout=10)

    with session_factory() as session:
        attempt = session.get(ExecutionAttempt, attempt_id)
        row = session.get(Execution, execution["id"])
        slot = session.scalar(
            select(AdapterExecutionSlot).where(AdapterExecutionSlot.adapter_id == adapter["id"])
        )
        assert attempt is not None and row is not None and slot is not None
        if competing_action == "renew" and outcomes["action"] == "renewed":
            assert outcomes["recover"] == 0
            assert attempt.status in {"claimed", "running"}
            assert row.status == "running"
            assert slot.active_attempt_id == attempt_id
            assert attempt.lease_expires_at > original_expiry + timedelta(seconds=1)
        elif competing_action == "result" and outcomes["action"] == "terminal_recorded":
            assert outcomes["recover"] == 0
            assert attempt.status == "succeeded" and row.status == "succeeded"
            assert slot.active_attempt_id is None
        else:
            assert outcomes["recover"] == 1
            assert outcomes["action"] == "already_terminal"
            assert attempt.status == "worker_lost" and row.status == "retry_wait"
            assert slot.active_attempt_id is None


def test_poison_recovery_does_not_block_due_retry_or_expired_hold(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue134-tick-worker")
    _active_attempt(
        api_client,
        session_factory,
        worker,
        "tick-poison",
        invalid_snapshot="retry_policy_invalid",
    )
    retry_adapter = _rabbit_adapter(api_client, worker, "issue134-tick-retry-adapter")
    retry_execution = _execution(api_client, retry_adapter["id"])
    artifact_id = create_artifact(
        session_factory,
        retry_adapter["id"],
        "issue134-expired-hold.txt",
        status="READY",
    )
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        retry_row = session.get(Execution, retry_execution["id"])
        assert retry_row is not None
        retry_row.status = "retry_wait"
        retry_row.next_attempt_at = now - timedelta(seconds=1)
        session.add(
            ExecutionArtifactHold(
                execution_id=retry_execution["id"],
                artifact_id=artifact_id,
                reason="dead_letter_replay",
                expires_at=now - timedelta(seconds=1),
                held_bytes=8,
            )
        )

    with session_factory() as session:
        result = attempt_service.attempt_reconciler_once(session, limit=10)
    assert result == {"recovered": 0, "retry_dispatched": 1, "holds_expired": 1}
    with session_factory() as session:
        retry_row = session.get(Execution, retry_execution["id"])
        hold = session.scalar(
            select(ExecutionArtifactHold).where(
                ExecutionArtifactHold.execution_id == retry_execution["id"]
            )
        )
        assert retry_row is not None and retry_row.status == "queued"
        assert retry_row.dispatch_generation == 2
        assert hold is not None and hold.purged_at is not None
        assert hold.purged_by == "system" and hold.purge_reason == "hold_expired"


def test_database_disconnect_and_unknown_error_remain_visible_without_convergence(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue134-visible-errors-worker")
    _adapter, execution, attempt_id = _active_attempt(
        api_client, session_factory, worker, "visible-errors"
    )

    original_reserve = attempt_service.reserve_recovery_candidates

    def reserve_then_disconnect(
        session: Session, *, limit: int = 100
    ) -> attempt_service.RecoveryCandidateReservation:
        reservation = original_reserve(session, limit=limit)
        driver = session.connection().connection.driver_connection
        driver.close()
        return reservation

    monkeypatch.setattr(attempt_service, "reserve_recovery_candidates", reserve_then_disconnect)
    with session_factory() as session, pytest.raises(DBAPIError):
        attempt_service.recover_expired_attempts(session, limit=1)
    monkeypatch.setattr(attempt_service, "reserve_recovery_candidates", original_reserve)

    def raise_unknown(_execution: Execution, *, attempt_id: int) -> None:
        del attempt_id
        raise RuntimeError("unknown-reconciliation-failure")

    monkeypatch.setattr(attempt_service, "_validate_recovery_snapshots", raise_unknown)
    with session_factory.begin() as session:
        cursor = session.get(RuntimeReconciliationCursor, "expired_attempts")
        assert cursor is not None
        cursor.after_id = 0
        cursor.upper_id = attempt_id
    with (
        session_factory() as session,
        pytest.raises(RuntimeError, match="unknown-reconciliation-failure"),
    ):
        attempt_service.recover_expired_attempts(session, limit=1)

    with session_factory() as session:
        attempt = session.get(ExecutionAttempt, attempt_id)
        row = session.get(Execution, execution["id"])
        assert attempt is not None and attempt.status in {"claimed", "running"}
        assert row is not None and row.status == "running"
        assert row.admission_released_at is None
