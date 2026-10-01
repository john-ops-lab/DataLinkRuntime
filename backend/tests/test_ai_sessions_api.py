"""Opt-in session API ownership and live Adapter edit checks on PostgreSQL."""

import uuid
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.services.accounts import CSRF_COOKIE_NAME, bootstrap_default_admin
from dlr.control.services.ai_sessions import cleanup_expired_sessions


def _account_path(path: str) -> str:
    return f"/__dlr_account{path}"


def _account_write(client: TestClient, method: str, path: str, **kwargs: Any) -> Any:
    csrf_response = client.get(_account_path("/api/auth/account/csrf"))
    assert csrf_response.status_code == 200
    csrf = client.cookies.get(CSRF_COOKIE_NAME)
    assert csrf is not None
    return client.request(
        method,
        _account_path(path),
        headers={"X-CSRF-Token": csrf},
        **kwargs,
    )


def _login(app: Any, username: str, password: str) -> TestClient:
    client = TestClient(app)
    response = _account_write(
        client,
        "POST",
        "/api/auth/account/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    if response.json()["principal"]["must_change_password"]:
        changed = _account_write(
            client,
            "POST",
            "/api/auth/account/change-password",
            json={"current_password": password, "new_password": f"{password}-changed"},
        )
        assert changed.status_code == 200, changed.text
        response = _account_write(
            client,
            "POST",
            "/api/auth/account/login",
            json={"username": username, "password": f"{password}-changed"},
        )
        assert response.status_code == 200, response.text
    return client


def _create_user(api_client: TestClient, name: str) -> dict[str, Any]:
    response = api_client.post(
        "/api/users", json={"username": name, "password": "test-password-123", "role": "user"}
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def _create_adapter(api_client: TestClient, name: str) -> int:
    response = api_client.post(
        "/api/adapters", json={"name": name, "language": "python", "adapter_type": "task"}
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def _grant(api_client: TestClient, adapter_id: int, user_id: int) -> None:
    response = api_client.put(
        f"/api/adapters/{adapter_id}/permissions/{user_id}", json={"permission": "edit"}
    )
    assert response.status_code == 200, response.text


def test_account_and_deployment_spaces_rotation_and_revoke(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with session_factory() as session:
        bootstrap_default_admin(session)
    adapter_id = _create_adapter(api_client, "session-owner-matrix")
    user_a = _create_user(api_client, "session-a")
    user_b = _create_user(api_client, "session-b")
    _grant(api_client, adapter_id, int(user_a["id"]))
    _grant(api_client, adapter_id, int(user_b["id"]))
    account_a = _login(api_client.app, "session-a", "test-password-123")
    account_b = _login(api_client.app, "session-b", "test-password-123")
    path = f"/api/adapters/{adapter_id}/ai/sessions"

    created_a = _account_write(account_a, "POST", path)
    created_b = _account_write(account_b, "POST", path)
    created_token = api_client.post(path)
    assert [item.status_code for item in (created_a, created_b, created_token)] == [201, 201, 201]
    a_id = created_a.json()["id"]
    b_id = created_b.json()["id"]
    token_id = created_token.json()["id"]
    assert [item["id"] for item in account_a.get(_account_path(path)).json()["sessions"]] == [a_id]
    assert [item["id"] for item in account_b.get(_account_path(path)).json()["sessions"]] == [b_id]
    assert [item["id"] for item in api_client.get(path).json()["sessions"]] == [token_id]
    for forbidden_id in (b_id, token_id, str(uuid.uuid4())):
        assert account_a.get(_account_path(f"{path}/{forbidden_id}")).status_code == 404
    assert api_client.get(f"{path}/{a_id}").status_code == 404
    attempt = {
        "session_id": b_id,
        "turn_id": str(uuid.uuid4()),
        "idempotency_key": str(uuid.uuid4()),
        "expected_generation": 0,
        "expected_session_revision": 0,
        "message": "Explain this",
        "working_copy": {
            "code": "def handle(context, input): return input",
            "requirements": "",
            "runtime_config": {},
        },
    }
    assert (
        _account_write(
            account_a, "POST", f"/api/adapters/{adapter_id}/ai/assist", json=attempt
        ).status_code
        == 404
    )

    monkeypatch.setattr(settings, "admin_token", "rotated-session-token")
    assert api_client.get(f"{path}/{token_id}").status_code == 401
    rotated_headers = {"Authorization": "Bearer rotated-session-token"}
    assert api_client.get(f"{path}/{token_id}", headers=rotated_headers).status_code == 200

    with session_factory() as session:
        session.execute(
            text("DELETE FROM adapter_permissions WHERE adapter_id=:adapter AND user_id=:user"),
            {"adapter": adapter_id, "user": int(user_a["id"])},
        )
        session.commit()
    assert account_a.get(_account_path(path)).status_code == 404
    assert account_a.get(_account_path(f"{path}/{a_id}")).status_code == 404
    assert _account_write(account_a, "POST", path).status_code == 404
    assert (
        _account_write(
            account_a,
            "POST",
            f"/api/adapters/{adapter_id}/ai/assist",
            json={
                **attempt,
                "session_id": a_id,
                "turn_id": str(uuid.uuid4()),
                "idempotency_key": str(uuid.uuid4()),
            },
        ).status_code
        == 404
    )
    assert account_b.get(_account_path(f"{path}/{b_id}")).status_code == 200


@pytest.mark.parametrize("summary_valid", [False, True])
def test_session_clear_delete_expiry_and_adapter_binding(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    summary_valid: bool,
) -> None:
    adapter_id = _create_adapter(api_client, "session-clear")
    other_id = _create_adapter(api_client, "session-other")
    path = f"/api/adapters/{adapter_id}/ai/sessions"
    created = api_client.post(path)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    assert api_client.get(f"/api/adapters/{other_id}/ai/sessions/{session_id}").status_code == 404
    with session_factory() as session:
        session.execute(
            text("UPDATE ai_conversations SET revision=5, next_sequence=3 WHERE id=:id"),
            {"id": uuid.UUID(session_id)},
        )
        session.execute(
            text(
                "INSERT INTO ai_conversation_messages "
                "(id, conversation_id, sequence, turn_id, role, content, generation, "
                "request_status, idempotency_key, request_hmac) "
                "VALUES (:id, :conversation, 1, :turn, 'user', 'Visible text', 1, "
                "'completed', :key, :hmac)"
            ),
            {
                "id": uuid.uuid4(),
                "conversation": uuid.UUID(session_id),
                "turn": uuid.uuid4(),
                "key": uuid.uuid4(),
                "hmac": bytes(32),
            },
        )
        session.execute(
            text(
                "UPDATE ai_conversations SET summary_json=CAST(:summary AS jsonb), "
                "summary_valid=:valid, summary_covered_from=1, "
                "summary_covered_through=1, "
                "summary_source_revisions=CAST(:revisions AS jsonb) WHERE id=:id"
            ),
            {
                "id": uuid.UUID(session_id),
                "summary": (
                    '{"version":1,"text":"Earlier request","sources":[],"covered_through":1}'
                ),
                "revisions": '[{"sequence":1,"revision":0}]',
                "valid": summary_valid,
            },
        )
        session.commit()
    detail = api_client.get(f"{path}/{session_id}")
    assert detail.status_code == 200
    assert [(item["role"], item["content"]) for item in detail.json()["messages"]] == [
        ("user", "Visible text")
    ]
    cleared = api_client.post(f"{path}/{session_id}/clear")
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["messages"] == [] and cleared.json()["revision"] == 6
    assert cleared.json()["summary_covered_through"] == 0

    with session_factory() as session:
        session.execute(
            text(
                "UPDATE ai_conversations SET created_at=now()-interval '31 days', "
                "expires_at=now()-interval '1 day' WHERE id=:id"
            ),
            {"id": uuid.UUID(session_id)},
        )
        session.commit()
    assert api_client.get(f"{path}/{session_id}").status_code == 404
    assert api_client.get(path).json()["sessions"] == []
    fresh_id = api_client.post(path).json()["id"]
    assert api_client.delete(f"{path}/{fresh_id}").status_code == 204
    assert api_client.get(f"{path}/{fresh_id}").status_code == 404


def test_expired_session_cleanup_is_bounded_and_cascades_only_its_messages(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter_id = _create_adapter(api_client, "session-retention")
    other_id = _create_adapter(api_client, "session-retention-other")
    path = f"/api/adapters/{adapter_id}/ai/sessions"
    older = uuid.UUID(api_client.post(path).json()["id"])
    newer = uuid.UUID(api_client.post(path).json()["id"])
    current = uuid.UUID(api_client.post(f"/api/adapters/{other_id}/ai/sessions").json()["id"])
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE ai_conversations SET created_at=now()-interval '31 days', "
                "expires_at=now()-interval '2 days' WHERE id=:id"
            ),
            {"id": older},
        )
        session.execute(
            text(
                "UPDATE ai_conversations SET created_at=now()-interval '31 days', "
                "expires_at=now()-interval '1 day' WHERE id=:id"
            ),
            {"id": newer},
        )
        for conversation_id in (older, newer, current):
            session.execute(
                text(
                    "INSERT INTO ai_conversation_messages "
                    "(id, conversation_id, sequence, turn_id, role, content, generation, "
                    "request_status, idempotency_key, request_hmac) "
                    "VALUES (:id, :conversation, 1, :turn, 'user', 'Visible text', 1, "
                    "'completed', :key, :hmac)"
                ),
                {
                    "id": uuid.uuid4(),
                    "conversation": conversation_id,
                    "turn": uuid.uuid4(),
                    "key": uuid.uuid4(),
                    "hmac": bytes(32),
                },
            )
        session.commit()

    with session_factory() as session:
        assert cleanup_expired_sessions(session, batch_size=1) == 1
        ids = set(session.scalars(text("SELECT id FROM ai_conversations")))
        message_owners = set(
            session.scalars(text("SELECT conversation_id FROM ai_conversation_messages"))
        )
        assert ids == {newer, current}
        assert message_owners == {newer, current}
        assert cleanup_expired_sessions(session, batch_size=1) == 1
        assert set(session.scalars(text("SELECT id FROM ai_conversations"))) == {current}
        assert set(
            session.scalars(text("SELECT conversation_id FROM ai_conversation_messages"))
        ) == {current}
