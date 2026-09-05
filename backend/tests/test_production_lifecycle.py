"""M5.4.1 Adapter type, runtime Worker and lifecycle foundation tests."""

import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.control.models import (
    Adapter,
    AdapterExecutionSlot,
    AdapterVersion,
    Execution,
    ExecutionAttempt,
)
from dlr.control.models.platform import AdapterCredentialBinding
from runtime_api_support import ISOLATION_PASS, claim_execution, report_attempt
from test_adapters import create_adapter, save_version
from test_credentials import create_credential
from test_workers import register_worker

WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}


def wait_for_postgres_lock(session: Session, backend_pid: int) -> None:
    """Wait until one racer is blocked on a PostgreSQL lock, without sleep."""
    deadline = time.monotonic() + 5
    statement = text(
        "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :backend_pid"
    )
    while time.monotonic() < deadline:
        if session.scalar(statement, {"backend_pid": backend_pid}) is True:
            return
    raise AssertionError("concurrent database session did not enter a lock wait")


def finish_pending(client: TestClient, execution_id: int) -> None:
    response = client.post(f"/api/executions/{execution_id}/cancel")
    assert response.status_code == 200, response.text


def test_adapter_type_is_required_immutable_and_database_constrained(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    task = create_adapter(api_client, name="typed-task", adapter_type="task")
    webhook = create_adapter(api_client, name="typed-webhook", adapter_type="webhook")
    assert task["adapter_type"] == "task"
    assert webhook["adapter_type"] == "webhook"

    response = api_client.patch(
        f"/api/adapters/{task['id']}",
        json={"adapter_type": "webhook"},
    )
    assert response.status_code == 422

    with session_factory() as session:
        stored = session.get(Adapter, task["id"])
        assert stored is not None
        stored.adapter_type = "generic"
        with pytest.raises(IntegrityError):
            session.commit()


def test_first_save_auto_selects_only_compatible_runtime_worker(api_client: TestClient) -> None:
    worker = register_worker(api_client, name="single-save-worker")
    adapter = create_adapter(api_client, name="auto-worker")

    response = api_client.post(
        f"/api/adapters/{adapter['id']}/versions",
        json={"code": "def handle(context, input):\n    return input\n"},
    )
    assert response.status_code == 201, response.text
    fetched = api_client.get(f"/api/adapters/{adapter['id']}").json()
    assert fetched["runtime_worker_id"] == worker["id"]


def test_first_save_requires_explicit_worker_when_multiple_are_compatible(
    api_client: TestClient,
) -> None:
    first = register_worker(api_client, name="save-worker-a")
    register_worker(api_client, name="save-worker-b")
    adapter = create_adapter(api_client, name="choose-worker")
    path = f"/api/adapters/{adapter['id']}/versions"
    payload = {"code": "def handle(context, input):\n    return input\n"}

    response = api_client.post(path, json=payload)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "runtime_worker_required"
    assert api_client.get(f"/api/adapters/{adapter['id']}").json()["latest_version_id"] is None

    selected = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"runtime_worker_id": first["id"]},
    )
    assert selected.status_code == 200
    assert api_client.post(path, json=payload).status_code == 201


def test_runtime_worker_assignment_requires_language_but_allows_offline(
    api_client: TestClient,
) -> None:
    incompatible_response = api_client.post(
        "/api/workers/register",
        json={
            "protocol_version": 3,
            "isolation_capabilities": dict(ISOLATION_PASS),
            "name": "javascript-only",
            "capabilities": ["javascript"],
        },
        headers=WORKER_HEADERS,
    )
    assert incompatible_response.status_code == 200
    incompatible = incompatible_response.json()
    offline = register_worker(api_client, name="offline-python")
    assert (
        api_client.post(
            f"/api/workers/{offline['id']}/offline",
            headers=WORKER_HEADERS,
        ).status_code
        == 204
    )
    adapter = create_adapter(api_client, name="worker-validation")

    mismatch = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"runtime_worker_id": incompatible["id"]},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "worker_capability_missing"
    assigned = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"runtime_worker_id": offline["id"]},
    )
    assert assigned.status_code == 200
    assert assigned.json()["runtime_worker_id"] == offline["id"]
    saved = api_client.post(
        f"/api/adapters/{adapter['id']}/versions",
        json={"code": "def handle(context, input):\n    return input\n"},
    )
    assert saved.status_code == 201


def test_active_execution_locks_runtime_writes_but_allows_metadata(
    api_client: TestClient,
) -> None:
    adapter = create_adapter(api_client, name="active-lock")
    save_version(api_client, adapter["id"])
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text
    worker_id = execution.json()["target_worker_id"]
    assert claim_execution(api_client, worker_id).status_code == 200

    locked_writes = (
        api_client.post(
            f"/api/adapters/{adapter['id']}/versions",
            json={"code": "def handle(context, input):\n    return 2\n"},
        ),
        api_client.patch(
            f"/api/adapters/{adapter['id']}",
            json={"runtime_worker_id": None},
        ),
        api_client.put(
            f"/api/adapters/{adapter['id']}/credential-bindings",
            json={"bindings": []},
        ),
        api_client.delete(f"/api/adapters/{adapter['id']}"),
    )
    for response in locked_writes:
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "adapter_runtime_locked"

    metadata = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"name": "active-lock-renamed", "description": "metadata remains editable"},
    )
    assert metadata.status_code == 200
    assert metadata.json()["name"] == "active-lock-renamed"
    assert metadata.json()["runtime_locked"] is True


def test_database_slots_allow_only_one_active_attempt_across_triggers(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter = create_adapter(api_client, name="one-active")
    save_version(api_client, adapter["id"])
    worker_id = api_client.get(f"/api/adapters/{adapter['id']}").json()["runtime_worker_id"]

    accepted = [
        api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}) for _ in range(2)
    ]
    assert all(response.status_code == 202 for response in accepted)
    with session_factory.begin() as session:
        scheduled = session.get(Execution, accepted[1].json()["id"])
        assert scheduled is not None
        scheduled.trigger = "schedule"
    claimed = claim_execution(api_client, worker_id)
    assert claimed.status_code == 200
    deferred = claim_execution(api_client, worker_id)
    assert deferred.status_code == 204
    assert deferred.headers["X-Test-Claim-Decision"] == "DEFER"
    with session_factory() as session:
        attempts = list(
            session.scalars(
                select(ExecutionAttempt).where(
                    ExecutionAttempt.adapter_id == adapter["id"],
                    ExecutionAttempt.status.in_(("claimed", "running")),
                )
            )
        )
        slot = session.get(AdapterExecutionSlot, (adapter["id"], 0))
        assert len(attempts) == 1
        assert slot is not None and slot.active_attempt_id == attempts[0].id
    assert (
        report_attempt(
            api_client,
            worker_id,
            accepted[0].json()["id"],
            {
                "status": "succeeded",
            },
        ).status_code
        == 200
    )
    assert claim_execution(api_client, worker_id).json()["execution_id"] == accepted[1].json()["id"]


def test_permanent_delete_removes_adapter_facts_without_deleting_credentials(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter = create_adapter(api_client, name="permanent-delete")
    version = save_version(api_client, adapter["id"])
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}).json()
    finish_pending(api_client, execution["id"])

    assert api_client.delete(f"/api/adapters/{adapter['id']}").status_code == 204
    assert api_client.get(f"/api/adapters/{adapter['id']}").status_code == 404
    with session_factory() as session:
        assert (
            session.scalar(select(AdapterVersion).where(AdapterVersion.id == version["id"])) is None
        )
        assert session.scalar(select(Execution).where(Execution.id == execution["id"])) is None


def test_clone_gets_own_revision_one_type_worker_and_bindings_without_executions(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    source = create_adapter(api_client, name="clone-source", adapter_type="webhook")
    credential = create_credential(
        api_client,
        name="clone-token",
        type_="token",
        fields={"token": "clone-secret"},
    )
    source_webhook = api_client.get(f"/api/adapters/{source['id']}/webhook").json()
    configured = api_client.put(
        f"/api/adapters/{source['id']}/webhook",
        json={
            "enabled": False,
            "public_id": "clone-upgrade-path",
            "credential_id": credential["id"],
        },
    )
    assert configured.status_code == 200, configured.text
    version = save_version(
        api_client,
        source["id"],
        code="def handle(context, input):\n    return {'source': True}\n",
        requirements="requests==2.32.0",
        runtime_config={"batch": 20},
    )
    assert (
        api_client.put(
            f"/api/adapters/{source['id']}/credential-bindings",
            json={
                "bindings": [
                    {"env_key": "TOKEN", "credential_id": credential["id"], "field": "token"}
                ]
            },
        ).status_code
        == 200
    )
    enabled_source = api_client.put(
        f"/api/adapters/{source['id']}/webhook",
        json={
            "enabled": True,
            "public_id": "clone-upgrade-path",
            "credential_id": credential["id"],
        },
    )
    assert enabled_source.status_code == 200, enabled_source.text

    response = api_client.post(
        f"/api/adapters/{source['id']}/clone",
        json={"name": "clone-target"},
    )
    assert response.status_code == 201, response.text
    clone = response.json()
    assert clone["adapter_type"] == "webhook"
    assert clone["language"] == source["language"]
    assert (
        clone["runtime_worker_id"]
        == api_client.get(f"/api/adapters/{source['id']}").json()["runtime_worker_id"]
    )
    clone_version = api_client.get(
        f"/api/adapters/{clone['id']}/versions/{clone['latest_version_id']}"
    ).json()
    assert clone_version["seq"] == 1
    assert clone_version["code"] == version["code"]
    assert clone_version["requirements"] == version["requirements"]
    assert clone_version["runtime_config"] == version["runtime_config"]
    assert api_client.get(f"/api/adapters/{clone['id']}/executions").json()["items"] == []
    clone_webhook = api_client.get(f"/api/adapters/{clone['id']}/webhook").json()
    assert clone_webhook["public_id"] == "clone-upgrade-path"
    assert clone_webhook["credential_id"] == credential["id"]
    assert clone_webhook["enabled"] is False
    assert source_webhook["enabled"] is False
    assert api_client.get(f"/api/adapters/{source['id']}/webhook").json()["enabled"] is True
    with session_factory() as session:
        bindings = session.scalars(
            select(AdapterCredentialBinding).where(
                AdapterCredentialBinding.adapter_id == clone["id"]
            )
        ).all()
        assert [(row.env_key, row.credential_id, row.field) for row in bindings] == [
            ("TOKEN", credential["id"], "token")
        ]


def test_manual_creation_race_accepts_both_into_durable_queue(
    api_client: TestClient,
) -> None:
    adapter = create_adapter(api_client, name="manual-race")
    save_version(api_client, adapter["id"])
    barrier = threading.Barrier(2)
    statuses: list[tuple[int, str | None]] = []

    def create() -> None:
        barrier.wait(timeout=5)
        response = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
        body = response.json()
        statuses.append((response.status_code, body.get("detail", {}).get("code")))

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(status for status, _ in statuses) == [202, 202]
    page = api_client.get(f"/api/adapters/{adapter['id']}/executions").json()
    assert len(page["items"]) == 2
    assert all(item["status"] == "queued" for item in page["items"])
