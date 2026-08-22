"""Disposable-fixture coverage for M5.11 Wave C lifecycle contracts."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.control.models import (
    Adapter,
    AdapterPermission,
    AdapterSchedule,
    AdapterVersion,
    AdapterWebhook,
    Execution,
    User,
    WorkerCleanupRequest,
)
from dlr.control.models.platform import AdapterCredentialBinding, Credential
from test_adapters import create_adapter, save_version
from test_credentials import create_credential
from test_workers import register_worker

WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}


def test_permanent_delete_cleans_adapter_facts_but_keeps_credential(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker = register_worker(api_client, name="wave-c-delete-worker")
    adapter = create_adapter(api_client, name="wave-c-delete")
    version = save_version(api_client, adapter["id"])
    credential = create_credential(
        api_client,
        name="wave-c-delete-secret",
        type_="secret",
        fields={"value": "disposable-secret"},
    )
    binding = api_client.put(
        f"/api/adapters/{adapter['id']}/credential-bindings",
        json={
            "bindings": [
                {"env_key": "EXAMPLE", "credential_id": credential["id"], "field": "value"}
            ]
        },
    )
    assert binding.status_code == 200, binding.text
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text
    assert api_client.post(f"/api/executions/{execution.json()['id']}/cancel").status_code == 200

    with session_factory.begin() as session:
        user = User(
            username="wave-c-shared-user",
            password_hash="disposable-hash",
            role="user",
        )
        session.add(user)
        session.flush()
        session.add(AdapterPermission(adapter_id=adapter["id"], user_id=user.id, permission="read"))
        session.add(
            AdapterSchedule(
                adapter_id=adapter["id"],
                cron="0 * * * *",
                timezone="UTC",
                input={"source": "disposable"},
                enabled=False,
            )
        )

    response = api_client.delete(f"/api/adapters/{adapter['id']}")
    assert response.status_code == 204, response.text
    assert api_client.get(f"/api/adapters/{adapter['id']}").status_code == 404

    with session_factory() as session:
        assert session.get(Adapter, adapter["id"]) is None
        assert session.get(AdapterVersion, version["id"]) is None
        assert (
            session.scalar(select(Execution).where(Execution.adapter_id == adapter["id"])) is None
        )
        assert (
            session.scalar(
                select(AdapterCredentialBinding).where(
                    AdapterCredentialBinding.adapter_id == adapter["id"]
                )
            )
            is None
        )
        assert (
            session.scalar(
                select(AdapterPermission).where(AdapterPermission.adapter_id == adapter["id"])
            )
            is None
        )
        assert (
            session.scalar(
                select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
            )
            is None
        )
        assert (
            session.scalar(select(AdapterWebhook).where(AdapterWebhook.adapter_id == adapter["id"]))
            is None
        )
        assert session.get(Credential, credential["id"]) is not None
        cleanup = session.scalar(
            select(WorkerCleanupRequest).where(
                WorkerCleanupRequest.adapter_id == adapter["id"],
                WorkerCleanupRequest.worker_id == worker["id"],
            )
        )
        assert cleanup is not None and cleanup.status == "pending"


def test_stop_and_delete_cancels_pending_and_then_removes_it(api_client: TestClient) -> None:
    worker = register_worker(api_client, name="wave-c-pending-worker")
    adapter = create_adapter(api_client, name="wave-c-pending")
    save_version(api_client, adapter["id"])
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}).json()

    blocked = api_client.delete(f"/api/adapters/{adapter['id']}")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "adapter_runtime_locked"

    deleted = api_client.delete(f"/api/adapters/{adapter['id']}?stop=true")
    assert deleted.status_code == 204, deleted.text
    assert api_client.get(f"/api/executions/{execution['id']}").status_code == 404
    assert worker["id"] > 0


def test_stop_and_delete_waits_for_running_worker_cancellation(api_client: TestClient) -> None:
    worker = register_worker(api_client, name="wave-c-running-worker")
    adapter = create_adapter(api_client, name="wave-c-running")
    save_version(api_client, adapter["id"])
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}).json()
    claimed = api_client.post(
        f"/api/workers/{worker['id']}/tasks/claim",
        params={"wait_seconds": 0},
        headers=WORKER_HEADERS,
    )
    assert claimed.status_code == 200

    waiting = api_client.delete(f"/api/adapters/{adapter['id']}?stop=true")
    assert waiting.status_code == 202
    assert waiting.json()["detail"]["code"] == "adapter_delete_waiting_for_worker"
    assert waiting.json()["detail"]["params"]["active_execution_id"] == execution["id"]

    cancelled = api_client.post(
        f"/api/workers/{worker['id']}/executions/{execution['id']}/result",
        json={"status": "cancelled"},
        headers=WORKER_HEADERS,
    )
    assert cancelled.status_code == 200, cancelled.text
    deleted = api_client.delete(f"/api/adapters/{adapter['id']}?stop=true")
    assert deleted.status_code == 204, deleted.text
    assert api_client.get(f"/api/adapters/{adapter['id']}").status_code == 404


def test_stop_and_delete_blocks_running_execution_when_worker_is_offline(
    api_client: TestClient,
) -> None:
    worker = register_worker(api_client, name="wave-c-running-offline-worker")
    adapter = create_adapter(api_client, name="wave-c-running-offline")
    save_version(api_client, adapter["id"])
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}).json()
    claimed = api_client.post(
        f"/api/workers/{worker['id']}/tasks/claim",
        params={"wait_seconds": 0},
        headers=WORKER_HEADERS,
    )
    assert claimed.status_code == 200
    offline = api_client.post(f"/api/workers/{worker['id']}/offline", headers=WORKER_HEADERS)
    assert offline.status_code == 204

    blocked = api_client.delete(f"/api/adapters/{adapter['id']}?stop=true")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "worker_offline"
    assert api_client.get(f"/api/adapters/{adapter['id']}").status_code == 200
    assert api_client.get(f"/api/executions/{execution['id']}").status_code == 200


def test_permanent_delete_removes_webhook_config_but_keeps_entry_credential(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter = create_adapter(api_client, name="wave-c-webhook-delete", adapter_type="webhook")
    save_version(api_client, adapter["id"])
    credential = create_credential(
        api_client,
        name="wave-c-webhook-delete-token",
        type_="token",
        fields={"token": "EXAMPLE_WEBHOOK_TOKEN"},
    )
    configured = api_client.put(
        f"/api/adapters/{adapter['id']}/webhook",
        json={
            "enabled": False,
            "public_id": "wave-c-delete-hook",
            "credential_id": credential["id"],
        },
    )
    assert configured.status_code == 200, configured.text

    deleted = api_client.delete(f"/api/adapters/{adapter['id']}")
    assert deleted.status_code == 204, deleted.text
    with session_factory() as session:
        assert session.get(Adapter, adapter["id"]) is None
        assert (
            session.scalar(select(AdapterWebhook).where(AdapterWebhook.adapter_id == adapter["id"]))
            is None
        )
        assert session.get(Credential, credential["id"]) is not None


def test_permanent_delete_blocks_safely_when_worker_is_offline(api_client: TestClient) -> None:
    worker = register_worker(api_client, name="wave-c-offline-worker")
    adapter = create_adapter(api_client, name="wave-c-offline")
    save_version(api_client, adapter["id"])
    offline = api_client.post(f"/api/workers/{worker['id']}/offline", headers=WORKER_HEADERS)
    assert offline.status_code == 204

    blocked = api_client.delete(f"/api/adapters/{adapter['id']}?stop=true")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "worker_offline"
    assert api_client.get(f"/api/adapters/{adapter['id']}").status_code == 200


def test_permanent_delete_cleans_every_worker_that_ran_the_adapter(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    first_worker = register_worker(api_client, name="wave-c-history-worker-1")
    second_worker = register_worker(api_client, name="wave-c-history-worker-2")
    adapter = create_adapter(api_client, name="wave-c-history-cleanup")
    save_version(api_client, adapter["id"])
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}).json()
    claimed = api_client.post(
        f"/api/workers/{first_worker['id']}/tasks/claim",
        params={"wait_seconds": 0},
        headers=WORKER_HEADERS,
    )
    assert claimed.status_code == 200
    finished = api_client.post(
        f"/api/workers/{first_worker['id']}/executions/{execution['id']}/result",
        json={"status": "succeeded", "output": {}},
        headers=WORKER_HEADERS,
    )
    assert finished.status_code == 200, finished.text
    switched = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"runtime_worker_id": second_worker["id"]},
    )
    assert switched.status_code == 200, switched.text

    assert (
        api_client.post(
            f"/api/workers/{first_worker['id']}/offline", headers=WORKER_HEADERS
        ).status_code
        == 204
    )
    blocked = api_client.delete(f"/api/adapters/{adapter['id']}?stop=true")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "worker_offline"

    restored = register_worker(api_client, name="wave-c-history-worker-1")
    assert restored["id"] == first_worker["id"]
    deleted = api_client.delete(f"/api/adapters/{adapter['id']}?stop=true")
    assert deleted.status_code == 204, deleted.text
    with session_factory() as session:
        cleanup_workers = set(
            session.scalars(
                select(WorkerCleanupRequest.worker_id).where(
                    WorkerCleanupRequest.adapter_id == adapter["id"]
                )
            ).all()
        )
    assert cleanup_workers == {first_worker["id"], second_worker["id"]}
