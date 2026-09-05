"""API-test delivery helpers for the sole RabbitMQ/Attempt protocol.

These read the real durable Outbox to simulate broker delivery; they never
call the removed polling/result endpoints. Sandbox enforcement is tested by
Worker tests and the real Linux integration gate, not by this API transport.
"""

from typing import Any
from weakref import WeakKeyDictionary

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import WORKER_TOKEN
from dlr.control import db
from dlr.control.models import Execution, ExecutionOutbox
from dlr.control.schemas.worker import REQUIRED_ISOLATION_CAPABILITIES
from dlr.control.services import rabbitmq

WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}
ISOLATION_PASS = {key: True for key in REQUIRED_ISOLATION_CAPABILITIES}
_attempt_credentials: WeakKeyDictionary[Any, dict[int, dict[str, Any]]] = WeakKeyDictionary()


def ready_registration(name: str, capabilities: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "capabilities": capabilities,
        "protocol_version": 3,
        "isolation_capabilities": dict(ISOLATION_PASS),
    }


def mark_broker_ready() -> None:
    """Explicit test seam for the broker topology probe, never the Claim service."""
    rabbitmq.mark_runtime_ready()


def claim_execution(
    client: TestClient,
    worker_id: int,
    *,
    execution_id: int | None = None,
) -> httpx.Response:
    """Deliver a real queued Outbox message and start the resulting Attempt."""
    with db.SessionLocal() as session:
        dispatch = session.scalar(
            select(ExecutionOutbox)
            .join(Execution, Execution.id == ExecutionOutbox.execution_id)
            .where(
                Execution.target_worker_id == worker_id,
                Execution.status == "queued",
                ExecutionOutbox.dispatch_generation == Execution.dispatch_generation,
                *([Execution.id == execution_id] if execution_id is not None else []),
            )
            .order_by(Execution.id)
            .limit(1)
        )
        message = dict(dispatch.payload_json) if dispatch is not None else None
    if message is None:
        return httpx.Response(204)
    response = client.post(
        f"/api/workers/{worker_id}/v3/claim", json=message, headers=WORKER_HEADERS
    )
    if response.status_code != 200:
        return response
    decision = response.json()
    if decision["decision"] != "EXECUTE":
        return httpx.Response(204, headers={"X-Test-Claim-Decision": decision["decision"]})
    payload = decision["payload"]
    _attempt_credentials.setdefault(client.app, {})[payload["execution_id"]] = payload
    started = client.post(
        f"/api/workers/{worker_id}/attempts/{payload['attempt_id']}/start",
        json=attempt_auth(payload),
        headers=WORKER_HEADERS,
    )
    assert started.status_code == 200, started.text
    assert started.json()["reason"] == "started", started.text
    return httpx.Response(200, json=payload)


def attempt_auth(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: payload[key] for key in ("attempt_id", "fencing_token", "claim_token")}


def execution_attempt_auth(client: TestClient, execution_id: int) -> dict[str, Any]:
    payload = _attempt_credentials.get(client.app, {}).get(execution_id)
    return (
        attempt_auth(payload)
        if payload is not None
        else {"attempt_id": 999999, "fencing_token": 1, "claim_token": "unknown-test-attempt"}
    )


def report_attempt(
    client: TestClient, worker_id: int, execution_id: int, report: dict[str, Any]
) -> httpx.Response:
    auth = execution_attempt_auth(client, execution_id)
    response = client.post(
        f"/api/workers/{worker_id}/attempts/{auth['attempt_id']}/result",
        json={**auth, **report},
        headers=WORKER_HEADERS,
    )
    if response.status_code != 200:
        return response
    return client.get(f"/api/executions/{execution_id}")


def progress_attempt(
    client: TestClient,
    worker_id: int,
    execution_id: int,
    report: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    auth = execution_attempt_auth(client, execution_id)
    return client.post(
        f"/api/workers/{worker_id}/attempts/{auth['attempt_id']}/progress",
        json={**auth, **report},
        headers=WORKER_HEADERS if headers is None else headers,
    )
