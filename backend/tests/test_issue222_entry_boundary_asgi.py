"""Pure entry-boundary contracts; no body reads, database, or native work."""

import asyncio
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.types import Message, Scope

from dlr.control import app as app_module
from dlr.control.api import ingress
from dlr.control.services.http_ingress_admission import HttpIngressAdmission
from dlr.control.services.subscription_source import ApplicationBufferBudget


def forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("Entry checks must not read the body or acquire native resources")


def scope(path: str, *, method: str = "GET", **extra: Any) -> Scope:
    return {
        "type": "http",
        "path": path,
        "method": method,
        "headers": [],
        "root_path": "/proxy",
        "query_string": b"query=unchanged",
        **extra,
    }


async def invoke(boundary: Any, actual_scope: Scope) -> list[Message]:
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    await boundary(actual_scope, forbidden, send)
    return messages


@pytest.mark.parametrize(
    ("path", "raw_path", "mode", "expected_path", "expected_raw"),
    [
        ("/api/adapters", b"/api/adapters", "token", "/api/adapters", b"/api/adapters"),
        ("/__dlr_accountx/api/adapters", None, "token", "/__dlr_accountx/api/adapters", None),
        ("/__dlr_account/", b"/__dlr_account/", "account", "/", b"/"),
        (
            "/__dlr_account/api/%value",
            b"/__dlr_account/api/%25value",
            "account",
            "/api/%value",
            b"/api/%25value",
        ),
        ("/__dlr_account/api/adapters", None, "account", "/api/adapters", None),
        ("/__dlr_account/api/adapters", "not-bytes", "account", "/api/adapters", "not-bytes"),
        (
            "/__dlr_account/__dlr_account/api/adapters",
            b"/__dlr_account/__dlr_account/api/adapters",
            "account",
            "/__dlr_account/api/adapters",
            b"/__dlr_account/api/adapters",
        ),
    ],
)
def test_trusted_prefix_only_rewrites_once_in_place(
    monkeypatch, path, raw_path, mode, expected_path, expected_raw
):
    actual_scope = scope(path, raw_path=raw_path, dlr_entry_mode="untrusted")
    seen = []
    monkeypatch.setattr(app_module, "require_csrf", forbidden)

    async def downstream(received_scope, receive, send):
        assert received_scope is actual_scope and receive is forbidden
        seen.append(dict(received_scope))

    assert asyncio.run(invoke(app_module._EntryBoundary(downstream), actual_scope)) == []
    assert len(seen) == 1
    assert actual_scope["dlr_entry_mode"] == mode
    assert actual_scope["path"] == expected_path and actual_scope["raw_path"] == expected_raw
    assert actual_scope["root_path"] == "/proxy"
    assert actual_scope["query_string"] == b"query=unchanged"


def test_bare_private_prefix_keeps_original_404_without_entering_or_reading(monkeypatch):
    actual_scope = scope("/__dlr_account", method="POST", raw_path=b"/__dlr_account")
    before = dict(actual_scope)
    monkeypatch.setattr(app_module, "require_csrf", forbidden)
    messages = asyncio.run(invoke(app_module._EntryBoundary(forbidden), actual_scope))
    assert messages[0]["status"] == 404
    assert messages[1]["body"] == b'{"detail":"Not found"}'
    assert actual_scope == before


@pytest.mark.parametrize("kind", ["websocket", "lifespan"])
def test_non_http_passes_the_same_scope_and_callbacks_unchanged(monkeypatch, kind):
    actual_scope: Scope = {"type": kind}
    before = dict(actual_scope)
    monkeypatch.setattr(app_module, "require_csrf", forbidden)

    async def run():
        async def send(message):
            forbidden()

        async def downstream(received_scope, receive, actual_send):
            assert received_scope is actual_scope
            assert receive is forbidden and actual_send is send

        await app_module._EntryBoundary(downstream)(actual_scope, forbidden, send)

    asyncio.run(run())
    assert actual_scope == before


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "get"])
def test_account_safe_methods_keep_csrf_exemption(monkeypatch, method):
    monkeypatch.setattr(app_module, "require_csrf", forbidden)

    async def downstream(actual_scope, receive, send):
        assert actual_scope["dlr_entry_mode"] == "account"

    asyncio.run(
        invoke(
            app_module._EntryBoundary(downstream),
            scope("/__dlr_account/api/adapters", method=method),
        )
    )


@pytest.mark.parametrize(
    "path",
    ["/api/hooks/key", "/api/ingress/key", "/api/hooks/key/extra", "/api/ingress/key/receipts/1"],
)
def test_account_public_prefixes_keep_original_bearer_exemption(monkeypatch, path):
    monkeypatch.setattr(app_module, "require_csrf", forbidden)

    async def downstream(actual_scope, receive, send):
        assert actual_scope["path"] == path

    asyncio.run(
        invoke(app_module._EntryBoundary(downstream), scope("/__dlr_account" + path, method="POST"))
    )


@pytest.mark.parametrize("path", ["/api/hooks", "/api/ingress", "/api/hooksx/key", "/api/adapters"])
@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
def test_account_other_writes_keep_real_csrf_error(path, method):
    messages = asyncio.run(
        invoke(app_module._EntryBoundary(forbidden), scope("/__dlr_account" + path, method=method))
    )
    assert messages[0]["status"] == 403
    assert messages[1]["body"] == (
        b'{"detail":{"code":"account_csrf_invalid","message":"Invalid CSRF protection token"}}'
    )


@pytest.mark.parametrize("valid", [False, True])
def test_cookie_header_double_submit_matches_original(valid):
    actual_scope = scope(
        "/__dlr_account/api/adapters",
        method="POST",
        headers=[
            (b"cookie", b"dlr_account_csrf=value"),
            (b"x-csrf-token", b"value" if valid else b"bad"),
        ],
    )
    entered = []

    async def downstream(actual_scope, receive, send):
        entered.append(True)

    messages = asyncio.run(invoke(app_module._EntryBoundary(downstream), actual_scope))
    assert entered == ([True] if valid else [])
    assert messages == [] if valid else messages[0]["status"] == 403


def test_token_entry_bypasses_csrf_even_with_account_cookies(monkeypatch):
    monkeypatch.setattr(app_module, "require_csrf", forbidden)

    async def downstream(actual_scope, receive, send):
        assert actual_scope["dlr_entry_mode"] == "token"

    actual_scope = scope(
        "/api/adapters", method="POST", headers=[(b"cookie", b"dlr_account_csrf=other")]
    )
    assert asyncio.run(invoke(app_module._EntryBoundary(downstream), actual_scope)) == []


def test_csrf_exception_preserves_original_status_detail_and_header_behavior(monkeypatch):
    def deny(request):
        assert request.scope["path"] == "/api/adapters"
        raise HTTPException(419, detail={"code": "custom_csrf"}, headers={"X-Unused": "unchanged"})

    monkeypatch.setattr(app_module, "require_csrf", deny)
    messages = asyncio.run(
        invoke(
            app_module._EntryBoundary(forbidden),
            scope("/__dlr_account/api/adapters", method="POST"),
        )
    )
    assert messages[0]["status"] == 419
    assert messages[1]["body"] == b'{"detail":{"code":"custom_csrf"}}'
    assert b"x-unused" not in dict(messages[0]["headers"])


def test_real_app_keeps_admission_outermost_and_has_no_basehttp_wrapper():
    app = app_module.create_app()
    assert [item.cls for item in app.user_middleware] == [
        HttpIngressAdmission,
        app_module._EntryBoundary,
    ]


def test_125_call_model_refuses_only_existing_full_budget_without_extra_tasks(monkeypatch):
    pool = ingress.IngressResources(buffer=ApplicationBufferBudget(), session_factory=forbidden)
    monkeypatch.setattr(ingress, "_resources", pool)

    async def run():
        full, release = asyncio.Event(), asyncio.Event()
        entered, statuses = [], []

        async def downstream(actual_scope, receive, send):
            assert asyncio.current_task() is actual_scope["request_task"]
            assert pool.raw_buffer.reserve(ingress.BODY_RESERVATION)
            entered.append(True)
            if len(entered) == 7:
                full.set()
            try:
                await release.wait()
                await send({"type": "http.response.start", "status": 202, "headers": []})
            finally:
                pool.raw_buffer.release(ingress.BODY_RESERVATION)

        target = HttpIngressAdmission(
            app_module._EntryBoundary(downstream), busy=ingress.admission_capacity_busy
        )

        async def request():
            actual_scope = scope("/api/ingress/route", method="POST")
            actual_scope["request_task"] = asyncio.current_task()
            messages = await invoke(target, actual_scope)
            statuses.append(messages[0]["status"])

        async with asyncio.timeout(5):
            tasks = [asyncio.create_task(request()) for _ in range(125)]
            try:
                await full.wait()
                await asyncio.sleep(0)
                assert len(entered) == 7 and statuses == [429] * 118
                assert pool.buffer.used_bytes == 7 * ingress.BODY_RESERVATION
                assert pool.work.used == 0
            finally:
                release.set()
                await asyncio.gather(*tasks)
        assert statuses.count(202) == 7 and statuses.count(429) == 118
        assert pool.buffer.used_bytes == 0 and pool.buffer.parent.used_bytes == 0

    try:
        asyncio.run(run())
    finally:
        pool.close()
