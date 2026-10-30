"""Boundaries before custody, and cancellation of real bounded native work."""

import asyncio
import json
import threading
import tracemalloc
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


def test_fragmented_stream_does_not_retain_per_chunk_overhead() -> None:
    async def run() -> None:
        count = 16_384
        remaining = count

        async def receive() -> dict[str, Any]:
            nonlocal remaining
            remaining -= 1
            return {"type": "http.request", "body": b"x", "more_body": remaining > 0}

        req = Request(
            {"type": "http", "method": "POST", "path": "/api/ingress/test", "headers": []},
            receive,
        )
        tracemalloc.start()
        try:
            body = await ingress._read_body(req)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert body == b"x" * count
        assert peak < 256 * 1024

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
                assert (
                    parent.used_bytes
                    == 2 + ingress.REQUEST_METADATA_BYTES + ingress.workspace_bytes(2)
                )
                assert pool.work.used == 1
                acquired = 0
                try:
                    for _ in range(129):
                        if not pool.requests.acquire(blocking=False):
                            break
                        acquired += 1
                    assert acquired == 127
                finally:
                    for _ in range(acquired):
                        pool.requests.release()
                release.set()
                while parent.used_bytes or pool.work.used or pool.requests._value != 128:
                    await asyncio.sleep(0.001)
                assert ended.is_set()
                assert pool.work.used == 0 and pool.requests._value == 128
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


@pytest.mark.parametrize(
    "body",
    [
        b'"' + b"x" * 65534 + b'"',
        ('"' + "\U0001f642" * 16383 + '"').encode(),
        json.dumps({str(i): {} for i in range(1000)}).encode(),
        b"[" + b"0," * 4094 + b"0]",
        b"[" + b"[]," * 16000 + b"[]]",
        b"[" * 12000 + b"0" + b"]" * 12000,
        b"[" + b"1e-7," * 300 + b"1e-7]",
    ],
    ids=[
        "max-string",
        "unicode",
        "maps",
        "node-limit",
        "invalid-nodes",
        "invalid-depth",
        "float-expansion",
    ],
)
def test_native_workspace_covers_parser_envelope_and_encoding_peak(body):
    from dlr.control.services import http_ingress as service
    from dlr.control.services.event_envelope import canonical
    from dlr.control.services.http_ingress_group import MAX_WORKSPACE_BYTES, workspace_bytes

    count = min(16, MAX_WORKSPACE_BYTES // workspace_bytes(len(body)))
    assert count >= 1  # Even a legal maximum body can make progress.
    held = []
    tracemalloc.start()
    try:
        for _index in range(count):
            try:
                payload = parse_body(body)
                envelope = service._build_envelope(
                    2**63 - 1, 2**31 - 1, "e" * 64, "r" * 64, "i" * 128, payload
                )
                # Retain the ORM input and encoded parameter at the same time;
                # JSON/JCS traversal/encoding scratch contributes to the peak.
                encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
                canonical(envelope)
                held.append((envelope, encoded))
            except HTTPException as error:
                assert error.status_code in {413, 422}
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak <= count * workspace_bytes(len(body))


@pytest.mark.parametrize("declared,status", [("1", 413), ("3", 422)])
def test_declared_length_never_allocates_or_submits_a_larger_body(monkeypatch, declared, status):
    async def run():
        pool = ingress.IngressResources(
            buffer=ApplicationBufferBudget(), session_factory=lambda: None
        )
        monkeypatch.setattr(ingress, "resources", lambda: pool)
        try:
            with pytest.raises(HTTPException) as raised:
                await ingress._request(
                    "route",
                    request(
                        [b"{}"],
                        [
                            (b"content-type", b"application/json"),
                            (b"content-length", declared.encode()),
                        ],
                    ),
                    "Bearer example",
                    "one",
                )
            assert raised.value.status_code == status
            assert pool.buffer.used_bytes == pool.buffer.parent.used_bytes == pool.work.used == 0
            assert pool.raw_buffer.peak_bytes == 3 * int(declared) + ingress.REQUEST_METADATA_BYTES
            assert pool.workspace_buffer.peak_bytes == 0 and pool.requests._value == 128
        finally:
            pool.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "value,valid",
    [
        ("a\x00b", False),
        ("\ud7ff", True),
        ("\ud800", False),
        ("\udfff", False),
        ("\ue000", True),
        ("\U0001f642", True),
    ],
)
def test_event_canonical_string_boundaries_keep_exact_jcs(value, valid):
    import rfc8785

    from dlr.control.services.event_envelope import canonical

    payload = {"field": [value]}
    if valid:
        assert canonical(payload) == rfc8785.dumps(payload)
    else:
        with pytest.raises(HTTPException) as raised:
            canonical(payload)
        assert raised.value.status_code == 422
