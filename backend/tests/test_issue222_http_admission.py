"""ASGI refusal is only a read-only shortcut around the existing body gate."""

import asyncio
import threading
from types import SimpleNamespace
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends
from starlette.requests import Request

from dlr.control import db, security
from dlr.control.api import ingress
from dlr.control.app import create_app
from dlr.control.services.http_ingress_admission import HttpIngressAdmission
from dlr.control.services.subscription_source import ApplicationBufferBudget


def forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("The shortcut must not enter or initialize the original path")


@pytest.fixture
def pool(monkeypatch):
    result = ingress.IngressResources(buffer=ApplicationBufferBudget(), session_factory=forbidden)
    monkeypatch.setattr(ingress, "_resources", result)
    yield result
    result.close()


def snapshot(pool):
    return pool.buffer.used_bytes, pool.buffer.parent.used_bytes, pool.buffer.parent.peak_bytes


def scope(path="/api/ingress/route", *, method="POST", kind="http"):
    return {
        "type": kind,
        "method": method,
        "path": path,
        "headers": [(b"content-type", b"application/json")],
    }


async def invoke(app, request_scope):
    messages = []

    async def send(message):
        messages.append(message)

    await app(request_scope, forbidden, send)
    return messages


def test_uninitialized_capacity_hint_does_not_initialize(monkeypatch):
    monkeypatch.setattr(ingress, "_resources", None)
    monkeypatch.setattr(ingress, "resources", forbidden)
    assert not ingress.body_capacity_busy()
    assert ingress._resources is None


@pytest.mark.parametrize("account", [False, True])
def test_known_full_local_budget_refuses_before_inner_or_receive(pool, monkeypatch, account):
    assert pool.buffer.reserve(25 * ingress.BODY_RESERVATION)
    before = snapshot(pool)
    monkeypatch.setattr(ingress, "resources", forbidden)
    path = ("/__dlr_account" if account else "") + "/api/ingress/route"
    app = HttpIngressAdmission(forbidden, busy=ingress.body_capacity_busy)
    response = asyncio.run(invoke(app, scope(path)))
    assert response == [
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-length", b"88"),
                (b"content-type", b"application/json"),
                (b"retry-after", b"1"),
            ],
        },
        {
            "type": "http.response.body",
            "body": (
                b'{"detail":{"code":"ingress_capacity_exceeded",'
                b'"message":"Reception resources are busy"}}'
            ),
        },
    ]
    assert snapshot(pool) == before


def test_global_scope_can_refuse_without_changing_other_owner(pool):
    external = pool.buffer.parent.scope(pool.buffer.parent.limit_bytes)
    assert external.reserve(external.limit_bytes - ingress.BODY_RESERVATION + 1)
    before, external_before = snapshot(pool), external.used_bytes
    app = HttpIngressAdmission(forbidden, busy=ingress.body_capacity_busy)
    assert asyncio.run(invoke(app, scope()))[0]["status"] == 429
    assert snapshot(pool) == before and external.used_bytes == external_before
    assert pool.buffer.used_bytes == 0


@pytest.mark.parametrize("global_scope", [False, True])
def test_exact_remaining_bytes_are_not_treated_as_full(pool, global_scope):
    target = (
        pool.buffer.parent.scope(pool.buffer.parent.limit_bytes) if global_scope else pool.buffer
    )
    assert target.reserve(target.limit_bytes - ingress.BODY_RESERVATION)
    before = snapshot(pool)
    assert not ingress.body_capacity_busy()
    assert snapshot(pool) == before


@pytest.mark.parametrize(
    ("path", "method", "kind"),
    [
        ("/api/ingress/route", "GET", "http"),
        ("/api/ingress/route", "OPTIONS", "http"),
        ("/api/ingress/route/receipts/1", "GET", "http"),
        ("/api/ingress/route/receipts/1", "POST", "http"),
        ("/api/ingress/", "POST", "http"),
        ("/api/ingress/route/", "POST", "http"),
        ("/api/ingress/route/other", "POST", "http"),
        ("/api/ingress//route", "POST", "http"),
        ("/api/hooks/route", "POST", "http"),
        ("/api/adapters", "GET", "http"),
        ("/__dlr_account/api/adapters", "POST", "http"),
        ("/__dlr_accountx/api/ingress/route", "POST", "http"),
        ("/__dlr_account/__dlr_account/api/ingress/route", "POST", "http"),
        ("/api/ingress/route", "POST", "websocket"),
        ("", "POST", "lifespan"),
    ],
)
def test_other_paths_and_protocols_pass_through_untouched(path, method, kind):
    request_scope = scope(path, method=method, kind=kind)
    seen = []

    async def inner(actual_scope, receive, send):
        assert actual_scope is request_scope and receive is forbidden
        seen.append(dict(actual_scope))

    app = HttpIngressAdmission(inner, busy=forbidden)
    assert asyncio.run(invoke(app, request_scope)) == []
    assert seen == [request_scope]


def test_available_target_is_not_rewritten_or_received_by_shortcut():
    request_scope = scope("/__dlr_account/api/ingress/route")
    seen = []

    async def inner(actual_scope, receive, send):
        assert actual_scope is request_scope and receive is forbidden
        seen.append(dict(actual_scope))

    app = HttpIngressAdmission(inner, busy=lambda: False)
    asyncio.run(invoke(app, request_scope))
    assert seen == [request_scope]


def test_app_wiring_is_outside_basehttp_and_matches_original_busy_response(pool):
    async def run():
        assert pool.buffer.reserve(25 * ingress.BODY_RESERVATION)
        before = snapshot(pool)
        app = create_app()
        assert app.user_middleware[0].cls is HttpIngressAdmission
        original = create_app()
        original.user_middleware = [
            item for item in original.user_middleware if item.cls is not HttpIngressAdmission
        ]
        responses = []
        for target in [app, original]:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=target), base_url="http://test"
            ) as client:
                responses.append(await client.post("/api/ingress/route", content=b"not JSON"))
        shortcut, old = responses
        assert shortcut.status_code == old.status_code == 429
        assert shortcut.content == old.content and shortcut.headers == old.headers
        assert snapshot(pool) == before

    asyncio.run(run())


def test_full_ingress_keeps_existing_cookie_auth_and_csrf_for_other_paths(pool, monkeypatch):
    async def run():
        assert pool.buffer.reserve(25 * ingress.BODY_RESERVATION)
        app = create_app()
        app.dependency_overrides[db.get_session] = lambda: object()
        seen = []

        def find_session(session, token):
            seen.append(token)
            if token != "valid-session":
                return None
            return SimpleNamespace(
                user=SimpleNamespace(id=1, role="user", username="user", must_change_password=False)
            )

        monkeypatch.setattr(security, "find_session", find_session)

        @app.post("/api/admission-auth-probe")
        def protected(
            principal: Annotated[security.Principal, Depends(security.require_principal)],
        ):
            return {"kind": principal.kind}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.cookies.set("dlr_account_session", "valid-session")
            client.cookies.set("dlr_account_csrf", "csrf-value")
            path = "/__dlr_account/api/admission-auth-probe"
            response = await client.post(path)
            assert response.status_code == 403
            assert response.json()["detail"]["code"] == "account_csrf_invalid"
            assert not seen
            response = await client.post(path, headers={"X-CSRF-Token": "csrf-value"})
            assert response.status_code == 200 and response.json() == {"kind": "account"}
            assert seen == ["valid-session"]
            client.cookies.set("dlr_account_session", "expired-session")
            response = await client.post(path, headers={"X-CSRF-Token": "csrf-value"})
            assert response.status_code == 401
            assert response.json()["detail"]["code"] == "account_session_required"
            response = await client.get("/api/auth/admin/verify")
            assert response.status_code == 401
            assert response.json()["detail"]["code"] == "unauthorized"

    asyncio.run(run())


def test_cancelling_passed_request_retains_native_body_reservation(pool, monkeypatch):
    async def run():
        started, unblock = threading.Event(), threading.Event()

        def blocked(*args):
            started.set()
            assert unblock.wait(5)
            return 202, {"receipt_id": 1}

        monkeypatch.setattr(ingress, "_run_sync", blocked)
        from dlr.control.services import subscription_runtime

        monkeypatch.setattr(
            subscription_runtime,
            "get_runtime",
            lambda: SimpleNamespace(http_batch_hint=lambda _: False),
        )

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message):
            pass

        async def inner(request_scope, receive, send):
            await ingress._request("route", Request(request_scope, receive), "Bearer token", "id")

        app = HttpIngressAdmission(inner, busy=ingress.body_capacity_busy)
        task = asyncio.create_task(app(scope(), receive, send))
        try:
            async with asyncio.timeout(5):
                while not started.is_set():
                    await asyncio.sleep(0.001)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert pool.buffer.used_bytes == ingress.BODY_RESERVATION
                assert pool.buffer.parent.used_bytes == ingress.BODY_RESERVATION
                unblock.set()
                while pool.buffer.used_bytes:
                    await asyncio.sleep(0.001)
                assert pool.buffer.parent.used_bytes == 0
        finally:
            unblock.set()

    asyncio.run(run())
