"""Issue #152B durable Incident disposition contracts."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import (
    AdapterExecutionAdmission,
    AdapterPermission,
    BuiltinPackage,
    Credential,
    Execution,
    ExecutionCredentialBindingSnapshot,
    ExecutionIncidentDisposition,
    ExecutionInfrastructureIncident,
    ExecutionOutbox,
    GlobalExecutionAdmission,
    ManagedInputArtifact,
    User,
    Worker,
)
from dlr.control.schemas.reliable_runtime import AttemptResultBody, IncidentDispositionBody
from dlr.control.security import SUPERADMIN_PRINCIPAL, Principal, require_principal
from dlr.control.services import attempt as attempt_service
from dlr.control.services import builtin_package
from dlr.control.services import execution as execution_service
from dlr.control.services import incident_disposition as incident_disposition_service
from dlr.control.services.artifact_store import LocalFileArtifactStore
from dlr.control.services.execution_cancellation import (
    lock_execution_in_admission_order,
    lock_execution_tail,
)
from dlr.control.services.incident_disposition import (
    IncidentDispositionResult,
    dispose_incident,
    lookup_idempotency,
    request_hash,
)
from dlr.control.services.secrets import encrypt_fields
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


def _incident(
    session: Session,
    execution_id: int,
    *,
    message_id: uuid.UUID | None = None,
    generation: int = 1,
) -> ExecutionInfrastructureIncident:
    row = ExecutionInfrastructureIncident(
        execution_id=execution_id,
        dispatch_generation=generation,
        message_id=message_id or uuid.uuid4(),
        kind="delivery_limit",
        attempts=3,
    )
    session.add(row)
    session.flush()
    return row


def _bound_incident(session: Session, execution_id: int) -> ExecutionInfrastructureIncident:
    row = session.scalar(
        select(ExecutionOutbox).where(
            ExecutionOutbox.execution_id == execution_id,
            ExecutionOutbox.dispatch_generation == 1,
        )
    )
    assert row is not None
    return _incident(session, execution_id, message_id=row.message_id)


def _receipt(
    incident: ExecutionInfrastructureIncident,
    *,
    key: uuid.UUID,
    digest: str,
    action: str = "recover",
    reason_code: str = "capacity_repaired",
    outcome: str = "dispatch_already_pending",
    code: str = "dispatch_already_pending",
) -> ExecutionIncidentDisposition:
    assert incident.execution_id is not None
    return ExecutionIncidentDisposition(
        incident_id=incident.id,
        execution_id=incident.execution_id,
        idempotency_key=key,
        request_hash=digest,
        actor_kind="superadmin",
        user_id=None,
        action=action,
        reason_code=reason_code,
        outcome=outcome,
        code=code,
        from_generation=1,
        to_generation=1,
        execution_status="queued",
    )


def test_disposition_migration_preserves_old_incident_without_backfill() -> None:
    with _isolated_schema("issue152_dispositions", "0039_issue134_reconcile") as (
        engine,
        database,
    ):
        with engine.begin() as connection:
            incident_id = connection.scalar(
                text(
                    "INSERT INTO execution_infrastructure_incidents "
                    "(kind, attempts) VALUES ('delivery_limit', 4) RETURNING id"
                )
            )

        _upgrade(database, "head")

        schema = inspect(engine)
        assert "execution_incident_dispositions" in schema.get_table_names()
        assert {"uq_execution_incident_dispositions_incident_key"} == {
            item["name"]
            for item in schema.get_unique_constraints("execution_incident_dispositions")
        }
        indexes = {
            item["name"]: item for item in schema.get_indexes("execution_incident_dispositions")
        }
        assert indexes["ix_execution_incident_dispositions_incident_page"]["column_names"] == [
            "incident_id",
            "created_at",
            "id",
        ]
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0040_issue152_dispositions"
            )
            assert (
                connection.scalar(
                    text("SELECT attempts FROM execution_infrastructure_incidents WHERE id = :id"),
                    {"id": incident_id},
                )
                == 4
            )
            assert (
                connection.scalar(text("SELECT count(*) FROM execution_incident_dispositions")) == 0
            )


def test_disposition_hash_is_closed_and_actor_independent() -> None:
    first = IncidentDispositionBody(
        action="recover", expected_generation=7, reason_code="capacity_repaired"
    )
    same = IncidentDispositionBody.model_validate(
        {"reason_code": "capacity_repaired", "expected_generation": 7, "action": "recover"}
    )
    changed = IncidentDispositionBody(
        action="recover", expected_generation=8, reason_code="capacity_repaired"
    )

    assert request_hash(first) == request_hash(same)
    assert request_hash(first) != request_hash(changed)
    assert len(request_hash(first)) == 64


def test_disposition_generation_stays_inside_jcs_safe_integer_domain() -> None:
    maximum = IncidentDispositionBody(
        action="recover",
        expected_generation=2**53 - 1,
        reason_code="capacity_repaired",
    )
    assert len(request_hash(maximum)) == 64
    with pytest.raises(ValueError):
        IncidentDispositionBody(
            action="recover",
            expected_generation=2**53,
            reason_code="capacity_repaired",
        )


def test_same_incident_key_replays_receipt_and_conflicts_on_changed_body(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-audit-worker")  # type: ignore[arg-type]
    adapter = _rabbit_adapter(api_client, worker, "issue152-audit-adapter")  # type: ignore[arg-type]
    execution = _execution(api_client, adapter["id"])  # type: ignore[arg-type]
    key = uuid.uuid4()
    body = IncidentDispositionBody(
        action="recover", expected_generation=1, reason_code="capacity_repaired"
    )

    with session_factory.begin() as session:
        incident = _incident(session, execution["id"])
        session.add(_receipt(incident, key=key, digest=request_hash(body)))

    with session_factory.begin() as session:
        incident = session.scalar(select(ExecutionInfrastructureIncident))
        assert incident is not None
        replay = lookup_idempotency(
            session, incident_id=incident.id, idempotency_key=key, body=body
        )
        assert replay.receipt is not None
        assert replay.receipt.outcome == "dispatch_already_pending"
        assert incident.attempts == 3
        assert session.scalar(select(ExecutionIncidentDisposition)) is replay.receipt

    changed = IncidentDispositionBody(
        action="terminate", expected_generation=1, reason_code="operator_cancel"
    )
    with session_factory.begin() as session, pytest.raises(HTTPException) as error:
        incident = session.scalar(select(ExecutionInfrastructureIncident))
        assert incident is not None
        lookup_idempotency(session, incident_id=incident.id, idempotency_key=key, body=changed)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "idempotency_key_conflict"


def test_database_enforces_one_key_per_incident(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-unique-worker")  # type: ignore[arg-type]
    adapter = _rabbit_adapter(api_client, worker, "issue152-unique-adapter")  # type: ignore[arg-type]
    execution = _execution(api_client, adapter["id"])  # type: ignore[arg-type]
    key = uuid.uuid4()
    body = IncidentDispositionBody(
        action="recover", expected_generation=1, reason_code="capacity_repaired"
    )

    with session_factory() as session:
        incident = _incident(session, execution["id"])
        session.add(_receipt(incident, key=key, digest=request_hash(body)))
        session.flush()
        session.add(_receipt(incident, key=key, digest=request_hash(body)))
        with pytest.raises(IntegrityError):
            session.flush()


def test_execution_lock_refreshes_a_cached_row_after_concurrent_commit(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-fresh-worker")  # type: ignore[arg-type]
    adapter = _rabbit_adapter(api_client, worker, "issue152-fresh-adapter")  # type: ignore[arg-type]
    execution = _execution(api_client, adapter["id"])  # type: ignore[arg-type]

    with session_factory() as cached_session:
        cached = cached_session.get(Execution, execution["id"])
        assert cached is not None and cached.status == "queued"
        with session_factory() as concurrent:
            execution_service.cancel_execution(concurrent, execution["id"])

        locked = lock_execution_in_admission_order(cached_session, execution["id"])
        assert locked is cached
        assert locked.status == "cancelled"
        cached_session.rollback()


def test_cancel_helper_and_receipt_rollback_as_one_transaction(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-rollback-worker")  # type: ignore[arg-type]
    adapter = _rabbit_adapter(api_client, worker, "issue152-rollback-adapter")  # type: ignore[arg-type]
    execution = _execution(api_client, adapter["id"])  # type: ignore[arg-type]
    key = uuid.uuid4()
    body = IncidentDispositionBody(
        action="terminate", expected_generation=1, reason_code="operator_cancel"
    )

    with session_factory.begin() as setup:
        incident_id = _incident(setup, execution["id"]).id

    mutating = session_factory()
    try:
        locked = lock_execution_in_admission_order(mutating, execution["id"])
        incident = mutating.get(ExecutionInfrastructureIncident, incident_id)
        assert locked is not None and incident is not None
        tail = lock_execution_tail(mutating, locked)
        receipt = _receipt(
            incident,
            key=key,
            digest=request_hash(body),
            action="terminate",
            reason_code="operator_cancel",
            outcome="cancellation_requested",
            code="cancellation_requested",
        )
        mutating.add(receipt)
        execution_service.cancel_execution_locked(mutating, locked, lock_tail=tail)
        mutating.flush()

        with session_factory() as observer:
            observed = observer.get(Execution, execution["id"])
            assert observed is not None and observed.status == "queued"
            assert observer.scalar(select(ExecutionIncidentDisposition)) is None
        mutating.rollback()
    finally:
        mutating.close()

    with session_factory() as observer:
        observed = observer.get(Execution, execution["id"])
        assert observed is not None and observed.status == "queued"
        assert observer.scalar(select(ExecutionIncidentDisposition)) is None


def test_running_terminate_receipt_converges_in_place_on_real_terminal(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-converge-worker")  # type: ignore[arg-type]
    adapter = _rabbit_adapter(api_client, worker, "issue152-converge-adapter")  # type: ignore[arg-type]
    execution = _execution(api_client, adapter["id"])  # type: ignore[arg-type]
    claimed = _claim(session_factory, worker["id"], _dispatch(session_factory, execution["id"]))
    assert claimed.payload is not None and claimed.attempt_id is not None
    body = IncidentDispositionBody(
        action="terminate", expected_generation=1, reason_code="operator_cancel"
    )

    with session_factory.begin() as session:
        incident = _incident(session, execution["id"])
        receipt = _receipt(
            incident,
            key=uuid.uuid4(),
            digest=request_hash(body),
            action="terminate",
            reason_code="operator_cancel",
            outcome="cancellation_requested",
            code="cancellation_requested",
        )
        session.add(receipt)
        session.flush()
        receipt_id = receipt.id

    with session_factory() as session:
        requested = execution_service.cancel_execution(session, execution["id"])
        assert requested.status == "running" and requested.cancel_requested is True

    with session_factory() as session:
        decision = attempt_service.finish_attempt(
            session,
            worker["id"],
            claimed.attempt_id,
            AttemptResultBody(
                attempt_id=claimed.attempt_id,
                fencing_token=claimed.payload.fencing_token,
                claim_token=claimed.payload.claim_token,
                status="succeeded",
                output={"ok": True},
                workspace_cleanup_status="completed",
            ),
        )
        assert decision.reason == "terminal_recorded"

    with session_factory() as session:
        receipts = list(session.scalars(select(ExecutionIncidentDisposition)))
        assert len(receipts) == 1 and receipts[0].id == receipt_id
        assert receipts[0].outcome == "execution_terminal"
        assert receipts[0].code == "execution_cancelled"
        assert receipts[0].execution_status == "cancelled"
        incident = session.scalar(select(ExecutionInfrastructureIncident))
        assert incident is not None and incident.status == "resolved"
        assert incident.resolved_at is not None


def _dispose(
    session: Session,
    *,
    execution_id: int,
    incident_id: int,
    action: Literal["recover", "terminate"],
    key: uuid.UUID | None = None,
) -> IncidentDispositionResult:
    return dispose_incident(
        session,
        execution_id,
        incident_id,
        action,
        1,
        key or uuid.uuid4(),
        cast(
            Literal[
                "capacity_repaired", "routing_repaired", "operator_cancel", "verified_terminal"
            ],
            "capacity_repaired" if action == "recover" else "operator_cancel",
        ),
        SUPERADMIN_PRINCIPAL,
    )


def _exclusive_flock_available(path: Path) -> bool:
    probe = (
        "import fcntl,sys; "
        "stream=open(sys.argv[1],'a+b'); "
        "fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, str(path)],
        check=False,
        capture_output=True,
        timeout=5,
    )
    return result.returncode == 0


def _managed_execution(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
) -> tuple[dict[str, object], dict[str, object], int, Path]:
    _enable_runtime(monkeypatch)
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    store_root = tmp_path / name
    monkeypatch.setattr(settings, "artifact_store_root", str(store_root))
    content = f"material-{name}".encode()
    store = LocalFileArtifactStore(store_root)
    storage_key = store.new_storage_key()
    with store.put_part(storage_key) as stream:
        stream.write(content)
    store.commit(storage_key)
    worker = _ready_worker(api_client, f"{name}-worker")  # type: ignore[arg-type]
    adapter = _rabbit_adapter(api_client, worker, f"{name}-adapter")  # type: ignore[arg-type]
    artifact_id = create_artifact(
        session_factory,
        int(adapter["id"]),
        f"{name}.txt",
        status="READY",
    )
    with session_factory.begin() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None
        artifact.storage_key = storage_key
        artifact.size_bytes = len(content)
        artifact.sha256 = hashlib.sha256(content).hexdigest()
    configured = api_client.put(  # type: ignore[union-attr]
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [artifact_id],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )
    assert configured.status_code == 200, configured.text
    execution = _execution(api_client, int(adapter["id"]))  # type: ignore[arg-type]
    return worker, execution, artifact_id, store.object_path(storage_key)


def test_disposition_api_validates_uuid_records_principal_and_updates_reliable_detail(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-api-worker")
    adapter = _rabbit_adapter(api_client, worker, "issue152-api-adapter")
    execution = _execution(api_client, adapter["id"])
    with session_factory.begin() as session:
        incident_id = _bound_incident(session, execution["id"]).id
    path = f"/api/executions/{execution['id']}/incidents/{incident_id}/dispositions"
    body = {
        "action": "recover",
        "expected_generation": 1,
        "reason_code": "capacity_repaired",
    }

    assert api_client.post(path, json=body).status_code == 422
    assert (
        api_client.post(path, headers={"Idempotency-Key": "not-a-uuid"}, json=body).status_code
        == 422
    )
    forged = api_client.post(
        path,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={**body, "actor_kind": "account"},
    )
    assert forged.status_code == 422

    key = uuid.uuid4()
    accepted = api_client.post(
        path,
        headers={"Idempotency-Key": str(key)},
        json=body,
    )
    assert accepted.status_code == 200, accepted.text
    receipt = accepted.json()["receipt"]
    assert receipt["actor_kind"] == "superadmin" and receipt["user_id"] is None
    replay = api_client.post(
        path,
        headers={"Idempotency-Key": str(key)},
        json=body,
    )
    assert replay.status_code == 200
    assert replay.json()["receipt"]["id"] == receipt["id"]

    detail = api_client.get(f"/api/executions/{execution['id']}/reliable-detail")
    assert detail.status_code == 200, detail.text
    incident = detail.json()["incidents"][0]
    assert incident["id"] == incident_id
    assert incident["observation_count"] == incident["attempts"] == 3
    assert incident["disposition_count"] == 1
    assert incident["recovery_dispatch_count"] == 0
    assert incident["recent_disposition"]["id"] == receipt["id"]
    assert incident["recover_available"] is False
    assert incident["recover_reason"] == "incident_closed"
    assert incident["dispositions_url"] == path

    listed = api_client.get(path)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [receipt["id"]]
    with session_factory() as session:
        assert len(list(session.scalars(select(ExecutionIncidentDisposition)))) == 1


def test_disposition_api_requires_authentication_and_hides_cross_adapter_incident(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-auth-worker")
    first_adapter = _rabbit_adapter(api_client, worker, "issue152-auth-first-adapter")
    second_adapter = _rabbit_adapter(api_client, worker, "issue152-auth-second-adapter")
    first_execution = _execution(api_client, first_adapter["id"])
    second_execution = _execution(api_client, second_adapter["id"])
    with session_factory.begin() as session:
        incident_id = _bound_incident(session, first_execution["id"]).id
    body = {
        "action": "recover",
        "expected_generation": 1,
        "reason_code": "capacity_repaired",
    }
    cross_path = f"/api/executions/{second_execution['id']}/incidents/{incident_id}/dispositions"

    unauthenticated = TestClient(api_client.app).post(
        cross_path,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json=body,
    )
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["detail"]["code"] == "unauthorized"

    hidden = api_client.post(
        cross_path,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json=body,
    )
    assert hidden.status_code == 404
    assert hidden.json()["detail"]["code"] == "incident_not_found"
    with session_factory() as session:
        assert session.scalar(select(ExecutionIncidentDisposition)) is None


def test_account_editor_is_the_persisted_disposition_actor(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-account-worker")
    adapter = _rabbit_adapter(api_client, worker, "issue152-account-adapter")
    execution = _execution(api_client, adapter["id"])
    with session_factory.begin() as session:
        incident_id = _bound_incident(session, execution["id"]).id
        editor = User(
            username="issue152-editor",
            password_hash="unused-test-hash",
            role="user",
            enabled=True,
            must_change_password=False,
        )
        session.add(editor)
        session.flush()
        session.add(
            AdapterPermission(adapter_id=adapter["id"], user_id=editor.id, permission="edit")
        )
        editor_principal = Principal(
            kind="account", role="user", user_id=editor.id, username=editor.username
        )
        editor_id = editor.id
    api_client.app.dependency_overrides[require_principal] = lambda: editor_principal
    try:
        accepted = api_client.post(
            f"/api/executions/{execution['id']}/incidents/{incident_id}/dispositions",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "action": "recover",
                "expected_generation": 1,
                "reason_code": "capacity_repaired",
            },
        )
    finally:
        api_client.app.dependency_overrides.pop(require_principal, None)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["receipt"]["actor_kind"] == "account"
    assert accepted.json()["receipt"]["user_id"] == editor_id
    with session_factory() as session:
        receipt = session.scalar(select(ExecutionIncidentDisposition))
        assert receipt is not None
        assert (receipt.actor_kind, receipt.user_id) == ("account", editor_id)


def test_disposition_page_uses_stable_uuid_cursor_at_equal_timestamp(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-page-worker")
    adapter = _rabbit_adapter(api_client, worker, "issue152-page-adapter")
    execution = _execution(api_client, adapter["id"])
    body = IncidentDispositionBody(
        action="recover", expected_generation=1, reason_code="capacity_repaired"
    )
    created_at = datetime(2031, 2, 3, 4, 5, 6, tzinfo=UTC)
    with session_factory.begin() as session:
        incident = _bound_incident(session, execution["id"])
        incident_id = incident.id
        for key in (uuid.uuid4(), uuid.uuid4(), uuid.uuid4()):
            receipt = _receipt(incident, key=key, digest=request_hash(body))
            receipt.created_at = created_at
            session.add(receipt)
    path = f"/api/executions/{execution['id']}/incidents/{incident_id}/dispositions"

    first = api_client.get(path, params={"limit": 2})
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert len(first_body["items"]) == 2
    assert first_body["next_before_id"] == first_body["items"][-1]["id"]
    second = api_client.get(
        path,
        params={"limit": 2, "before_id": first_body["next_before_id"]},
    )
    assert second.status_code == 200, second.text
    assert len(second.json()["items"]) == 1
    assert second.json()["next_before_id"] is None
    with session_factory() as session:
        expected_ids = {
            str(row.id) for row in session.scalars(select(ExecutionIncidentDisposition)).all()
        }
    assert {item["id"] for item in first_body["items"] + second.json()["items"]} == expected_ids
    assert api_client.get(path, params={"limit": 0}).status_code == 422
    assert api_client.get(path, params={"limit": 101}).status_code == 422
    assert api_client.get(path, params={"before_id": str(uuid.uuid4())}).status_code == 422


def test_read_only_incident_detail_disables_actions_and_post_writes_no_audit(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-readonly-worker")
    adapter = _rabbit_adapter(api_client, worker, "issue152-readonly-adapter")
    execution = _execution(api_client, adapter["id"])
    with session_factory.begin() as session:
        incident_id = _bound_incident(session, execution["id"]).id
        reader = User(
            username="issue152-reader",
            password_hash="unused-test-hash",
            role="user",
            enabled=True,
            must_change_password=False,
        )
        session.add(reader)
        session.flush()
        session.add(
            AdapterPermission(adapter_id=adapter["id"], user_id=reader.id, permission="read")
        )
        reader_principal = Principal(
            kind="account", role="user", user_id=reader.id, username=reader.username
        )
    api_client.app.dependency_overrides[require_principal] = lambda: reader_principal
    try:
        detail = api_client.get(f"/api/executions/{execution['id']}/reliable-detail")
        assert detail.status_code == 200, detail.text
        incident = detail.json()["incidents"][0]
        assert incident["recover_available"] is False
        assert incident["terminate_available"] is False
        assert incident["recover_reason"] == "adapter_read_only"
        path = f"/api/executions/{execution['id']}/incidents/{incident_id}/dispositions"
        rejected = api_client.post(
            path,
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={
                "action": "recover",
                "expected_generation": 1,
                "reason_code": "capacity_repaired",
            },
        )
        assert rejected.status_code == 403
        assert rejected.json()["detail"]["code"] == "adapter_read_only"
    finally:
        api_client.app.dependency_overrides.pop(require_principal, None)
    with session_factory() as session:
        assert session.scalar(select(ExecutionIncidentDisposition)) is None


@pytest.mark.parametrize(
    ("transition", "expected_status", "expected_outcome"),
    [
        ("claim", 409, "incident_execution_active"),
        ("cancel_requested", 409, "incident_cancellation_pending"),
        ("terminal", 200, "execution_terminal"),
    ],
)
def test_recover_rechecks_state_after_http_detail_read(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
    expected_status: int,
    expected_outcome: str,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"issue152-read-race-{transition}-worker")
    adapter = _rabbit_adapter(api_client, worker, f"issue152-read-race-{transition}-adapter")
    execution = _execution(api_client, adapter["id"])
    with session_factory.begin() as session:
        incident_id = _bound_incident(session, execution["id"]).id

    detail = api_client.get(f"/api/executions/{execution['id']}/reliable-detail")
    assert detail.status_code == 200, detail.text
    assert detail.json()["incidents"][0]["recover_available"] is True

    if transition == "claim":
        with session_factory.begin() as session:
            outbox_row = session.scalar(
                select(ExecutionOutbox).where(ExecutionOutbox.execution_id == execution["id"])
            )
            assert outbox_row is not None
            outbox_row.status = "published"
            outbox_row.published_at = datetime.now(UTC)
        claimed = _claim(session_factory, worker["id"], _dispatch(session_factory, execution["id"]))
        assert claimed.attempt_id is not None
    elif transition == "cancel_requested":
        with session_factory.begin() as session:
            row = session.get(Execution, execution["id"])
            assert row is not None
            row.cancel_requested = True
    else:
        with session_factory() as session:
            cancelled = execution_service.cancel_execution(session, execution["id"])
            assert cancelled.status == "cancelled"

    response = api_client.post(
        f"/api/executions/{execution['id']}/incidents/{incident_id}/dispositions",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "action": "recover",
            "expected_generation": 1,
            "reason_code": "capacity_repaired",
        },
    )
    assert response.status_code == expected_status, response.text
    assert response.json()["receipt"]["outcome"] == expected_outcome
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None and row.dispatch_generation == 1
        assert len(list(session.scalars(select(ExecutionOutbox)))) == 1


def test_recover_rechecks_managed_material_after_http_detail_read(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _worker, execution, _artifact_id, blob = _managed_execution(
        api_client,
        session_factory,
        monkeypatch,
        tmp_path,
        "issue152-http-material-race",
    )
    with session_factory.begin() as session:
        incident_id = _bound_incident(session, int(execution["id"])).id

    detail = api_client.get(f"/api/executions/{execution['id']}/reliable-detail")
    assert detail.status_code == 200, detail.text
    assert detail.json()["incidents"][0]["recover_available"] is True
    blob.unlink()

    response = api_client.post(
        f"/api/executions/{execution['id']}/incidents/{incident_id}/dispositions",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "action": "recover",
            "expected_generation": 1,
            "reason_code": "capacity_repaired",
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["receipt"]["outcome"] == "incident_materials_unavailable"
    with session_factory() as session:
        row = session.get(Execution, int(execution["id"]))
        assert row is not None and row.dispatch_generation == 1


@pytest.mark.parametrize(
    ("state", "recover_available", "recover_reason", "terminate_available", "terminate_reason"),
    [
        ("closed", False, "incident_closed", False, "incident_closed"),
        ("terminal", False, "execution_terminal", False, "execution_terminal"),
        ("stale", False, "incident_stale_generation", True, None),
        (
            "future",
            False,
            "incident_dispatch_identity_invalid",
            False,
            "incident_dispatch_identity_invalid",
        ),
        ("cancel_requested", False, "incident_cancellation_pending", True, None),
        ("retry_wait", False, "incident_execution_not_queued", True, None),
    ],
)
def test_reliable_detail_exposes_stable_incident_capability_semantics(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    recover_available: bool,
    recover_reason: str,
    terminate_available: bool,
    terminate_reason: str | None,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"issue152-capability-{state}-worker")
    adapter = _rabbit_adapter(api_client, worker, f"issue152-capability-{state}-adapter")
    execution = _execution(api_client, adapter["id"])
    with session_factory.begin() as session:
        incident = _bound_incident(session, execution["id"])
        row = session.get(Execution, execution["id"])
        assert row is not None
        if state == "closed":
            incident.status = "resolved"
            incident.resolved_at = datetime.now(UTC)
        elif state == "terminal":
            row.status = "cancelled"
        elif state == "stale":
            row.dispatch_generation = 2
        elif state == "future":
            incident.dispatch_generation = 2
        elif state == "cancel_requested":
            row.cancel_requested = True
        else:
            row.status = "retry_wait"

    detail = api_client.get(f"/api/executions/{execution['id']}/reliable-detail")
    assert detail.status_code == 200, detail.text
    incident_body = detail.json()["incidents"][0]
    assert incident_body["recover_available"] is recover_available
    assert incident_body["recover_reason"] == recover_reason
    assert incident_body["terminate_available"] is terminate_available
    assert incident_body["terminate_reason"] == terminate_reason


def test_managed_recovery_accepts_clean_pending_delete_and_replays_after_blob_loss(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _worker, execution, artifact_id, blob = _managed_execution(
        api_client,
        session_factory,
        monkeypatch,
        tmp_path,
        "issue152-managed-pending",
    )
    with session_factory.begin() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None
        artifact.status = "PENDING_DELETE"
        incident_id = _bound_incident(session, int(execution["id"])).id

    key = uuid.uuid4()
    with session_factory() as session:
        accepted = _dispose(
            session,
            execution_id=int(execution["id"]),
            incident_id=incident_id,
            action="recover",
            key=key,
        )
    assert accepted.response.receipt.outcome == "dispatch_already_pending"

    blob.unlink()
    with session_factory() as session:
        replay = _dispose(
            session,
            execution_id=int(execution["id"]),
            incident_id=incident_id,
            action="recover",
            key=key,
        )
    assert replay.response.receipt.id == accepted.response.receipt.id
    assert replay.response.receipt.outcome == "dispatch_already_pending"


@pytest.mark.parametrize(
    ("status", "delete_attempts"),
    [("DELETING", 0), ("DELETE_FAILED", 0), ("PENDING_DELETE", 1)],
)
def test_managed_recovery_rejects_prior_deletion_authority(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: str,
    delete_attempts: int,
) -> None:
    _worker, execution, artifact_id, _blob = _managed_execution(
        api_client,
        session_factory,
        monkeypatch,
        tmp_path,
        f"issue152-managed-{status.lower()}-{delete_attempts}",
    )
    with session_factory.begin() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None
        artifact.status = status
        artifact.delete_attempts = delete_attempts
        incident_id = _bound_incident(session, int(execution["id"])).id

    with session_factory() as session:
        rejected = _dispose(
            session,
            execution_id=int(execution["id"]),
            incident_id=incident_id,
            action="recover",
        )
    assert rejected.status_code == 409
    assert rejected.response.receipt.outcome == "incident_materials_unavailable"
    with session_factory() as session:
        row = session.get(Execution, int(execution["id"]))
        incident = session.get(ExecutionInfrastructureIncident, incident_id)
        assert row is not None and row.dispatch_generation == 1
        assert incident is not None and incident.status == "open"


def test_managed_blob_removed_between_preflight_and_final_validation_is_rejected(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _worker, execution, _artifact_id, blob = _managed_execution(
        api_client,
        session_factory,
        monkeypatch,
        tmp_path,
        "issue152-managed-race",
    )
    with session_factory.begin() as session:
        incident_id = _bound_incident(session, int(execution["id"])).id

    from dlr.control.services import incident_disposition as service

    validate = service.validate_recovery_materials

    def remove_then_validate(*args: object, **kwargs: object) -> bool:
        blob.unlink()
        return validate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "validate_recovery_materials", remove_then_validate)
    with session_factory() as session:
        rejected = _dispose(
            session,
            execution_id=int(execution["id"]),
            incident_id=incident_id,
            action="recover",
        )
    assert rejected.response.receipt.outcome == "incident_materials_unavailable"


@pytest.mark.parametrize(
    "shape",
    ["missing_revision", "unknown_key", "none_nonnull", "fake_dependency"],
)
def test_recovery_rejects_unknown_or_inconsistent_nonmanaged_input_snapshot(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"issue152-input-invalid-{shape}-worker")
    adapter = _rabbit_adapter(api_client, worker, f"issue152-input-invalid-{shape}-adapter")
    execution = _execution(api_client, adapter["id"])
    with session_factory.begin() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        if shape == "missing_revision":
            row.input_snapshot = {"source_type": row.input_source_type}
        elif shape == "unknown_key":
            row.input_snapshot = {**row.input_snapshot, "unrecognized": True}
        elif shape == "none_nonnull":
            row.input_source_type = "none"
            row.input_snapshot = {"source_type": "none", "revision": row.input_config_revision}
            row.input = {"contradiction": True}
        else:
            assert row.dependency_check is False
            row.input_source_type = "none"
            row.input_snapshot = {"source_type": "none", "dependency_check": True}
        incident_id = _bound_incident(session, row.id).id

    with session_factory() as session:
        result = _dispose(
            session,
            execution_id=execution["id"],
            incident_id=incident_id,
            action="recover",
        )
    assert result.response.receipt.outcome == "incident_materials_unavailable"


@pytest.mark.parametrize(
    ("source_type", "snapshot", "runtime_input", "dependency_check"),
    [
        ("json", {"source_type": "json", "revision": 1}, None, False),
        ("json", {"source_type": "json", "revision": 1}, "scalar", False),
        ("json", {"source_type": "json", "revision": 1}, 2**60, False),
        ("none", {"source_type": "none", "revision": 1}, None, False),
        (
            "json",
            {"source_type": "json", "revision": 1, "legacy_override": True},
            {"legacy": True},
            False,
        ),
        ("none", {"source_type": "none", "dependency_check": True}, None, True),
    ],
)
def test_recovery_accepts_known_nonmanaged_input_snapshot_shapes(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    source_type: str,
    snapshot: dict[str, object],
    runtime_input: object,
    dependency_check: bool,
) -> None:
    _enable_runtime(monkeypatch)
    name = f"issue152-input-valid-{source_type}-{dependency_check}-{type(runtime_input).__name__}"
    worker = _ready_worker(api_client, f"{name}-worker")
    adapter = _rabbit_adapter(api_client, worker, f"{name}-adapter")
    execution = _execution(api_client, adapter["id"])
    with session_factory.begin() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        row.input_source_type = source_type
        row.input_snapshot = snapshot
        row.input = runtime_input
        row.dependency_check = dependency_check
        incident_id = _bound_incident(session, row.id).id

    with session_factory() as session:
        result = _dispose(
            session,
            execution_id=execution["id"],
            incident_id=incident_id,
            action="recover",
        )
    assert result.response.receipt.outcome == "dispatch_already_pending"


def test_recovery_rejects_runtime_input_changed_after_material_preflight(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-input-race-worker")
    adapter = _rabbit_adapter(api_client, worker, "issue152-input-race-adapter")
    execution = _execution(api_client, adapter["id"])
    with session_factory.begin() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        row.input = {"original": "input"}
        incident_id = _bound_incident(session, row.id).id

    preflight = incident_disposition_service.preflight_recovery_materials

    def change_after_preflight(session: Session, row: Execution):
        proof = preflight(session, row)
        with session_factory.begin() as concurrent:
            changed = concurrent.get(Execution, execution["id"])
            assert changed is not None
            changed.input = {"changed": "input"}
        return proof

    monkeypatch.setattr(
        incident_disposition_service,
        "preflight_recovery_materials",
        change_after_preflight,
    )
    with session_factory() as session:
        result = _dispose(
            session,
            execution_id=execution["id"],
            incident_id=incident_id,
            action="recover",
        )
    assert result.response.receipt.outcome == "incident_materials_unavailable"


@pytest.mark.parametrize("corrupt", [False, True])
def test_recovery_validates_frozen_credential_rows_without_current_binding_or_secret_output(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    corrupt: bool,
) -> None:
    _enable_runtime(monkeypatch)
    monkeypatch.setattr(settings, "master_key", "issue152-material-test-master-key")
    worker = _ready_worker(api_client, f"issue152-credential-{corrupt}-worker")  # type: ignore[arg-type]
    adapter = _rabbit_adapter(  # type: ignore[arg-type]
        api_client, worker, f"issue152-credential-{corrupt}-adapter"
    )
    execution = _execution(api_client, adapter["id"])  # type: ignore[arg-type]
    secret = "issue152-secret-must-not-leak"
    with session_factory.begin() as session:
        credential = Credential(
            name=f"issue152-credential-{corrupt}",
            type="token",
            ciphertext=encrypt_fields({"token": secret}),
        )
        session.add(credential)
        session.flush()
        if corrupt:
            credential.ciphertext = "corrupt-ciphertext"
        row = session.get(Execution, execution["id"])
        assert row is not None
        snapshot = {
            "binding_id": 991,
            "credential_id": credential.id,
            "env_key": "ISSUE152_TOKEN",
            "field": "token",
        }
        row.credential_bindings_snapshot = [snapshot]
        session.add(
            ExecutionCredentialBindingSnapshot(
                execution_id=row.id,
                **snapshot,
            )
        )
        incident_id = _bound_incident(session, row.id).id

    with session_factory() as session:
        result = _dispose(
            session,
            execution_id=execution["id"],
            incident_id=incident_id,
            action="recover",
        )
    assert result.response.receipt.outcome == (
        "incident_materials_unavailable" if corrupt else "dispatch_already_pending"
    )
    assert secret not in result.response.model_dump_json()


def test_recovery_holds_and_validates_original_builtin_package(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _enable_runtime(monkeypatch)
    monkeypatch.setattr(settings, "builtin_package_root", str(tmp_path / "builtin"))
    worker = _ready_worker(api_client, "issue152-builtin-worker")  # type: ignore[arg-type]
    adapter = _rabbit_adapter(api_client, worker, "issue152-builtin-adapter")  # type: ignore[arg-type]
    execution = _execution(api_client, adapter["id"])  # type: ignore[arg-type]
    storage_key = str(uuid.uuid4())
    content = b"frozen-builtin-material"
    digest = hashlib.sha256(content).hexdigest()
    metadata = {"name": "offline", "version": "1.0.0", "environment": "any"}
    builtin_package.storage_path(storage_key).write_bytes(content)
    with session_factory.begin() as session:
        package = BuiltinPackage(
            kind="pypi",
            name="offline",
            version="1.0.0",
            environment="any",
            filename="offline-1.0.0-py3-none-any.whl",
            repository_path="offline/offline-1.0.0-py3-none-any.whl",
            size_bytes=len(content),
            sha256=digest,
            storage_key=storage_key,
            package_metadata=metadata,
            status="uploaded",
        )
        session.add(package)
        session.flush()
        row = session.get(Execution, execution["id"])
        worker_row = session.get(Worker, worker["id"])
        assert row is not None and worker_row is not None
        row.builtin_package_snapshot = {
            "kind": "pypi",
            "files": [
                {
                    "id": package.id,
                    "filename": package.filename,
                    "repository_path": package.repository_path,
                    "size_bytes": package.size_bytes,
                    "sha256": package.sha256,
                    "metadata": metadata,
                }
            ],
        }
        worker_row.isolation_capabilities = {
            **worker_row.isolation_capabilities,
            "builtin_packages_v1": True,
        }
        incident_id = _bound_incident(session, row.id).id

    record = incident_disposition_service._record

    def fail_after_validation(*args: object, **kwargs: object):
        assert not _exclusive_flock_available(builtin_package.storage_path(storage_key, ".lock"))
        raise RuntimeError("injected disposition audit failure")

    monkeypatch.setattr(incident_disposition_service, "_record", fail_after_validation)
    with (
        session_factory() as session,
        pytest.raises(RuntimeError, match="injected disposition audit failure"),
    ):
        _dispose(
            session,
            execution_id=execution["id"],
            incident_id=incident_id,
            action="recover",
        )
    assert _exclusive_flock_available(builtin_package.storage_path(storage_key, ".lock"))
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        incident = session.get(ExecutionInfrastructureIncident, incident_id)
        assert row is not None and row.dispatch_generation == 1
        assert incident is not None and incident.status == "open"
        assert session.scalar(select(ExecutionIncidentDisposition)) is None

    monkeypatch.setattr(incident_disposition_service, "_record", record)
    with session_factory() as session:
        accepted = _dispose(
            session,
            execution_id=execution["id"],
            incident_id=incident_id,
            action="recover",
        )
    assert accepted.response.receipt.outcome == "dispatch_already_pending"


def test_pending_recovery_reuses_responsibility_and_idempotency_key(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-pending-worker")  # type: ignore[arg-type]
    adapter = _rabbit_adapter(api_client, worker, "issue152-pending-adapter")  # type: ignore[arg-type]
    execution = _execution(api_client, adapter["id"])  # type: ignore[arg-type]
    with session_factory.begin() as session:
        incident_id = _bound_incident(session, execution["id"]).id
        adapter_count = session.get(AdapterExecutionAdmission, adapter["id"])
        global_count = session.get(GlobalExecutionAdmission, "global")
        assert adapter_count is not None and global_count is not None
        before_counts = (adapter_count.outstanding_count, global_count.outstanding_count)

    key = uuid.uuid4()
    with session_factory() as session:
        result = _dispose(
            session,
            execution_id=execution["id"],
            incident_id=incident_id,
            action="recover",
            key=key,
        )
        assert result.status_code == 200
        assert result.response.receipt.outcome == "dispatch_already_pending"

    with session_factory() as session:
        replay = _dispose(
            session,
            execution_id=execution["id"],
            incident_id=incident_id,
            action="recover",
            key=key,
        )
        assert replay.response.receipt.id == result.response.receipt.id
        assert replay.response.incident_status == "resolved"

    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        adapter_count = session.get(AdapterExecutionAdmission, adapter["id"])
        global_count = session.get(GlobalExecutionAdmission, "global")
        assert row is not None and row.dispatch_generation == 1 and row.attempt_count == 0
        assert adapter_count is not None and global_count is not None
        assert (adapter_count.outstanding_count, global_count.outstanding_count) == before_counts
        assert len(list(session.scalars(select(ExecutionIncidentDisposition)))) == 1


def test_published_recovery_creates_one_new_generation_without_rewriting_old_row(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-published-worker")  # type: ignore[arg-type]
    adapter = _rabbit_adapter(api_client, worker, "issue152-published-adapter")  # type: ignore[arg-type]
    execution = _execution(api_client, adapter["id"])  # type: ignore[arg-type]
    with session_factory.begin() as session:
        old = session.scalar(select(ExecutionOutbox))
        assert old is not None
        old.status = "published"
        old.published_at = datetime.now(UTC)
        old_id = old.id
        old_message_id = old.message_id
        incident_id = _incident(session, execution["id"], message_id=old.message_id).id

    with session_factory() as session:
        result = _dispose(
            session,
            execution_id=execution["id"],
            incident_id=incident_id,
            action="recover",
        )
        assert result.status_code == 200
        assert result.response.receipt.outcome == "recovery_dispatched"

    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        rows = list(
            session.scalars(select(ExecutionOutbox).order_by(ExecutionOutbox.dispatch_generation))
        )
        assert row is not None and row.dispatch_generation == 2 and row.attempt_count == 0
        assert len(rows) == 2
        assert rows[0].id == old_id and rows[0].message_id == old_message_id
        assert rows[0].status == "published" and rows[0].last_error_code is None
        assert rows[1].status == "pending" and rows[1].message_id != old_message_id
        assert rows[1].payload_json["dispatch_generation"] == 2

    detail = api_client.get(f"/api/executions/{execution['id']}/reliable-detail")  # type: ignore[union-attr]
    assert detail.status_code == 200, detail.text
    incident_body = detail.json()["incidents"][0]
    assert incident_body["observation_count"] == 3
    assert incident_body["disposition_count"] == 1
    assert incident_body["recovery_dispatch_count"] == 1
    assert incident_body["recent_disposition"]["outcome"] == "recovery_dispatched"
    assert incident_body["recover_available"] is False
    assert incident_body["recover_reason"] == "incident_closed"


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("live_lease", "incident_dispatch_inflight"),
        ("settled", "incident_dispatch_settled"),
        ("identity", "incident_dispatch_identity_invalid"),
        ("language", "incident_dispatch_identity_invalid"),
        ("target_current", "incident_dispatch_identity_invalid"),
        ("empty_owner", "incident_dispatch_identity_unverifiable"),
    ],
)
def test_recovery_rejects_unowned_or_unverifiable_outbox(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    setup: str,
    expected: str,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"issue152-{setup}-worker")  # type: ignore[arg-type]
    adapter = _rabbit_adapter(api_client, worker, f"issue152-{setup}-adapter")  # type: ignore[arg-type]
    execution = _execution(api_client, adapter["id"])  # type: ignore[arg-type]
    with session_factory.begin() as session:
        outbox_row = session.scalar(select(ExecutionOutbox))
        assert outbox_row is not None
        incident_id = _incident(session, execution["id"], message_id=outbox_row.message_id).id
        if setup == "live_lease":
            outbox_row.lease_owner = "relay"
            outbox_row.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        elif setup == "empty_owner":
            outbox_row.lease_owner = ""
            outbox_row.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        elif setup == "settled":
            outbox_row.status = "published"
            outbox_row.published_at = datetime.now(UTC)
            outbox_row.last_error_code = "execution_cancelled"
        elif setup == "identity":
            outbox_row.payload_json = {**outbox_row.payload_json, "resource_class": "tampered"}
        elif setup == "language":
            outbox_row.payload_json = {**outbox_row.payload_json, "language": "java"}
        else:
            execution_row = session.get(Execution, execution["id"])
            assert execution_row is not None
            execution_row.target_worker_id = None

    with session_factory() as session:
        result = _dispose(
            session,
            execution_id=execution["id"],
            incident_id=incident_id,
            action="recover",
        )
        assert result.status_code == 409
        assert result.response.receipt.outcome == expected
        if setup == "live_lease":
            assert result.response.retry_after_seconds is not None
            assert 1 <= result.response.retry_after_seconds <= 61

    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        incident = session.get(ExecutionInfrastructureIncident, incident_id)
        assert row is not None and row.dispatch_generation == 1
        assert incident is not None and incident.status == "open"


def test_active_recover_rejects_but_terminate_only_requests_cancellation(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-active-worker")  # type: ignore[arg-type]
    adapter = _rabbit_adapter(api_client, worker, "issue152-active-adapter")  # type: ignore[arg-type]
    execution = _execution(api_client, adapter["id"])  # type: ignore[arg-type]
    with session_factory.begin() as session:
        outbox_row = session.scalar(select(ExecutionOutbox))
        assert outbox_row is not None
        outbox_row.status = "published"
        outbox_row.published_at = datetime.now(UTC)
        incident_id = _incident(session, execution["id"], message_id=outbox_row.message_id).id
    claimed = _claim(session_factory, worker["id"], _dispatch(session_factory, execution["id"]))
    assert claimed.attempt_id is not None

    with session_factory() as session:
        rejected = _dispose(
            session,
            execution_id=execution["id"],
            incident_id=incident_id,
            action="recover",
        )
        assert rejected.status_code == 409
        assert rejected.response.receipt.outcome == "incident_execution_active"

    with session_factory() as session:
        accepted = _dispose(
            session,
            execution_id=execution["id"],
            incident_id=incident_id,
            action="terminate",
        )
        assert accepted.status_code == 202
        assert accepted.response.receipt.outcome == "cancellation_requested"

    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        incident = session.get(ExecutionInfrastructureIncident, incident_id)
        assert row is not None and row.status == "running" and row.cancel_requested is True
        assert row.admission_released_at is None
        assert incident is not None and incident.status == "open"


def test_retry_wait_with_active_attempt_terminate_is_audited_rejection(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-retry-active-worker")  # type: ignore[arg-type]
    adapter = _rabbit_adapter(api_client, worker, "issue152-retry-active-adapter")  # type: ignore[arg-type]
    execution = _execution(api_client, adapter["id"])  # type: ignore[arg-type]
    with session_factory.begin() as session:
        outbox_row = session.scalar(select(ExecutionOutbox))
        assert outbox_row is not None
        outbox_row.status = "published"
        outbox_row.published_at = datetime.now(UTC)
        incident_id = _incident(session, execution["id"], message_id=outbox_row.message_id).id
    claimed = _claim(session_factory, worker["id"], _dispatch(session_factory, execution["id"]))
    assert claimed.attempt_id is not None
    with session_factory.begin() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        row.status = "retry_wait"

    with session_factory() as session:
        result = _dispose(
            session,
            execution_id=execution["id"],
            incident_id=incident_id,
            action="terminate",
        )
    assert result.response.receipt.outcome == "incident_execution_active"
    with session_factory() as session:
        receipts = list(session.scalars(select(ExecutionIncidentDisposition)))
        assert len(receipts) == 1
        row = session.get(Execution, execution["id"])
        assert row is not None and row.status == "retry_wait"


def test_recovery_capacity_rejection_is_audited_without_generation_change(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-capacity-worker")  # type: ignore[arg-type]
    adapter = _rabbit_adapter(api_client, worker, "issue152-capacity-adapter")  # type: ignore[arg-type]
    execution = _execution(api_client, adapter["id"])  # type: ignore[arg-type]
    with session_factory.begin() as session:
        outbox_row = session.scalar(select(ExecutionOutbox))
        assert outbox_row is not None
        outbox_row.status = "published"
        outbox_row.published_at = datetime.now(UTC)
        incident_id = _incident(session, execution["id"], message_id=outbox_row.message_id).id
    monkeypatch.setattr(settings, "outbox_max_pending_count", 0)

    with session_factory() as session:
        result = _dispose(
            session,
            execution_id=execution["id"],
            incident_id=incident_id,
            action="recover",
        )
        assert result.status_code == 503
        assert result.response.receipt.outcome == "outbox_backlog_full"

    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        rows = list(session.scalars(select(ExecutionOutbox)))
        assert row is not None and row.dispatch_generation == 1
        assert len(rows) == 1 and rows[0].status == "published"
        assert len(list(session.scalars(select(ExecutionIncidentDisposition)))) == 1


def test_stale_incident_cannot_recover_and_terminate_only_ignores_it(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-stale-worker")  # type: ignore[arg-type]
    adapter = _rabbit_adapter(api_client, worker, "issue152-stale-adapter")  # type: ignore[arg-type]
    execution = _execution(api_client, adapter["id"])  # type: ignore[arg-type]
    with session_factory.begin() as session:
        old = session.scalar(select(ExecutionOutbox))
        row = session.get(Execution, execution["id"])
        assert old is not None and row is not None
        old.status = "published"
        old.published_at = datetime.now(UTC)
        row.dispatch_generation = 2
        stale_id = _incident(session, execution["id"], message_id=old.message_id).id

    # expected_generation is current generation, while the Incident remains generation 1.
    with session_factory() as session:
        rejected = dispose_incident(
            session,
            execution["id"],
            stale_id,
            "recover",
            2,
            uuid.uuid4(),
            "capacity_repaired",
            SUPERADMIN_PRINCIPAL,
        )
        assert rejected.status_code == 409
        assert rejected.response.receipt.outcome == "incident_stale_generation"

    with session_factory() as session:
        ignored = dispose_incident(
            session,
            execution["id"],
            stale_id,
            "terminate",
            2,
            uuid.uuid4(),
            "operator_cancel",
            SUPERADMIN_PRINCIPAL,
        )
        assert ignored.status_code == 200
        assert ignored.response.receipt.outcome == "stale_incident_ignored"
        assert ignored.response.incident_status == "ignored"

    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None and row.status == "queued" and row.dispatch_generation == 2
        assert row.cancel_requested is False


@pytest.mark.parametrize("action", ["recover", "terminate"])
def test_terminal_execution_is_only_verified_and_never_reopened(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    action: Literal["recover", "terminate"],
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"issue152-terminal-{action}-worker")  # type: ignore[arg-type]
    adapter = _rabbit_adapter(api_client, worker, f"issue152-terminal-{action}-adapter")  # type: ignore[arg-type]
    execution = _execution(api_client, adapter["id"])  # type: ignore[arg-type]
    with session_factory.begin() as session:
        incident_id = _bound_incident(session, execution["id"]).id
    with session_factory() as session:
        cancelled = execution_service.cancel_execution(session, execution["id"])
        assert cancelled.status == "cancelled"

    with session_factory() as session:
        result = _dispose(
            session,
            execution_id=execution["id"],
            incident_id=incident_id,
            action=action,
        )
        assert result.status_code == 200
        assert result.response.receipt.outcome == "execution_terminal"
        assert result.response.receipt.code == "execution_cancelled"

    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None and row.status == "cancelled" and row.dispatch_generation == 1


def test_concurrent_distinct_keys_create_only_one_replacement_generation(
    api_client: object,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-concurrent-worker")  # type: ignore[arg-type]
    adapter = _rabbit_adapter(api_client, worker, "issue152-concurrent-adapter")  # type: ignore[arg-type]
    execution = _execution(api_client, adapter["id"])  # type: ignore[arg-type]
    with session_factory.begin() as session:
        old = session.scalar(select(ExecutionOutbox))
        assert old is not None
        old.status = "published"
        old.published_at = datetime.now(UTC)
        incident_id = _incident(session, execution["id"], message_id=old.message_id).id

    barrier = threading.Barrier(2)

    def recover() -> IncidentDispositionResult:
        barrier.wait(timeout=10)
        with session_factory() as session:
            return _dispose(
                session,
                execution_id=execution["id"],
                incident_id=incident_id,
                action="recover",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result(timeout=15) for future in (pool.submit(recover), pool.submit(recover))
        ]

    assert sorted(result.status_code for result in results) == [200, 409]
    assert {result.response.receipt.outcome for result in results} == {
        "recovery_dispatched",
        "incident_generation_conflict",
    }
    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        outbox_rows = list(session.scalars(select(ExecutionOutbox)))
        receipts = list(session.scalars(select(ExecutionIncidentDisposition)))
        assert row is not None and row.dispatch_generation == 2
        assert len(outbox_rows) == 2 and len(receipts) == 2
