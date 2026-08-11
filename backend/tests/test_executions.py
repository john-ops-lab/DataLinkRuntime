"""Tests for the M2 Manual Execution management API against real PostgreSQL."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import Execution
from test_adapters import create_adapter, save_version


def create_execution(client: TestClient, adapter_id: int, payload: dict | None = None) -> dict:
    response = client.post(f"/api/adapters/{adapter_id}/executions", json=payload or {})
    assert response.status_code == 202, response.text
    return response.json()


def test_create_execution_binds_latest_version(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="exec-latest")
    v1 = save_version(api_client, adapter["id"], code="# v1\n")
    v2 = save_version(api_client, adapter["id"], code="# v2\n")

    execution = create_execution(api_client, adapter["id"], {"input": {"k": 1}})
    assert execution["status"] == "pending"
    assert execution["trigger"] == "manual"
    assert execution["version_id"] == v2["id"], "manual execution defaults to latest"
    assert execution["adapter_id"] == adapter["id"]
    assert execution["input"] == {"k": 1}
    assert execution["worker_id"] is None
    assert execution["started_at"] is None
    assert execution["ended_at"] is None

    # A later Save must not move the already-created Execution.
    save_version(api_client, adapter["id"], code="# v3\n")
    fetched = api_client.get(f"/api/executions/{execution['id']}").json()
    assert fetched["version_id"] == v2["id"]
    assert v1["id"] != v2["id"]


def test_create_execution_explicit_historical_version(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="exec-history")
    v1 = save_version(api_client, adapter["id"], code="# v1\n")
    save_version(api_client, adapter["id"], code="# v2\n")

    execution = create_execution(api_client, adapter["id"], {"version_id": v1["id"], "input": None})
    assert execution["version_id"] == v1["id"]
    # JSON null is a valid input.
    assert execution["input"] is None


def test_create_execution_adapter_not_found(api_client: TestClient) -> None:
    response = api_client.post("/api/adapters/999999/executions", json={})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "adapter_not_found"


def test_create_execution_without_any_version_conflicts(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="exec-empty")
    response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_has_no_version"


def test_create_execution_cross_adapter_version_not_found(api_client: TestClient) -> None:
    adapter_a = create_adapter(api_client, name="exec-a")
    adapter_b = create_adapter(api_client, name="exec-b")
    foreign_version = save_version(api_client, adapter_b["id"])

    response = api_client.post(
        f"/api/adapters/{adapter_a['id']}/executions",
        json={"version_id": foreign_version["id"]},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "version_not_found"


def test_create_execution_input_too_large_not_persisted(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "execution_input_max_bytes", 1024)
    adapter = create_adapter(api_client, name="exec-big-input")
    save_version(api_client, adapter["id"])

    response = api_client.post(
        f"/api/adapters/{adapter['id']}/executions", json={"input": "x" * 4096}
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "execution_input_too_large"

    with session_factory() as session:
        rows = session.scalars(select(Execution)).all()
        assert rows == [], "oversized input must never be persisted"

    # A fitting input still works after the rejection.
    ok = create_execution(api_client, adapter["id"], {"input": "small"})
    assert ok["input"] == "small"


def test_get_execution_not_found(api_client: TestClient) -> None:
    response = api_client.get("/api/executions/999999")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "execution_not_found"


def test_delete_adapter_with_executions_conflicts(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter = create_adapter(api_client, name="exec-protected")
    save_version(api_client, adapter["id"])
    create_execution(api_client, adapter["id"])

    response = api_client.delete(f"/api/adapters/{adapter['id']}")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_has_executions"

    # The Adapter and its history are still fully intact.
    assert api_client.get(f"/api/adapters/{adapter['id']}").status_code == 200
    with session_factory() as session:
        assert len(session.scalars(select(Execution)).all()) == 1


def test_delete_adapter_without_executions_still_allowed(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="exec-deletable")
    save_version(api_client, adapter["id"])
    assert api_client.delete(f"/api/adapters/{adapter['id']}").status_code == 204
    assert api_client.get(f"/api/adapters/{adapter['id']}").status_code == 404
