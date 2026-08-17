"""M5.6 Wave 1 deployment locale contracts."""

from fastapi.testclient import TestClient

from conftest import ADMIN_TOKEN, WORKER_TOKEN


def test_default_locale_is_zh_cn_and_public_response_has_no_other_settings(
    api_client: TestClient,
) -> None:
    public_client = TestClient(api_client.app)

    response = public_client.get("/api/locale")

    assert response.status_code == 200
    assert response.json() == {"locale": "zh-CN"}


def test_public_locale_query_does_not_require_authentication(api_client: TestClient) -> None:
    public_client = TestClient(api_client.app)

    response = public_client.get("/api/locale", headers={"Authorization": "Bearer wrong"})

    assert response.status_code == 200
    assert response.json() == {"locale": "zh-CN"}


def test_authorized_update_persists_and_public_read_reflects_locale(
    api_client: TestClient,
) -> None:
    updated = api_client.put("/api/locale", json={"locale": "en"})
    public_client = TestClient(api_client.app)

    assert updated.status_code == 200
    assert updated.json() == {"locale": "en"}
    assert public_client.get("/api/locale").json() == {"locale": "en"}


def test_locale_update_requires_admin_authentication(api_client: TestClient) -> None:
    public_client = TestClient(api_client.app)

    missing = public_client.put("/api/locale", json={"locale": "en"})
    worker = public_client.put(
        "/api/locale",
        json={"locale": "en"},
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
    )

    assert missing.status_code == 401
    assert worker.status_code == 401


def test_invalid_locale_is_rejected_without_changing_authoritative_value(
    api_client: TestClient,
) -> None:
    invalid = api_client.put("/api/locale", json={"locale": "fr-FR"})
    public_client = TestClient(api_client.app)

    assert invalid.status_code == 422
    assert public_client.get("/api/locale").json() == {"locale": "zh-CN"}


def test_locale_update_does_not_mutate_adapter_or_revision(api_client: TestClient) -> None:
    created = api_client.post(
        "/api/adapters",
        json={"name": "locale-contract", "language": "python", "adapter_type": "task"},
    )
    assert created.status_code == 201
    adapter_before = created.json()

    updated = api_client.put("/api/locale", json={"locale": "en"})
    adapter_after = api_client.get(f"/api/adapters/{adapter_before['id']}")
    versions = api_client.get(f"/api/adapters/{adapter_before['id']}/versions")

    assert updated.status_code == 200
    assert adapter_after.status_code == 200
    assert adapter_after.json() == adapter_before
    assert versions.status_code == 200
    assert versions.json() == []


def test_authorized_update_uses_the_admin_token_contract(api_client: TestClient) -> None:
    response = api_client.put(
        "/api/locale",
        json={"locale": "en"},
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )

    assert response.status_code == 200
    assert response.json() == {"locale": "en"}
