"""M5.4.1 Webhook integration with latest Revision and runtime lock."""

import json
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import Execution, Worker
from test_adapters import create_adapter, save_version
from test_credentials import create_credential
from test_workers import register_worker

WEBHOOK_TOKEN = "webhook-test-token"
_ABSENT = object()


def put_webhook(client: TestClient, adapter_id: int, credential_id: int, enabled: bool = True):
    return client.put(
        f"/api/adapters/{adapter_id}/webhook",
        json={"enabled": enabled, "credential_id": credential_id},
    )


def post_hook(
    client: TestClient,
    public_id: str,
    token: str | None = None,
    json_body: object = _ABSENT,
    content: bytes | None = None,
):
    headers = {"Authorization": f"Bearer {token}" if token is not None else ""}
    data = content if content is not None else json.dumps(json_body).encode()
    return client.post(f"/api/hooks/{public_id}", content=data, headers=headers)


def setup_webhook(
    client: TestClient,
    name: str,
    *,
    enabled: bool = True,
    token: str = WEBHOOK_TOKEN,
) -> tuple[dict, dict, dict, dict, dict]:
    worker = register_worker(client, name=f"{name}-worker")
    adapter = create_adapter(client, name=name, adapter_type="webhook")
    version = save_version(client, adapter["id"])
    credential = create_credential(
        client,
        name=f"{name}-token",
        type_="token",
        fields={"token": token},
    )
    response = put_webhook(client, adapter["id"], credential["id"], enabled=enabled)
    assert response.status_code == 200, response.text
    return adapter, version, worker, credential, response.json()


def executions_of(session_factory: sessionmaker[Session], adapter_id: int) -> list[Execution]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(Execution).where(Execution.adapter_id == adapter_id).order_by(Execution.id)
            ).all()
        )


def finish_active(session_factory: sessionmaker[Session], adapter_id: int) -> None:
    with session_factory.begin() as session:
        session.execute(
            update(Execution)
            .where(
                Execution.adapter_id == adapter_id,
                Execution.status.in_(("pending", "running")),
            )
            .values(status="succeeded")
        )


def test_webhook_is_webhook_type_only_and_get_before_configuration_is_404(
    api_client: TestClient,
) -> None:
    task = create_adapter(api_client, name="webhook-wrong-type", adapter_type="task")
    credential = create_credential(
        api_client,
        name="webhook-wrong-type-token",
        type_="token",
        fields={"token": WEBHOOK_TOKEN},
    )
    mismatch = put_webhook(api_client, task["id"], credential["id"])
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "adapter_type_mismatch"

    webhook = create_adapter(api_client, name="webhook-empty", adapter_type="webhook")
    response = api_client.get(f"/api/adapters/{webhook['id']}/webhook")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "webhook_not_configured"


def test_put_requires_token_credential_and_never_returns_secret(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="webhook-credential", adapter_type="webhook")
    password = create_credential(
        api_client,
        name="webhook-password",
        type_="password",
        fields={"username": "u", "password": WEBHOOK_TOKEN},
    )
    invalid = put_webhook(api_client, adapter["id"], password["id"])
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "webhook_credential_type_invalid"

    _, _, _, _, webhook = setup_webhook(api_client, "webhook-secret-response")
    serialized = json.dumps(webhook)
    assert WEBHOOK_TOKEN not in serialized
    assert "ciphertext" not in serialized


def test_public_id_is_stable_and_distinct(api_client: TestClient) -> None:
    first = setup_webhook(api_client, "webhook-id-a", enabled=False)
    second = setup_webhook(api_client, "webhook-id-b", enabled=False)
    first_id = first[4]["public_id"]
    assert first_id != second[4]["public_id"]
    updated = put_webhook(api_client, first[0]["id"], first[3]["id"], enabled=True)
    assert updated.status_code == 200
    assert updated.json()["public_id"] == first_id


def test_enabled_webhook_locks_token_change_but_can_be_disabled(api_client: TestClient) -> None:
    adapter, _, _, credential, webhook = setup_webhook(api_client, "webhook-lock")
    other = create_credential(
        api_client,
        name="webhook-lock-other",
        type_="token",
        fields={"token": "other"},
    )
    changed = put_webhook(api_client, adapter["id"], other["id"], enabled=True)
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "adapter_runtime_locked"
    assert api_client.get(f"/api/adapters/{adapter['id']}").json()["runtime_locked"] is True

    disabled = put_webhook(api_client, adapter["id"], credential["id"], enabled=False)
    assert disabled.status_code == 200
    assert disabled.json()["public_id"] == webhook["public_id"]
    assert api_client.get(f"/api/adapters/{adapter['id']}").json()["runtime_locked"] is False


def test_success_creates_execution_from_latest_revision_on_runtime_worker(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, version, worker, _, webhook = setup_webhook(api_client, "webhook-success")
    response = post_hook(
        api_client,
        webhook["public_id"],
        WEBHOOK_TOKEN,
        {"event": "created"},
    )
    assert response.status_code == 202, response.text
    execution = executions_of(session_factory, adapter["id"])[0]
    assert execution.id == response.json()["execution_id"]
    assert execution.trigger == "webhook"
    assert execution.version_id == version["id"]
    assert execution.target_worker_id == worker["id"]
    assert execution.input == {"event": "created"}


def test_disabled_save_then_reenabled_webhook_uses_new_latest_revision(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, first, _, credential, webhook = setup_webhook(api_client, "webhook-revision")
    accepted = post_hook(api_client, webhook["public_id"], WEBHOOK_TOKEN, {"revision": 1})
    assert accepted.status_code == 202
    finish_active(session_factory, adapter["id"])
    assert (
        put_webhook(api_client, adapter["id"], credential["id"], enabled=False).status_code == 200
    )
    second = save_version(api_client, adapter["id"], code="# webhook revision 2\n")
    assert put_webhook(api_client, adapter["id"], credential["id"], enabled=True).status_code == 200
    assert (
        post_hook(api_client, webhook["public_id"], WEBHOOK_TOKEN, {"revision": 2}).status_code
        == 202
    )
    assert [row.version_id for row in executions_of(session_factory, adapter["id"])] == [
        first["id"],
        second["id"],
    ]


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_auth_failure_creates_no_execution(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    token: str | None,
) -> None:
    adapter, _, _, _, webhook = setup_webhook(api_client, f"webhook-auth-{token}")
    response = post_hook(api_client, webhook["public_id"], token, {})
    assert response.status_code == 401
    assert executions_of(session_factory, adapter["id"]) == []


def test_unknown_disabled_and_busy_requests_create_no_extra_execution(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    assert post_hook(api_client, "unknown-public-id", WEBHOOK_TOKEN, {}).status_code == 404
    adapter, _, _, credential, webhook = setup_webhook(
        api_client,
        "webhook-disabled",
        enabled=False,
    )
    assert post_hook(api_client, webhook["public_id"], WEBHOOK_TOKEN, {}).status_code == 409
    assert put_webhook(api_client, adapter["id"], credential["id"], enabled=True).status_code == 200
    first = post_hook(api_client, webhook["public_id"], WEBHOOK_TOKEN, {})
    assert first.status_code == 202
    busy = post_hook(api_client, webhook["public_id"], WEBHOOK_TOKEN, {})
    assert busy.status_code == 409
    assert busy.json()["detail"]["code"] == "adapter_busy"
    assert len(executions_of(session_factory, adapter["id"])) == 1


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        (b"{broken", "webhook_body_invalid_json"),
        (b'{"value":NaN}', "webhook_body_invalid_json"),
        (b'{"value":"\\u0000"}', "webhook_body_invalid_json"),
    ],
)
def test_invalid_json_contract_is_rejected_without_execution(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    content: bytes,
    expected_code: str,
) -> None:
    adapter, _, _, _, webhook = setup_webhook(api_client, f"webhook-invalid-{len(content)}")
    response = post_hook(
        api_client,
        webhook["public_id"],
        WEBHOOK_TOKEN,
        content=content,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == expected_code
    assert executions_of(session_factory, adapter["id"]) == []


def test_input_size_cap_precedes_route_and_auth(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "execution_input_max_bytes", 32)
    response = post_hook(api_client, "unknown", content=b"x" * 64)
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "execution_input_too_large"


def test_offline_or_incompatible_runtime_worker_rejects_without_execution(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _, worker, _, webhook = setup_webhook(api_client, "webhook-worker-gate")
    with session_factory.begin() as session:
        session.execute(update(Worker).where(Worker.id == worker["id"]).values(status="offline"))
    offline = post_hook(api_client, webhook["public_id"], WEBHOOK_TOKEN, {})
    assert offline.json()["detail"]["code"] == "worker_offline"
    with session_factory.begin() as session:
        session.execute(
            update(Worker)
            .where(Worker.id == worker["id"])
            .values(status="online", capabilities=["javascript"])
        )
    mismatch = post_hook(api_client, webhook["public_id"], WEBHOOK_TOKEN, {})
    assert mismatch.json()["detail"]["code"] == "worker_capability_missing"
    assert executions_of(session_factory, adapter["id"]) == []


def test_webhook_credential_reference_blocks_deletion(api_client: TestClient) -> None:
    _, _, _, credential, _ = setup_webhook(api_client, "webhook-credential-delete")
    response = api_client.delete(f"/api/credentials/{credential['id']}")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "credential_in_use"


def test_concurrent_requests_create_only_one_active_execution(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _, _, _, webhook = setup_webhook(api_client, "webhook-race")
    barrier = threading.Barrier(2)
    statuses: list[tuple[int, str | None]] = []

    def send() -> None:
        barrier.wait(timeout=5)
        response = post_hook(api_client, webhook["public_id"], WEBHOOK_TOKEN, {})
        body = response.json()
        statuses.append((response.status_code, body.get("detail", {}).get("code")))

    threads = [threading.Thread(target=send) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(status for status, _ in statuses) == [202, 409]
    assert any(code == "adapter_busy" for _, code in statuses)
    assert len(executions_of(session_factory, adapter["id"])) == 1
