"""Tests for the Control Node health endpoint."""

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from dlr.common.config import settings
from dlr.control import db
from dlr.control.api import health as health_api
from dlr.control.app import create_app
from dlr.control.services import rabbitmq


@pytest.fixture()
def client() -> Iterator[TestClient]:
    yield TestClient(create_app())


def test_health_ok_when_database_reachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "check_database", lambda: True)
    monkeypatch.setattr(
        health_api,
        "read_outbox_health",
        lambda _database_ok, **_kwargs: {
            "status": "ok",
            "pending_count": 0,
            "pending_bytes": 0,
            "oldest_age_seconds": 0.0,
            "protection_reasons": [],
        },
    )
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] is True


def test_health_degraded_when_database_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "check_database", lambda: False)
    response = client.get("/api/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] is False


def test_health_reports_backlog_query_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "check_database", lambda: True)
    monkeypatch.setattr(
        health_api,
        "read_outbox_health",
        lambda _database_ok, **_kwargs: {
            "status": "unavailable",
            "pending_count": None,
            "pending_bytes": None,
            "oldest_age_seconds": None,
            "error_code": "outbox_backlog_unavailable",
        },
    )

    response = client.get("/api/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["outbox"]["error_code"] == "outbox_backlog_unavailable"


def _rabbitmq_status(
    *, status: str, ready: bool, worker_count: int, error_code: str | None = None
) -> dict[str, object]:
    return {
        "enabled": True,
        "status": status,
        "ready": ready,
        "last_error_code": error_code,
        "worker_count": worker_count,
        "ingress": {
            "enabled": True,
            "status": "ready" if ready else "degraded",
            "ready": ready,
            "last_error_code": error_code,
        },
        "repair": {
            "configured": True,
            "status": status,
            "ready": ready,
            "last_error_code": error_code,
            "worker_count": worker_count,
        },
    }


def _outbox_status(pending_count: int) -> dict[str, object]:
    return {
        "status": "ok",
        "pending_count": pending_count,
        "pending_bytes": pending_count * 10,
        "oldest_age_seconds": 0.0,
        "protection_reasons": [],
    }


def test_fresh_configured_control_is_healthy_while_waiting_for_worker(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rabbitmq_url", "amqp://dlr:password@rabbitmq:5672/%2F")
    monkeypatch.setattr(settings, "rabbitmq_execution_enabled", True)
    monkeypatch.setattr(
        rabbitmq,
        "runtime_health",
        lambda _session=None: _rabbitmq_status(
            status="waiting_for_worker", ready=False, worker_count=0
        ),
    )
    monkeypatch.setattr(health_api, "read_outbox_health", lambda _ok, **_kwargs: _outbox_status(0))
    monkeypatch.setattr(db, "check_database", lambda: True)

    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["rabbitmq"]["ingress"]["ready"] is False
    assert body["rabbitmq"]["repair"]["status"] == "waiting_for_worker"


def test_waiting_for_worker_with_pending_outbox_is_degraded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rabbitmq_url", "amqp://dlr:password@rabbitmq:5672/%2F")
    monkeypatch.setattr(settings, "rabbitmq_execution_enabled", False)
    monkeypatch.setattr(
        rabbitmq,
        "runtime_health",
        lambda _session=None: _rabbitmq_status(
            status="waiting_for_worker", ready=False, worker_count=0
        ),
    )
    monkeypatch.setattr(health_api, "read_outbox_health", lambda _ok, **_kwargs: _outbox_status(1))
    monkeypatch.setattr(db, "check_database", lambda: True)

    response = client.get("/api/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["rabbitmq"]["repair"]["status"] == "waiting_for_worker"


def test_registered_worker_runtime_is_healthy_and_ready(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rabbitmq_url", "amqp://dlr:password@rabbitmq:5672/%2F")
    monkeypatch.setattr(settings, "rabbitmq_execution_enabled", True)
    monkeypatch.setattr(
        rabbitmq,
        "runtime_health",
        lambda _session=None: _rabbitmq_status(status="ready", ready=True, worker_count=1),
    )
    monkeypatch.setattr(health_api, "read_outbox_health", lambda _ok, **_kwargs: _outbox_status(0))
    monkeypatch.setattr(db, "check_database", lambda: True)

    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["rabbitmq"]["ingress"]["ready"] is True
    assert body["rabbitmq"]["repair"]["status"] == "ready"


def test_topology_drift_keeps_health_degraded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rabbitmq_url", "amqp://dlr:password@rabbitmq:5672/%2F")
    monkeypatch.setattr(settings, "rabbitmq_execution_enabled", False)
    monkeypatch.setattr(
        rabbitmq,
        "runtime_health",
        lambda _session=None: _rabbitmq_status(
            status="degraded",
            ready=False,
            worker_count=1,
            error_code="topology_drift",
        ),
    )
    monkeypatch.setattr(health_api, "read_outbox_health", lambda _ok, **_kwargs: _outbox_status(0))
    monkeypatch.setattr(db, "check_database", lambda: True)

    response = client.get("/api/health")

    assert response.status_code == 503
    body = response.json()
    assert body["rabbitmq"]["repair"]["last_error_code"] == "topology_drift"


def test_check_database_logs_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def connect_failed() -> None:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(db.engine, "connect", connect_failed)
    with caplog.at_level(logging.ERROR, logger="dlr.control.db"):
        assert db.check_database() is False
    assert "database health check failed" in caplog.text
    assert "connection refused" in caplog.text
