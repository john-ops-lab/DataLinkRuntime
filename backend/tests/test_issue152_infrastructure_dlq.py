"""Issue #152B structured x-death classification contracts."""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.models import (
    Execution,
    ExecutionIncidentDisposition,
    ExecutionInfrastructureIncident,
    ExecutionOutbox,
)
from dlr.control.security import SUPERADMIN_PRINCIPAL
from dlr.control.services import incident_disposition, infrastructure_dlq, rabbitmq
from test_issue130_b2_runtime import (
    _dispatch,
    _enable_runtime,
    _execution,
    _rabbit_adapter,
    _ready_worker,
)
from test_issue152_incident_disposition import _bound_incident


def _published_execution(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> tuple[dict, dict, bytes]:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, f"{name}-worker")
    adapter = _rabbit_adapter(api_client, worker, f"{name}-adapter")
    execution = _execution(api_client, adapter["id"])
    body = json.dumps(_dispatch(session_factory, execution["id"])).encode()
    with session_factory.begin() as session:
        outbox = session.scalar(
            select(ExecutionOutbox).where(ExecutionOutbox.execution_id == execution["id"])
        )
        assert outbox is not None
        outbox.status = "published"
        outbox.published_at = datetime.now(UTC)
    return worker, execution, body


@pytest.mark.parametrize(
    ("reason", "expected_action"),
    [
        ("delivery_limit", "manual_review"),
        ("rejected", "requeue"),
        ("expired", "requeue"),
        ("maxlen", "requeue"),
    ],
)
def test_structured_x_death_reason_controls_incident_and_automatic_flow(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    expected_action: str,
) -> None:
    worker, execution, body = _published_execution(
        api_client,
        session_factory,
        monkeypatch,
        f"issue152-death-{reason}",
    )
    queue = rabbitmq.topology_names(worker["id"]).queue

    with session_factory() as session:
        result = infrastructure_dlq.reconcile_message(
            session,
            body,
            headers={"x-death": [{"queue": queue, "reason": reason, "count": 1}]},
        )
    assert result.kind == reason
    assert result.action == expected_action

    with session_factory() as session:
        incident = session.get(ExecutionInfrastructureIncident, result.incident_id)
        outbox = session.scalar(
            select(ExecutionOutbox).where(ExecutionOutbox.execution_id == execution["id"])
        )
        execution_row = session.get(Execution, execution["id"])
        assert incident is not None and incident.kind == reason and incident.status == "open"
        assert execution_row is not None and execution_row.status == "queued"
        assert outbox is not None
        if reason == "delivery_limit":
            assert outbox.status == "published"
            assert outbox.last_error_code is None
        else:
            assert outbox.status == "pending"
            assert outbox.last_error_code == "infrastructure_dlq_requeue"


def test_x_death_uses_newest_event_for_the_matching_dispatch_queue(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, _execution_body, body = _published_execution(
        api_client,
        session_factory,
        monkeypatch,
        "issue152-death-history",
    )
    queue = rabbitmq.topology_names(worker["id"]).queue
    headers = {
        "x-death": [
            {"queue": "unrelated.queue", "reason": "delivery_limit", "count": 8},
            {"queue": queue, "reason": "rejected", "count": 1},
            {"queue": queue, "reason": "delivery_limit", "count": 4},
        ]
    }

    with session_factory() as session:
        result = infrastructure_dlq.reconcile_message(session, body, headers=headers)
    assert result.kind == "rejected"
    assert result.action == "requeue"


@pytest.mark.parametrize(
    "malformed",
    [
        "missing",
        "configuration_header",
        "not_list",
        "empty",
        "different_queue",
        "missing_count",
        "boolean_count",
        "zero_count",
        "unknown_reason",
    ],
)
def test_missing_malformed_or_other_queue_x_death_is_unknown_not_delivery_limit(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    malformed: str,
) -> None:
    worker, _execution_body, body = _published_execution(
        api_client,
        session_factory,
        monkeypatch,
        f"issue152-death-unknown-{malformed}",
    )
    queue = rabbitmq.topology_names(worker["id"]).queue
    event: object = {"queue": queue, "reason": "delivery_limit", "count": 1}
    if malformed == "missing":
        headers = {}
    elif malformed == "configuration_header":
        headers = {"x-delivery-limit": 5}
    elif malformed == "not_list":
        headers = {"x-death": event}
    elif malformed == "empty":
        headers = {"x-death": []}
    else:
        assert isinstance(event, dict)
        if malformed == "different_queue":
            event["queue"] = "unrelated.queue"
        elif malformed == "missing_count":
            event.pop("count")
        elif malformed == "boolean_count":
            event["count"] = True
        elif malformed == "zero_count":
            event["count"] = 0
        else:
            event["reason"] = "unexpected"
        headers = {"x-death": [event]}

    with session_factory() as session:
        result = infrastructure_dlq.reconcile_message(session, body, headers=headers)
    assert result.kind == "unknown"
    assert result.action == "requeue"


def test_dlq_requeue_and_manual_disposition_share_lock_order_without_deadlock(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-death-disposition-race-worker")
    adapter = _rabbit_adapter(api_client, worker, "issue152-death-disposition-race-adapter")
    execution = _execution(api_client, adapter["id"])
    body = json.dumps(_dispatch(session_factory, execution["id"])).encode()
    queue = rabbitmq.topology_names(worker["id"]).queue
    with session_factory.begin() as session:
        incident_id = _bound_incident(session, execution["id"]).id

    barrier = threading.Barrier(2)

    def dispose() -> str:
        with session_factory() as session:
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            barrier.wait(timeout=5)
            result = incident_disposition.dispose_incident(
                session,
                execution["id"],
                incident_id,
                "recover",
                1,
                uuid.uuid4(),
                "capacity_repaired",
                SUPERADMIN_PRINCIPAL,
            )
            return result.response.receipt.outcome

    def reconcile() -> tuple[str, str]:
        with session_factory() as session:
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            barrier.wait(timeout=5)
            result = infrastructure_dlq.reconcile_message(
                session,
                body,
                headers={"x-death": [{"queue": queue, "reason": "rejected", "count": 1}]},
            )
            return result.action, result.kind

    with ThreadPoolExecutor(max_workers=2) as pool:
        dispose_future = pool.submit(dispose)
        reconcile_future = pool.submit(reconcile)
        assert dispose_future.result(timeout=10) == "dispatch_already_pending"
        assert reconcile_future.result(timeout=10) == ("requeue", "rejected")

    with session_factory() as session:
        execution_row = session.get(Execution, execution["id"])
        original_incident = session.get(ExecutionInfrastructureIncident, incident_id)
        incidents = list(
            session.scalars(
                select(ExecutionInfrastructureIncident).where(
                    ExecutionInfrastructureIncident.execution_id == execution["id"]
                )
            )
        )
        receipts = list(session.scalars(select(ExecutionIncidentDisposition)))
        assert execution_row is not None and execution_row.dispatch_generation == 1
        assert original_incident is not None and original_incident.status == "resolved"
        assert {(row.kind, row.attempts) for row in incidents} == {
            ("delivery_limit", 3),
            ("rejected", 1),
        }
        assert len(receipts) == 1
        assert receipts[0].outcome == "dispatch_already_pending"
