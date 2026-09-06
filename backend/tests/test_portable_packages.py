"""Portable content trust boundaries, target gates, and transactional round trips."""

import base64
import io
import json
import stat
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    AdapterCredentialBinding,
    AdapterInputConfig,
    AdapterPermission,
    AdapterSchedule,
    AdapterVersion,
    AdapterWebhook,
    ManagedInputArtifact,
    ManagedInputCapacity,
    ManagedInputUploadReservation,
    User,
    UserTemplate,
    Worker,
)
from dlr.control.schemas.portable import PortablePackage
from dlr.control.security import Principal, require_principal
from dlr.control.services import adapter as adapters
from dlr.control.services.artifact_store import LocalFileArtifactStore
from dlr.control.services.portable_zip import decode_package, encode_package


def package(
    kind: str = "adapter", language: str = "python", adapter_type: str = "task"
) -> dict[str, Any]:
    return PortablePackage.model_validate(
        {
            "object_type": kind,
            "name": "Portable example",
            "adapter_type": adapter_type,
            "variants": [
                {"language": language, "code": "DO_NOT_EXECUTE", "requirements": "declaration-only"}
            ],
        }
    ).model_dump(mode="json")


def worker(session_factory: sessionmaker[Session], language: str) -> int:
    with session_factory() as session:
        row = Worker(name="target-worker", status="offline", capabilities=[language])
        session.add(row)
        session.commit()
        return row.id


def import_adapter(client: TestClient, value: dict[str, Any], worker_id: int | None = None) -> Any:
    return client.post(
        "/api/portable/adapters",
        json={
            "package": value,
            "runtime_worker_id": worker_id,
            "configuration_reviewed": True,
        },
    )


def import_template(client: TestClient, value: dict[str, Any]) -> Any:
    return client.post(
        "/api/portable/templates", json={"package": value, "sharing_confirmed": True}
    )


@pytest.mark.parametrize("language", ["python", "javascript", "java"])
@pytest.mark.parametrize("kind", ["adapter", "template"])
def test_codec_readable_roundtrip(language: str, kind: str) -> None:
    original = PortablePackage.model_validate(package(kind, language))
    raw = encode_package(original)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert "manifest.json" in archive.namelist()
        assert any(path.startswith("code/") for path in archive.namelist())
        assert "DO_NOT_EXECUTE" not in archive.read("manifest.json").decode()
    assert decode_package(raw) == original


def test_portable_upgrade_preserves_builtin_library() -> None:
    from test_unified_runtime_migration import _isolated_schema, _upgrade

    with _isolated_schema("portable_after_builtin", "0035_builtin_packages") as (engine, database):
        with engine.begin() as connection:
            connection.execute(text("UPDATE builtin_package_settings SET quota_bytes = 2147483648"))
            connection.execute(
                text(
                    "INSERT INTO builtin_package_uploads "
                    "(id, kind, filename, repository_path, size_bytes) "
                    "VALUES ('pending-before-upgrade', 'npm', 'pending.tgz', '', 1024)"
                )
            )
            sources = connection.execute(
                text("SELECT id, index_url FROM package_sources ORDER BY id")
            ).all()
        _upgrade(database, "head")
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == ["0036_portable"]
            assert (
                connection.scalar(text("SELECT quota_bytes FROM builtin_package_settings"))
                == 2147483648
            )
            assert connection.scalar(text("SELECT size_bytes FROM builtin_package_uploads")) == 1024
            assert (
                connection.execute(
                    text("SELECT id, index_url FROM package_sources ORDER BY id")
                ).all()
                == sources
            )
            assert (
                connection.scalar(text("SELECT to_regclass('user_templates')")) == "user_templates"
            )


def test_export_with_builtin_default_contains_only_dependency_declarations(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_builtin_packages import upload, wheel

    monkeypatch.setattr(settings, "builtin_package_root", str(tmp_path / "library"))
    monkeypatch.setattr(settings, "builtin_package_min_free_bytes", 0)
    filename, material = wheel()
    stored = upload(api_client, filename, material)
    selected = api_client.put("/api/builtin-packages/sources/pypi")
    assert selected.status_code == 200, selected.text
    value = package()
    value["variants"][0]["requirements"] = "offline_demo==1.0"
    imported = import_adapter(api_client, value, worker(session_factory, "python"))
    assert imported.status_code == 201, imported.text
    preview = api_client.post(f"/api/adapters/{imported.json()['id']}/portable-preview", json={})
    assert preview.status_code == 200, preview.text
    assert preview.json()["variants"][0]["requirements"] == "offline_demo==1.0"
    exported = api_client.post("/api/portable/export", json=preview.json())
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert not any(
            name.endswith((".whl", ".tgz", ".jar", ".pom")) for name in archive.namelist()
        )
        content = b"\n".join(archive.read(name) for name in archive.namelist())
    for forbidden in (
        b"dlr-builtin://",
        b"package_source_id",
        b"builtin_package_snapshot",
        stored["sha256"].encode(),
    ):
        assert forbidden not in content
    assert api_client.get(f"/api/builtin-packages/{stored['id']}/content").content == material


@pytest.mark.parametrize(
    "path", ["../evil", "/absolute", "C:/windows", "a\\b", "a/../b", "a//b", "./code"]
)
def test_unsafe_zip_paths_rejected(path: str) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(path, "bad")
    with pytest.raises(HTTPException) as exc:
        decode_package(buffer.getvalue())
    assert exc.value.status_code == 422


def test_symlinks_duplicate_entries_and_excess_files_rejected() -> None:
    for mode in ("symlink", "duplicate", "many"):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            if mode == "symlink":
                entry = zipfile.ZipInfo("link")
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(entry, "target")
            elif mode == "duplicate":
                archive.writestr("same", "a")
                with pytest.warns(UserWarning):
                    archive.writestr("same", "b")
            else:
                for index in range(33):
                    archive.writestr(str(index), "")
        with pytest.raises(HTTPException):
            decode_package(buffer.getvalue())


@pytest.mark.parametrize(
    "field,value",
    [
        ("format_version", 2),
        ("variants", [{"language": "go", "code": "main"}]),
        ("credential_id", 17),
        ("runtime_worker_id", 4),
        ("verified", True),
        ("source", "system"),
    ],
)
def test_manifest_unknown_versions_languages_and_identity_rejected(field: str, value: Any) -> None:
    original = encode_package(PortablePackage.model_validate(package()))
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(buffer, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            if name == "manifest.json":
                manifest = json.loads(content)
                manifest[field] = value
                content = json.dumps(manifest).encode()
            target.writestr(name, content)
    with pytest.raises(HTTPException):
        decode_package(buffer.getvalue())


@pytest.mark.parametrize("language", ["python", "javascript", "java"])
@pytest.mark.parametrize("adapter_type", ["task", "webhook"])
def test_adapter_import_roundtrip_stopped_new_identity(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    language: str,
    adapter_type: str,
) -> None:
    value = package(language=language, adapter_type=adapter_type)
    value["variants"][0]["runtime_config"] = {"mapping": {"id": "external_id"}}
    value["variants"][0]["required_parameters"] = ["customer_url"]
    value["timeout_seconds"] = 123
    if adapter_type == "task":
        value["schedule"] = {"cron": "0 2 * * *", "timezone": "Asia/Shanghai"}
        value["input"] = {"source_type": "json", "included": True, "json_value": {"test": 1}}
    else:
        value["webhook"] = {"response_mode": "completed", "response_timeout_seconds": 23}
    target = worker(session_factory, language)
    result = import_adapter(api_client, value, target)
    assert result.status_code == 201, result.text
    row = result.json()
    assert row["run_mode"] == "manual" and not row["runtime_locked"]
    assert row["timeout_seconds"] == 123
    assert row["configuration_notes"]["required_parameters"] == ["customer_url"]
    export = api_client.post(
        f"/api/adapters/{row['id']}/portable-preview", json={"include_json": True}
    )
    assert export.status_code == 200, export.text
    exported = export.json()
    assert exported["variants"][0]["code"] == "DO_NOT_EXECUTE"
    assert exported["variants"][0]["runtime_config"] == {"mapping": {"id": "external_id"}}
    exported["name"] = "Second independent copy"
    copy = import_adapter(api_client, exported, target)
    assert copy.status_code == 201, copy.text
    assert copy.json()["id"] != row["id"]
    assert copy.json()["latest_version_id"] != row["latest_version_id"]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AdapterCredentialBinding)) == 0
        if adapter_type == "task":
            schedules = list(session.scalars(select(AdapterSchedule)))
            assert len(schedules) == 2
            assert all(
                not item.enabled and item.next_run_at is None and item.last_processed_due_at is None
                for item in schedules
            )
        else:
            hooks = list(session.scalars(select(AdapterWebhook)))
            assert len({item.public_id for item in hooks}) == 2
            assert all(
                not item.enabled
                and item.credential_id is None
                and item.response_mode == "completed"
                for item in hooks
            )


def test_task_worker_gate_conflict_and_preview_have_no_side_effects(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    raw = encode_package(PortablePackage.model_validate(package()))
    response = api_client.post(
        "/api/portable/preview", content=raw, headers={"Content-Type": "application/zip"}
    )
    assert response.status_code == 200, response.text
    assert import_adapter(api_client, response.json()).status_code == 409
    target = worker(session_factory, "java")
    assert import_adapter(api_client, response.json(), target).status_code == 409
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Adapter)) == 0
    value = package(adapter_type="webhook")
    assert import_adapter(api_client, value).status_code == 201
    assert import_adapter(api_client, value).json()["detail"]["code"] == "adapter_name_conflict"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Adapter)) == 1
        assert session.scalar(select(func.count()).select_from(AdapterVersion)) == 1


def test_json_null_and_omitted_input_template_defaults(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    value = package()
    value["input"] = {"source_type": "json", "included": True, "json_value": None}
    value["variants"][0]["runtime_config"] = {"customer_address": "sensitive.example"}
    result = import_adapter(api_client, value, worker(session_factory, "python"))
    assert result.status_code == 201, result.text
    aid = result.json()["id"]
    default = api_client.post(f"/api/adapters/{aid}/portable-preview", json={}).json()
    assert default["input"]["included"] is False
    explicit = api_client.post(
        f"/api/adapters/{aid}/portable-preview", json={"include_json": True}
    ).json()
    assert explicit["input"]["included"] is True and explicit["input"]["json_value"] is None
    template = api_client.post(
        f"/api/adapters/{aid}/portable-preview", json={"as_template": True, "include_json": True}
    ).json()
    assert template["input"]["source_type"] == "none"
    assert template["variants"][0]["runtime_config"] == {}
    assert template["variants"][0]["required_parameters"] == ["customer_address"]
    assert "sensitive.example" not in json.dumps(template)


def test_user_template_persistence_gallery_edit_delete_independence(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    value = package("template", "javascript")
    value["category"] = "foreign-category"
    value["tags"] = ["portable-tag"]
    created = import_template(api_client, value)
    assert created.status_code == 201, created.text
    detail = created.json()
    slug = detail["slug"]
    assert detail["theme_slug"] == "other" and detail["source"] == "imported"
    assert detail["can_manage"] is True
    assert [v["language"] for v in detail["variants"]] == ["javascript"]
    assert import_template(api_client, value).status_code == 409
    listing = api_client.get(
        "/api/templates/scenarios",
        params={"theme": "other", "q": "portable-tag", "language": "javascript"},
    ).json()
    assert listing["total"] == 1 and listing["items"][0]["slug"] == slug
    assert api_client.get(f"/api/templates/scenarios/{slug}/variants/python").status_code == 404
    copied = api_client.post(
        f"/api/templates/scenarios/{slug}/variants/javascript/instantiate",
        json={"name": "independent", "expected_template_version": "1"},
    )
    assert copied.status_code == 201, copied.text
    assert copied.json()["latest_version_id"] is None and copied.json()["runtime_worker_id"] is None
    value["category"] = "databases"
    value["variants"][0]["code"] = "EDITED_NOT_EXECUTED"
    update = api_client.put(
        f"/api/templates/scenarios/{slug}",
        json={"package": value, "sharing_confirmed": True, "expected_version": "1"},
    )
    assert update.status_code == 200, update.text
    assert update.json()["template_version"] == "2"
    assert (
        api_client.delete(f"/api/templates/scenarios/{slug}?expected_version=1").status_code == 409
    )
    assert (
        api_client.delete(f"/api/templates/scenarios/{slug}?expected_version=2").status_code == 204
    )
    assert api_client.get(f"/api/adapters/{copied.json()['id']}").status_code == 200
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(UserTemplate)) == 0


def test_builtin_templates_export_all_languages_and_license(api_client: TestClient) -> None:
    value = api_client.get("/api/templates/scenarios/rest-single-request/portable")
    assert value.status_code == 200, value.text
    assert len(value.json()["variants"]) == 3
    assert value.json()["provenance"] and value.json()["license"]
    assert import_template(api_client, value.json()).status_code == 409
    renamed = value.json() | {"name": "Imported built-in"}
    imported = import_template(api_client, renamed)
    assert imported.status_code == 201 and imported.json()["source"] == "imported"
    assert (
        api_client.delete(
            "/api/templates/scenarios/rest-single-request?expected_version=1"
        ).status_code
        == 404
    )


def test_owner_only_export_and_creator_only_template_management(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    created = import_adapter(api_client, package(adapter_type="webhook")).json()
    value = package("template")
    saved = import_template(api_client, value).json()
    with session_factory() as session:
        user = User(username="ordinary", password_hash="unused", role="user", enabled=True)
        session.add(user)
        session.flush()
        session.add(AdapterPermission(adapter_id=created["id"], user_id=user.id, permission="edit"))
        session.commit()
        uid = user.id
    principal = Principal(kind="account", role="user", user_id=uid, username="ordinary")
    api_client.app.dependency_overrides[require_principal] = lambda: principal
    assert (
        api_client.post(f"/api/adapters/{created['id']}/portable-preview", json={}).status_code
        == 403
    )
    assert (
        api_client.post(
            f"/api/adapters/{created['id']}/templates",
            json={"package": value, "sharing_confirmed": True},
        ).status_code
        == 403
    )
    assert api_client.get(f"/api/templates/scenarios/{saved['slug']}").json()["can_manage"] is False
    assert (
        api_client.delete(
            f"/api/templates/scenarios/{saved['slug']}?expected_version=1"
        ).status_code
        == 403
    )
    assert (
        api_client.put(
            f"/api/templates/scenarios/{saved['slug']}",
            json={"package": value, "sharing_confirmed": True, "expected_version": "1"},
        ).status_code
        == 403
    )
    assert api_client.get(f"/api/templates/scenarios/{saved['slug']}/portable").status_code == 200
    api_client.app.dependency_overrides.pop(require_principal)


@pytest.mark.parametrize("fail", [False, True])
def test_managed_file_import_uses_new_objects_target_policy_and_atomic_rollback(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fail: bool,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    monkeypatch.setattr(settings, "artifact_store_root", str(tmp_path / "store"))
    value = package()
    content = b'{"fixture": true}'
    value["input"] = {
        "source_type": "managed_files",
        "included": True,
        "files": [
            {
                "filename": "fixture.json",
                "content_type": "application/json",
                "data_base64": base64.b64encode(content).decode(),
            },
        ],
    }
    target = worker(session_factory, "python")
    if fail:

        def fail_save(*args: Any, **kwargs: Any) -> Any:
            raise HTTPException(status_code=409, detail="injected after input binding")

        monkeypatch.setattr(adapters, "save_version", fail_save)
    result = import_adapter(api_client, value, target)
    assert result.status_code == (409 if fail else 201), result.text
    with session_factory() as session:
        capacity = session.get(ManagedInputCapacity, 1)
        assert capacity is not None and capacity.reserved_bytes == 0
        if fail:
            assert capacity.actual_bytes == 0
            for model in (
                Adapter,
                AdapterInputConfig,
                AdapterVersion,
                ManagedInputArtifact,
                ManagedInputUploadReservation,
            ):
                assert session.scalar(select(func.count()).select_from(model)) == 0
            assert list(LocalFileArtifactStore().iter_objects()) == []
            assert list(LocalFileArtifactStore().iter_parts()) == []
        else:
            assert capacity.actual_bytes == len(content)
            artifact = session.scalar(select(ManagedInputArtifact))
            assert (
                artifact
                and artifact.status == "READY"
                and artifact.retention_mode == "system_default"
            )
            assert artifact.expires_at and artifact.expires_at > datetime.now(UTC)
            with LocalFileArtifactStore().open(artifact.storage_key) as handle:
                assert handle.read() == content
            exported = api_client.post(
                f"/api/adapters/{result.json()['id']}/portable-preview",
                json={"include_files": True},
            )
            assert exported.status_code == 200, exported.text
            assert exported.json()["input"]["files"] == value["input"]["files"]
            assert "storage_key" not in exported.text and "artifact_id" not in exported.text


def test_template_example_files_do_not_require_managed_input(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", False)
    value = package("template")
    value["example_files"] = [{"filename": "sample.json", "data_base64": "e30="}]
    created = import_template(api_client, value)
    assert created.status_code == 201, created.text
    exported = api_client.get(f"/api/templates/scenarios/{created.json()['slug']}/portable")
    assert exported.json()["example_files"][0]["data_base64"] == "e30="


def test_package_size_time_and_trailing_payload_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    from dlr.control.services import portable_zip

    raw = encode_package(PortablePackage.model_validate(package()))
    with pytest.raises(HTTPException):
        decode_package(raw + b"untrusted trailer")
    monkeypatch.setattr(portable_zip, "MAX_SECONDS", -1)
    with pytest.raises(HTTPException):
        decode_package(raw)
    monkeypatch.setattr(portable_zip, "MAX_SECONDS", 5)
    monkeypatch.setattr(portable_zip, "MAX_EXPANDED_BYTES", 50)
    with pytest.raises(HTTPException):
        decode_package(raw)


@pytest.mark.parametrize("field", ["variants", "example_files"])
def test_malformed_manifest_entries_are_rejected(field: str) -> None:
    raw = encode_package(PortablePackage.model_validate(package()))
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(buffer, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            if name == "manifest.json":
                manifest = json.loads(content)
                manifest[field] = ["not-an-object"]
                content = json.dumps(manifest).encode()
            target.writestr(name, content)
    with pytest.raises(HTTPException) as rejected:
        decode_package(buffer.getvalue())
    assert rejected.value.status_code == 422


def test_body_limits_unknown_input_and_validation_do_not_echo_content(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dlr.control.api import portable as portable_api

    monkeypatch.setattr(portable_api, "MAX_ZIP_BYTES", 100)
    assert api_client.post("/api/portable/preview", content=b"x" * 101).status_code == 413
    value = package()
    value["credential_id"] = "customer-secret-never-echo"
    response = import_adapter(api_client, value)
    assert response.status_code == 422 and "customer-secret" not in response.text
    value = package()
    value["variants"][0]["required_parameters"] = ["address"]
    value["variants"][0]["runtime_config"] = {"address": "customer-secret-never-echo"}
    response = import_adapter(api_client, value)
    assert response.status_code == 422 and "customer-secret" not in response.text


def test_save_template_retains_language_and_survives_source_delete(api_client: TestClient) -> None:
    value = package(adapter_type="webhook", language="java")
    source = import_adapter(api_client, value).json()
    preview = api_client.post(
        f"/api/adapters/{source['id']}/portable-preview", json={"as_template": True}
    ).json()
    preview["name"] = "Independent java template"
    saved = api_client.post(
        f"/api/adapters/{source['id']}/templates",
        json={"package": preview, "sharing_confirmed": True},
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["source"] == "saved"
    preview["variants"][0]["language"] = "python"
    assert (
        api_client.post(
            f"/api/adapters/{source['id']}/templates",
            json={"package": preview, "sharing_confirmed": True},
        ).status_code
        == 422
    )
    assert api_client.delete(f"/api/adapters/{source['id']}").status_code == 204
    detail = api_client.get(f"/api/templates/scenarios/{saved.json()['slug']}").json()
    assert [variant["language"] for variant in detail["variants"]] == ["java"]


def test_export_does_not_change_live_webhook_or_serialize_token(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    source = import_adapter(api_client, package(adapter_type="webhook")).json()
    with session_factory() as session:
        hook = session.scalar(
            select(AdapterWebhook).where(AdapterWebhook.adapter_id == source["id"])
        )
        assert hook is not None
        hook.enabled = True
        hook.public_id = "source-entry-must-not-travel"
        session.commit()
    exported = api_client.post(f"/api/adapters/{source['id']}/portable-preview", json={})
    assert exported.status_code == 200 and "source-entry" not in exported.text
    with session_factory() as session:
        hook = session.scalar(
            select(AdapterWebhook).where(AdapterWebhook.adapter_id == source["id"])
        )
        assert (
            hook is not None and hook.enabled and hook.public_id == "source-entry-must-not-travel"
        )
