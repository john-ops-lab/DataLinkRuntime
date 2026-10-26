"""Boundaries before custody, and cancellation of real bounded native work."""

import asyncio
import json
import threading
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from dlr.control.api import ingress
from dlr.control.services.http_ingress import parse_body, require_key
from dlr.control.services.subscription_source import ApplicationBufferBudget


@pytest.mark.parametrize("value", [None, "", "x" * 129, "中文", "a\nb", "\x7f"])
def test_request_identity_is_printable_ascii(value: str | None) -> None:
    with pytest.raises(HTTPException) as raised:
        require_key(value)
    assert raised.value.status_code == 422


@pytest.mark.parametrize(
    "body",
    [b'{"a":1,"a":2}', b'"\\u0000"', b'"\\ud800"', b"NaN", b"1e309", b"\xff", b"9007199254740992"],
)
def test_unstorable_json_never_gets_normalized(body: bytes) -> None:
    with pytest.raises(HTTPException) as raised:
        parse_body(body)
    assert raised.value.status_code == 422


def test_scalar_nested_and_body_boundaries() -> None:
    assert parse_body(b"null") is None
    assert parse_body(b"false") is False
    assert parse_body(b"0") == 0
    assert parse_body(b" " * (65536 - 2) + b"[]") == []
    with pytest.raises(HTTPException) as raised:
        parse_body(b" " * 65536 + b"[]")
    assert raised.value.status_code == 413
    assert parse_body(b"[" * 31 + b"0" + b"]" * 31)
    with pytest.raises(HTTPException) as raised:
        parse_body(b"[" * 32 + b"0" + b"]" * 32)
    assert raised.value.status_code == 413
    with pytest.raises(HTTPException) as raised:
        parse_body(json.dumps([0] * 4096).encode())
    assert raised.value.status_code == 413


def request(
    chunks: list[bytes], headers: list[tuple[bytes, bytes]] | None = None, delay: float = 0
) -> Request:
    pending = iter(enumerate(chunks))

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(delay)
        i, body = next(pending)
        return {"type": "http.request", "body": body, "more_body": i < len(chunks) - 1}

    return Request(
        {"type": "http", "method": "POST", "path": "/api/ingress/test", "headers": headers or []},
        receive,
    )


def test_unknown_length_stream_is_bounded() -> None:
    async def run() -> None:
        assert await ingress._read_body(request([b"{", b"}"])) == b"{}"
        with pytest.raises(HTTPException) as raised:
            await ingress._read_body(request([b"a" * 65536, b"a"]))
        assert raised.value.status_code == 413

    asyncio.run(run())


def test_slow_stream_has_idle_deadline() -> None:
    async def run() -> None:
        with pytest.raises(HTTPException) as raised:
            await ingress._read_body(request([b"{}"], delay=2.1))
        assert raised.value.status_code == 408

    asyncio.run(run())


@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-type", b"text/plain")],
        [(b"content-type", b"application/json"), (b"content-encoding", b"gzip")],
        [(b"content-type", b"application/json;charset=gbk")],
    ],
)
def test_unsupported_media(headers: list[tuple[bytes, bytes]]) -> None:
    with pytest.raises(HTTPException) as raised:
        ingress._require_media(request([], headers))
    assert raised.value.status_code == 415


def test_cancelled_request_keeps_native_work_and_bytes_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        parent = ApplicationBufferBudget()
        pool = ingress.IngressResources(buffer=parent, session_factory=lambda: None)  # type: ignore[arg-type,return-value]
        started = threading.Event()
        release = threading.Event()
        ended = threading.Event()

        def blocked(*args: Any) -> tuple[int, dict[str, Any]]:
            started.set()
            try:
                assert release.wait(5)
                return 202, {"receipt_id": 1}
            finally:
                ended.set()

        monkeypatch.setattr(ingress, "resources", lambda: pool)
        monkeypatch.setattr(ingress, "_run_sync", blocked)
        task = asyncio.create_task(
            ingress._request(
                "test",
                request([b"{}"], [(b"content-type", b"application/json")]),
                "Bearer example",
                "one",
            )
        )
        try:
            while not started.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert parent.used_bytes == ingress.BODY_RESERVATION
            assert pool.work._value == 7
            release.set()
            while parent.used_bytes:
                await asyncio.sleep(0.001)
            assert ended.is_set()
            assert pool.work._value == 8 and pool.requests._value == 32
        finally:
            release.set()
            pool.close()

    asyncio.run(run())
