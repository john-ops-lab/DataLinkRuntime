"""ASGI refusal is only a read-only shortcut around existing body and native-work gates."""

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
from dlr.control.app import _EntryBoundary, create_app
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
    assert not ingress.admission_capacity_busy()
    assert ingress._resources is None


@pytest.mark.parametrize("account", [False, True])
def test_known_full_local_budget_refuses_before_inner_or_receive(pool, monkeypatch, account):
    assert pool.buffer.reserve(pool.buffer.limit_bytes - ingress.BODY_RESERVATION + 1)
    before = snapshot(pool)
    monkeypatch.setattr(ingress, "resources", forbidden)
    path = ("/__dlr_account" if account else "") + "/api/ingress/route"
    app = HttpIngressAdmission(forbidden, busy=ingress.admission_capacity_busy)
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
    app = HttpIngressAdmission(forbidden, busy=ingress.admission_capacity_busy)
    assert asyncio.run(invoke(app, scope()))[0]["status"] == 429
    assert snapshot(pool) == before and external.used_bytes == external_before
    assert pool.buffer.used_bytes == 0


@pytest.mark.parametrize("account", [False, True])
def test_native_work_full_refuses_before_body_or_inner(pool, monkeypatch, account):
    for _ in range(8):
        assert pool.work.acquire()
    before = snapshot(pool)
    monkeypatch.setattr(ingress, "resources", forbidden)
    path = ("/__dlr_account" if account else "") + "/api/ingress/route"
    app = HttpIngressAdmission(forbidden, busy=ingress.admission_capacity_busy)
    response = asyncio.run(invoke(app, scope(path)))
    assert response[0]["status"] == 429
    assert (b"retry-after", b"1") in response[0]["headers"]
    assert b'"code":"ingress_capacity_exceeded"' in response[1]["body"]
    assert snapshot(pool) == before and pool.buffer.used_bytes == 0
    assert pool.work.used == 8
    for _ in range(8):
        pool.work.release()
    assert not ingress.admission_capacity_busy()


def test_native_snapshot_neither_reserves_nor_changes_authoritative_limit(pool):
    for _ in range(7):
        assert pool.work.acquire()
    assert pool.work.used == 7 and not ingress.admission_capacity_busy()
    assert pool.work.acquire()
    assert pool.work.saturated and ingress.admission_capacity_busy()
    assert not pool.work.acquire()
    assert pool.work.used == 8
    for _ in range(8):
        pool.work.release()
    with pytest.raises(ValueError):
        pool.work.release()
    assert pool.work.used == 0 and not pool.work.saturated


@pytest.mark.parametrize("global_scope", [False, True])
def test_exact_remaining_bytes_are_not_treated_as_full(pool, global_scope):
    target = (
        pool.buffer.parent.scope(pool.buffer.parent.limit_bytes) if global_scope else pool.buffer
    )
    assert target.reserve(target.limit_bytes - ingress.BODY_RESERVATION)
    before = snapshot(pool)
    assert not ingress.admission_capacity_busy()
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


@pytest.mark.parametrize("gate", ["body", "native"])
def test_app_wiring_is_outside_basehttp_and_matches_original_busy_response(pool, monkeypatch, gate):
    async def run():
        if gate == "body":
            assert pool.buffer.reserve(
                pool.buffer.limit_bytes - (3 * 2 + ingress.REQUEST_METADATA_BYTES) + 1
            )
        else:
            for _ in range(8):
                assert pool.work.acquire()
            from dlr.control.services import subscription_runtime

            monkeypatch.setattr(
                subscription_runtime,
                "get_runtime",
                lambda: SimpleNamespace(http_batch_hint=lambda _: False),
            )
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
                responses.append(
                    await client.post(
                        "/api/ingress/route",
                        content=b"{}",
                        headers={"Idempotency-Key": "id", "Content-Type": "application/json"},
                    )
                )
            if target is app:
                assert snapshot(pool) == before
        shortcut, old = responses
        assert shortcut.status_code == old.status_code == 429
        assert shortcut.content == old.content and shortcut.headers == old.headers
        assert snapshot(pool)[:2] == before[:2]
        assert pool.buffer.parent.peak_bytes == max(
            before[2], 3 * 2 + ingress.REQUEST_METADATA_BYTES
        )

    asyncio.run(run())


@pytest.mark.parametrize("gate", ["body", "native"])
def test_full_ingress_keeps_existing_cookie_auth_and_csrf_for_other_paths(pool, monkeypatch, gate):
    async def run():
        if gate == "body":
            assert pool.buffer.reserve(pool.buffer.limit_bytes - ingress.BODY_RESERVATION + 1)
        else:
            for _ in range(8):
                assert pool.work.acquire()
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
            assert request_scope["dlr_entry_mode"] == "token"
            await ingress._request("route", Request(request_scope, receive), "Bearer token", "id")

        app = HttpIngressAdmission(_EntryBoundary(inner), busy=ingress.admission_capacity_busy)
        task = asyncio.create_task(app(scope(), receive, send))
        try:
            async with asyncio.timeout(5):
                while not started.is_set():
                    await asyncio.sleep(0.001)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert (
                    pool.buffer.used_bytes
                    == 2 + ingress.REQUEST_METADATA_BYTES + ingress.workspace_bytes(2)
                )
                assert (
                    pool.buffer.parent.used_bytes
                    == 2 + ingress.REQUEST_METADATA_BYTES + ingress.workspace_bytes(2)
                )
                assert pool.work.used == 1
                unblock.set()
                while pool.buffer.used_bytes:
                    await asyncio.sleep(0.001)
                assert pool.buffer.parent.used_bytes == 0
                assert pool.work.used == 0
        finally:
            unblock.set()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["single", "status", "group"])
def test_cancelled_native_work_keeps_busy_hint_until_real_future_finishes(pool, monkeypatch, mode):
    async def run():
        started, unblock = threading.Event(), threading.Event()
        for _ in range(7):
            assert pool.work.acquire()

        def blocked(*args):
            started.set()
            assert unblock.wait(5)
            return [(202, {"receipt_id": 1})] if mode == "group" else (202, {"receipt_id": 1})

        monkeypatch.setattr(ingress, "_run_group_sync" if mode == "group" else "_run_sync", blocked)
        from dlr.control.services import subscription_runtime

        monkeypatch.setattr(
            subscription_runtime,
            "get_runtime",
            lambda: SimpleNamespace(http_batch_hint=lambda _: mode == "group"),
        )

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        task = asyncio.create_task(
            ingress._request(
                "route",
                Request(scope(), receive),
                "Bearer token",
                "id",
                receipt_id=1 if mode == "status" else None,
            )
        )
        reservation = (
            64 * 1024
            if mode == "status"
            else 2 + ingress.REQUEST_METADATA_BYTES + ingress.workspace_bytes(2)
        )
        try:
            async with asyncio.timeout(5):
                while not started.is_set():
                    await asyncio.sleep(0.001)
                assert pool.work.used == 8 and ingress.admission_capacity_busy()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert pool.work.used == 8 and ingress.admission_capacity_busy()
                assert pool.buffer.used_bytes == reservation
                response = await invoke(
                    HttpIngressAdmission(forbidden, busy=ingress.admission_capacity_busy), scope()
                )
                assert response[0]["status"] == 429
                assert pool.work.used == 8 and pool.buffer.used_bytes == reservation
                unblock.set()
                while pool.work.used != 7 or pool.buffer.used_bytes:
                    await asyncio.sleep(0.001)
                assert not ingress.admission_capacity_busy()
        finally:
            unblock.set()
            pool.close()
            for _ in range(7):
                pool.work.release()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["single", "status", "group"])
def test_native_submit_failure_returns_work_and_request_reservations(pool, monkeypatch, mode):
    async def run():
        from dlr.control.services import subscription_runtime

        monkeypatch.setattr(
            subscription_runtime,
            "get_runtime",
            lambda: SimpleNamespace(http_batch_hint=lambda _: mode == "group"),
        )
        if mode == "group":
            # Keep ready admission open so this reaches the real executor's
            # closing RuntimeError after the request has shrunk its bytes.
            pool.executor.shutdown(wait=True)
        else:
            pool.close()

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        with pytest.raises(ingress.HTTPException) as raised:
            await ingress._request(
                "route",
                Request(scope(method="GET" if mode == "status" else "POST"), receive),
                "Bearer token",
                "id",
                receipt_id=1 if mode == "status" else None,
            )
        assert raised.value.status_code == 503
        assert raised.value.detail == {
            "code": "ingress_unavailable",
            "message": "Reception is closing",
        }
        assert raised.value.headers == {"Retry-After": "1"}
        assert pool.work.used == 0 and pool.buffer.used_bytes == 0
        assert pool.buffer.parent.used_bytes == 0
        for _ in range(8):
            assert pool.work.acquire()
        assert not pool.work.acquire()
        for _ in range(8):
            pool.work.release()
        for _ in range(128):
            assert pool.requests.acquire(blocking=False)
        assert not pool.requests.acquire(blocking=False)
        for _ in range(128):
            pool.requests.release()
        assert not ingress.admission_capacity_busy()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["single", "status", "group"])
def test_native_failure_returns_work_only_after_the_real_future_ends(pool, monkeypatch, mode):
    async def run():
        from dlr.control.services import subscription_runtime

        monkeypatch.setattr(
            subscription_runtime,
            "get_runtime",
            lambda: SimpleNamespace(http_batch_hint=lambda _: mode == "group"),
        )

        observed_buffer = []

        def failed(*args):
            observed_buffer.append(pool.buffer.used_bytes)
            raise RuntimeError("native work failed")

        monkeypatch.setattr(ingress, "_run_group_sync" if mode == "group" else "_run_sync", failed)

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        with pytest.raises(Exception) as raised:
            await ingress._request(
                "route",
                Request(scope(method="GET" if mode == "status" else "POST"), receive),
                "Bearer token",
                "id",
                receipt_id=1 if mode == "status" else None,
            )
        if mode == "group":
            assert isinstance(raised.value, ingress.HTTPException)
            assert raised.value.status_code == 503
            assert observed_buffer == [
                len(b"{}") + ingress.REQUEST_METADATA_BYTES + ingress.workspace_bytes(2)
            ]
        else:
            assert isinstance(raised.value, RuntimeError)
            assert len(observed_buffer) == 1
        assert pool.work.used == 0 and pool.buffer.used_bytes == 0
        assert not ingress.admission_capacity_busy()

    asyncio.run(run())


@pytest.mark.parametrize("logging_fails", [False, True])
def test_batch_native_failure_is_diagnosable_without_logging_secrets(
    pool, monkeypatch, caplog, logging_fails
):
    async def run():
        from dlr.control.services import subscription_runtime

        monkeypatch.setattr(
            subscription_runtime,
            "get_runtime",
            lambda: SimpleNamespace(http_batch_hint=lambda _: True),
        )

        def failed(*args):
            raise RuntimeError("private-token-and-request-body")

        monkeypatch.setattr(ingress, "_run_group_sync", failed)
        if logging_fails:

            def logging_failure(*args):
                raise RuntimeError("logger failed")

            monkeypatch.setattr(ingress.logger, "error", logging_failure)

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        with pytest.raises(ingress.HTTPException) as raised:
            await ingress._request("route", Request(scope(), receive), "Bearer token", "id")
        assert raised.value.status_code == 503
        assert raised.value.detail["code"] == "ingress_unavailable"
        assert pool.work.used == 0 and pool.buffer.used_bytes == 0

    with caplog.at_level("ERROR", logger="dlr.control.ingress"):
        asyncio.run(run())
    if logging_fails:
        assert not caplog.records
    else:
        assert len(caplog.records) == 1
        assert "RuntimeError" in caplog.text and "failed" in caplog.text
        assert "private-token-and-request-body" not in caplog.text
        assert "Bearer token" not in caplog.text
        assert caplog.records[0].exc_info is None


@pytest.mark.parametrize("mode", ["single", "status", "group"])
def test_authoritative_native_acquire_still_refuses_if_snapshot_was_available(
    pool, monkeypatch, mode
):
    async def run():
        assert not ingress.admission_capacity_busy()
        for _ in range(8):
            assert pool.work.acquire()
        from dlr.control.services import subscription_runtime

        monkeypatch.setattr(
            subscription_runtime,
            "get_runtime",
            lambda: SimpleNamespace(http_batch_hint=lambda _: mode == "group"),
        )

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        with pytest.raises(ingress.HTTPException) as raised:
            await ingress._request(
                "route",
                Request(scope(), receive),
                "Bearer token",
                "id",
                receipt_id=1 if mode == "status" else None,
            )
        assert raised.value.status_code == 429
        assert raised.value.headers == {"Retry-After": "1"}
        assert pool.work.used == 8 and pool.buffer.used_bytes == 0
        for _ in range(8):
            pool.work.release()

    asyncio.run(run())


@pytest.mark.parametrize("account", [False, True])
def test_route_window_refuses_without_reading_and_recovers_without_new_resources(pool, account):
    for _ in range(32):
        assert pool.acquire_route("route")
    assert not pool.acquire_route("route")
    before = snapshot(pool)
    app = HttpIngressAdmission(forbidden, busy=forbidden, busy_route=ingress.route_capacity_busy)
    path = ("/__dlr_account" if account else "") + "/api/ingress/route"
    assert asyncio.run(invoke(app, scope(path)))[0]["status"] == 429
    assert snapshot(pool) == before and pool._route_requests == {"route": 32}
    assert not ingress.route_capacity_busy("other")
    for _ in range(32):
        pool.release_route("route")
    assert not ingress.route_capacity_busy("route") and not pool._route_requests
