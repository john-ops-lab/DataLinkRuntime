"""M5.9 Wave C Adapter ownership, ACL and nested-resource authorization."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.control.models import Adapter, AdapterPermission, AdapterVersion, Execution
from dlr.control.security import Principal
from dlr.control.services import adapter_access
from dlr.control.services.accounts import (
    CSRF_COOKIE_NAME,
    bootstrap_default_admin,
)

ACCOUNT_PREFIX = "/__dlr_account"
STARTER_CODE = "def handle(context, input):\n    return input\n"


def account_path(path: str) -> str:
    return f"{ACCOUNT_PREFIX}{path}"


def csrf(client: TestClient) -> str:
    response = client.get(account_path("/api/auth/account/csrf"))
    assert response.status_code == 200, response.text
    value = client.cookies.get(CSRF_COOKIE_NAME)
    assert value is not None
    return value


def account_write(client: TestClient, method: str, path: str, **kwargs: Any) -> Any:
    headers = dict(kwargs.pop("headers", {}))
    headers["X-CSRF-Token"] = csrf(client)
    return client.request(method, account_path(path), headers=headers, **kwargs)


def login_account(app: Any, username: str, password: str) -> TestClient:
    client = TestClient(app)
    response = account_write(
        client,
        "POST",
        "/api/auth/account/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    if response.json()["principal"]["must_change_password"]:
        changed = account_write(
            client,
            "POST",
            "/api/auth/account/change-password",
            json={"current_password": password, "new_password": f"{password}-changed"},
        )
        assert changed.status_code == 200, changed.text
        password = f"{password}-changed"
        response = account_write(
            client,
            "POST",
            "/api/auth/account/login",
            json={"username": username, "password": password},
        )
        assert response.status_code == 200, response.text
    return client


def create_account(
    client: TestClient, username: str, password: str, role: str = "user"
) -> dict[str, Any]:
    response = client.post(
        "/api/users",
        json={"username": username, "password": password, "role": role},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def wave_c_accounts(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> Iterator[dict[str, Any]]:
    with session_factory() as session:
        bootstrap_default_admin(session)
    owner = create_account(api_client, "wave-c-owner", "wave-c-owner-1")
    reader = create_account(api_client, "wave-c-reader", "wave-c-reader-1")
    editor = create_account(api_client, "wave-c-editor", "wave-c-editor-1")
    account_admin = create_account(api_client, "wave-c-admin", "wave-c-admin-1", role="admin")
    yield {
        "owner": owner,
        "reader": reader,
        "editor": editor,
        "account_admin": account_admin,
        "owner_client": login_account(api_client.app, "wave-c-owner", "wave-c-owner-1"),
        "reader_client": login_account(api_client.app, "wave-c-reader", "wave-c-reader-1"),
        "editor_client": login_account(api_client.app, "wave-c-editor", "wave-c-editor-1"),
        "account_admin_client": login_account(api_client.app, "wave-c-admin", "wave-c-admin-1"),
    }


def create_adapter(client: TestClient, name: str, adapter_type: str = "task") -> dict[str, Any]:
    response = client.post(
        "/api/adapters",
        json={"name": name, "language": "python", "adapter_type": adapter_type},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_account_adapter(
    client: TestClient, name: str, adapter_type: str = "task"
) -> dict[str, Any]:
    response = account_write(
        client,
        "POST",
        "/api/adapters",
        json={"name": name, "language": "python", "adapter_type": adapter_type},
    )
    assert response.status_code == 201, response.text
    return response.json()


def configure_version(api_client: TestClient, adapter_id: int) -> dict[str, Any]:
    worker = api_client.post(
        "/api/workers/register",
        json={"name": "wave-c-worker", "capabilities": ["python"]},
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
    )
    assert worker.status_code == 200, worker.text
    worker_id = worker.json()["id"]
    patched = api_client.patch(f"/api/adapters/{adapter_id}", json={"runtime_worker_id": worker_id})
    assert patched.status_code == 200, patched.text
    version = api_client.post(f"/api/adapters/{adapter_id}/versions", json={"code": STARTER_CODE})
    assert version.status_code == 201, version.text
    return version.json()


def grant(
    client: TestClient,
    adapter_id: int,
    user_id: int,
    permission: str,
) -> Any:
    return account_write(
        client,
        "PUT",
        f"/api/adapters/{adapter_id}/permissions/{user_id}",
        json={"permission": permission},
    )


def grant_token(
    client: TestClient,
    adapter_id: int,
    user_id: int,
    permission: str,
) -> Any:
    return client.put(
        f"/api/adapters/{adapter_id}/permissions/{user_id}",
        json={"permission": permission},
    )


def test_owner_system_owned_admin_bypass_and_acl_listing(
    api_client: TestClient,
    wave_c_accounts: dict[str, Any],
) -> None:
    owner_client = wave_c_accounts["owner_client"]
    reader_client = wave_c_accounts["reader_client"]
    owner_id = int(wave_c_accounts["owner"]["id"])
    reader_id = int(wave_c_accounts["reader"]["id"])

    system = create_adapter(api_client, "wave-c-system")
    owned = create_account_adapter(owner_client, "wave-c-owned")
    assert system["owner_user_id"] is None
    assert owned["owner_user_id"] == owner_id

    assert [row["id"] for row in reader_client.get(account_path("/api/adapters")).json()] == []
    guessed = reader_client.get(account_path(f"/api/adapters/{owned['id']}"))
    assert guessed.status_code == 404
    assert guessed.json()["detail"]["code"] == "adapter_not_found"

    shared = grant_token(api_client, int(system["id"]), reader_id, "read")
    assert shared.status_code == 200, shared.text
    assert shared.json()["permission"] == "read"
    visible = reader_client.get(account_path("/api/adapters"))
    assert visible.status_code == 200, visible.text
    assert [row["id"] for row in visible.json()] == [system["id"]]

    owner_permissions = owner_client.get(account_path(f"/api/adapters/{owned['id']}/permissions"))
    assert owner_permissions.status_code == 200, owner_permissions.text
    assert owner_permissions.json() == []
    admin_permissions = api_client.get(f"/api/adapters/{system['id']}/permissions")
    assert admin_permissions.status_code == 200, admin_permissions.text
    assert admin_permissions.json()[0]["user_id"] == reader_id

    account_admin = wave_c_accounts["account_admin_client"]
    assert account_admin.get(account_path("/api/adapters")).status_code == 200
    assert {row["id"] for row in account_admin.get(account_path("/api/adapters")).json()} == {
        system["id"],
        owned["id"],
    }


def test_read_edit_owner_matrix_covers_nested_ids_and_credentials(
    api_client: TestClient,
    wave_c_accounts: dict[str, Any],
) -> None:
    owner_client = wave_c_accounts["owner_client"]
    reader_client = wave_c_accounts["reader_client"]
    editor_client = wave_c_accounts["editor_client"]
    reader_id = int(wave_c_accounts["reader"]["id"])
    editor_id = int(wave_c_accounts["editor"]["id"])
    adapter = create_account_adapter(owner_client, "wave-c-matrix")
    adapter_id = int(adapter["id"])
    version = configure_version(api_client, adapter_id)

    assert grant(owner_client, adapter_id, reader_id, "read").status_code == 200
    assert grant(owner_client, adapter_id, editor_id, "edit").status_code == 200

    reader_path = account_path(f"/api/adapters/{adapter_id}")
    assert reader_client.get(reader_path).status_code == 200
    assert reader_client.get(f"{reader_path}/versions").status_code == 200
    assert reader_client.get(f"{reader_path}/versions/{version['id']}").status_code == 200
    assert reader_client.get(f"{reader_path}/executions").status_code == 200
    assert reader_client.get(f"{reader_path}/credential-bindings").status_code == 200

    forbidden_writes = (
        ("PATCH", reader_path, {"description": "blocked"}),
        ("POST", f"{reader_path}/versions", {"code": "blocked"}),
        ("POST", f"{reader_path}/executions", {}),
        (
            "PUT",
            f"{reader_path}/schedule",
            {"enabled": False, "cron": "* * * * *", "timezone": "UTC", "input": None},
        ),
        (
            "PUT",
            f"{reader_path}/webhook",
            {"enabled": False, "public_id": "blocked-hook", "credential_id": None},
        ),
        (
            "POST",
            f"{reader_path}/ai/assist",
            {
                "message": "blocked",
                "working_copy": {"code": "x", "requirements": "", "runtime_config": {}},
                "recent_messages": [],
            },
        ),
        ("POST", f"{reader_path}/clone", {"name": "blocked-clone"}),
    )
    for method, path, body in forbidden_writes:
        response = account_write(
            reader_client, method, path.removeprefix(ACCOUNT_PREFIX), json=body
        )
        assert response.status_code == 403, (method, path, response.text)
        assert response.json()["detail"]["code"] == "adapter_read_only"

    binding_forbidden = account_write(
        reader_client,
        "PUT",
        f"/api/adapters/{adapter_id}/credential-bindings",
        json={"bindings": []},
    )
    assert binding_forbidden.status_code == 403, binding_forbidden.text
    assert binding_forbidden.json()["detail"]["code"] == "adapter_owner_required"

    assert (
        account_write(
            reader_client,
            "DELETE",
            f"/api/adapters/{adapter_id}",
        ).json()["detail"]["code"]
        == "adapter_delete_forbidden"
    )
    assert (
        reader_client.get(account_path(f"/api/adapters/{adapter_id}/permissions")).json()["detail"][
            "code"
        ]
        == "adapter_permission_management_forbidden"
    )

    changed = account_write(
        editor_client,
        "PATCH",
        f"/api/adapters/{adapter_id}",
        json={"description": "editor update", "run_mode": "schedule"},
    )
    assert changed.status_code == 200, changed.text
    saved = account_write(
        editor_client,
        "POST",
        f"/api/adapters/{adapter_id}/versions",
        json={"code": "def handle(context, input):\n    return {'edited': True}\n"},
    )
    assert saved.status_code == 201, saved.text
    scheduled = account_write(
        editor_client,
        "PUT",
        f"/api/adapters/{adapter_id}/schedule",
        json={"enabled": False, "cron": "* * * * *", "timezone": "UTC", "input": None},
    )
    assert scheduled.status_code == 200, scheduled.text
    binding = account_write(
        editor_client,
        "PUT",
        f"/api/adapters/{adapter_id}/credential-bindings",
        json={"bindings": []},
    )
    assert binding.status_code == 403, binding.text
    assert binding.json()["detail"]["code"] == "adapter_owner_required"

    execution = api_client.post(f"/api/adapters/{adapter_id}/executions", json={})
    assert execution.status_code == 202, execution.text
    execution_id = execution.json()["id"]
    assert editor_client.get(account_path(f"/api/executions/{execution_id}")).status_code == 200
    assert (
        account_write(
            editor_client,
            "POST",
            f"/api/executions/{execution_id}/cancel",
        ).status_code
        == 200
    )
    assert (
        account_write(
            editor_client,
            "DELETE",
            f"/api/adapters/{adapter_id}",
        ).json()["detail"]["code"]
        == "adapter_delete_forbidden"
    )


def test_id_guessing_is_denied_for_every_nested_adapter_path(
    api_client: TestClient,
    wave_c_accounts: dict[str, Any],
) -> None:
    owner_client = wave_c_accounts["owner_client"]
    reader_client = wave_c_accounts["reader_client"]
    adapter = create_account_adapter(owner_client, "wave-c-hidden")
    adapter_id = int(adapter["id"])
    paths = (
        f"/api/adapters/{adapter_id}",
        f"/api/adapters/{adapter_id}/versions",
        f"/api/adapters/{adapter_id}/versions/999999",
        f"/api/adapters/{adapter_id}/executions",
        f"/api/adapters/{adapter_id}/schedule",
        f"/api/adapters/{adapter_id}/webhook",
        f"/api/adapters/{adapter_id}/credential-bindings",
        f"/api/adapters/{adapter_id}/permissions",
    )
    for path in paths:
        response = reader_client.get(account_path(path))
        assert response.status_code == 404, (path, response.text)
        assert response.json()["detail"]["code"] == "adapter_not_found"

    unknown_execution = reader_client.get(account_path("/api/executions/999999"))
    assert unknown_execution.status_code == 404
    assert unknown_execution.json()["detail"]["code"] == "execution_not_found"


def test_public_webhook_bearer_behavior_and_secret_metadata_remain_unchanged(
    api_client: TestClient,
    wave_c_accounts: dict[str, Any],
) -> None:
    owner_client = wave_c_accounts["owner_client"]
    editor_client = wave_c_accounts["editor_client"]
    editor_id = int(wave_c_accounts["editor"]["id"])
    adapter = create_account_adapter(owner_client, "wave-c-secret", adapter_type="webhook")
    credential = api_client.post(
        "/api/credentials",
        json={"name": "wave-c-token", "type": "token", "fields": {"token": "wave-c-secret-value"}},
    )
    assert credential.status_code == 201, credential.text
    credential_id = credential.json()["id"]
    assert grant(owner_client, int(adapter["id"]), editor_id, "edit").status_code == 200

    binding = account_write(
        owner_client,
        "PUT",
        f"/api/adapters/{adapter['id']}/credential-bindings",
        json={"bindings": [{"env_key": "TOKEN", "credential_id": credential_id, "field": "token"}]},
    )
    assert binding.status_code == 200, binding.text
    assert "wave-c-secret-value" not in binding.text
    assert "ciphertext" not in binding.text
    forbidden_global = editor_client.get(account_path(f"/api/credentials/{credential_id}"))
    assert forbidden_global.status_code == 403
    assert forbidden_global.json()["detail"]["code"] == "account_admin_required"

    token_webhook = api_client.get(f"/api/adapters/{adapter['id']}/webhook")
    assert token_webhook.status_code == 200
    assert "wave-c-secret-value" not in token_webhook.text
    assert "ciphertext" not in token_webhook.text


def test_disabled_owner_and_shared_user_keep_rows_and_history(
    api_client: TestClient,
    wave_c_accounts: dict[str, Any],
    session_factory: sessionmaker[Session],
) -> None:
    owner_client = wave_c_accounts["owner_client"]
    owner_id = int(wave_c_accounts["owner"]["id"])
    reader_id = int(wave_c_accounts["reader"]["id"])
    adapter = create_account_adapter(owner_client, "wave-c-disabled")
    adapter_id = int(adapter["id"])
    version = configure_version(api_client, adapter_id)
    execution = api_client.post(f"/api/adapters/{adapter_id}/executions", json={})
    assert execution.status_code == 202, execution.text
    execution_id = execution.json()["id"]
    assert grant(owner_client, adapter_id, reader_id, "read").status_code == 200

    disabled = api_client.patch(f"/api/users/{owner_id}", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    stale = wave_c_accounts["owner_client"].get(account_path("/api/auth/account/me"))
    assert stale.status_code == 401

    with session_factory() as session:
        persisted_adapter = session.get(Adapter, adapter_id)
        assert persisted_adapter is not None
        assert persisted_adapter.owner_user_id == owner_id
        assert (
            session.scalar(
                select(AdapterPermission).where(
                    AdapterPermission.adapter_id == adapter_id,
                    AdapterPermission.user_id == reader_id,
                )
            )
            is not None
        )
        assert session.get(Execution, execution_id) is not None
        assert session.get(AdapterVersion, int(version["id"])) is not None


def test_acl_upsert_is_concurrent_and_unique(
    api_client: TestClient,
    wave_c_accounts: dict[str, Any],
    session_factory: sessionmaker[Session],
) -> None:
    adapter = create_adapter(api_client, "wave-c-concurrent")
    adapter_id = int(adapter["id"])
    target_id = int(wave_c_accounts["reader"]["id"])
    barrier = Barrier(2)
    principal = Principal(kind="superadmin")
    outcomes: list[str] = []

    def worker(permission: str) -> None:
        with session_factory() as session:
            barrier.wait(timeout=5)
            result = adapter_access.set_permission(
                session,
                adapter_id,
                target_id,
                permission,
                principal,  # type: ignore[arg-type]
            )
            outcomes.append(result.permission)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, permission) for permission in ("read", "edit")]
        for future in futures:
            future.result(timeout=10)
    assert sorted(outcomes) == ["edit", "read"]

    with session_factory() as session:
        grants = list(
            session.scalars(
                select(AdapterPermission).where(AdapterPermission.adapter_id == adapter_id)
            ).all()
        )
        assert len(grants) == 1
        with pytest.raises(IntegrityError):
            session.execute(
                insert(AdapterPermission).values(
                    adapter_id=adapter_id,
                    user_id=target_id,
                    permission="read",
                )
            )
        session.rollback()


def test_wave_d_relationship_metadata_grantee_discovery_and_immediate_acl_changes(
    api_client: TestClient,
    wave_c_accounts: dict[str, Any],
) -> None:
    """Wave D exposes only safe relationship/grantee metadata to the UI."""
    owner_client = wave_c_accounts["owner_client"]
    reader_client = wave_c_accounts["reader_client"]
    editor_client = wave_c_accounts["editor_client"]
    admin_client = wave_c_accounts["account_admin_client"]
    reader_id = int(wave_c_accounts["reader"]["id"])
    editor_id = int(wave_c_accounts["editor"]["id"])

    system = create_adapter(api_client, "wave-d-system")
    owned = create_account_adapter(owner_client, "wave-d-owned")
    owned_id = int(owned["id"])
    system_id = int(system["id"])

    owner_row = owner_client.get(account_path("/api/adapters")).json()[0]
    assert owner_row["id"] == owned_id
    assert owner_row["access_level"] == "owner"
    assert owner_row["owner_username"] == wave_c_accounts["owner"]["username"]

    assert grant(owner_client, owned_id, reader_id, "read").status_code == 200
    assert grant(owner_client, owned_id, editor_id, "edit").status_code == 200

    reader_row = reader_client.get(account_path("/api/adapters")).json()[0]
    editor_row = editor_client.get(account_path("/api/adapters")).json()[0]
    assert reader_row["access_level"] == "read"
    assert editor_row["access_level"] == "edit"
    assert reader_row["owner_username"] == wave_c_accounts["owner"]["username"]

    token_system = api_client.get(f"/api/adapters/{system_id}")
    assert token_system.status_code == 200, token_system.text
    assert token_system.json()["access_level"] == "admin"
    assert token_system.json()["owner_username"] is None

    admin_system = admin_client.get(account_path(f"/api/adapters/{system_id}"))
    assert admin_system.status_code == 200, admin_system.text
    assert admin_system.json()["access_level"] == "admin"

    system_candidates = admin_client.get(
        account_path(f"/api/adapters/{system_id}/permission-candidates")
    )
    assert system_candidates.status_code == 200, system_candidates.text
    assert {row["id"] for row in system_candidates.json()} >= {reader_id, editor_id}

    candidates = owner_client.get(account_path(f"/api/adapters/{owned_id}/permission-candidates"))
    assert candidates.status_code == 200, candidates.text
    candidate_rows = candidates.json()
    assert {row["id"] for row in candidate_rows} >= {reader_id, editor_id}
    assert all(set(row) == {"id", "username", "role", "enabled"} for row in candidate_rows)
    assert all(row["role"] == "user" for row in candidate_rows)
    assert "wave-c-owner" not in {row["username"] for row in candidate_rows}
    assert not any(
        secret_key in candidates.text
        for secret_key in ("password", "password_hash", "session", "must_change_password")
    )

    admin_candidates = admin_client.get(
        account_path(f"/api/adapters/{owned_id}/permission-candidates")
    )
    assert admin_candidates.status_code == 200, admin_candidates.text
    assert {row["role"] for row in admin_candidates.json()} == {"admin", "user"}

    for client in (reader_client, editor_client):
        forbidden = client.get(account_path(f"/api/adapters/{owned_id}/permission-candidates"))
        assert forbidden.status_code == 403, forbidden.text
        assert forbidden.json()["detail"]["code"] == "adapter_permission_management_forbidden"

    revoked = account_write(
        owner_client,
        "DELETE",
        f"/api/adapters/{owned_id}/permissions/{reader_id}",
    )
    assert revoked.status_code == 204, revoked.text
    assert reader_client.get(account_path("/api/adapters")).json() == []
    guessed = reader_client.get(account_path(f"/api/adapters/{owned_id}"))
    assert guessed.status_code == 404
    assert guessed.json()["detail"]["code"] == "adapter_not_found"

    assert grant(owner_client, owned_id, reader_id, "edit").status_code == 200
    refreshed = reader_client.get(account_path(f"/api/adapters/{owned_id}"))
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["access_level"] == "edit"


def test_wave_d_business_metadata_and_credential_binding_permissions(
    api_client: TestClient,
    wave_c_accounts: dict[str, Any],
) -> None:
    owner_client = wave_c_accounts["owner_client"]
    editor_client = wave_c_accounts["editor_client"]
    reader_client = wave_c_accounts["reader_client"]
    editor_id = int(wave_c_accounts["editor"]["id"])
    reader_id = int(wave_c_accounts["reader"]["id"])
    adapter = create_account_adapter(owner_client, "wave-d-credential")
    adapter_id = int(adapter["id"])
    credential = api_client.post(
        "/api/credentials",
        json={"name": "wave-d-safe", "type": "token", "fields": {"token": "wave-d-secret"}},
    )
    assert credential.status_code == 201, credential.text
    credential_id = credential.json()["id"]
    assert grant(owner_client, adapter_id, editor_id, "edit").status_code == 200
    assert grant(owner_client, adapter_id, reader_id, "read").status_code == 200

    options = owner_client.get(account_path(f"/api/adapters/{adapter_id}/credential-options"))
    assert options.status_code == 200, options.text
    assert options.json()[0]["id"] == credential_id
    assert "wave-d-secret" not in options.text
    assert "ciphertext" not in options.text

    for client in (editor_client, reader_client):
        forbidden = client.get(account_path(f"/api/adapters/{adapter_id}/credential-options"))
        assert forbidden.status_code == 403, forbidden.text
        assert forbidden.json()["detail"]["code"] == "adapter_owner_required"

    workers = reader_client.get(account_path("/api/workers"))
    assert workers.status_code == 200, workers.text


def test_wave_d_platform_role_does_not_expand_credential_management(
    api_client: TestClient,
    wave_c_accounts: dict[str, Any],
) -> None:
    """Credential management follows platform role, not Adapter ownership."""
    owner_client = wave_c_accounts["owner_client"]
    reader_client = wave_c_accounts["reader_client"]
    admin_client = wave_c_accounts["account_admin_client"]
    reader_id = int(wave_c_accounts["reader"]["id"])
    adapter = create_account_adapter(owner_client, "wave-d-role-boundary")
    adapter_id = int(adapter["id"])
    credential = api_client.post(
        "/api/credentials",
        json={
            "name": "wave-d-role-boundary-credential",
            "type": "token",
            "fields": {"token": "fixture-only-value"},
        },
    )
    assert credential.status_code == 201, credential.text
    credential_id = int(credential.json()["id"])
    assert grant(owner_client, adapter_id, reader_id, "read").status_code == 200

    admin_global = admin_client.get(account_path("/api/credentials"))
    assert admin_global.status_code == 200, admin_global.text
    assert "fixture-only-value" not in admin_global.text
    assert "ciphertext" not in admin_global.text

    for client in (owner_client, reader_client):
        forbidden_global_reads = client.get(account_path(f"/api/credentials/{credential_id}"))
        assert forbidden_global_reads.status_code == 403, forbidden_global_reads.text
        assert forbidden_global_reads.json()["detail"]["code"] == "account_admin_required"

        forbidden_create = account_write(
            client,
            "POST",
            "/api/credentials",
            json={
                "name": "not-allowed",
                "type": "token",
                "fields": {"token": "fixture-only-value"},
            },
        )
        assert forbidden_create.status_code == 403, forbidden_create.text
        assert forbidden_create.json()["detail"]["code"] == "account_admin_required"

        forbidden_update = account_write(
            client,
            "PATCH",
            f"/api/credentials/{credential_id}",
            json={"name": "not-allowed"},
        )
        assert forbidden_update.status_code == 403, forbidden_update.text
        assert forbidden_update.json()["detail"]["code"] == "account_admin_required"

        forbidden_delete = account_write(
            client,
            "DELETE",
            f"/api/credentials/{credential_id}",
        )
        assert forbidden_delete.status_code == 403, forbidden_delete.text
        assert forbidden_delete.json()["detail"]["code"] == "account_admin_required"

    for client in (admin_client, owner_client):
        options = client.get(account_path(f"/api/adapters/{adapter_id}/credential-options"))
        assert options.status_code == 200, options.text
        assert options.json()[0]["id"] == credential_id
        assert "fixture-only-value" not in options.text
        assert "ciphertext" not in options.text

    forbidden_options = reader_client.get(
        account_path(f"/api/adapters/{adapter_id}/credential-options")
    )
    assert forbidden_options.status_code == 403, forbidden_options.text
    assert forbidden_options.json()["detail"]["code"] == "adapter_owner_required"

    saved = account_write(
        owner_client,
        "PUT",
        f"/api/adapters/{adapter_id}/credential-bindings",
        json={"bindings": [{"env_key": "TOKEN", "credential_id": credential_id, "field": "token"}]},
    )
    assert saved.status_code == 200, saved.text
    assert "fixture-only-value" not in saved.text
    metadata = reader_client.get(account_path(f"/api/adapters/{adapter_id}/credential-bindings"))
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()[0]["credential_name"] == "wave-d-role-boundary-credential"
    forbidden_binding_write = account_write(
        reader_client,
        "PUT",
        f"/api/adapters/{adapter_id}/credential-bindings",
        json={"bindings": []},
    )
    assert forbidden_binding_write.status_code == 403, forbidden_binding_write.text
    assert forbidden_binding_write.json()["detail"]["code"] == "adapter_owner_required"

    unshared = create_account_adapter(owner_client, "wave-d-role-boundary-unshared")
    hidden = reader_client.get(
        account_path(f"/api/adapters/{unshared['id']}/credential-bindings")
    )
    assert hidden.status_code == 404, hidden.text
    assert hidden.json()["detail"]["code"] == "adapter_not_found"
