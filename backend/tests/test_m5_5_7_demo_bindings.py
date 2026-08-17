"""M5.5.7 tests: demo Credential bootstrap, default Adapter bindings and the
access_key field standardization (access_key_id / access_key_secret).

Covers the Issue contract: demo Credentials carry fresh random values (never
fixed, never readable), new Task/Webhook Adapters default-bind
PASSWORD/TOKEN to them, and legacy access_key ciphertext/bindings keep
working after the field rename without exposing plaintext.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models.platform import Credential
from dlr.control.services import secrets as secrets_service
from test_adapters import create_adapter

DEMO_PASSWORD_VALUE = "demo-password-plain-53"
DEMO_TOKEN_VALUE = "demo-token-plain-53"


def list_bindings(client: TestClient, adapter_id: int) -> list[dict]:
    response = client.get(f"/api/adapters/{adapter_id}/credential-bindings")
    assert response.status_code == 200, response.text
    return response.json()


# --- demo bootstrap ----------------------------------------------------------------


def test_bootstrap_demo_credentials_creates_random_value_credentials(
    session_factory: sessionmaker[Session],
) -> None:
    session = session_factory()
    try:
        secrets_service.bootstrap_demo_credentials(session)
        secrets_service.bootstrap_demo_credentials(session)  # idempotent
        rows = list(session.scalars(select(Credential).order_by(Credential.name)).all())
        assert [row.name for row in rows] == ["demo-passwd", "demo-token"]
        by_name = {row.name: row for row in rows}
        assert by_name["demo-passwd"].type == "password"
        assert by_name["demo-token"].type == "token"
        passwd_fields = secrets_service.decrypt_fields(by_name["demo-passwd"].ciphertext)
        token_fields = secrets_service.decrypt_fields(by_name["demo-token"].ciphertext)
        assert set(passwd_fields) == {"username", "password"}
        assert list(token_fields) == ["token"]
        # Values are fresh random hex; never fixed demo values.
        assert len(passwd_fields["password"]) == 32
        assert len(token_fields["token"]) == 32
        assert passwd_fields["password"] != token_fields["token"]
    finally:
        session.close()


def test_bootstrap_demo_credentials_skipped_without_master_key(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "master_key", None)
    session = session_factory()
    try:
        before = session.scalar(select(func.count(Credential.id)))
        secrets_service.bootstrap_demo_credentials(session)
        after = session.scalar(select(func.count(Credential.id)))
        assert after == before
    finally:
        session.close()


def test_demo_credentials_metadata_only_and_bindable(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    session = session_factory()
    try:
        secrets_service.bootstrap_demo_credentials(session)
    finally:
        session.close()
    listing = api_client.get("/api/credentials")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    names = {credential["name"]: credential for credential in body}
    assert set(names) == {"demo-passwd", "demo-token"}
    # Metadata only: no plaintext or ciphertext ever leaves the API.
    assert DEMO_PASSWORD_VALUE not in listing.text
    assert "ciphertext" not in listing.text
    assert "password" not in names["demo-passwd"]
    assert "token" not in names["demo-token"]


# --- default demo bindings ---------------------------------------------------------


def test_new_task_default_binds_demo_passwd_password(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    session = session_factory()
    try:
        secrets_service.bootstrap_demo_credentials(session)
    finally:
        session.close()
    adapter = create_adapter(api_client, name="demo-task")
    bindings = list_bindings(api_client, adapter["id"])
    assert len(bindings) == 1
    assert bindings[0]["env_key"] == "PASSWORD"
    assert bindings[0]["field"] == "password"
    assert bindings[0]["credential_name"] == "demo-passwd"
    assert bindings[0]["credential_type"] == "password"


def test_new_webhook_default_binds_demo_token_token(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    session = session_factory()
    try:
        secrets_service.bootstrap_demo_credentials(session)
    finally:
        session.close()
    adapter = create_adapter(api_client, name="demo-webhook", adapter_type="webhook")
    bindings = list_bindings(api_client, adapter["id"])
    assert len(bindings) == 1
    assert bindings[0]["env_key"] == "TOKEN"
    assert bindings[0]["field"] == "token"
    assert bindings[0]["credential_name"] == "demo-token"
    assert bindings[0]["credential_type"] == "token"
    # M5.4 Webhook lifecycle unchanged: the receiving Token Credential is
    # still explicitly chosen by the user and never preset to the demo row.
    webhook = api_client.get(f"/api/adapters/{adapter['id']}/webhook")
    assert webhook.status_code == 200
    assert webhook.json()["credential_id"] is None


def test_new_adapter_without_demo_credentials_has_no_default_bindings(
    api_client: TestClient,
) -> None:
    adapter = create_adapter(api_client, name="no-demo")
    assert list_bindings(api_client, adapter["id"]) == []


def test_default_binding_resolves_at_claim_time(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    session = session_factory()
    try:
        secrets_service.bootstrap_demo_credentials(session)
    finally:
        session.close()
    adapter = create_adapter(api_client, name="demo-claim")
    bindings = list_bindings(api_client, adapter["id"])
    check_session = session_factory()
    try:
        resolved = secrets_service.resolve_adapter_secrets(check_session, adapter["id"])
    finally:
        check_session.close()
    assert set(resolved) == {"PASSWORD"}
    assert len(resolved["PASSWORD"]) == 32
    assert bindings[0]["credential_name"] == "demo-passwd"


# --- access_key field standardization ----------------------------------------------


def test_access_key_credential_uses_standardized_fields(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    created = api_client.post(
        "/api/credentials",
        json={
            "name": "cloud-ak",
            "type": "access_key",
            "fields": {"access_key_id": "AKID", "access_key_secret": "SK-123"},
        },
    )
    assert created.status_code == 201, created.text
    assert "AKID" not in created.text
    assert "SK-123" not in created.text
    session = session_factory()
    try:
        row = session.get(Credential, created.json()["id"])
        assert row is not None
        assert secrets_service.decrypt_fields(row.ciphertext) == {
            "access_key_id": "AKID",
            "access_key_secret": "SK-123",
        }
    finally:
        session.close()

    adapter = create_adapter(api_client, name="ak-adapter")
    binding = api_client.put(
        f"/api/adapters/{adapter['id']}/credential-bindings",
        json={
            "bindings": [
                {
                    "env_key": "ACCESS_KEY_ID",
                    "credential_id": created.json()["id"],
                    "field": "access_key_id",
                }
            ]
        },
    )
    assert binding.status_code == 200, binding.text
    assert api_client.get("/api/credentials").status_code == 200


def test_legacy_access_key_ciphertext_reads_back_under_new_names(
    session_factory: sessionmaker[Session],
) -> None:
    session = session_factory()
    try:
        legacy = Credential(
            name="legacy-ak",
            type="access_key",
            ciphertext=secrets_service.encrypt_fields(
                {"access_key": "LEGACY-ID", "secret_key": "LEGACY-SK"}
            ),
        )
        session.add(legacy)
        session.commit()
        row = session.get(Credential, legacy.id)
        assert row is not None
        assert secrets_service.decrypt_fields(row.ciphertext) == {
            "access_key_id": "LEGACY-ID",
            "access_key_secret": "LEGACY-SK",
        }
    finally:
        session.close()


def test_legacy_field_names_rejected_on_new_bindings(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    session = session_factory()
    try:
        session.add(
            Credential(
                name="new-ak",
                type="access_key",
                ciphertext=secrets_service.encrypt_fields(
                    {"access_key_id": "AKID", "access_key_secret": "SK"}
                ),
            )
        )
        session.commit()
        credential_id = session.scalar(select(Credential.id).where(Credential.name == "new-ak"))
    finally:
        session.close()
    adapter = create_adapter(api_client, name="legacy-field-reject")
    assert credential_id is not None
    for legacy_field in ("access_key", "secret_key"):
        response = api_client.put(
            f"/api/adapters/{adapter['id']}/credential-bindings",
            json={
                "bindings": [
                    {"env_key": "AK", "credential_id": credential_id, "field": legacy_field}
                ]
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "binding_field_invalid"
    assert list_bindings(api_client, adapter["id"]) == []
