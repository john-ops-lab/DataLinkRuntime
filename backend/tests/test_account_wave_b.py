"""M5.9 Wave B account management, role gates and lifecycle contracts."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from conftest import ADMIN_TOKEN
from dlr.control.models.account import User
from dlr.control.services.accounts import (
    CSRF_COOKIE_NAME,
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USERNAME,
    SESSION_COOKIE_NAME,
    bootstrap_default_admin,
)

ACCOUNT_PREFIX = "/__dlr_account"


def account_path(path: str) -> str:
    return f"{ACCOUNT_PREFIX}{path}"


def csrf(client: TestClient) -> str:
    response = client.get(account_path("/api/auth/account/csrf"))
    assert response.status_code == 200, response.text
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token is not None
    return token


def login(client: TestClient, username: str, password: str) -> TestClient:
    token = csrf(client)
    response = client.post(
        account_path("/api/auth/account/login"),
        json={"username": username, "password": password},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200, response.text
    return client


@pytest.fixture()
def account_admin(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> Iterator[TestClient]:
    with session_factory() as session:
        bootstrap_default_admin(session)
    login(api_client, DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
    token = csrf(api_client)
    changed = api_client.post(
        account_path("/api/auth/account/change-password"),
        json={"current_password": DEFAULT_ADMIN_PASSWORD, "new_password": "admin-wave-b-1"},
        headers={"X-CSRF-Token": token},
    )
    assert changed.status_code == 200, changed.text
    login(api_client, DEFAULT_ADMIN_USERNAME, "admin-wave-b-1")
    yield api_client


def create_account(
    client: TestClient,
    *,
    username: str,
    password: str,
    role: str = "user",
) -> dict[str, object]:
    token = csrf(client)
    response = client.post(
        account_path("/api/users"),
        json={"username": username, "password": password, "role": role},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert password not in response.text
    assert "password_hash" not in response.text
    assert "scrypt$" not in response.text
    assert "session" not in response.text.lower()
    return body


def test_admin_can_manage_users_without_secret_echo(account_admin: TestClient) -> None:
    created = create_account(
        account_admin,
        username="wave-b-user",
        password="wave-b-user-1",
    )
    assert created["role"] == "user"
    assert created["enabled"] is True
    assert created["must_change_password"] is True

    listed = account_admin.get(account_path("/api/users"))
    assert listed.status_code == 200, listed.text
    assert {row["username"] for row in listed.json()} >= {"admin", "wave-b-user"}
    assert "password_hash" not in listed.text

    user_id = int(created["id"])
    renamed = account_admin.patch(
        account_path(f"/api/users/{user_id}"),
        json={"username": "wave-b-renamed"},
        headers={"X-CSRF-Token": csrf(account_admin)},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["username"] == "wave-b-renamed"

    promoted = account_admin.patch(
        account_path(f"/api/users/{user_id}"),
        json={"role": "admin"},
        headers={"X-CSRF-Token": csrf(account_admin)},
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "admin"


def test_disable_reset_and_role_change_invalidate_old_sessions(
    account_admin: TestClient,
) -> None:
    created = create_account(
        account_admin,
        username="wave-b-lifecycle",
        password="wave-b-old-1",
    )
    user_id = int(created["id"])
    user_client = TestClient(account_admin.app)
    login(user_client, "wave-b-lifecycle", "wave-b-old-1")
    old_session = user_client.cookies.get(SESSION_COOKIE_NAME)
    assert old_session is not None

    disabled = account_admin.patch(
        account_path(f"/api/users/{user_id}"),
        json={"enabled": False},
        headers={"X-CSRF-Token": csrf(account_admin)},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False
    stale = TestClient(account_admin.app)
    response = stale.get(
        account_path("/api/auth/account/me"),
        cookies={SESSION_COOKIE_NAME: old_session},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "account_session_required"
    assert (
        user_client.post(
            account_path("/api/auth/account/login"),
            json={"username": "wave-b-lifecycle", "password": "wave-b-old-1"},
            headers={"X-CSRF-Token": csrf(user_client)},
        ).json()["detail"]["code"]
        == "account_disabled"
    )

    enabled = account_admin.patch(
        account_path(f"/api/users/{user_id}"),
        json={"enabled": True},
        headers={"X-CSRF-Token": csrf(account_admin)},
    )
    assert enabled.status_code == 200, enabled.text
    login(user_client, "wave-b-lifecycle", "wave-b-old-1")
    old_session = user_client.cookies.get(SESSION_COOKIE_NAME)
    assert old_session is not None

    reset = account_admin.post(
        account_path(f"/api/users/{user_id}/reset-password"),
        json={"new_password": "wave-b-new-1"},
        headers={"X-CSRF-Token": csrf(account_admin)},
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["must_change_password"] is True
    assert "wave-b-new-1" not in reset.text
    assert (
        stale.get(
            account_path("/api/auth/account/me"),
            cookies={SESSION_COOKIE_NAME: old_session},
        ).status_code
        == 401
    )
    login(user_client, "wave-b-lifecycle", "wave-b-new-1")
    old_session = user_client.cookies.get(SESSION_COOKIE_NAME)
    assert old_session is not None

    role_changed = account_admin.patch(
        account_path(f"/api/users/{user_id}"),
        json={"role": "admin"},
        headers={"X-CSRF-Token": csrf(account_admin)},
    )
    assert role_changed.status_code == 200, role_changed.text
    assert (
        stale.get(
            account_path("/api/auth/account/me"),
            cookies={SESSION_COOKIE_NAME: old_session},
        ).status_code
        == 401
    )


def test_last_enabled_admin_cannot_be_disabled_or_demoted(account_admin: TestClient) -> None:
    admin_id = int(account_admin.get(account_path("/api/users")).json()[0]["id"])
    for payload in ({"enabled": False}, {"role": "user"}):
        response = account_admin.patch(
            account_path(f"/api/users/{admin_id}"),
            json=payload,
            headers={"X-CSRF-Token": csrf(account_admin)},
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "last_admin_protected"

    second = create_account(
        account_admin,
        username="wave-b-second-admin",
        password="wave-b-second-1",
        role="admin",
    )
    demoted = account_admin.patch(
        account_path(f"/api/users/{admin_id}"),
        json={"role": "user"},
        headers={"X-CSRF-Token": csrf(account_admin)},
    )
    assert demoted.status_code == 200, demoted.text
    token_client = TestClient(account_admin.app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    final_admin_disable = token_client.patch(
        f"/api/users/{int(second['id'])}",
        json={"enabled": False},
    )
    assert final_admin_disable.status_code == 409
    assert final_admin_disable.json()["detail"]["code"] == "last_admin_protected"


def test_account_user_cannot_bypass_management_but_reaches_acl_checked_business_api(
    account_admin: TestClient,
) -> None:
    created = create_account(
        account_admin,
        username="wave-b-ordinary",
        password="wave-b-ordinary-1",
    )
    user_id = int(created["id"])
    user_client = TestClient(account_admin.app)
    login(user_client, "wave-b-ordinary", "wave-b-ordinary-1")
    change = user_client.post(
        account_path("/api/auth/account/change-password"),
        json={"current_password": "wave-b-ordinary-1", "new_password": "wave-b-ordinary-2"},
        headers={"X-CSRF-Token": csrf(user_client)},
    )
    assert change.status_code == 200, change.text
    login(user_client, "wave-b-ordinary", "wave-b-ordinary-2")

    assert user_client.get(account_path("/api/users")).json()["detail"]["code"] == (
        "account_admin_required"
    )
    other = int(account_admin.get(account_path("/api/users")).json()[0]["id"])
    own = user_client.get(account_path(f"/api/users/{user_id}"))
    assert own.status_code == 200, own.text
    other_response = user_client.get(account_path(f"/api/users/{other}"))
    assert other_response.status_code == 403
    assert other_response.json()["detail"]["code"] == "account_user_self_only"
    role_attempt = user_client.patch(
        account_path(f"/api/users/{user_id}"),
        json={"role": "admin"},
        headers={"X-CSRF-Token": csrf(user_client)},
    )
    assert role_attempt.status_code == 403
    assert role_attempt.json()["detail"]["code"] == "account_user_profile_forbidden"

    for path in (
        "/api/workers",
        "/api/credentials",
        "/api/package-sources",
        "/api/knowledge-sources",
        "/api/ai/settings",
    ):
        response = user_client.get(account_path(path))
        assert response.status_code == 403, (path, response.text)
        assert response.json()["detail"]["code"] == "account_admin_required"

    created_adapter = user_client.post(
        account_path("/api/adapters"),
        json={"name": "wave-c-user-adapter", "language": "python", "adapter_type": "task"},
        headers={"X-CSRF-Token": csrf(user_client)},
    )
    assert created_adapter.status_code == 201, created_adapter.text
    assert created_adapter.json()["owner_user_id"] == user_id


def test_account_write_csrf_is_uniform_and_token_superadmin_is_compatible(
    account_admin: TestClient,
) -> None:
    missing = account_admin.post(
        account_path("/api/users"),
        json={"username": "csrf-missing", "password": "csrf-missing-1", "role": "user"},
    )
    assert missing.status_code == 403
    assert missing.json()["detail"]["code"] == "account_csrf_invalid"

    token_client = TestClient(account_admin.app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    created = token_client.post(
        "/api/users",
        json={"username": "token-created", "password": "token-created-1", "role": "user"},
    )
    assert created.status_code == 201, created.text
    assert token_client.get("/api/users").status_code == 200


def test_default_admin_remains_account_role_only(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        bootstrap_default_admin(session)
        admin = session.scalar(select(User).where(User.username == DEFAULT_ADMIN_USERNAME))
        assert admin is not None
        assert admin.role == "admin"
        assert admin.enabled is True
        assert session.scalar(select(User).where(User.role == "superadmin")) is None

    assert api_client.get("/api/users").status_code == 200
