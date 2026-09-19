"""The Compose smoke waits for live runtime readiness, not cached health."""

import importlib.util
import json
import sys
from collections.abc import Callable
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
