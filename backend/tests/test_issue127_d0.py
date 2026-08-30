"""Issue #127 D0 public Managed Input capability contract tests."""

import pytest

from dlr.common.config import settings
from dlr.control.security import Principal, require_principal


def test_managed_input_capability_exposes_safe_release_and_retention_facts(
    api_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", False)

    response = api_client.get("/api/system/managed-input-capability")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "managed_files_enabled": False,
        "ready": False,
        "default_retention_seconds": 86_400,
        "max_custom_retention_seconds": 2_592_000,
        "allow_manual_delete": True,
        "allowed_extensions": [".xlsx", ".xls", ".csv", ".log", ".txt", ".json"],
    }
    assert "artifact_store_root" not in response.text
    assert "token" not in response.text.casefold()
    assert "quota" not in response.text.casefold()


def test_managed_input_capability_tracks_open_feature_flag(
    api_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)

    response = api_client.get("/api/system/managed-input-capability")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "managed_files_enabled": True,
        "ready": True,
        "default_retention_seconds": 86_400,
        "max_custom_retention_seconds": 2_592_000,
        "allow_manual_delete": True,
        "allowed_extensions": [".xlsx", ".xls", ".csv", ".log", ".txt", ".json"],
    }


def test_managed_input_capability_is_available_to_business_accounts(
    api_client,
) -> None:
    api_client.app.dependency_overrides[require_principal] = lambda: Principal(
        kind="account", role="user", user_id=123, username="d0-business"
    )
    try:
        response = api_client.get("/api/system/managed-input-capability")
    finally:
        api_client.app.dependency_overrides.pop(require_principal, None)

    assert response.status_code == 200, response.text
    assert set(response.json()) == {
        "managed_files_enabled",
        "ready",
        "default_retention_seconds",
        "max_custom_retention_seconds",
        "allow_manual_delete",
        "allowed_extensions",
    }
