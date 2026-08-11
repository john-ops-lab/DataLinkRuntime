"""Tests for the Control Node health endpoint."""

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from dlr.control import db
from dlr.control.app import create_app


@pytest.fixture()
def client() -> Iterator[TestClient]:
    yield TestClient(create_app())


def test_health_ok_when_database_reachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "check_database", lambda: True)
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
