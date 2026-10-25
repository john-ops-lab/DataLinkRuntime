"""Actual slow header/body I/O must end after the caller's deadline."""

import asyncio
import socket
import socketserver
import threading
import time
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx2
import pytest

from ai_runtime_support import response
from dlr.control.ai.provider_config import AiProviderError
from dlr.control.ai.runtime.pydantic_ai import work
from dlr.control.ai.runtime.pydantic_ai.transport import OwnedTransport
from dlr.control.ai.runtime.pydantic_ai.work import WorkOwner


@pytest.mark.parametrize(
    ("phase", "setup_delay", "pause_warmup_cleanup"),
    [
        pytest.param("headers", 0.0, False, id="headers"),
        pytest.param("body", 0.0, False, id="body"),
        pytest.param("keepalive_headers", 0.0, False, id="keepalive_headers"),
        pytest.param("keepalive_headers", 0.35, False, id="keepalive_headers_slow_setup"),
        pytest.param("keepalive_headers", 0.0, True, id="keepalive_headers_pending_cleanup"),
    ],
)
def test_slow_drip_releases_physical_work_before_server_stops(
    phase: str, setup_delay: float, pause_warmup_cleanup: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    slow_started = threading.Event()
    server_finished = threading.Event()
    warmup_published = threading.Event()
    release_warmup_cleanup = threading.Event()
    warmup_slot_observed = threading.Event()
    connections: list[int] = []
    requests: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            connections.append(1)

        def log_message(self, *_: object) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(1)
            if phase == "keepalive_headers" and len(requests) == 1:
                data = response("openai").body
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                self.wfile.flush()
                return
            slow_started.set()
            try:
                self.wfile.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n"
                    if phase == "body"
                    else b"HTTP/1.1 200 OK\r\nX-Slow: "
                )
                self.wfile.flush()
                while not release.wait(0.01):
                    self.wfile.write(b"x")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                server_finished.set()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    permits = threading.BoundedSemaphore(1)
    owner = WorkOwner(1, "dlr-header-deadline-test")
    transport = OwnedTransport(
        protocol="openai_compatible",
        authenticated=False,
        hard_deadline=time.monotonic() + 5,
        permits=permits,
        owner=owner,
    )
    if pause_warmup_cleanup:

        class WarmupFuture(Future[Any]):
            def set_result(self, result: Any) -> None:
                first = not warmup_published.is_set()
                if first:
                    warmup_published.set()
                super().set_result(result)
                if first:
                    assert release_warmup_cleanup.wait(2), "Warmup cleanup gate was not released"

        acquire = owner.slots.acquire

        def observe_warmup_slot(*args: Any, **kwargs: Any) -> bool:
            acquired = acquire(*args, **kwargs)
            if not acquired and warmup_published.is_set():
                warmup_slot_observed.set()
                release_warmup_cleanup.set()
            return acquired

        monkeypatch.setattr(work, "Future", WarmupFuture)
        monkeypatch.setattr(owner.slots, "acquire", observe_warmup_slot)

    # Client construction and connection warmup are setup, outside the short
    # deadline being tested. A slow runner must still reach the slow response.
    time.sleep(setup_delay)

    async def execute() -> AiProviderError:
        async with httpx2.AsyncClient(transport=transport, timeout=0.2) as client:
            if phase == "keepalive_headers":
                first = await client.post(
                    f"http://127.0.0.1:{httpd.server_port}", content=b"{}", timeout=2
                )
                assert first.status_code == 200
                # Future completion precedes WorkOwner's finally/slot release.
                # Drain physical warmup ownership before starting the I/O budget;
                # otherwise capacity rejection can masquerade as a header timeout.
                warmup_cleanup_deadline = time.monotonic() + 2
                while not owner.slots.acquire(blocking=False):
                    assert time.monotonic() < warmup_cleanup_deadline, "Warmup owner did not drain"
                    await asyncio.sleep(0.005)
                owner.slots.release()
                with owner.lock:
                    assert not owner.active
            transport.hard_deadline = time.monotonic() + 0.3
            with pytest.raises(AiProviderError) as caught:
                await client.post(f"http://127.0.0.1:{httpd.server_port}", content=b"{}")
            return caught.value

    try:
        error = asyncio.run(execute())
        assert error.code == "ai_timeout"
        assert slow_started.is_set(), "deadline expired before the slow response started"
        if pause_warmup_cleanup:
            assert warmup_slot_observed.is_set()
        assert not release.is_set()
        assert transport.closed.wait(0.5), "response returned but physical header work survived"
        assert server_finished.wait(0.5)
        deadline = time.monotonic() + 0.5
        while owner.active and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not owner.active
        assert permits.acquire(blocking=False)
        permits.release()
        assert len(connections) == 1
        assert len(requests) == (2 if phase == "keepalive_headers" else 1)
    finally:
        release_warmup_cleanup.set()
        release.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(2)
        assert not thread.is_alive()
        assert transport.closed.wait(2)


@pytest.mark.parametrize("cancel", [False, True])
def test_tls_handshake_has_a_live_cancellation_handle(cancel: bool) -> None:
    entered = threading.Event()
    release = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            assert self.request.recv(4096)
            entered.set()
            release.wait(5)

    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    permits = threading.BoundedSemaphore(1)
    owner = WorkOwner(1, "dlr-tls-cancellation-test")
    transport = OwnedTransport(
        protocol="openai_compatible",
        authenticated=False,
        hard_deadline=time.monotonic() + (5 if cancel else 0.3),
        permits=permits,
        owner=owner,
    )

    async def execute() -> None:
        async with httpx2.AsyncClient(transport=transport, timeout=2) as client:
            task = asyncio.create_task(
                client.post(f"https://127.0.0.1:{httpd.server_address[1]}", content=b"{}")
            )
            if cancel:
                while not entered.is_set():
                    await asyncio.sleep(0.005)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(AiProviderError) as caught:
                    await task
                assert caught.value.code == "ai_timeout"

    try:
        asyncio.run(execute())
        assert entered.is_set()
        assert not release.is_set()
        assert transport.closed.wait(0.5), "cancelled TLS handshake retained real capacity"
        deadline = time.monotonic() + 0.5
        while owner.active and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not owner.active
        assert permits.acquire(blocking=False)
        permits.release()
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(2)
        assert not thread.is_alive()
        assert transport.closed.wait(2)


def test_late_cancelled_connect_cannot_close_new_finalization_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns_entered = threading.Event()
    release_dns = threading.Event()
    request_entered = threading.Event()
    release_server = threading.Event()
    dns_lock = threading.Lock()
    dns_calls = 0
    original_dns = socket.getaddrinfo
    original_connect = socket.create_connection
    physical_sockets: list[socket.socket] = []

    def slow_first_dns(*args: Any, **kwargs: Any) -> Any:
        nonlocal dns_calls
        with dns_lock:
            dns_calls += 1
            first = dns_calls == 1
        if first:
            dns_entered.set()
            assert release_dns.wait(3)
        return original_dns(*args, **kwargs)

    def retain_socket(*args: Any, **kwargs: Any) -> socket.socket:
        sock = original_connect(*args, **kwargs)
        physical_sockets.append(sock)
        return sock

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request_entered.set()
            try:
                self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                self.wfile.flush()
                while not release_server.wait(0.01):
                    self.wfile.write(b"x")
                    self.wfile.flush()
            except OSError:
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    monkeypatch.setattr(socket, "getaddrinfo", slow_first_dns)
    monkeypatch.setattr(socket, "create_connection", retain_socket)
    owner = WorkOwner(2, "dlr-late-connect-test")
    permits = threading.BoundedSemaphore(2)
    transport = OwnedTransport(
        protocol="openai_compatible",
        authenticated=False,
        hard_deadline=time.monotonic() + 0.1,
        owner=owner,
        permits=permits,
    )

    async def execute() -> None:
        async with httpx2.AsyncClient(transport=transport, timeout=2) as client:
            url = f"http://127.0.0.1:{httpd.server_port}"
            with pytest.raises(AiProviderError) as caught:
                await client.post(url, content=b"{}")
            assert caught.value.code == "ai_timeout"
            assert dns_entered.is_set()
            assert len(owner.active) == 1
            transport.hard_deadline = time.monotonic() + 2
            task = asyncio.create_task(client.post(url, content=b"{}"))
            deadline = time.monotonic() + 1
            while not request_entered.is_set():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.005)
            release_dns.set()
            while len(owner.active) != 1:
                assert time.monotonic() < deadline
                await asyncio.sleep(0.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    try:
        asyncio.run(execute())
        assert not release_server.is_set()
        assert transport.closed.wait(0.5), "late old connect removed the live cancellation guard"
        deadline = time.monotonic() + 0.5
        while owner.active and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not owner.active
        assert len(physical_sockets) == 2
        assert all(sock.fileno() == -1 for sock in physical_sockets)
        for _ in range(2):
            assert permits.acquire(blocking=False)
        for _ in range(2):
            permits.release()
    finally:
        release_dns.set()
        release_server.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(2)
        assert not thread.is_alive()
        assert transport.closed.wait(2)
