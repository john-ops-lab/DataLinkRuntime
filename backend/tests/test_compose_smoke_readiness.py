"""The Compose smoke waits for live runtime readiness, not cached health."""

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture()
def readiness():
    path = Path(__file__).resolve().parents[2] / "scripts" / "compose-smoke-readiness.py"
    spec = importlib.util.spec_from_file_location("compose_smoke_readiness", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def health(*, ready: object, workers: object = 1, pending: int = 0) -> dict[str, object]:
    status = "ready" if ready is True else "waiting_for_worker"
    error = None if ready is True else "rabbitmq_not_verified"
    return {
        "service": "dlr-control",
        "status": "ok" if ready is True else "degraded",
        "database": True,
        "outbox": {"status": "ok", "pending_count": pending},
        "rabbitmq": {
            "enabled": True,
            "status": status,
            "ready": ready,
            "last_error_code": error,
            "worker_count": workers,
            "ingress": {
                "enabled": True,
                "status": status,
                "ready": ready,
                "last_error_code": error,
            },
            "repair": {
                "configured": True,
                "status": status,
                "ready": ready,
                "last_error_code": error,
                "worker_count": workers,
            },
        },
    }


def observation(module, url: str, status: int, body: object):
    try:
        ready = status == 200 and module.health_payload_ready(body)
        reason = "ready" if ready else "not_ready"
    except (TypeError, ValueError) as error:
        ready, reason = False, type(error).__name__
    return module.HealthObservation(url, status, ready, reason)


def fake_clock() -> tuple[Callable[[], float], Callable[[float], None]]:
    now = [0.0]
    return (lambda: now[0], lambda seconds: now.__setitem__(0, now[0] + seconds))


@pytest.fixture()
def loopback_health_server():
    ready_body = json.dumps(health(ready=True)).encode()
    recover_calls = 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            nonlocal recover_calls
            slow = self.path == "/slow" or (self.path == "/recover" and recover_calls == 0)
            if self.path == "/recover":
                recover_calls += 1
            status = 503 if slow and self.path == "/recover" else 200
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(ready_body)))
            self.end_headers()
            try:
                if slow:
                    for byte in ready_body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.01)
                else:
                    self.wfile.write(ready_body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_waits_for_both_fresh_health_paths_in_the_same_round(readiness) -> None:
    urls = ["http://token/api/health", "http://account/api/health"]
    rounds = [health(ready=False, workers=1), health(ready=False, workers=1), health(ready=True)]
    calls: list[str] = []

    def observe(url: str, _timeout: float):
        calls.append(url)
        round_index = (len(calls) - 1) // 2
        status = 503 if round_index < 2 else 200
        return observation(readiness, url, status, rounds[round_index])

    monotonic, sleep = fake_clock()
    result = readiness.wait_for_startup(
        timeout=2,
        urls=urls,
        services_healthy=lambda _remaining: True,
        observe=observe,
        monotonic=monotonic,
        sleep=sleep,
        interval=0.25,
    )
    assert [item.url for item in result] == urls
    assert calls == urls * 3


@pytest.mark.parametrize("other_status", [503, 502, None])
def test_one_ready_path_never_caches_across_failed_peer_rounds(readiness, other_status) -> None:
    urls = ["http://token/api/health", "http://account/api/health"]
    calls: list[str] = []

    def observe(url: str, _timeout: float):
        calls.append(url)
        if url == urls[0]:
            return observation(readiness, url, 200, health(ready=True))
        return readiness.HealthObservation(url, other_status, False, "not_ready")

    monotonic, sleep = fake_clock()
    with pytest.raises(TimeoutError, match="did not become ready"):
        readiness.wait_for_startup(
            timeout=1,
            urls=urls,
            services_healthy=lambda _remaining: True,
            observe=observe,
            monotonic=monotonic,
            sleep=sleep,
            interval=0.25,
        )
    assert calls.count(urls[0]) == calls.count(urls[1]) >= 2


def test_strict_predicate_rejects_false_200_shapes_and_allows_pending(readiness) -> None:
    assert readiness.health_payload_ready(health(ready=True, pending=4)) is True
    empty_bootstrap = health(ready=False, workers=1)
    empty_bootstrap["status"] = "ok"
    assert readiness.health_payload_ready(empty_bootstrap) is False
    with pytest.raises(ValueError, match="positive integer"):
        readiness.health_payload_ready(health(ready=True, workers=True))
    malformed = health(ready=True)
    del malformed["rabbitmq"]["repair"]
    with pytest.raises(TypeError, match="repair must be an object"):
        readiness.health_payload_ready(malformed)
    with pytest.raises(TypeError, match="health must be an object"):
        readiness.health_payload_ready(json.loads("[]"))
    for container in ("rabbitmq", "ingress", "repair"):
        missing_error = health(ready=True)
        target = missing_error["rabbitmq"]
        if container != "rabbitmq":
            target = target[container]
        del target["last_error_code"]
        with pytest.raises(ValueError, match="last_error_code.*required"):
            readiness.health_payload_ready(missing_error)


def test_permanent_degraded_health_exhausts_the_shared_deadline(readiness) -> None:
    urls = ["http://token/api/health", "http://account/api/health"]
    degraded = health(ready=False, workers=1)
    degraded["database"] = False
    monotonic, sleep = fake_clock()
    with pytest.raises(TimeoutError, match="did not become ready"):
        readiness.wait_for_startup(
            timeout=1,
            urls=urls,
            services_healthy=lambda _remaining: True,
            observe=lambda url, _timeout: observation(readiness, url, 503, degraded),
            monotonic=monotonic,
            sleep=sleep,
            interval=0.25,
        )


def test_docker_health_is_rechecked_before_each_live_round(readiness) -> None:
    docker = Mock(side_effect=[False, True])
    observe = Mock(return_value=observation(readiness, "url", 200, health(ready=True)))
    monotonic, sleep = fake_clock()
    readiness.wait_for_startup(
        timeout=1,
        urls=["url"],
        services_healthy=lambda remaining: docker(remaining),
        observe=observe,
        monotonic=monotonic,
        sleep=sleep,
        interval=0.25,
    )
    assert docker.call_count == 2
    assert observe.call_count == 1


def test_late_ready_result_cannot_pass_after_shared_deadline(readiness) -> None:
    now = [0.0]

    def observe(url: str, _timeout: float):
        now[0] += 0.6
        return observation(readiness, url, 200, health(ready=True))

    with pytest.raises(TimeoutError, match="did not become ready"):
        readiness.wait_for_startup(
            timeout=1,
            urls=["first", "second"],
            services_healthy=lambda _remaining: True,
            observe=observe,
            monotonic=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
            interval=0.1,
        )


def test_real_curl_slow_drip_is_killed_within_request_budget(
    readiness, loopback_health_server
) -> None:
    started = time.monotonic()
    result = readiness.fetch_health(f"{loopback_health_server}/slow", 0.2)
    elapsed = time.monotonic() - started
    assert result.ready is False
    assert elapsed < 0.8


def test_real_curl_503_slow_body_retries_and_then_recovers(
    readiness, loopback_health_server
) -> None:
    result = readiness.wait_for_startup(
        timeout=1.5,
        urls=[f"{loopback_health_server}/recover", f"{loopback_health_server}/fast"],
        services_healthy=lambda _remaining: True,
        interval=0.05,
        request_timeout=0.2,
    )
    assert all(item.ready for item in result)


def test_cli_slow_drip_exits_nonzero_within_hard_parent_bound(
    readiness, loopback_health_server, tmp_path
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = compose ]; then echo fake-container; exit 0; fi\n'
        'if [ "$1" = inspect ]; then echo healthy; exit 0; fi\n'
        "exit 64\n"
    )
    fake_docker.chmod(0o755)
    script = Path(readiness.__file__)
    started = time.monotonic()
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--project",
            "test-project",
            "--service",
            "control",
            "--url",
            f"{loopback_health_server}/fast",
            "--url",
            f"{loopback_health_server}/slow",
            "--timeout",
            "0.4",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )
    assert completed.returncode == 1
    assert "did not become ready" in completed.stdout
    assert time.monotonic() - started < 1.5
