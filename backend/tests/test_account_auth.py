"""M5.9 Wave A account identity, session and security contract tests."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

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
    LOGIN_THROTTLE_BASE_DELAY_SECONDS,
    LOGIN_THROTTLE_MAX_FAILURES,
    LOGIN_THROTTLE_WINDOW_SECONDS,
    SESSION_COOKIE_NAME,
    LoginThrottle,
    account_login_throttle,
    bootstrap_default_admin,
    login_throttle_key,
    verify_password,
)

ACCOUNT_PREFIX = "/__dlr_account"


def account_path(path: str) -> str:
    return f"{ACCOUNT_PREFIX}{path}"


@pytest.fixture()
def account_client(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> Iterator[TestClient]:
    account_login_throttle.reset()
    with session_factory() as session:
        bootstrap_default_admin(session)
    try:
        yield api_client
    finally:
        account_login_throttle.reset()


def csrf(client: TestClient) -> str:
    response = client.get(account_path("/api/auth/account/csrf"))
    assert response.status_code == 200
    value = client.cookies.get(CSRF_COOKIE_NAME)
    assert value is not None
    return value


def login(
    client: TestClient,
    *,
    password: str = DEFAULT_ADMIN_PASSWORD,
) -> object:
    token = csrf(client)
    response = client.post(
        account_path("/api/auth/account/login"),
        json={"username": DEFAULT_ADMIN_USERNAME, "password": password},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200, response.text
    return response


def test_default_admin_bootstrap_is_idempotent_and_hashed(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        bootstrap_default_admin(session)
        first = session.scalar(select(User).where(User.username == DEFAULT_ADMIN_USERNAME))
        assert first is not None
        first_hash = first.password_hash
        bootstrap_default_admin(session)
        second = session.scalar(select(User).where(User.username == DEFAULT_ADMIN_USERNAME))
        assert second is not None
        assert second.id == first.id
        assert second.password_hash == first_hash
        assert second.password_hash != DEFAULT_ADMIN_PASSWORD
        assert second.password_hash.startswith("scrypt$")
        assert verify_password(DEFAULT_ADMIN_PASSWORD, second.password_hash)
        assert second.role == "admin"
        assert second.enabled is True
        assert second.must_change_password is True
        assert session.scalar(select(User).where(User.role == "superadmin")) is None

    # The legacy Token surface remains independent of the account bootstrap.
    response = api_client.get("/api/auth/admin/verify")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_account_login_uses_cookie_session_and_forced_change_gate(
    account_client: TestClient,
) -> None:
    response = login(account_client)
    body = response.json()
    assert body["principal"] == {
        "id": 1,
        "username": "admin",
        "role": "admin",
        "enabled": True,
        "must_change_password": True,
    }
    assert "admin123" not in response.text
    assert "scrypt$" not in response.text
    set_cookie = response.headers["set-cookie"]
    assert "dlr_account_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Max-Age=28800" in set_cookie
    assert account_client.cookies.get(SESSION_COOKIE_NAME) is not None

    me = account_client.get(account_path("/api/auth/account/me"))
    assert me.status_code == 200
    assert me.json()["principal"]["must_change_password"] is True

    blocked = account_client.get(account_path("/api/adapters"))
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "account_password_change_required"


def test_password_change_invalidates_old_session_and_clears_gate(
    account_client: TestClient,
) -> None:
    login(account_client)
    old_session = account_client.cookies.get(SESSION_COOKIE_NAME)
    assert old_session is not None
    token = csrf(account_client)
    changed = account_client.post(
        account_path("/api/auth/account/change-password"),
        json={"current_password": DEFAULT_ADMIN_PASSWORD, "new_password": "new-admin-123"},
        headers={"X-CSRF-Token": token},
    )
    assert changed.status_code == 200, changed.text
    assert "new-admin-123" not in changed.text

    stale = TestClient(account_client.app)
    stale_response = stale.get(
        account_path("/api/auth/account/me"),
        cookies={SESSION_COOKIE_NAME: old_session},
    )
    assert stale_response.status_code == 401
    assert stale_response.json()["detail"]["code"] == "account_session_required"

    relogin = login(account_client, password="new-admin-123")
    assert relogin.json()["principal"]["must_change_password"] is False
    allowed = account_client.get(account_path("/api/adapters"))
    assert allowed.status_code == 200
    worker_list = account_client.get(account_path("/api/workers"))
    assert worker_list.status_code == 200


def test_logout_invalidates_server_session(account_client: TestClient) -> None:
    login(account_client, password=DEFAULT_ADMIN_PASSWORD)
    old_session = account_client.cookies.get(SESSION_COOKIE_NAME)
    assert old_session is not None
    token = csrf(account_client)
    response = account_client.post(
        account_path("/api/auth/account/logout"),
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 200
    assert account_client.cookies.get(SESSION_COOKIE_NAME) is None

    stale = TestClient(account_client.app)
    assert (
        stale.get(
            account_path("/api/auth/account/me"),
            cookies={SESSION_COOKIE_NAME: old_session},
        ).status_code
        == 401
    )


def test_superadmin_reset_invalidates_sessions_and_forces_next_change(
    account_client: TestClient,
) -> None:
    login(account_client, password=DEFAULT_ADMIN_PASSWORD)
    old_session = account_client.cookies.get(SESSION_COOKIE_NAME)
    assert old_session is not None

    reset = account_client.post(
        "/api/auth/account/reset",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        json={"username": "admin", "new_password": "reset-admin-123"},
    )
    assert reset.status_code == 200, reset.text
    assert "reset-admin-123" not in reset.text

    stale = TestClient(account_client.app)
    assert (
        stale.get(
            account_path("/api/auth/account/me"),
            cookies={SESSION_COOKIE_NAME: old_session},
        ).status_code
        == 401
    )
    response = login(account_client, password="reset-admin-123")
    assert response.json()["principal"]["must_change_password"] is True


def test_disabled_account_is_rejected_on_login_and_every_session_request(
    account_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    login(account_client, password=DEFAULT_ADMIN_PASSWORD)
    old_session = account_client.cookies.get(SESSION_COOKIE_NAME)
    assert old_session is not None
    with session_factory() as session:
        user = session.scalar(select(User).where(User.username == DEFAULT_ADMIN_USERNAME))
        assert user is not None
        user.enabled = False
        session.commit()

    stale = TestClient(account_client.app)
    response = stale.get(
        account_path("/api/auth/account/me"),
        cookies={SESSION_COOKIE_NAME: old_session},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "account_session_required"

    token = csrf(account_client)
    login_response = account_client.post(
        account_path("/api/auth/account/login"),
        json={"username": DEFAULT_ADMIN_USERNAME, "password": DEFAULT_ADMIN_PASSWORD},
        headers={"X-CSRF-Token": token},
    )
    assert login_response.status_code == 403
    assert login_response.json()["detail"]["code"] == "account_disabled"


def test_account_writes_require_csrf_but_token_reset_is_header_authenticated(
    account_client: TestClient,
) -> None:
    csrf(account_client)
    missing = account_client.post(
        account_path("/api/auth/account/login"),
        json={"username": "admin", "password": DEFAULT_ADMIN_PASSWORD},
    )
    assert missing.status_code == 403
    assert missing.json()["detail"]["code"] == "account_csrf_invalid"


def test_entry_boundary_does_not_trust_cross_entry_credentials(
    account_client: TestClient,
) -> None:
    login(account_client)

    # Account cookies cannot authenticate the legacy Token port.
    token_entry = account_client.get("/api/adapters", headers={"Authorization": ""})
    assert token_entry.status_code == 401
    assert token_entry.json()["detail"]["code"] == "unauthorized"

    # A forged Authorization header cannot turn the account port into the
    # superadmin entry; its server-side marker still requires the account Session.
    account_entry = account_client.get(
        account_path("/api/adapters"),
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert account_entry.status_code == 403
    assert account_entry.json()["detail"]["code"] == "account_password_change_required"

    # Account endpoints are not reachable through the Token entry marker.
    wrong_entry = account_client.get("/api/auth/account/me")
    assert wrong_entry.status_code == 401
    assert wrong_entry.json()["detail"]["code"] == "account_entry_required"


def test_account_login_throttle_ignores_spoofed_forwarded_source_headers(
    account_client: TestClient,
) -> None:
    token = csrf(account_client)
    headers = {"X-CSRF-Token": token, "Content-Type": "application/json"}
    for attempt in range(LOGIN_THROTTLE_MAX_FAILURES):
        response = account_client.post(
            account_path("/api/auth/account/login"),
            headers={
                **headers,
                "X-Forwarded-For": f"198.51.100.{attempt + 1}",
                "X-Real-IP": f"203.0.113.{attempt + 1}",
            },
            json={"username": "throttle-probe", "password": "wrong-password"},
        )
        assert response.status_code == 401
        assert "wrong-password" not in response.text

    blocked = account_client.post(
        account_path("/api/auth/account/login"),
        headers={
            **headers,
            "X-Forwarded-For": "192.0.2.200",
            "X-Real-IP": "192.0.2.200",
        },
        json={"username": "throttle-probe", "password": "wrong-password"},
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert "wrong-password" not in blocked.text
    assert "scrypt$" not in blocked.text


def test_login_throttle_covers_concurrency_window_and_success_reset() -> None:
    class FakeClock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = FakeClock()
    throttle = LoginThrottle(clock=clock)
    key = login_throttle_key("trusted-proxy", "Admin")
    barrier = Barrier(LOGIN_THROTTLE_MAX_FAILURES + 3)

    def reserve() -> object:
        barrier.wait()
        return throttle.begin(key)

    with ThreadPoolExecutor(max_workers=LOGIN_THROTTLE_MAX_FAILURES + 3) as pool:
        decisions = list(pool.map(lambda _: reserve(), range(LOGIN_THROTTLE_MAX_FAILURES + 3)))

    permits = [decision.permit for decision in decisions if decision.permit is not None]
    blocked = [decision for decision in decisions if decision.permit is None]
    assert len(permits) == LOGIN_THROTTLE_MAX_FAILURES
    assert len(blocked) == 3
    assert all(decision.retry_after_seconds is not None for decision in blocked)
    for permit in permits:
        throttle.record_failure(permit)
    assert throttle.begin(key).permit is None

    clock.now = LOGIN_THROTTLE_WINDOW_SECONDS + 0.001
    reopened = throttle.begin(key).permit
    assert reopened is not None
    throttle.record_failure(reopened)

    first = throttle.begin(key).permit
    assert first is not None
    assert first.delay_seconds == LOGIN_THROTTLE_BASE_DELAY_SECONDS * 2
    throttle.record_success(first)
    # A stale concurrent failure cannot recreate the cleared bucket.
    throttle.record_failure(reopened)
    fresh = throttle.begin(key).permit
    assert fresh is not None
    assert fresh.delay_seconds == LOGIN_THROTTLE_BASE_DELAY_SECONDS
