"""M5.8-006 productized KnowledgeSource configuration and API contracts."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.ai import ima as ima_adapter
from dlr.control.ai.knowledge import (
    KS_AUTH_FAILED,
    KnowledgeBaseSummary,
    KnowledgeSourceError,
)
from dlr.control.models import KnowledgeSourceSetting

DB_CLIENT_ID = "db-client-id-test-sentinel"
DB_API_KEY = "db-api-key-test-sentinel"
ENV_CLIENT_ID = "env-client-id-test-sentinel"
ENV_API_KEY = "env-api-key-test-sentinel"


def create_credential(
    client: TestClient,
    name: str,
    credential_type: str,
    fields: dict[str, str],
) -> dict[str, Any]:
    response = client.post(
        "/api/credentials",
        json={"name": name, "type": credential_type, "fields": fields},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_knowledge_source_config_persists_reference_without_secret(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "dlr_ima_endpoint", "https://ima.qq.com")
    monkeypatch.setattr(settings, "dlr_ima_credential_name", None)

    before = api_client.get("/api/knowledge-sources/ima")
    assert before.status_code == 200, before.text
    assert before.json()["config_source"] == "environment"
    assert before.json()["endpoint"] == "https://ima.qq.com"
    assert before.json()["status"] == "unconfigured"

    credential = create_credential(
        api_client,
        "ima-product-credential",
        "access_key",
        {"access_key_id": DB_CLIENT_ID, "access_key_secret": DB_API_KEY},
    )
    saved = api_client.put(
        "/api/knowledge-sources/ima",
        json={"enabled": True, "credential_id": credential["id"]},
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["source_id"] == "ima"
    assert body["kind"] == "ima"
    assert body["name"] == "Tencent ima"
    assert body["endpoint"] == "https://ima.qq.com"
    assert body["config_source"] == "database"
    assert body["status"] == "configured"
    assert body["credential_id"] == credential["id"]
    assert body["credential_name"] == "ima-product-credential"
    assert DB_CLIENT_ID not in json.dumps(body)
    assert DB_API_KEY not in json.dumps(body)

    with session_factory() as session:
        row = session.get(KnowledgeSourceSetting, 1)
        assert row is not None
        assert row.enabled is True
        assert row.credential_id == credential["id"]
        assert session.scalar(select(KnowledgeSourceSetting.id)) == 1

    disabled = api_client.put(
        "/api/knowledge-sources/ima", json={"enabled": False, "credential_id": None}
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"
    assert disabled.json()["credential_id"] is None


def test_knowledge_source_requires_access_key_reference_without_reflecting_secret(
    api_client: TestClient,
) -> None:
    password_secret = "password-secret-test-sentinel"
    password = create_credential(
        api_client,
        "ima-wrong-kind",
        "password",
        {"username": "ima", "password": password_secret},
    )
    wrong_kind = api_client.put(
        "/api/knowledge-sources/ima",
        json={"enabled": True, "credential_id": password["id"]},
    )
    assert wrong_kind.status_code == 422
    assert wrong_kind.json()["detail"]["code"] == "knowledge_source_credential_invalid"
    assert password_secret not in wrong_kind.text

    missing = api_client.put(
        "/api/knowledge-sources/ima", json={"enabled": True, "credential_id": 999999}
    )
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "knowledge_source_credential_invalid"

    endpoint_override = api_client.put(
        "/api/knowledge-sources/ima",
        json={
            "enabled": True,
            "credential_id": None,
            "endpoint": "https://arbitrary.example.test",
        },
    )
    assert endpoint_override.status_code == 422
    assert endpoint_override.json()["detail"]["code"] == "ks_config_invalid"
    assert "arbitrary.example.test" not in endpoint_override.text

    rejected_secret = "rejected-secret-test-sentinel"
    secret_override = api_client.put(
        "/api/knowledge-sources/ima",
        json={
            "enabled": True,
            "credential_id": None,
            "access_key_secret": rejected_secret,
        },
    )
    assert secret_override.status_code == 422
    assert secret_override.json()["detail"]["code"] == "ks_config_invalid"
    assert rejected_secret not in secret_override.text


def test_db_config_wins_over_environment_fallback(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_credential = create_credential(
        api_client,
        "ima-env-credential",
        "access_key",
        {"access_key_id": ENV_CLIENT_ID, "access_key_secret": ENV_API_KEY},
    )
    db_credential = create_credential(
        api_client,
        "ima-db-credential",
        "access_key",
        {"access_key_id": DB_CLIENT_ID, "access_key_secret": DB_API_KEY},
    )
    monkeypatch.setattr(settings, "dlr_ima_endpoint", "https://ima.qq.com")
    monkeypatch.setattr(settings, "dlr_ima_credential_name", "ima-env-credential")

    with session_factory() as session:
        fallback_source = ima_adapter.build_source(session)
        assert fallback_source.auth == {"client_id": ENV_CLIENT_ID, "api_key": ENV_API_KEY}

    saved = api_client.put(
        "/api/knowledge-sources/ima",
        json={"enabled": True, "credential_id": db_credential["id"]},
    )
    assert saved.status_code == 200, saved.text

    with session_factory() as session:
        db_source = ima_adapter.build_source(session)
        assert db_source.auth == {"client_id": DB_CLIENT_ID, "api_key": DB_API_KEY}
        assert db_source.auth != {
            "client_id": ENV_CLIENT_ID,
            "api_key": ENV_API_KEY,
        }

    assert env_credential["id"] != db_credential["id"]


def test_knowledge_source_test_and_list_return_only_safe_metadata(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = create_credential(
        api_client,
        "ima-list-credential",
        "access_key",
        {"access_key_id": DB_CLIENT_ID, "access_key_secret": DB_API_KEY},
    )
    saved = api_client.put(
        "/api/knowledge-sources/ima",
        json={"enabled": True, "credential_id": credential["id"]},
    )
    assert saved.status_code == 200, saved.text

    def fake_list(_source: object) -> list[KnowledgeBaseSummary]:
        return [
            KnowledgeBaseSummary(
                id="kb-1",
                name="DLR product docs",
                description="fixture",
                item_count=0,
                source="ima:v1:kb-1",
            )
        ]

    monkeypatch.setattr(ima_adapter.TencentImaKnowledgeSource, "list_knowledge_bases", fake_list)

    tested = api_client.post("/api/knowledge-sources/ima/test")
    assert tested.status_code == 200, tested.text
    body = tested.json()
    assert body["ok"] is True
    assert body["status"] == "connected"
    assert body["error_code"] is None
    assert body["knowledge_bases"] == [
        {"id": "kb-1", "name": "DLR product docs", "status": "accessible"}
    ]
    assert DB_CLIENT_ID not in tested.text
    assert DB_API_KEY not in tested.text

    listed = api_client.get("/api/knowledge-sources/ima/knowledge-bases")
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["name"] == "DLR product docs"


def test_knowledge_source_test_returns_stable_error_without_secret(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = create_credential(
        api_client,
        "ima-error-credential",
        "access_key",
        {"access_key_id": DB_CLIENT_ID, "access_key_secret": DB_API_KEY},
    )
    saved = api_client.put(
        "/api/knowledge-sources/ima",
        json={"enabled": True, "credential_id": credential["id"]},
    )
    assert saved.status_code == 200, saved.text

    def failing_list(_source: object) -> list[KnowledgeBaseSummary]:
        raise KnowledgeSourceError(KS_AUTH_FAILED, "upstream message must not escape")

    monkeypatch.setattr(
        ima_adapter.TencentImaKnowledgeSource,
        "list_knowledge_bases",
        failing_list,
    )

    tested = api_client.post("/api/knowledge-sources/ima/validate")
    assert tested.status_code == 200, tested.text
    body = tested.json()
    assert body["ok"] is False
    assert body["status"] == "error"
    assert body["error_code"] == KS_AUTH_FAILED
    assert "upstream message" not in tested.text
    assert DB_CLIENT_ID not in tested.text
    assert DB_API_KEY not in tested.text
