"""Issue #127 B0 red/green tests for Managed Input policy contracts."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import Settings, settings
from dlr.control.app import create_app
from dlr.control.models import (
    ManagedInputArtifact,
    ManagedInputCapacity,
    ManagedInputUploadReservation,
)
from dlr.control.security import Principal, require_principal

SETTINGS_FIELDS = (
    "default_retention_seconds",
    "max_file_bytes",
    "platform_quota_bytes",
    "adapter_quota_bytes",
    "allow_manual_delete",
    "max_custom_retention_seconds",
    "min_free_space_bytes",
    "staged_ttl_seconds",
)


def settings_payload(body: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    payload = {field: body[field] for field in SETTINGS_FIELDS}
    payload.update(overrides)
    return payload


def create_task(api_client: TestClient, name: str) -> dict[str, Any]:
    response = api_client.post(
        "/api/adapters",
        json={"name": name, "language": "python", "adapter_type": "task"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_managed_input_settings_usage_contract_is_available(
    api_client: TestClient,
) -> None:
    """The B0 settings surface must expose policy and current usage."""
    response = api_client.get("/api/system/managed-input-settings")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == 1
    assert body["platform_quota_bytes"] > 0
    assert body["adapter_quota_bytes"] <= body["platform_quota_bytes"]
    assert body["usage"]["platform_actual_bytes"] >= 0
    assert body["usage"]["platform_reserved_bytes"] >= 0
    assert "over_quota" in body


def test_settings_seed_and_response_exclude_deployment_values(api_client: TestClient) -> None:
    response = api_client.get("/api/system/managed-input-settings")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["default_retention_seconds"] == 86_400
    assert body["max_file_bytes"] == 100 * 1024 * 1024
    assert body["platform_quota_bytes"] == 10 * 1024 * 1024 * 1024
    assert body["adapter_quota_bytes"] == 1024 * 1024 * 1024
    assert body["allow_manual_delete"] is True
    assert body["max_custom_retention_seconds"] == 2_592_000
    assert body["min_free_space_bytes"] == 1024 * 1024 * 1024
    assert body["staged_ttl_seconds"] == 3_600
    assert "artifact_store_root" not in response.text
    assert "managed_files_enabled" not in response.text


def test_usage_counts_active_reservation_in_platform_and_adapter_totals(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter = create_task(api_client, "b0-active-reservation-usage")
    adapter_id = int(adapter["id"])
    with session_factory() as session:
        session.add(
            ManagedInputUploadReservation(
                adapter_id=adapter_id,
                upload_session_id="b0-active-reservation-session",
                reserved_bytes=3 * 1024 * 1024,
                status="ACTIVE",
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        )
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        capacity.actual_bytes = 4 * 1024 * 1024
        capacity.reserved_bytes = 3 * 1024 * 1024
        session.commit()

    body = api_client.get("/api/system/managed-input-settings").json()
    assert body["usage"]["platform_actual_bytes"] == 4 * 1024 * 1024
    assert body["usage"]["platform_reserved_bytes"] == 3 * 1024 * 1024
    assert body["usage"]["platform_total_bytes"] == 7 * 1024 * 1024
    assert body["usage"]["adapters"] == [
        {
            "adapter_id": adapter_id,
            "actual_bytes": 0,
            "reserved_bytes": 3 * 1024 * 1024,
            "total_bytes": 3 * 1024 * 1024,
            "quota_bytes": 1024 * 1024 * 1024,
            "over_quota": False,
        }
    ]


def test_settings_update_is_admin_only_and_does_not_rewrite_artifacts(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter = create_task(api_client, "b0-settings-artifact-preservation")
    adapter_id = int(adapter["id"])
    expires_at = datetime.now(UTC) + timedelta(days=5)
    with session_factory() as session:
        reservation = ManagedInputUploadReservation(
            adapter_id=adapter_id,
            upload_session_id="b0-anonymous-session",
            reserved_bytes=2 * 1024 * 1024,
            status="CONSUMED",
            expires_at=expires_at,
            consumed_at=datetime.now(UTC),
        )
        session.add(reservation)
        session.flush()
        session.add(
            ManagedInputArtifact(
                adapter_id=adapter_id,
                created_by_user_id=None,
                upload_session_id="b0-anonymous-session",
                upload_reservation_id=reservation.id,
                original_filename="anonymous.txt",
                storage_key="b0-random-storage-key",
                content_type="text/plain",
                size_bytes=2 * 1024 * 1024,
                sha256="a" * 64,
                status="READY",
                retention_mode="custom",
                expires_at=expires_at,
            )
        )
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        capacity.actual_bytes = 2 * 1024 * 1024
        capacity.reserved_bytes = 0
        session.commit()

    current = api_client.get("/api/system/managed-input-settings").json()
    response = api_client.put(
        "/api/system/managed-input-settings",
        json=settings_payload(
            current,
            platform_quota_bytes=1 * 1024 * 1024,
            adapter_quota_bytes=1 * 1024 * 1024,
            allow_manual_delete=False,
        ),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["over_quota"] is True
    assert body["platform_over_quota"] is True
    assert adapter_id in body["adapter_over_quota"]
    assert body["usage"]["platform_actual_bytes"] == 2 * 1024 * 1024
    assert body["usage"]["platform_reserved_bytes"] == 0
    assert body["usage"]["adapters"] == [
        {
            "adapter_id": adapter_id,
            "actual_bytes": 2 * 1024 * 1024,
            "reserved_bytes": 0,
            "total_bytes": 2 * 1024 * 1024,
            "quota_bytes": 1 * 1024 * 1024,
            "over_quota": True,
        }
    ]

    with session_factory() as session:
        artifact = session.scalar(
            select(ManagedInputArtifact).where(ManagedInputArtifact.adapter_id == adapter_id)
        )
        assert artifact is not None
        assert artifact.status == "READY"
        assert artifact.expires_at == expires_at
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None
        assert capacity.actual_bytes == 2 * 1024 * 1024


def test_ordinary_account_cannot_read_or_update_managed_input_settings(
    api_client: TestClient,
) -> None:
    api_client.app.dependency_overrides[require_principal] = lambda: Principal(
        kind="account", role="user", user_id=123, username="b0-ordinary"
    )
    try:
        read = api_client.get("/api/system/managed-input-settings")
        update = api_client.put(
            "/api/system/managed-input-settings",
            json={"default_retention_seconds": 86_400},
        )
    finally:
        api_client.app.dependency_overrides.pop(require_principal, None)

    assert read.status_code == 403
    assert read.json()["detail"]["code"] == "account_admin_required"
    assert update.status_code == 403
    assert update.json()["detail"]["code"] == "account_admin_required"


@pytest.mark.parametrize(
    "override",
    [
        {"platform_quota_bytes": None},
        {"adapter_quota_bytes": None},
        {"default_retention_seconds": None},
        {"platform_quota_bytes": 0},
        {"max_file_bytes": 2 * 1024 * 1024 * 1024 + 1},
        {"adapter_quota_bytes": 2 * 1024 * 1024, "platform_quota_bytes": 1 * 1024 * 1024},
        {"default_retention_seconds": 86_400, "max_custom_retention_seconds": 3_600},
    ],
)
def test_settings_reject_null_unbounded_and_cross_field_values(
    api_client: TestClient,
    override: dict[str, Any],
) -> None:
    before = api_client.get("/api/system/managed-input-settings").json()
    response = api_client.put(
        "/api/system/managed-input-settings",
        json=settings_payload(before, **override),
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "managed_input_settings_invalid"
    after = api_client.get("/api/system/managed-input-settings").json()
    assert settings_payload(after) == settings_payload(before)


def test_settings_cannot_override_deployment_only_values(api_client: TestClient) -> None:
    current = api_client.get("/api/system/managed-input-settings").json()
    payload = settings_payload(current, artifact_store_root="/tmp/not-a-setting")
    response = api_client.put("/api/system/managed-input-settings", json=payload)

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "managed_input_settings_invalid"


def test_startup_gate_rejects_invalid_artifact_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLR_ARTIFACT_STORE_ROOT", "")
    with pytest.raises(ValueError, match="DLR_ARTIFACT_STORE_ROOT"):
        Settings()

    monkeypatch.delenv("DLR_ARTIFACT_STORE_ROOT")
    monkeypatch.setenv("DLR_ARTIFACT_STORE_ROOT", "relative-artifacts")
    with pytest.raises(ValueError, match="DLR_ARTIFACT_STORE_ROOT"):
        Settings()
    monkeypatch.delenv("DLR_ARTIFACT_STORE_ROOT")
    for variable, value in (
        ("DLR_ARTIFACT_GC_INTERVAL_SECONDS", "0"),
        ("DLR_ARTIFACT_GC_INTERVAL_SECONDS", "86401"),
        ("DLR_ARTIFACT_AUDIT_INTERVAL_SECONDS", "0"),
        ("DLR_ARTIFACT_AUDIT_INTERVAL_SECONDS", "604801"),
    ):
        monkeypatch.setenv(variable, value)
        with pytest.raises(ValueError):
            Settings()
        monkeypatch.delenv(variable)

    monkeypatch.setattr(settings, "artifact_gc_interval_seconds", 0)
    with pytest.raises(ValueError, match="DLR_ARTIFACT_GC_INTERVAL_SECONDS"):
        create_app()
