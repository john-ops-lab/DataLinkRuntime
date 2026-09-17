"""Issue #152B durable Incident disposition contracts."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.models import (
    ExecutionIncidentDisposition,
    ExecutionInfrastructureIncident,
)
from dlr.control.schemas.reliable_runtime import IncidentDispositionBody
from dlr.control.services.incident_disposition import lookup_idempotency, request_hash
from test_issue130_b2_runtime import _enable_runtime, _execution, _rabbit_adapter, _ready_worker
from test_unified_runtime_migration import _isolated_schema, _upgrade


def _incident(session: Session, execution_id: int) -> ExecutionInfrastructureIncident:
    row = ExecutionInfrastructureIncident(
        execution_id=execution_id,
        dispatch_generation=1,
        message_id=uuid.uuid4(),
        kind="delivery_limit",
        attempts=3,
    )
    session.add(row)
    session.flush()
    return row


def _receipt(
    incident: ExecutionInfrastructureIncident,
    *,
    key: uuid.UUID,
    digest: str,
) -> ExecutionIncidentDisposition:
    assert incident.execution_id is not None
    return ExecutionIncidentDisposition(
        incident_id=incident.id,
        execution_id=incident.execution_id,
        idempotency_key=key,
        request_hash=digest,
        actor_kind="superadmin",
        user_id=None,
        action="recover",
        reason_code="capacity_repaired",
        outcome="dispatch_already_pending",
        code="dispatch_already_pending",
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
