"""Issue #127 A0 red/green contract tests for the Adapter input object."""

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import Adapter, AdapterInputConfig, AdapterPermission, AdapterSchedule, User
from dlr.control.security import Principal, require_principal
from test_adapters import create_adapter, save_version


def input_config(client: TestClient, adapter_id: int) -> dict[str, Any]:
    response = client.get(f"/api/adapters/{adapter_id}/input-config")
    assert response.status_code == 200, response.text
    return response.json()


def test_a1_compat_schedule_input_becomes_saved_config(api_client: TestClient) -> None:
    """The compatibility Schedule field mirrors the unified saved input."""
    adapter = create_adapter(api_client, name="a0-input-red-light")
    save_version(api_client, adapter["id"])
    mode = api_client.patch(f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"})
    assert mode.status_code == 200, mode.text
    schedule = api_client.put(
        f"/api/adapters/{adapter['id']}/schedule",
        json={
            "enabled": False,
            "cron": "0 * * * *",
            "timezone": "UTC",
            "input": {"legacy_schedule": True},
        },
    )
    assert schedule.status_code == 200, schedule.text
    manual = api_client.post(
        f"/api/adapters/{adapter['id']}/executions",
        json={"input": {"legacy_manual": True}},
    )
    assert manual.status_code == 202, manual.text
    assert manual.json()["input"] == {"legacy_manual": True}

    cancelled = api_client.post(f"/api/executions/{manual.json()['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    manual_without_input = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert manual_without_input.status_code == 202, manual_without_input.text
    assert manual_without_input.json()["input"] == {"legacy_schedule": True}

    body = input_config(api_client, adapter["id"])
    assert body["source_type"] == "json"
    assert body["json_value"] == {"legacy_schedule": True}
    assert body["revision"] == 2
    assert body["valid_for_run"] is True


def test_new_task_gets_none_config_and_safe_response(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter = create_adapter(api_client, name="a0-none-default")

    body = input_config(api_client, adapter["id"])

    assert body == {
        "adapter_id": adapter["id"],
        "revision": 1,
        "source_type": "none",
        "json_value": None,
        "retention": {"mode": "system_default", "seconds": None},
        "artifacts": [],
        "valid_for_run": True,
        "invalid_reason": None,
    }
    with session_factory() as session:
        config = session.get(AdapterInputConfig, adapter["id"])
        assert config is not None
        assert config.source_type == "none"
        assert config.revision == 1


def test_input_config_json_size_limit_does_not_persist(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create_adapter(api_client, name="a0-json-size-limit")
    monkeypatch.setattr(settings, "execution_input_max_bytes", 64)

    response = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "json",
            "json_value": {"value": "x" * 128},
        },
    )

    assert response.status_code == 413
    assert response.json()["detail"] == {
        "code": "execution_input_too_large",
        "message": "Input exceeds the 64 byte limit",
        "params": {"max_bytes": 64},
    }
    with session_factory() as session:
        config = session.get(AdapterInputConfig, adapter["id"])
        assert config is not None
        assert config.source_type == "none"
        assert config.revision == 1


def test_input_config_read_edit_acl_and_resource_errors(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter = create_adapter(api_client, name="a0-input-acl")
    with session_factory() as session:
        reader = User(
            username="a0-input-reader",
            password_hash="anonymous-test-hash",
            role="user",
        )
        session.add(reader)
        session.flush()
        session.add(
            AdapterPermission(adapter_id=adapter["id"], user_id=reader.id, permission="read")
        )
        session.commit()
        reader_id = reader.id

    api_client.app.dependency_overrides[require_principal] = lambda: Principal(
        kind="account", role="user", user_id=reader_id, username="a0-input-reader"
    )
    try:
        readable = api_client.get(f"/api/adapters/{adapter['id']}/input-config")
        assert readable.status_code == 200, readable.text
        forbidden = api_client.put(
            f"/api/adapters/{adapter['id']}/input-config",
            json={"expected_revision": 1, "source_type": "none"},
        )
        assert forbidden.status_code == 403, forbidden.text
        assert forbidden.json()["detail"]["code"] == "adapter_read_only"
    finally:
        api_client.app.dependency_overrides.pop(require_principal, None)

    unknown = api_client.put(
        "/api/adapters/999999/input-config",
        json={"expected_revision": 1, "source_type": "none"},
    )
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "adapter_not_found"

    archived = create_adapter(api_client, name="a0-input-archived")
    with session_factory() as session:
        archived_row = session.get(Adapter, archived["id"])
        assert archived_row is not None
        archived_row.archived_at = datetime.now(UTC)
        session.commit()
    archived_response = api_client.put(
        f"/api/adapters/{archived['id']}/input-config",
        json={"expected_revision": 1, "source_type": "none"},
    )
    assert archived_response.status_code == 404
    assert archived_response.json()["detail"]["code"] == "adapter_not_found"

    webhook = create_adapter(api_client, name="a0-input-webhook", adapter_type="webhook")
    webhook_response = api_client.put(
        f"/api/adapters/{webhook['id']}/input-config",
        json={"expected_revision": 1, "source_type": "none"},
    )
    assert webhook_response.status_code == 409
    assert webhook_response.json()["detail"]["code"] == "adapter_type_mismatch"


def test_none_save_rejects_type_specific_fields_without_revision_change(
    api_client: TestClient,
) -> None:
    adapter = create_adapter(api_client, name="a0-none-fields")

    rejected = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={"expected_revision": 1, "source_type": "none", "json_value": None},
    )

    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "input_invalid"
    assert input_config(api_client, adapter["id"])["revision"] == 1


@pytest.mark.parametrize("json_value", [{"region": "cn"}, [1, "two"], "scalar", 7, True, None])
def test_json_save_preserves_every_json_top_level_value(
    api_client: TestClient, json_value: Any
) -> None:
    adapter = create_adapter(api_client, name=f"a0-json-{str(json_value)[:8]}")

    response = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={"expected_revision": 1, "source_type": "json", "json_value": json_value},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"] == 2
    assert body["source_type"] == "json"
    assert body["json_value"] == json_value
    assert body["valid_for_run"] is True
    assert body["invalid_reason"] is None


def test_json_type_specific_fields_are_rejected(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="a0-json-fields")

    response = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "json",
            "json_value": {},
            "artifact_ids": [],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "input_invalid"
    assert input_config(api_client, adapter["id"])["revision"] == 1


def test_json_null_is_stored_as_json_null_not_sql_null(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter = create_adapter(api_client, name="a0-json-null-storage")
    response = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={"expected_revision": 1, "source_type": "json", "json_value": None},
    )
    assert response.status_code == 200, response.text

    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT json_value IS NULL AS sql_null, jsonb_typeof(json_value) AS json_type "
                "FROM adapter_input_configs WHERE adapter_id = :adapter_id"
            ),
            {"adapter_id": adapter["id"]},
        ).one()
        assert row.sql_null is False
        assert row.json_type == "null"


def test_managed_files_empty_is_saveable_but_not_runnable(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="a0-managed-empty")

    response = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [],
            "retention": {"mode": "system_default", "seconds": None},
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"] == 2
    assert body["valid_for_run"] is False
    assert body["invalid_reason"] == "managed_files_empty"
    assert body["artifacts"] == []


def test_managed_files_unknown_artifact_is_not_found(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    adapter = create_adapter(api_client, name="a0-managed-not-ready")

    response = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [101],
            "retention": {"mode": "custom", "seconds": 3600},
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "input_artifact_not_found"
    assert input_config(api_client, adapter["id"])["revision"] == 1


def test_managed_files_disabled_hides_unknown_artifact(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", False)
    adapter = create_adapter(api_client, name="a0-managed-disabled-not-ready")

    response = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={
            "expected_revision": 1,
            "source_type": "managed_files",
            "artifact_ids": [101],
            "retention": {"mode": "custom", "seconds": 3600},
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "input_source_not_available"
    assert input_config(api_client, adapter["id"])["revision"] == 1


def test_remote_files_is_stably_rejected(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="a0-remote-files")

    response = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={"expected_revision": 1, "source_type": "remote_files"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "input_source_not_available"
    assert input_config(api_client, adapter["id"])["revision"] == 1


def test_revision_conflict_has_no_side_effect(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="a0-revision")
    endpoint = f"/api/adapters/{adapter['id']}/input-config"
    first = api_client.put(
        endpoint,
        json={"expected_revision": 1, "source_type": "json", "json_value": {"v": 1}},
    )
    assert first.status_code == 200, first.text

    stale = api_client.put(
        endpoint,
        json={"expected_revision": 1, "source_type": "json", "json_value": {"v": 2}},
    )

    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "input_config_revision_conflict"
    assert stale.json()["detail"]["params"] == {
        "expected_revision": 1,
        "current_revision": 2,
    }
    current = input_config(api_client, adapter["id"])
    assert current["revision"] == 2
    assert current["json_value"] == {"v": 1}


def test_input_config_runtime_lock_is_authoritative(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="a0-runtime-lock")
    save_version(api_client, adapter["id"])
    mode = api_client.patch(f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"})
    assert mode.status_code == 200, mode.text
    schedule = api_client.put(
        f"/api/adapters/{adapter['id']}/schedule",
        json={
            "enabled": True,
            "cron": "0 * * * *",
            "timezone": "UTC",
            "input": None,
        },
    )
    assert schedule.status_code == 200, schedule.text

    readable = api_client.get(f"/api/adapters/{adapter['id']}/input-config")
    assert readable.status_code == 200, readable.text

    response = api_client.put(
        f"/api/adapters/{adapter['id']}/input-config",
        json={"expected_revision": 1, "source_type": "json", "json_value": {"blocked": True}},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_runtime_locked"
    assert input_config(api_client, adapter["id"])["revision"] == 1


def test_missing_input_config_uses_registered_error_code(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter = create_adapter(api_client, name="a0-input-not-initialized")
    with session_factory() as session:
        config = session.get(AdapterInputConfig, adapter["id"])
        assert config is not None
        session.delete(config)
        session.commit()

    response = api_client.get(f"/api/adapters/{adapter['id']}/input-config")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "input_config_not_initialized"


def test_webhook_has_no_task_input_config(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="a0-webhook", adapter_type="webhook")

    response = api_client.get(f"/api/adapters/{adapter['id']}/input-config")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_type_mismatch"


def test_schedule_blocked_columns_are_migrated(
    session_factory: sessionmaker[Session],
) -> None:
    columns = {
        column["name"]
        for column in inspect(session_factory.kw["bind"]).get_columns("adapter_schedules")
    }
    assert {
        "last_blocked_reason",
        "last_blocked_at",
        "last_processed_due_at",
    }.issubset(columns)


def test_input_config_catalog_has_constraints_indexes_and_fk(
    session_factory: sessionmaker[Session],
) -> None:
    bind = session_factory.kw["bind"]
    inspector = inspect(bind)
    assert inspector.get_pk_constraint("adapter_input_configs")["constrained_columns"] == [
        "adapter_id"
    ]
    foreign_keys = inspector.get_foreign_keys("adapter_input_configs")
    assert any(
        foreign_key["referred_table"] == "adapters"
        and foreign_key["constrained_columns"] == ["adapter_id"]
        and foreign_key["options"].get("ondelete") == "CASCADE"
        for foreign_key in foreign_keys
    )
    check_names = {
        check["name"] for check in inspector.get_check_constraints("adapter_input_configs")
    }
    assert {
        "ck_adapter_input_configs_source_type",
        "ck_adapter_input_configs_retention_mode",
        "ck_adapter_input_configs_revision_positive",
        "ck_adapter_input_configs_source_fields",
        "ck_adapter_input_configs_retention_fields",
    }.issubset(check_names)
    index_names = {index["name"] for index in inspector.get_indexes("adapter_input_configs")}
    assert {
        "ix_adapter_input_configs_source_type",
        "ix_adapter_input_configs_revision",
    }.issubset(index_names)


def test_schedule_blocked_fields_are_persistable(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    adapter = create_adapter(api_client, name="a0-blocked-fields")
    save_version(api_client, adapter["id"])
    mode = api_client.patch(f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"})
    assert mode.status_code == 200, mode.text
    response = api_client.put(
        f"/api/adapters/{adapter['id']}/schedule",
        json={
            "enabled": False,
            "cron": "0 * * * *",
            "timezone": "UTC",
            "input": None,
        },
    )
    assert response.status_code == 200, response.text
    with session_factory() as session:
        schedule = session.scalar(select(AdapterSchedule))
        assert schedule is not None
        schedule.last_blocked_reason = "input_invalid"
        session.commit()
        session.refresh(schedule)
        assert schedule.last_blocked_reason == "input_invalid"
