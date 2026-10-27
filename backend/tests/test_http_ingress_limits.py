"""Boundaries before custody, and cancellation of real bounded native work."""

import asyncio
import json
import threading
from typing import Any

import httptools
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
    [
        b'{"a":1,"a":2}',
        b'"\\u0000"',
        b'"\\ud800"',
        b"NaN",
        b"1e309",
        b"\xff",
        b"9007199254740992",
        b"1e20",
        b"-1e20",
        b"1e308",
        b"9007199254740992.0",
    ],
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


@pytest.mark.parametrize(
    ("length", "status"),
    [
        ("2048", None),
        ("65536", None),
        ("65537", 413),
        ("x", 422),
        ("0" * 4301, None),
        ("0" * 4301 + "65536", None),
        ("0" * 4301 + "65537", 413),
        ("9" * 4301, 413),
    ],
    ids=[
        "normal",
        "limit",
        "over-limit",
        "invalid",
        "long-zero",
        "long-limit",
        "long-over",
        "long-nines",
    ],
)
def test_content_length_is_checked_without_unbounded_integer_conversion(
    length: str, status: int | None
) -> None:
    headers = [(b"content-type", b"application/json"), (b"content-length", length.encode())]
    if status is None:
        ingress._require_media(request([], headers))
    else:
        with pytest.raises(HTTPException) as raised:
            ingress._require_media(request([], headers))
        assert raised.value.status_code == status
        assert raised.value.detail["code"] == (
            "ingress_body_too_large" if status == 413 else "ingress_length_invalid"
        )


def test_http_parser_accepts_long_zero_length_without_server_error() -> None:
    class Protocol:
        def __init__(self) -> None:
            self.headers: list[tuple[bytes, bytes]] = []

        def on_header(self, name: bytes, value: bytes) -> None:
            self.headers.append((name.lower(), value))

    protocol = Protocol()
    parser = httptools.HttpRequestParser(protocol)
    parser.feed_data(
        b"POST /api/ingress/test HTTP/1.1\r\n"
        b"Content-Type: application/json\r\nContent-Length: " + b"0" * 4301 + b"\r\n\r\n"
    )
    ingress._require_media(request([], protocol.headers))
    with pytest.raises(HTTPException) as raised:
        parse_body(b"")
    assert raised.value.status_code == 422


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
            async with asyncio.timeout(5):
                while not started.is_set():
                    await asyncio.sleep(0.001)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert parent.used_bytes == ingress.BODY_RESERVATION
                assert pool.work.used == 1
                acquired = 0
                try:
                    for _ in range(33):
                        if not pool.requests.acquire(blocking=False):
                            break
                        acquired += 1
                    assert acquired == 31
                finally:
                    for _ in range(acquired):
                        pool.requests.release()
                release.set()
                while parent.used_bytes or pool.work.used or pool.requests._value != 32:
                    await asyncio.sleep(0.001)
                assert ended.is_set()
                assert pool.work.used == 0 and pool.requests._value == 32
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            pool.close()

    asyncio.run(run())


def test_http_float_storage_safe_boundaries() -> None:
    assert parse_body(b"9007199254740991.0") == 9007199254740991
    assert parse_body(b"-9007199254740991.0") == -9007199254740991
    assert parse_body(b"1e-308") == 1e-308
