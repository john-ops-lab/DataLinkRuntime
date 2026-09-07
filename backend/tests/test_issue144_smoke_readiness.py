"""Lifecycle smoke must wait for broker admission after Worker registration."""

import importlib.util
import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def smoke(monkeypatch):
    monkeypatch.setenv("DLR_ADMIN_TOKEN", "synthetic-smoke-token")
    path = Path(__file__).resolve().parents[2] / "scripts/issue144-runtime-api.py"
    spec = importlib.util.spec_from_file_location("issue144_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def health(ready, status="ok"):
    return {"status": status, "rabbitmq": {"ingress": {"ready": ready}}}


def test_online_worker_waits_for_topology_verification(smoke, monkeypatch):
    row = {
        "id": 2,
        "name": "synthetic-peer",
        "status": "online",
        "isolation_preflight_status": "passed",
        "isolation_capabilities": dict.fromkeys(smoke.REQUIRED_ISOLATION_CAPABILITIES, True),
    }
    request = Mock(side_effect=[[row], health(False), health(False, "degraded"), health(True)])
    monkeypatch.setattr(smoke, "request", request)
    sleep = Mock()
    monkeypatch.setattr(smoke.time, "sleep", sleep)
    assert smoke.worker("synthetic-peer") == row
    assert sleep.call_count == 2
    assert [call.args[1] for call in request.call_args_list] == [
        "/workers",
        "/health",
        "/health",
        "/health",
    ]


def test_ingress_wait_is_bounded(smoke, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(smoke.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        smoke.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    request = Mock(return_value=health(False))
    monkeypatch.setattr(smoke, "request", request)
    with pytest.raises(AssertionError, match="within the deadline"):
        smoke.wait_ingress_ready(timeout=1)
    assert request.call_count == 2


def test_health_can_report_503_without_retrying_failed_execution_posts(smoke, monkeypatch):
    def unavailable(*args, **kwargs):
        raise urllib.error.HTTPError(
            "http://example.invalid",
            503,
            "unavailable",
            {},
            io.BytesIO(json.dumps(health(False, "degraded")).encode()),
        )

    transport = Mock(side_effect=unavailable)
    monkeypatch.setattr(smoke.urllib.request, "urlopen", transport)
    assert smoke.request("GET", "/health", expected=(200, 503)) == health(False, "degraded")
    with pytest.raises(AssertionError):
        smoke.run(1)
    assert transport.call_count == 2
