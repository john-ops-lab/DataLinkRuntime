"""M5.4.3 Webhook final-model integration tests."""

import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import Credential, Execution, Worker
from dlr.control.services import secrets as secrets_service
from dlr.control.services import webhook as webhook_service
from dlr.control.services.retention import cleanup_execution_retention
from test_adapters import create_adapter, save_version
from test_credentials import create_credential
from test_workers import register_worker

WEBHOOK_TOKEN = "webhook-test-token"
_ABSENT = object()


def put_webhook(
    client: TestClient,
    adapter_id: int,
    credential_id: int | None,
    enabled: bool = True,
    public_id: str | None = None,
):
    if public_id is None:
        current = client.get(f"/api/adapters/{adapter_id}/webhook")
        assert current.status_code == 200, current.text
        public_id = current.json()["public_id"]
    return client.put(
        f"/api/adapters/{adapter_id}/webhook",
        json={"enabled": enabled, "public_id": public_id, "credential_id": credential_id},
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
    credential = create_credential(
        client,
        name=f"{name}-token",
        type_="token",
        fields={"token": token},
    )
    configured = put_webhook(client, adapter["id"], credential["id"], enabled=False)
    assert configured.status_code == 200, configured.text
    version = save_version(client, adapter["id"])
    response = (
        put_webhook(client, adapter["id"], credential["id"], enabled=True)
        if enabled
        else configured
    )
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


def test_webhook_is_webhook_type_only_and_created_with_random_stopped_path(
    api_client: TestClient,
) -> None:
    task = create_adapter(api_client, name="webhook-wrong-type", adapter_type="task")
    credential = create_credential(
        api_client,
        name="webhook-wrong-type-token",
        type_="token",
        fields={"token": WEBHOOK_TOKEN},
    )
    mismatch = put_webhook(api_client, task["id"], credential["id"], public_id="wrong-type-hook")
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "adapter_type_mismatch"

    webhook = create_adapter(api_client, name="webhook-empty", adapter_type="webhook")
    response = api_client.get(f"/api/adapters/{webhook['id']}/webhook")
    assert response.status_code == 200
    created = response.json()
    assert created["enabled"] is False
    assert created["credential_id"] is None
    assert len(created["public_id"]) == 16
    assert created["public_id"].isalnum() and created["public_id"].islower()


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


def test_first_revision_is_decoupled_from_entry_token_and_start_still_requires_it(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker = register_worker(api_client, name="webhook-first-save-worker")
    adapter = create_adapter(api_client, name="webhook-first-save", adapter_type="webhook")
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}", json={"runtime_worker_id": worker["id"]}
        ).status_code
        == 200
    )

    saved_without_token = api_client.post(
        f"/api/adapters/{adapter['id']}/versions",
        json={"code": "def handle(context, input):\n    return input\n"},
    )
    assert saved_without_token.status_code == 201, saved_without_token.text
    assert executions_of(session_factory, adapter["id"]) == []

    start_without_token = put_webhook(api_client, adapter["id"], None, enabled=True)
    assert start_without_token.status_code == 409
    assert start_without_token.json()["detail"]["code"] == "webhook_token_required"

    credential = create_credential(
        api_client,
        name="webhook-first-save-token",
        type_="token",
        fields={"token": WEBHOOK_TOKEN},
    )
    configured = put_webhook(api_client, adapter["id"], credential["id"], enabled=False)
    assert configured.status_code == 200
    save_version(api_client, adapter["id"])
    assert executions_of(session_factory, adapter["id"]) == []
    assert put_webhook(api_client, adapter["id"], credential["id"], enabled=True).status_code == 200
    assert executions_of(session_factory, adapter["id"]) == []


def test_public_id_is_stable_and_distinct(api_client: TestClient) -> None:
    first = setup_webhook(api_client, "webhook-id-a", enabled=False)
    second = setup_webhook(api_client, "webhook-id-b", enabled=False)
    first_id = first[4]["public_id"]
    assert first_id != second[4]["public_id"]
    updated = put_webhook(api_client, first[0]["id"], first[3]["id"], enabled=True)
    assert updated.status_code == 200
    assert updated.json()["public_id"] == first_id


@pytest.mark.parametrize(
    "public_id",
    ["ABCD", "has_underscore", "-leading", "ab", "contains/slash", "has space"],
)
def test_stopped_path_format_is_validated(api_client: TestClient, public_id: str) -> None:
    adapter = create_adapter(api_client, name=f"invalid-path-{public_id}", adapter_type="webhook")
    response = put_webhook(api_client, adapter["id"], None, enabled=False, public_id=public_id)
    assert response.status_code == 422


def test_stopped_adapters_can_share_path_but_only_one_can_start(api_client: TestClient) -> None:
    first = setup_webhook(api_client, "shared-path-a", enabled=False)
    second = setup_webhook(api_client, "shared-path-b", enabled=False)
    path = "receive-sys1-data"
    assert put_webhook(api_client, first[0]["id"], first[3]["id"], False, path).status_code == 200
    assert put_webhook(api_client, second[0]["id"], second[3]["id"], False, path).status_code == 200
    assert put_webhook(api_client, first[0]["id"], first[3]["id"], True, path).status_code == 200
    conflict = put_webhook(api_client, second[0]["id"], second[3]["id"], True, path)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "webhook_path_in_use"
    assert put_webhook(api_client, first[0]["id"], first[3]["id"], False, path).status_code == 200
    assert put_webhook(api_client, second[0]["id"], second[3]["id"], True, path).status_code == 200


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


def test_enabled_webhook_blocks_token_value_update_without_mutating_credential(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _, _, credential, webhook = setup_webhook(api_client, "webhook-token-lock")
    with session_factory() as session:
        before = session.get(Credential, credential["id"])
        assert before is not None
        before_ciphertext = before.ciphertext

    blocked = api_client.patch(
        f"/api/credentials/{credential['id']}",
        json={"name": "should-not-stick", "fields": {"token": "new-webhook-token"}},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "credential_webhook_runtime_locked"
    with session_factory() as session:
        unchanged = session.get(Credential, credential["id"])
        assert unchanged is not None
        assert unchanged.name == credential["name"]
        assert unchanged.ciphertext == before_ciphertext
        assert secrets_service.decrypt_fields(unchanged.ciphertext) == {"token": WEBHOOK_TOKEN}

    assert (
        put_webhook(api_client, adapter["id"], credential["id"], enabled=False).status_code == 200
    )
    updated = api_client.patch(
        f"/api/credentials/{credential['id']}",
        json={"fields": {"token": "new-webhook-token"}},
    )
    assert updated.status_code == 200
    assert put_webhook(api_client, adapter["id"], credential["id"], enabled=True).status_code == 200
    assert post_hook(api_client, webhook["public_id"], WEBHOOK_TOKEN, {}).status_code == 401
    assert post_hook(api_client, webhook["public_id"], "new-webhook-token", {}).status_code == 202


def test_stop_during_active_call_does_not_cancel_and_unlocks_only_after_terminal(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _, _, credential, webhook = setup_webhook(api_client, "webhook-stop-active")
    accepted = post_hook(api_client, webhook["public_id"], WEBHOOK_TOKEN, {"active": True})
    assert accepted.status_code == 202

    stopped = put_webhook(api_client, adapter["id"], credential["id"], enabled=False)
    assert stopped.status_code == 200
    assert stopped.json()["enabled"] is False
    with session_factory() as session:
        execution = session.get(Execution, accepted.json()["execution_id"])
        assert execution is not None and execution.status == "pending"
    assert api_client.get(f"/api/adapters/{adapter['id']}").json()["runtime_locked"] is True
    assert post_hook(api_client, webhook["public_id"], WEBHOOK_TOKEN, {}).status_code == 404
    other_worker = register_worker(api_client, name="webhook-stop-active-other-worker")
    locked_worker = api_client.patch(
        f"/api/adapters/{adapter['id']}", json={"runtime_worker_id": other_worker["id"]}
    )
    assert locked_worker.status_code == 409
    assert locked_worker.json()["detail"]["code"] == "adapter_runtime_locked"
    locked_token = api_client.patch(
        f"/api/credentials/{credential['id']}",
        json={"fields": {"token": "after-active-token"}},
    )
    assert locked_token.status_code == 409
    assert locked_token.json()["detail"]["code"] == "credential_webhook_runtime_locked"

    finish_active(session_factory, adapter["id"])
    assert api_client.get(f"/api/adapters/{adapter['id']}").json()["runtime_locked"] is False
    assert (
        api_client.patch(
            f"/api/credentials/{credential['id']}",
            json={"fields": {"token": "after-active-token"}},
        ).status_code
        == 200
    )
    editable = put_webhook(
        api_client,
        adapter["id"],
        credential["id"],
        enabled=False,
        public_id="webhook-stop-active-next",
    )
    assert editable.status_code == 200


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


def test_bearer_webhook_ingress_survives_account_entry_without_csrf(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, version, worker, _, webhook = setup_webhook(api_client, "webhook-account-entry")
    response = api_client.post(
        f"/__dlr_account/api/hooks/{webhook['public_id']}",
        content=json.dumps({"entry": "account"}).encode(),
        headers={"Authorization": f"Bearer {WEBHOOK_TOKEN}"},
    )
    assert response.status_code == 202, response.text
    with session_factory() as session:
        execution = session.get(Execution, response.json()["execution_id"])
        assert execution is not None
        assert execution.adapter_id == adapter["id"]
        assert execution.version_id == version["id"]
        assert execution.target_worker_id == worker["id"]


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
    assert post_hook(api_client, webhook["public_id"], WEBHOOK_TOKEN, {}).status_code == 404
    assert put_webhook(api_client, adapter["id"], credential["id"], enabled=True).status_code == 200
    first = post_hook(api_client, webhook["public_id"], WEBHOOK_TOKEN, {})
    assert first.status_code == 202
    busy = post_hook(api_client, webhook["public_id"], WEBHOOK_TOKEN, {})
    assert busy.status_code == 409
    assert busy.json()["detail"]["code"] == "adapter_busy"
    assert len(executions_of(session_factory, adapter["id"])) == 1


def test_webhook_reraises_unrelated_integrity_errors(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _adapter, _, _, _, webhook = setup_webhook(api_client, "webhook-integrity-boundary")

    def fail_flush(*_args: object, **_kwargs: object) -> None:
        raise IntegrityError(
            "INSERT INTO executions ...",
            {},
            SimpleNamespace(
                diag=SimpleNamespace(constraint_name="unrelated_data_integrity_constraint")
            ),
        )

    monkeypatch.setattr(webhook_service.Session, "flush", fail_flush)
    with pytest.raises(IntegrityError):
        post_hook(api_client, webhook["public_id"], WEBHOOK_TOKEN, {})


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


def test_concurrent_start_same_path_has_one_database_winner(
    api_client: TestClient,
) -> None:
    first = setup_webhook(api_client, "start-race-a", enabled=False)
    second = setup_webhook(api_client, "start-race-b", enabled=False)
    path = "concurrent-receiver"
    for item in (first, second):
        assert put_webhook(api_client, item[0]["id"], item[3]["id"], False, path).status_code == 200
    barrier = threading.Barrier(2)
    statuses: list[tuple[int, str | None]] = []

    def start(item: tuple[dict, dict, dict, dict, dict]) -> None:
        barrier.wait(timeout=5)
        response = put_webhook(api_client, item[0]["id"], item[3]["id"], True, path)
        statuses.append((response.status_code, response.json().get("detail", {}).get("code")))

    threads = [threading.Thread(target=start, args=(item,)) for item in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(status for status, _ in statuses) == [200, 409]
    assert any(code == "webhook_path_in_use" for _, code in statuses)


def test_concurrent_start_and_token_update_serialize_to_one_consistent_value(
    api_client: TestClient,
) -> None:
    adapter, _, _, credential, webhook = setup_webhook(
        api_client, "webhook-token-race", enabled=False
    )
    new_token = "concurrent-new-token"
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, int, str | None]] = []

    def start() -> None:
        barrier.wait(timeout=5)
        response = put_webhook(api_client, adapter["id"], credential["id"], enabled=True)
        outcomes.append(
            ("start", response.status_code, response.json().get("detail", {}).get("code"))
        )

    def update_token() -> None:
        barrier.wait(timeout=5)
        response = api_client.patch(
            f"/api/credentials/{credential['id']}", json={"fields": {"token": new_token}}
        )
        outcomes.append(
            ("update", response.status_code, response.json().get("detail", {}).get("code"))
        )

    threads = [threading.Thread(target=start), threading.Thread(target=update_token)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    start_outcome = next(outcome for outcome in outcomes if outcome[0] == "start")
    update_outcome = next(outcome for outcome in outcomes if outcome[0] == "update")
    assert start_outcome[1:] == (200, None)
    assert update_outcome[1] in (200, 409)
    if update_outcome[1] == 409:
        assert update_outcome[2] == "credential_webhook_runtime_locked"
        accepted_token, rejected_token = WEBHOOK_TOKEN, new_token
    else:
        accepted_token, rejected_token = new_token, WEBHOOK_TOKEN

    assert post_hook(api_client, webhook["public_id"], rejected_token, {}).status_code == 401
    assert post_hook(api_client, webhook["public_id"], accepted_token, {}).status_code == 202


def test_retention_keeps_latest_configured_webhook_calls_only(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, _, _, _, webhook = setup_webhook(api_client, "webhook-retention")
    for sequence in range(101):
        response = post_hook(
            api_client, webhook["public_id"], WEBHOOK_TOKEN, {"sequence": sequence}
        )
        assert response.status_code == 202, response.text
        finish_active(session_factory, adapter["id"])
    with session_factory() as session:
        report = cleanup_execution_retention(session)
    assert report.deleted == 1
    rows = executions_of(session_factory, adapter["id"])
    assert len(rows) == 100
    assert [row.input["sequence"] for row in rows] == list(range(1, 101))


def test_retention_never_deletes_task_or_active_execution(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, version, worker, _, webhook = setup_webhook(
        api_client, "webhook-retention-scope", enabled=False
    )
    with session_factory.begin() as session:
        session.add(
            Execution(
                adapter_id=adapter["id"],
                version_id=version["id"],
                trigger="manual",
                status="succeeded",
                target_worker_id=worker["id"],
                input={"manual": True},
            )
        )
        for sequence in range(100):
            session.add(
                Execution(
                    adapter_id=adapter["id"],
                    version_id=version["id"],
                    trigger="webhook",
                    status="succeeded",
                    target_worker_id=worker["id"],
                    input={"sequence": sequence},
                )
            )
    assert put_webhook(api_client, adapter["id"], webhook["credential_id"], True).status_code == 200
    accepted = post_hook(api_client, webhook["public_id"], WEBHOOK_TOKEN, {"active": True})
    assert accepted.status_code == 202
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(Execution)
                .where(Execution.adapter_id == adapter["id"], Execution.trigger == "webhook")
            )
            == 101
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(Execution)
                .where(Execution.adapter_id == adapter["id"], Execution.trigger == "manual")
            )
            == 1
        )
        active = session.get(Execution, accepted.json()["execution_id"])
        assert active is not None and active.status == "pending"


def test_webhook_history_filter_excludes_legacy_manual_runs_and_keeps_cursor_paging(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter, version, worker, _, _ = setup_webhook(
        api_client, "webhook-filtered-history", enabled=False
    )
    webhook_ids: list[int] = []
    all_ids: list[int] = []
    with session_factory.begin() as session:
        for trigger in ("manual", "webhook", "manual", "webhook", "webhook"):
            execution = Execution(
                adapter_id=adapter["id"],
                version_id=version["id"],
                trigger=trigger,
                status="succeeded",
                target_worker_id=worker["id"],
                input={},
            )
            session.add(execution)
            session.flush()
            all_ids.append(execution.id)
            if trigger == "webhook":
                webhook_ids.append(execution.id)

    first = api_client.get(
        f"/api/adapters/{adapter['id']}/executions",
        params={"trigger": "webhook", "limit": 2},
    )
    assert first.status_code == 200
    first_page = first.json()
    assert [item["id"] for item in first_page["items"]] == list(reversed(webhook_ids[-2:]))
    assert all(item["trigger"] == "webhook" for item in first_page["items"])
    assert first_page["next_before_id"] == webhook_ids[-2]

    second = api_client.get(
        f"/api/adapters/{adapter['id']}/executions",
        params={
            "trigger": "webhook",
            "limit": 2,
            "before_id": first_page["next_before_id"],
        },
    )
    assert second.status_code == 200
    second_page = second.json()
    assert [item["id"] for item in second_page["items"]] == [webhook_ids[0]]
    assert second_page["next_before_id"] is None

    unfiltered = api_client.get(f"/api/adapters/{adapter['id']}/executions").json()
    assert [item["id"] for item in unfiltered["items"]] == list(reversed(all_ids))


def test_history_rejects_unknown_trigger_filter(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="webhook-history-filter", adapter_type="webhook")
    response = api_client.get(
        f"/api/adapters/{adapter['id']}/executions",
        params={"trigger": "production"},
    )
    assert response.status_code == 422
