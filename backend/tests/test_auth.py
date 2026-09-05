"""Tests for M2 static token authentication (admin / worker)."""

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN_TOKEN, WORKER_TOKEN
from dlr.common.config import settings
from runtime_api_support import ISOLATION_PASS


def test_protected_api_rejects_missing_token(api_client: TestClient) -> None:
    response = api_client.get("/api/adapters", headers={"Authorization": ""})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "unauthorized"


def test_protected_api_rejects_absent_authorization_header(api_client: TestClient) -> None:
    # A request without any Authorization header at all must still reach the
    # token check and answer 401, not FastAPI's 422 validation error.
    bare_client = TestClient(api_client.app)
    response = bare_client.get("/api/adapters")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "unauthorized"


def test_protected_api_rejects_wrong_token(api_client: TestClient) -> None:
    response = api_client.get("/api/adapters", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "unauthorized"


def test_protected_api_rejects_non_bearer_scheme(api_client: TestClient) -> None:
    response = api_client.get("/api/adapters", headers={"Authorization": f"Basic {ADMIN_TOKEN}"})
    assert response.status_code == 401


def test_admin_api_rejects_worker_token(api_client: TestClient) -> None:
    response = api_client.get("/api/adapters", headers={"Authorization": f"Bearer {WORKER_TOKEN}"})
    assert response.status_code == 401


def test_worker_api_rejects_admin_token(api_client: TestClient) -> None:
    response = api_client.post(
        "/api/workers/register",
        json={
            "protocol_version": 3,
            "isolation_capabilities": dict(ISOLATION_PASS),
            "name": "worker-1",
        },
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert response.status_code == 401


def test_admin_token_not_configured_yields_503(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "admin_token", None)
    response = api_client.get("/api/adapters")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "auth_not_configured"


def test_worker_token_not_configured_yields_503(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "worker_token", None)
    response = api_client.post(
        "/api/workers/register",
        json={
            "protocol_version": 3,
            "isolation_capabilities": dict(ISOLATION_PASS),
            "name": "worker-1",
        },
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
    )
    assert response.status_code == 503


def test_admin_verify_endpoint(api_client: TestClient) -> None:
    response = api_client.get("/api/auth/admin/verify")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    bad = api_client.get("/api/auth/admin/verify", headers={"Authorization": "Bearer wrong-token"})
    assert bad.status_code == 401


def test_health_needs_no_auth(api_client: TestClient) -> None:
    response = api_client.get("/api/health", headers={"Authorization": ""})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
