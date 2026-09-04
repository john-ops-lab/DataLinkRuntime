"""Final Cutover gates and post-Cutover invariants for Issue #130 Batch 3."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import Settings, settings
from dlr.control.models import (
    AdapterExecutionAdmission,
    AdapterExecutionSlot,
    Execution,
    ExecutionAttempt,
    ExecutionOutbox,
    GlobalExecutionAdmission,
)
from dlr.control.schemas.reliable_runtime import AttemptResultBody, ClaimDecision
from dlr.control.schemas.worker import REQUIRED_ISOLATION_CAPABILITIES
from dlr.control.services import attempt as attempt_service
from dlr.control.services import rabbitmq
from test_adapters import create_adapter, save_version

WORKER_HEADERS = {"Authorization": "Bearer test-worker-token"}
ISOLATION_PASS = {name: True for name in REQUIRED_ISOLATION_CAPABILITIES}
LEGACY_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_executions_active_adapter "
    "ON executions (adapter_id) "
    "WHERE dispatch_backend = 'legacy' AND status IN ('pending', 'running')"
)


def _register_worker(
    client: TestClient,
    name: str,
    *,
    protocol_version: int = 3,
) -> dict[str, Any]:
    response = client.post(
        "/api/workers/register",
        json={
            "name": name,
            "capabilities": ["python"],
            "protocol_version": protocol_version,
            "isolation_capabilities": ISOLATION_PASS if protocol_version == 3 else {},
        },
        headers=WORKER_HEADERS,
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def _configure_verified_runtime(
    monkeypatch: pytest.MonkeyPatch,
    worker_ids: list[int],
    *,
    ingress: bool,
    minimum_protocol: int,
    legacy_claim: bool = True,
    backup_restore_gate: bool = True,
    sandbox_gate: bool = True,
    slot_gate: bool = False,
) -> None:
    monkeypatch.setattr(settings, "rabbitmq_execution_enabled", ingress)
    monkeypatch.setattr(settings, "rabbitmq_execution_canary_enabled", False)
    monkeypatch.setattr(
        settings,
        "rabbitmq_url",
        "amqp://dlr:test-password@rabbitmq:5672",
    )
    monkeypatch.setattr(settings, "rabbitmq_vhost", "/")
    monkeypatch.setattr(settings, "rabbitmq_management_url", "http://rabbitmq:15672")
    monkeypatch.setattr(settings, "min_worker_protocol_version", minimum_protocol)
    monkeypatch.setattr(settings, "legacy_execution_claim_enabled", legacy_claim)
    monkeypatch.setattr(
        settings,
        "cutover_backup_restore_gate_passed",
        backup_restore_gate,
    )
    monkeypatch.setattr(settings, "cutover_sandbox_gate_passed", sandbox_gate)
    monkeypatch.setattr(settings, "cutover_slot_gate_passed", slot_gate)
    runtime_values: dict[str, object] = {
        "status": "ready",
        "last_error_code": None,
        "worker_count": len(worker_ids),
        "capability_verified": True,
        "configuration_fingerprint": rabbitmq.configuration_fingerprint(worker_ids),
        "verified_worker_ids": frozenset(worker_ids),
        "broker_observations": {},
    }
    for key, value in runtime_values.items():
        monkeypatch.setitem(rabbitmq._runtime_status, key, value)
    monkeypatch.setattr(
        rabbitmq,
        "infrastructure_dlq_observation",
        lambda: {"messages_ready": 0, "messages_unacknowledged": 0},
    )


def _index_present(session_factory: sessionmaker[Session]) -> bool:
    with session_factory() as session:
        return bool(
            session.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_indexes "
                    "WHERE schemaname = current_schema() "
                    "AND indexname = 'uq_executions_active_adapter')"
                )
            )
        )


def _restore_legacy_index(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text(LEGACY_INDEX_SQL))


def _adapter(client: TestClient, worker_id: int, name: str) -> dict[str, Any]:
    adapter = create_adapter(client, name=name)
    save_version(client, adapter["id"])
    response = client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"runtime_worker_id": worker_id},
    )
    assert response.status_code == 200, response.text
    return adapter


def _execution(client: TestClient, adapter_id: int, marker: str) -> dict[str, Any]:
    response = client.post(
        f"/api/adapters/{adapter_id}/executions",
        json={"input": {"marker": marker}},
    )
    assert response.status_code == 202, response.text
    body = cast(dict[str, Any], response.json())
    assert body["dispatch_backend"] == "rabbitmq"
    assert body["status"] == "queued"
    return body


def _dispatch(
    session_factory: sessionmaker[Session],
    execution_id: int,
) -> dict[str, Any]:
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        row = session.scalar(
            select(ExecutionOutbox).where(
                ExecutionOutbox.execution_id == execution_id,
                ExecutionOutbox.dispatch_generation == execution.dispatch_generation,
            )
        )
        assert row is not None
        return cast(dict[str, Any], dict(row.payload_json))


def _claim(
    session_factory: sessionmaker[Session],
    worker_id: int,
    dispatch: dict[str, Any],
) -> ClaimDecision:
    with session_factory() as session:
        return attempt_service.claim_dispatch(session, worker_id, dispatch)


def _finish(
    session_factory: sessionmaker[Session],
    worker_id: int,
    decision: ClaimDecision,
) -> None:
    assert decision.attempt_id is not None and decision.payload is not None
    with session_factory() as session:
        result = attempt_service.finish_attempt(
            session,
            worker_id,
            decision.attempt_id,
            AttemptResultBody.model_validate(
                {
                    "attempt_id": decision.attempt_id,
                    "fencing_token": decision.payload.fencing_token,
                    "claim_token": decision.payload.claim_token,
                    "status": "succeeded",
                    "workspace_cleanup_status": "completed",
                }
            ),
        )
    assert result.reason == "terminal_recorded"


def _exercise_slot_claim_result_and_recovery(
    client: TestClient,
    session_factory: sessionmaker[Session],
    worker_id: int,
    marker: str,
) -> None:
    same_adapter = _adapter(client, worker_id, f"b3-{marker}-same")
    other_adapter = _adapter(client, worker_id, f"b3-{marker}-other")
    same_first = _execution(client, same_adapter["id"], f"{marker}-same-1")
    same_second = _execution(client, same_adapter["id"], f"{marker}-same-2")
    other = _execution(client, other_adapter["id"], f"{marker}-other")
    dispatches = {
        same_first["id"]: _dispatch(session_factory, same_first["id"]),
        same_second["id"]: _dispatch(session_factory, same_second["id"]),
        other["id"]: _dispatch(session_factory, other["id"]),
    }
    start = threading.Barrier(3)

    def concurrent_claim(execution_id: int) -> tuple[int, ClaimDecision]:
        start.wait(timeout=10)
        return execution_id, _claim(session_factory, worker_id, dispatches[execution_id])

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = dict(pool.map(concurrent_claim, dispatches))

    same_decisions = [results[same_first["id"]], results[same_second["id"]]]
    assert sorted(decision.decision for decision in same_decisions) == ["DEFER", "EXECUTE"]
    assert (
        next(decision for decision in same_decisions if decision.decision == "DEFER").reason
        == "adapter_slot_busy"
    )
    assert results[other["id"]].decision == "EXECUTE"

    same_active = next(decision for decision in same_decisions if decision.decision == "EXECUTE")
    same_deferred_id = next(
        execution_id
        for execution_id in (same_first["id"], same_second["id"])
        if results[execution_id].decision == "DEFER"
    )

    recovered = results[other["id"]]
    assert recovered.attempt_id is not None
    expired_at = datetime.now(UTC) - timedelta(minutes=1)
    with session_factory.begin() as session:
        attempt = session.get(ExecutionAttempt, recovered.attempt_id)
        slot = session.get(AdapterExecutionSlot, (other_adapter["id"], 0))
        assert attempt is not None and slot is not None
        attempt.lease_expires_at = expired_at
        slot.lease_expires_at = expired_at
    with session_factory() as session:
        assert attempt_service.recover_expired_attempts(session, limit=10) == 1
    with session_factory() as session:
        assert (
            attempt_service.retry_dispatcher_once(
                session,
                limit=10,
                now=datetime.now(UTC) + timedelta(hours=1),
            )
            == 1
        )
    retried = _claim(session_factory, worker_id, _dispatch(session_factory, other["id"]))
    assert retried.decision == "EXECUTE"
    _finish(session_factory, worker_id, retried)

    _finish(session_factory, worker_id, same_active)
    deferred = _claim(
        session_factory,
        worker_id,
        dispatches[same_deferred_id],
    )
    assert deferred.decision == "EXECUTE"
    _finish(session_factory, worker_id, deferred)

    with session_factory() as session:
        active_attempts = int(
            session.scalar(
                select(func.count(ExecutionAttempt.id)).where(
                    ExecutionAttempt.adapter_id.in_((same_adapter["id"], other_adapter["id"])),
                    ExecutionAttempt.status.in_(attempt_service.ACTIVE_ATTEMPT_STATUSES),
                )
            )
            or 0
        )
        slots = list(
            session.scalars(
                select(AdapterExecutionSlot).where(
                    AdapterExecutionSlot.adapter_id.in_((same_adapter["id"], other_adapter["id"]))
                )
            )
        )
        adapter_admission = list(
            session.scalars(
                select(AdapterExecutionAdmission).where(
                    AdapterExecutionAdmission.adapter_id.in_(
                        (same_adapter["id"], other_adapter["id"])
                    )
                )
            )
        )
        global_admission = session.get(GlobalExecutionAdmission, "global")
        assert active_attempts == 0
        assert slots and all(slot.active_attempt_id is None for slot in slots)
        assert adapter_admission and all(
            row.outstanding_count == 0 and row.outstanding_bytes == 0 for row in adapter_admission
        )
        assert global_admission is not None
        assert global_admission.outstanding_count == 0
        assert global_admission.outstanding_bytes == 0


def _retire_body(schema_revision: str) -> dict[str, str]:
    return {
        "confirmation": "retire-legacy-active-index",
        "expected_schema_revision": schema_revision,
        "backup_restore_evidence_id": "issue130-b3-test-restore-1",
    }


def test_cutover_settings_default_closed_and_protocol_three_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "DLR_MIN_WORKER_PROTOCOL_VERSION",
        "DLR_LEGACY_EXECUTION_CLAIM_ENABLED",
        "DLR_CUTOVER_BACKUP_RESTORE_GATE_PASSED",
        "DLR_CUTOVER_SANDBOX_GATE_PASSED",
        "DLR_CUTOVER_SLOT_GATE_PASSED",
    ):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings()
    assert defaults.min_worker_protocol_version == 1
    assert defaults.legacy_execution_claim_enabled is True
    assert defaults.cutover_backup_restore_gate_passed is False
    assert defaults.cutover_sandbox_gate_passed is False
    assert defaults.cutover_slot_gate_passed is False

    monkeypatch.setenv("DLR_MIN_WORKER_PROTOCOL_VERSION", "3")
    assert Settings().min_worker_protocol_version == 3

    monkeypatch.setenv("DLR_LEGACY_EXECUTION_CLAIM_ENABLED", "false")
    with pytest.raises(ValueError, match="may be false only after"):
        Settings()
    for name, value in {
        "DLR_RABBITMQ_EXECUTION_ENABLED": "true",
        "DLR_RABBITMQ_URL": "amqp://dlr:example-password@rabbitmq:5672",
        "DLR_RABBITMQ_MANAGEMENT_URL": "http://rabbitmq:15672",
        "DLR_CUTOVER_BACKUP_RESTORE_GATE_PASSED": "true",
        "DLR_CUTOVER_SANDBOX_GATE_PASSED": "true",
        "DLR_CUTOVER_SLOT_GATE_PASSED": "true",
    }.items():
        monkeypatch.setenv(name, value)
    assert Settings().legacy_execution_claim_enabled is False


def test_preflight_is_read_only_and_fails_closed_until_every_worker_and_gate_pass(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v3 = _register_worker(api_client, "b3-preflight-v3")
    v2 = _register_worker(api_client, "b3-preflight-v2", protocol_version=2)
    _configure_verified_runtime(
        monkeypatch,
        [v3["id"], v2["id"]],
        ingress=False,
        minimum_protocol=1,
        backup_restore_gate=False,
        sandbox_gate=True,
    )

    first = api_client.get("/api/admin/reliable-runtime/cutover/preflight")
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["status"] == "blocked"
    assert first_body["read_only"] is True
    assert first_body["backup_restore_evidence_required"] is True
    assert set(first_body["blockers"]) == {
        "backup_restore_gate_not_attested",
        "worker_v3_isolation_not_ready",
    }
    assert _index_present(session_factory) is True

    upgraded = _register_worker(api_client, "b3-preflight-v2", protocol_version=3)
    assert upgraded["id"] == v2["id"]
    _configure_verified_runtime(
        monkeypatch,
        [v3["id"], v2["id"]],
        ingress=False,
        minimum_protocol=1,
        backup_restore_gate=True,
        sandbox_gate=True,
    )
    second = api_client.get("/api/admin/reliable-runtime/cutover/preflight")
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "ready"
    assert second.json()["blockers"] == []
    assert _index_present(session_factory) is True


def test_minimum_v3_and_closed_legacy_claim_reject_old_paths_explicitly(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v1 = _register_worker(api_client, "b3-old-v1", protocol_version=1)
    v2 = _register_worker(api_client, "b3-old-v2", protocol_version=2)
    v3 = _register_worker(api_client, "b3-current-v3")
    monkeypatch.setattr(settings, "min_worker_protocol_version", 3)

    for worker in (v1, v2):
        response = api_client.post(
            f"/api/workers/{worker['id']}/tasks/claim",
            params={"wait_seconds": 0},
            headers=WORKER_HEADERS,
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "worker_protocol_incompatible"

    monkeypatch.setattr(settings, "legacy_execution_claim_enabled", False)
    closed = api_client.post(
        f"/api/workers/{v3['id']}/tasks/claim",
        params={"wait_seconds": 0},
        headers=WORKER_HEADERS,
    )
    assert closed.status_code == 409, closed.text
    assert closed.json()["detail"]["code"] == "legacy_claim_disabled"


def test_cutover_sequence_and_slot_authority_hold_before_and_after_index_retirement(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    test_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _register_worker(api_client, "b3-cutover-worker")
    _configure_verified_runtime(
        monkeypatch,
        [worker["id"]],
        ingress=False,
        minimum_protocol=1,
        slot_gate=False,
    )
    historical_adapter = _adapter(api_client, worker["id"], "b3-historical-adapter")
    legacy = api_client.post(
        f"/api/adapters/{historical_adapter['id']}/executions",
        json={"input": {"kind": "legacy-history"}},
    )
    assert legacy.status_code == 202, legacy.text
    assert legacy.json()["dispatch_backend"] == "legacy"
    with session_factory.begin() as session:
        row = session.get(Execution, legacy.json()["id"])
        assert row is not None
        row.status = "succeeded"
        row.ended_at = datetime.now(UTC)

    preflight = api_client.get("/api/admin/reliable-runtime/cutover/preflight")
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["status"] == "ready"
    schema_revision = preflight.json()["inventory"]["schema_revision"]

    _configure_verified_runtime(
        monkeypatch,
        [worker["id"]],
        ingress=True,
        minimum_protocol=1,
        slot_gate=False,
    )
    _exercise_slot_claim_result_and_recovery(
        api_client,
        session_factory,
        worker["id"],
        "before-index",
    )

    monkeypatch.setattr(settings, "min_worker_protocol_version", 3)
    malformed = api_client.post(
        "/api/admin/reliable-runtime/cutover/retire-legacy-index",
        json={**_retire_body(schema_revision), "confirmation": "drop-it"},
    )
    assert malformed.status_code == 422
    blocked = api_client.post(
        "/api/admin/reliable-runtime/cutover/retire-legacy-index",
        json=_retire_body(schema_revision),
    )
    assert blocked.status_code == 409, blocked.text
    assert "slot_gate_not_attested" in blocked.json()["detail"]["params"]["blockers"]
    assert _index_present(session_factory) is True

    monkeypatch.setattr(settings, "cutover_slot_gate_passed", True)
    mismatch = api_client.post(
        "/api/admin/reliable-runtime/cutover/retire-legacy-index",
        json=_retire_body("wrong_revision"),
    )
    assert mismatch.status_code == 409, mismatch.text
    assert mismatch.json()["detail"]["code"] == "cutover_schema_revision_mismatch"
    assert _index_present(session_factory) is True

    try:
        retired = api_client.post(
            "/api/admin/reliable-runtime/cutover/retire-legacy-index",
            json=_retire_body(schema_revision),
        )
        assert retired.status_code == 200, retired.text
        assert retired.json() == {
            "status": "completed",
            "schema_revision": schema_revision,
            "backup_restore_evidence_id": "issue130-b3-test-restore-1",
            "old_active_index_present": False,
            "changed": True,
        }
        repeated = api_client.post(
            "/api/admin/reliable-runtime/cutover/retire-legacy-index",
            json=_retire_body(schema_revision),
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["changed"] is False
        assert _index_present(session_factory) is False

        _exercise_slot_claim_result_and_recovery(
            api_client,
            session_factory,
            worker["id"],
            "after-index",
        )
        monkeypatch.setattr(settings, "legacy_execution_claim_enabled", False)
        first = api_client.get("/api/admin/reliable-runtime/cutover/invariants")
        second = api_client.get("/api/admin/reliable-runtime/cutover/invariants")
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json() == second.json()
        assert first.json()["status"] == "passed"
        assert first.json()["violations"] == []

        claim = api_client.post(
            f"/api/workers/{worker['id']}/tasks/claim",
            params={"wait_seconds": 0},
            headers=WORKER_HEADERS,
        )
        assert claim.status_code == 409, claim.text
        assert claim.json()["detail"]["code"] == "legacy_claim_disabled"
        historical = api_client.get(f"/api/executions/{legacy.json()['id']}")
        assert historical.status_code == 200, historical.text
        assert historical.json()["dispatch_backend"] == "legacy"
        assert historical.json()["status"] == "succeeded"
    finally:
        _restore_legacy_index(test_engine)


def test_post_cutover_invariants_report_bounded_non_sensitive_corruption(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    test_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _register_worker(api_client, "b3-invariant-worker")
    _configure_verified_runtime(
        monkeypatch,
        [worker["id"]],
        ingress=True,
        minimum_protocol=3,
        slot_gate=True,
    )
    schema_revision = api_client.get("/api/admin/reliable-runtime/inventory").json()[
        "schema_revision"
    ]
    try:
        retired = api_client.post(
            "/api/admin/reliable-runtime/cutover/retire-legacy-index",
            json=_retire_body(schema_revision),
        )
        assert retired.status_code == 200, retired.text
        monkeypatch.setattr(settings, "legacy_execution_claim_enabled", False)

        adapter = _adapter(api_client, worker["id"], "b3-invariant-adapter")
        malformed = _execution(api_client, adapter["id"], "malformed-outbox")
        running = _execution(api_client, adapter["id"], "missing-attempt")
        with session_factory.begin() as session:
            outbox_row = session.scalar(
                select(ExecutionOutbox).where(ExecutionOutbox.execution_id == malformed["id"])
            )
            running_row = session.get(Execution, running["id"])
            assert outbox_row is not None and running_row is not None
            payload = dict(outbox_row.payload_json)
            payload["execution_id"] = -1
            outbox_row.payload_json = payload
            running_row.status = "running"

        response = api_client.get("/api/admin/reliable-runtime/cutover/invariants")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "failed"
        violations = {item["code"]: item for item in body["violations"]}
        assert "orphan_or_future_outbox" in violations
        assert "running_without_single_attempt_and_slot" in violations
        assert violations["orphan_or_future_outbox"]["count"] == 1
        assert len(violations["orphan_or_future_outbox"]["sample_ids"]) == 1
        assert violations["running_without_single_attempt_and_slot"]["sample_ids"] == [
            str(running["id"])
        ]
    finally:
        _restore_legacy_index(test_engine)
