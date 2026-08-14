"""M3.2 tests: Secret Store (credential CRUD, bindings, claim-time resolution).

Covers the Issue Secret Store contract: plaintext only travels in create/
update request bodies, the database holds Fernet ciphertext only, no API
response ever returns plaintext, bindings resolve at claim time into the
TaskPayload and the Worker injects/redacts them.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models.platform import Credential
from dlr.control.services import secrets as secrets_service
from dlr.worker import executor
from test_adapters import create_adapter, save_version
from test_executions import create_execution
from test_runtime import make_payload, runtime_settings
from test_workers import claim, register_worker

PLAIN_PASSWORD = "db-plain-42"
PLAIN_TOKEN = "token-plain-77"


def create_credential(
    client: TestClient,
    name: str = "db-password",
    type_: str = "password",
    fields: dict | None = None,
) -> dict:
    payload = {
        "name": name,
        "type": type_,
        "fields": fields or {"username": "svc-user", "password": PLAIN_PASSWORD},
    }
    response = client.post("/api/credentials", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def set_bindings(client: TestClient, adapter_id: int, bindings: list[dict]) -> "object":
    return client.put(
        f"/api/adapters/{adapter_id}/credential-bindings", json={"bindings": bindings}
    )


# --- encryption at rest --------------------------------------------------------


def test_credential_plaintext_never_reaches_database(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    create_credential(api_client)
    session = session_factory()
    try:
        row = session.scalar(select(Credential).where(Credential.name == "db-password"))
        assert row is not None
        assert PLAIN_PASSWORD not in row.ciphertext
        assert "svc-user" not in row.ciphertext
        # The stored ciphertext decrypts back to exactly the submitted fields.
        assert secrets_service.decrypt_fields(row.ciphertext) == {
            "username": "svc-user",
            "password": PLAIN_PASSWORD,
        }
    finally:
        session.close()


def test_credential_api_never_returns_plaintext(api_client: TestClient) -> None:
    created = api_client.post(
        "/api/credentials",
        json={
            "name": "no-leak",
            "type": "password",
            "fields": {"username": "svc-user", "password": PLAIN_PASSWORD},
        },
    )
    assert created.status_code == 201
    credential_id = created.json()["id"]

    for body in (
        created.text,
        api_client.get("/api/credentials").text,
        api_client.get(f"/api/credentials/{credential_id}").text,
        api_client.patch(
            f"/api/credentials/{credential_id}", json={"name": "no-leak-renamed"}
        ).text,
    ):
        assert PLAIN_PASSWORD not in body
        assert "svc-user" not in body
        assert "ciphertext" not in body


def test_credential_api_requires_master_key(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "master_key", None)
    response = api_client.post(
        "/api/credentials",
        json={"name": "blocked", "type": "secret", "fields": {"value": "x"}},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "secret_store_unavailable"
    assert api_client.get("/api/credentials").status_code == 503


# --- validation and conflicts ----------------------------------------------------


def test_credential_fields_must_match_type(api_client: TestClient) -> None:
    # Wrong field set for the type.
    bad = api_client.post(
        "/api/credentials",
        json={"name": "bad-fields", "type": "token", "fields": {"password": "x"}},
    )
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "credential_fields_invalid"
    # Extra field.
    extra = api_client.post(
        "/api/credentials",
        json={"name": "bad-fields", "type": "token", "fields": {"token": "x", "user": "y"}},
    )
    assert extra.status_code == 422
    # Empty value.
    empty = api_client.post(
        "/api/credentials",
        json={"name": "bad-fields", "type": "token", "fields": {"token": ""}},
    )
    assert empty.status_code == 422


def test_credential_name_conflict(api_client: TestClient) -> None:
    create_credential(api_client, name="dup")
    conflict = api_client.post(
        "/api/credentials",
        json={"name": "dup", "type": "token", "fields": {"token": "t"}},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "credential_name_conflict"

    other = create_credential(api_client, name="other", type_="token", fields={"token": "t2"})
    rename = api_client.patch(f"/api/credentials/{other['id']}", json={"name": "dup"})
    assert rename.status_code == 409


def test_credential_update_reencrypts_fields(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    credential = create_credential(api_client, type_="token", fields={"token": "old-value"})
    updated = api_client.patch(
        f"/api/credentials/{credential['id']}", json={"fields": {"token": "new-value"}}
    )
    assert updated.status_code == 200
    session = session_factory()
    try:
        row = session.get(Credential, credential["id"])
        assert row is not None
        assert "old-value" not in row.ciphertext
        assert secrets_service.decrypt_fields(row.ciphertext) == {"token": "new-value"}
    finally:
        session.close()


# --- adapter bindings ---------------------------------------------------------------


def test_bindings_validation(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="bind-validation")
    password_credential = create_credential(api_client)
    token_credential = create_credential(
        api_client, name="api-token", type_="token", fields={"token": PLAIN_TOKEN}
    )

    # env_key must look like an environment variable name.
    bad_key = set_bindings(
        api_client,
        adapter["id"],
        [{"env_key": "1bad-key", "credential_id": password_credential["id"], "field": "password"}],
    )
    assert bad_key.status_code == 422
    assert bad_key.json()["detail"]["code"] == "binding_env_key_invalid"

    # The same env_key twice is rejected.
    duplicated = set_bindings(
        api_client,
        adapter["id"],
        [
            {"env_key": "DB", "credential_id": password_credential["id"], "field": "password"},
            {"env_key": "DB", "credential_id": password_credential["id"], "field": "username"},
        ],
    )
    assert duplicated.status_code == 422

    # Unknown credential.
    unknown = set_bindings(
        api_client, adapter["id"], [{"env_key": "DB", "credential_id": 999999, "field": "token"}]
    )
    assert unknown.status_code == 404

    # The field must exist on the credential's type.
    bad_field = set_bindings(
        api_client,
        adapter["id"],
        [{"env_key": "DB", "credential_id": token_credential["id"], "field": "password"}],
    )
    assert bad_field.status_code == 422
    assert bad_field.json()["detail"]["code"] == "binding_field_invalid"

    # A failed replacement must not touch the previous binding set.
    first = set_bindings(
        api_client,
        adapter["id"],
        [{"env_key": "DB", "credential_id": password_credential["id"], "field": "password"}],
    )
    assert first.status_code == 200
    set_bindings(api_client, adapter["id"], [{"env_key": "1bad", "credential_id": 1, "field": "x"}])
    current = api_client.get(f"/api/adapters/{adapter['id']}/credential-bindings")
    assert len(current.json()) == 1


def test_bindings_full_replacement_and_metadata(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="bind-replace")
    password_credential = create_credential(api_client)
    token_credential = create_credential(
        api_client, name="api-token", type_="token", fields={"token": PLAIN_TOKEN}
    )

    password_binding = {
        "env_key": "DB_PASSWORD",
        "credential_id": password_credential["id"],
        "field": "password",
    }

    set_response = set_bindings(api_client, adapter["id"], [password_binding])
    assert set_response.status_code == 200
    body = set_response.json()
    assert body[0]["env_key"] == "DB_PASSWORD"
    assert body[0]["credential_name"] == "db-password"
    assert body[0]["credential_type"] == "password"
    assert PLAIN_PASSWORD not in set_response.text

    # Replacement swaps the whole set.
    token_binding = {
        "env_key": "API_TOKEN",
        "credential_id": token_credential["id"],
        "field": "token",
    }
    replaced = set_bindings(api_client, adapter["id"], [token_binding])
    assert len(replaced.json()) == 1
    assert replaced.json()[0]["env_key"] == "API_TOKEN"

    # Empty list clears all bindings.
    cleared = set_bindings(api_client, adapter["id"], [])
    assert cleared.json() == []
    assert api_client.get(f"/api/adapters/{adapter['id']}/credential-bindings").json() == []


def test_delete_credential_in_use_is_rejected(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="bind-delete")
    credential = create_credential(api_client)
    set_bindings(
        api_client,
        adapter["id"],
        [{"env_key": "DB", "credential_id": credential["id"], "field": "password"}],
    )

    blocked = api_client.delete(f"/api/credentials/{credential['id']}")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "credential_in_use"

    set_bindings(api_client, adapter["id"], [])
    assert api_client.delete(f"/api/credentials/{credential['id']}").status_code == 204
    assert api_client.get(f"/api/credentials/{credential['id']}").status_code == 404


# --- claim-time resolution -------------------------------------------------------------


def test_claim_payload_only_carries_bound_secrets(api_client: TestClient) -> None:
    bound = create_adapter(api_client, name="secret-bound")
    save_version(api_client, bound["id"])
    unbound = create_adapter(api_client, name="secret-unbound")
    save_version(api_client, unbound["id"])

    password_credential = create_credential(api_client)
    create_credential(api_client, name="unused", type_="token", fields={"token": "unused-value"})
    bound_binding = {
        "env_key": "DB_PASSWORD",
        "credential_id": password_credential["id"],
        "field": "password",
    }
    set_bindings(api_client, bound["id"], [bound_binding])

    worker = register_worker(api_client, name="secret-worker")
    for adapter in (bound, unbound):
        updated = api_client.patch(
            f"/api/adapters/{adapter['id']}", json={"runtime_worker_id": worker["id"]}
        )
        assert updated.status_code == 200, updated.text

    bound_execution = create_execution(api_client, bound["id"])
    response = claim(api_client, worker["id"])
    assert response.status_code == 200
    payload = response.json()
    assert payload["execution_id"] == bound_execution["id"]
    # Exactly the bound field, decrypted; nothing else.
    assert payload["secrets"] == {"DB_PASSWORD": PLAIN_PASSWORD}

    unbound_execution = create_execution(api_client, unbound["id"])
    response = claim(api_client, worker["id"])
    assert response.status_code == 200
    payload = response.json()
    assert payload["execution_id"] == unbound_execution["id"]
    assert payload["secrets"] == {}


# --- worker-side injection and redaction -----------------------------------------------


def test_executor_injects_payload_secrets_and_redacts(tmp_path: object) -> None:
    code = (
        "import os\n"
        "\n\n"
        "def handle(context, input):\n"
        "    via_context = context.secrets.get('API_TOKEN')\n"
        "    via_env = os.environ.get('DLR_SECRET_API_TOKEN')\n"
        "    print('leak ' + str(via_env), flush=True)\n"
        "    return {'length': len(str(via_env)), 'consistent': via_context == via_env}\n"
    )
    payload = make_payload(code=code)
    payload["secrets"] = {"API_TOKEN": "bound-secret-value"}

    result = executor.run(payload, runtime_settings(tmp_path))
    assert result["status"] == "succeeded", result.get("error")
    # The subprocess saw the injected secret (via env and Runtime Contract).
    assert result["output"]["length"] == len("bound-secret-value")
    assert result["output"]["consistent"] is True
    # The plaintext never reaches stored stdout.
    assert "bound-secret-value" not in result["stdout"]
    assert "[REDACTED]" in result["stdout"]


def test_executor_output_json_redacts_payload_secrets(tmp_path: object) -> None:
    code = (
        "import os\n"
        "\n\n"
        "def handle(context, input):\n"
        "    return {'token': os.environ['DLR_SECRET_DB_PASSWORD'], 'plain': 'visible'}\n"
    )
    payload = make_payload(code=code)
    payload["secrets"] = {"DB_PASSWORD": "output-leak-secret"}

    result = executor.run(payload, runtime_settings(tmp_path))
    assert result["status"] == "succeeded", result.get("error")
    assert "output-leak-secret" not in str(result["output"])
    assert result["output"]["token"] == "[REDACTED]"
    assert result["output"]["plain"] == "visible"
