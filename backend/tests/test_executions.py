"""Tests for the M2 Manual Execution management API against real PostgreSQL."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.common.config import settings
from dlr.control.models import Execution
from runtime_api_support import mark_broker_ready, ready_registration
from test_adapters import create_adapter, save_version

WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}


def register_worker(client: TestClient, name: str = "execution-worker") -> dict:
    existing = client.get("/api/workers").json()
    if existing:
        return existing[0]
    response = client.post(
        "/api/workers/register",
        json=ready_registration(name, ["python"]),
        headers=WORKER_HEADERS,
    )
    assert response.status_code == 200, response.text
    mark_broker_ready()
    return response.json()


def create_execution(client: TestClient, adapter_id: int, payload: dict | None = None) -> dict:
    response = client.post(f"/api/adapters/{adapter_id}/executions", json=payload or {})
    assert response.status_code == 202, response.text
    return response.json()


def test_create_execution_binds_latest_version(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="exec-latest")
    v1 = save_version(api_client, adapter["id"], code="# v1\n")
    v2 = save_version(api_client, adapter["id"], code="# v2\n")
    worker = register_worker(api_client)

    execution = create_execution(api_client, adapter["id"], {"input": {"k": 1}})
    assert execution["status"] == "queued"
    assert execution["trigger"] == "manual"
    assert execution["version_id"] == v2["id"], "manual execution defaults to latest"
    assert execution["adapter_id"] == adapter["id"]
    assert execution["input"] == {"k": 1}
    assert execution["target_worker_id"] == worker["id"]
    assert execution["worker_id"] is None
    assert execution["started_at"] is None
    assert execution["ended_at"] is None

    # Saving is locked while active; after the Execution reaches a terminal
    # state, a later Revision must not move the already-created audit fact.
    assert api_client.post(f"/api/executions/{execution['id']}/cancel").status_code == 200
    save_version(api_client, adapter["id"], code="# v3\n")
    fetched = api_client.get(f"/api/executions/{execution['id']}").json()
    assert fetched["version_id"] == v2["id"]
    assert v1["id"] != v2["id"]


def test_create_execution_rejects_historical_version_selection(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="exec-history")
    v1 = save_version(api_client, adapter["id"], code="# v1\n")
    save_version(api_client, adapter["id"], code="# v2\n")
    register_worker(api_client)

    response = api_client.post(
        f"/api/adapters/{adapter['id']}/executions",
        json={"version_id": v1["id"], "input": None},
    )
    assert response.status_code == 422


def test_create_execution_adapter_not_found(api_client: TestClient) -> None:
    response = api_client.post("/api/adapters/999999/executions", json={})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "adapter_not_found"


def test_create_execution_without_any_version_conflicts(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="exec-empty")
    response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_has_no_version"


def test_create_execution_rejects_any_version_id(api_client: TestClient) -> None:
    adapter_a = create_adapter(api_client, name="exec-a")
    adapter_b = create_adapter(api_client, name="exec-b")
    foreign_version = save_version(api_client, adapter_b["id"])

    response = api_client.post(
        f"/api/adapters/{adapter_a['id']}/executions",
        json={"version_id": foreign_version["id"]},
    )
    assert response.status_code == 422


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
    register_worker(api_client)
    ok = create_execution(api_client, adapter["id"], {"input": "small"})
    assert ok["input"] == "small"


def test_get_execution_not_found(api_client: TestClient) -> None:
    response = api_client.get("/api/executions/999999")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "execution_not_found"


def test_delete_adapter_with_active_execution_is_locked(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter = create_adapter(api_client, name="exec-protected")
    save_version(api_client, adapter["id"])
    register_worker(api_client)
    create_execution(api_client, adapter["id"])

    response = api_client.delete(f"/api/adapters/{adapter['id']}")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_runtime_locked"

    # The Adapter and its history are still fully intact.
    assert api_client.get(f"/api/adapters/{adapter['id']}").status_code == 200
    with session_factory() as session:
        assert len(session.scalars(select(Execution)).all()) == 1


def test_delete_adapter_without_executions_still_allowed(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="exec-deletable")
    save_version(api_client, adapter["id"])
    assert api_client.delete(f"/api/adapters/{adapter['id']}").status_code == 204
    deleted = api_client.get(f"/api/adapters/{adapter['id']}")
    assert deleted.status_code == 404


# --- M3 execution history (cursor pagination) --------------------------------


SUMMARY_FIELDS = {
    "id",
    "adapter_id",
    "version_id",
    "version_seq",
    "worker_id",
    "worker_name",
    "trigger",
    "scheduled_for",
    "status",
    "created_at",
    "started_at",
    "ended_at",
    "duration_ms",
}


def test_history_lists_newest_first_and_isolates_adapters(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="hist-main")
    save_version(api_client, adapter["id"])
    other = create_adapter(api_client, name="hist-other")
    save_version(api_client, other["id"])
    register_worker(api_client)
    ids = []
    for _ in range(3):
        execution_id = create_execution(api_client, adapter["id"])["id"]
        ids.append(execution_id)
        assert api_client.post(f"/api/executions/{execution_id}/cancel").status_code == 200
    other_execution = create_execution(api_client, other["id"])
    assert api_client.post(f"/api/executions/{other_execution['id']}/cancel").status_code == 200

    response = api_client.get(f"/api/adapters/{adapter['id']}/executions")
    assert response.status_code == 200
    page = response.json()
    assert [item["id"] for item in page["items"]] == list(reversed(ids))
    assert page["next_before_id"] is None
    assert all(item["adapter_id"] == adapter["id"] for item in page["items"])


def test_history_summaries_never_carry_big_fields(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="hist-summary")
    save_version(api_client, adapter["id"])
    register_worker(api_client)
    create_execution(api_client, adapter["id"], {"input": {"secret": "payload"}})

    page = api_client.get(f"/api/adapters/{adapter['id']}/executions").json()
    item = page["items"][0]
    assert set(item.keys()) == SUMMARY_FIELDS
    for forbidden in ("input", "output", "stdout", "stderr", "output_preview"):
        assert forbidden not in item
    assert item["status"] == "queued"
    assert item["trigger"] == "manual"
    assert item["scheduled_for"] is None
    assert item["worker_id"] is None
    assert item["worker_name"] is None


def test_history_before_id_cursor_walks_all_pages(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="hist-cursor")
    save_version(api_client, adapter["id"])
    register_worker(api_client)
    ids = []
    for _ in range(5):
        execution_id = create_execution(api_client, adapter["id"])["id"]
        ids.append(execution_id)
        assert api_client.post(f"/api/executions/{execution_id}/cancel").status_code == 200

    first = api_client.get(f"/api/adapters/{adapter['id']}/executions", params={"limit": 2}).json()
    assert [item["id"] for item in first["items"]] == [ids[4], ids[3]]
    assert first["next_before_id"] == ids[3]

    second = api_client.get(
        f"/api/adapters/{adapter['id']}/executions",
        params={"limit": 2, "before_id": first["next_before_id"]},
    ).json()
    assert [item["id"] for item in second["items"]] == [ids[2], ids[1]]
    assert second["next_before_id"] == ids[1]

    third = api_client.get(
        f"/api/adapters/{adapter['id']}/executions",
        params={"limit": 2, "before_id": second["next_before_id"]},
    ).json()
    assert [item["id"] for item in third["items"]] == [ids[0]]
    assert third["next_before_id"] is None, "no cursor when the history ends"


def test_history_limit_is_clamped(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="hist-limit")
    save_version(api_client, adapter["id"])
    register_worker(api_client)
    ids = []
    for _ in range(3):
        execution_id = create_execution(api_client, adapter["id"])["id"]
        ids.append(execution_id)
        assert api_client.post(f"/api/executions/{execution_id}/cancel").status_code == 200

    small = api_client.get(f"/api/adapters/{adapter['id']}/executions", params={"limit": 0}).json()
    assert len(small["items"]) == 1, "limit clamps up to 1"
    assert small["next_before_id"] == ids[2]

    huge = api_client.get(
        f"/api/adapters/{adapter['id']}/executions", params={"limit": 10_000}
    ).json()
    assert len(huge["items"]) == 3, "limit clamps down to the 100 cap"
    assert huge["next_before_id"] is None


def test_history_adapter_not_found(api_client: TestClient) -> None:
    response = api_client.get("/api/adapters/999999/executions")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "adapter_not_found"


def test_history_requires_admin_token(api_client: TestClient) -> None:
    response = api_client.get("/api/adapters/1/executions", headers={"Authorization": ""})
    assert response.status_code == 401
