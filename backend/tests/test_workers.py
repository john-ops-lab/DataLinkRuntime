"""Tests for the M2 worker-internal API against real PostgreSQL."""

import threading

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.common.config import settings
from dlr.control.models import Execution, Worker
from dlr.control.services import worker as worker_service
from test_adapters import create_adapter, save_version
from test_executions import create_execution

WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}


def register_worker(client: TestClient, name: str = "worker-1") -> dict:
    response = client.post(
        "/api/workers/register",
        json={"name": name, "capabilities": ["python"]},
        headers=WORKER_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()


def claim(client: TestClient, worker_id: int, wait_seconds: int = 0) -> Response:
    return client.post(
        f"/api/workers/{worker_id}/tasks/claim",
        params={"wait_seconds": wait_seconds},
        headers=WORKER_HEADERS,
    )


def report(client: TestClient, worker_id: int, execution_id: int, payload: dict) -> Response:
    return client.post(
        f"/api/workers/{worker_id}/executions/{execution_id}/result",
        json=payload,
        headers=WORKER_HEADERS,
    )


def setup_claimed_execution(
    api_client: TestClient, adapter_name: str = "worker-basic"
) -> tuple[dict, dict, dict]:
    """Create adapter + version + execution, register a worker and claim it."""
    adapter = create_adapter(api_client, name=adapter_name)
    save_version(api_client, adapter["id"], runtime_config={"stage": "s1"})
    execution = create_execution(api_client, adapter["id"], {"input": {"n": 7}})
    worker = register_worker(api_client)
    response = claim(api_client, worker["id"])
    assert response.status_code == 200, response.text
    return worker, execution, response.json()


# --- registration / heartbeat / offline --------------------------------------


def test_register_upserts_same_name(api_client: TestClient) -> None:
    first = register_worker(api_client, name="worker-dup")
    second = register_worker(api_client, name="worker-dup")
    assert first["id"] == second["id"], "restart with the same name reuses the row"
    assert second["status"] == "online"
    assert second["capabilities"] == ["python"]

    other = register_worker(api_client, name="worker-other")
    assert other["id"] != first["id"]


def test_heartbeat_updates_last_heartbeat_and_marks_online(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    worker = register_worker(api_client)
    response = api_client.post(f"/api/workers/{worker['id']}/offline", headers=WORKER_HEADERS)
    assert response.status_code == 204

    heartbeat = api_client.post(f"/api/workers/{worker['id']}/heartbeat", headers=WORKER_HEADERS)
    assert heartbeat.status_code == 204

    with session_factory() as session:
        row = session.get(Worker, worker["id"])
        assert row is not None
        assert row.status == "online"
        assert row.last_heartbeat is not None


def test_offline_marks_worker_offline(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    worker = register_worker(api_client)
    assert (
        api_client.post(f"/api/workers/{worker['id']}/offline", headers=WORKER_HEADERS).status_code
        == 204
    )
    with session_factory() as session:
        row = session.get(Worker, worker["id"])
        assert row is not None and row.status == "offline"


def test_worker_endpoints_require_existing_worker(api_client: TestClient) -> None:
    assert (
        api_client.post("/api/workers/999999/heartbeat", headers=WORKER_HEADERS).status_code == 404
    )
    assert claim(api_client, 999999).status_code == 404


# --- claiming ----------------------------------------------------------------


def test_claim_returns_payload_and_marks_running(api_client: TestClient) -> None:
    worker, execution, payload = setup_claimed_execution(api_client)

    assert payload["execution_id"] == execution["id"]
    assert payload["adapter_id"] == execution["adapter_id"]
    assert payload["version_id"] == execution["version_id"]
    assert payload["code"].startswith("def handle")
    assert payload["runtime_config"] == {"stage": "s1"}
    assert payload["input"] == {"n": 7}
    assert payload["latest_version_id"] == execution["version_id"]
    assert payload["execution_timeout_seconds"] == settings.execution_timeout_seconds

    fetched = api_client.get(f"/api/executions/{execution['id']}").json()
    assert fetched["status"] == "running"
    assert fetched["worker_id"] == worker["id"]
    assert fetched["started_at"] is not None
    assert fetched["ended_at"] is None


def test_claim_without_pending_task_returns_204(api_client: TestClient) -> None:
    worker = register_worker(api_client)
    assert claim(api_client, worker["id"]).status_code == 204


def test_claim_serves_oldest_first(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="worker-order")
    save_version(api_client, adapter["id"])
    first = create_execution(api_client, adapter["id"], {"input": 1})
    second = create_execution(api_client, adapter["id"], {"input": 2})
    worker = register_worker(api_client)

    payload = claim(api_client, worker["id"]).json()
    assert payload["execution_id"] == first["id"]
    payload = claim(api_client, worker["id"]).json()
    assert payload["execution_id"] == second["id"]
    assert claim(api_client, worker["id"]).status_code == 204


def test_concurrent_claims_never_claim_same_execution_twice(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter = create_adapter(api_client, name="worker-race")
    save_version(api_client, adapter["id"])
    execution = create_execution(api_client, adapter["id"])
    w1 = register_worker(api_client, name="racer-1")
    w2 = register_worker(api_client, name="racer-2")

    start = threading.Barrier(2)
    claimed: list[object] = []
    errors: list[BaseException] = []

    def racer(worker_id: int) -> None:
        session = session_factory()
        try:
            start.wait(timeout=5)
            claimed.append(worker_service.claim_task(session, worker_id, wait_seconds=0))
        except BaseException as exc:  # noqa: BLE001 - collect any failure
            errors.append(exc)
        finally:
            session.close()

    threads = [
        threading.Thread(target=racer, args=(worker_id,)) for worker_id in (w1["id"], w2["id"])
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    payloads = [payload for payload in claimed if payload is not None]
    assert len(payloads) == 1, "one pending execution can only be claimed once"
    assert payloads[0].execution_id == execution["id"]

    fetched = api_client.get(f"/api/executions/{execution['id']}").json()
    assert fetched["status"] == "running"
    assert fetched["worker_id"] in {w1["id"], w2["id"]}


# --- result reporting ----------------------------------------------------------


def test_result_writes_terminal_fields(api_client: TestClient) -> None:
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="worker-result")

    response = report(
        api_client,
        worker["id"],
        execution["id"],
        {
            "status": "succeeded",
            "output": {"ok": True},
            "output_size": 11,
            "stdout": "hello\n",
            "stderr": "",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["output"] == {"ok": True}
    assert body["output_size"] == len(b'{"ok":true}')
    assert body["output_truncated"] is False
    assert body["stdout"] == "hello\n"
    assert body["ended_at"] is not None
    assert body["duration_ms"] is not None and body["duration_ms"] >= 0

    fetched = api_client.get(f"/api/executions/{execution['id']}").json()
    assert fetched["status"] == "succeeded"
    assert fetched["output"] == {"ok": True}


def test_result_failed_and_timeout_statuses(api_client: TestClient) -> None:
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="worker-failed")
    response = report(
        api_client,
        worker["id"],
        execution["id"],
        {"status": "failed", "error": "boom", "stdout": "", "stderr": "Traceback..."},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["error"] == "boom"
    assert response.json()["stderr"] == "Traceback..."


def test_result_timeout_status(api_client: TestClient) -> None:
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="worker-timeout")
    response = report(
        api_client,
        worker["id"],
        execution["id"],
        {"status": "timeout", "error": "execution timed out after 300s"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "timeout"


def test_result_requires_owning_worker(api_client: TestClient) -> None:
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="worker-owner")
    intruder = register_worker(api_client, name="worker-intruder")

    response = report(api_client, intruder["id"], execution["id"], {"status": "failed"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "execution_not_owned"

    # The real owner can still finish the execution.
    ok = report(api_client, worker["id"], execution["id"], {"status": "succeeded"})
    assert ok.status_code == 200


def test_result_not_found(api_client: TestClient) -> None:
    worker = register_worker(api_client)
    response = report(api_client, worker["id"], 999999, {"status": "succeeded"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "execution_not_found"


def test_result_is_idempotent_for_terminal_executions(api_client: TestClient) -> None:
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="worker-idempotent")
    first = report(
        api_client,
        worker["id"],
        execution["id"],
        {"status": "succeeded", "output": {"attempt": 1}},
    )
    assert first.status_code == 200
    ended_at = first.json()["ended_at"]

    # Simulates a retry after the response of the first report was lost.
    retry = report(
        api_client,
        worker["id"],
        execution["id"],
        {"status": "failed", "output": {"attempt": 2}},
    )
    assert retry.status_code == 200, "re-report must succeed idempotently"
    body = retry.json()
    assert body["status"] == "succeeded", "terminal state must not change"
    assert body["output"] == {"attempt": 1}
    assert body["ended_at"] == ended_at


def test_result_rejects_non_terminal_status(api_client: TestClient) -> None:
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="worker-status")
    response = report(api_client, worker["id"], execution["id"], {"status": "running"})
    assert response.status_code == 422, "only succeeded/failed/timeout are accepted"


# --- Control re-validates big-field contracts ---------------------------------


def test_control_revalidates_oversized_output(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "execution_output_max_bytes", 64)
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="worker-big-output")
    response = report(
        api_client,
        worker["id"],
        execution["id"],
        {
            "status": "succeeded",
            "output": {"blob": "x" * 4096},
            "output_truncated": False,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded", "oversized output is not a failure"
    assert body["output"] is None
    assert body["output_truncated"] is True
    assert body["output_size"] is not None and body["output_size"] > 64
    assert body["output_preview"] is not None


def test_control_caps_streams(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "execution_stream_max_bytes", 1024)
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="worker-big-stream")
    marker_tail = "TAIL-SENTINEL\n"
    stdout = "a" * 5000 + marker_tail
    response = report(
        api_client,
        worker["id"],
        execution["id"],
        {"status": "succeeded", "stdout": stdout, "stderr": "b" * 5000},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stdout_truncated"] is True
    assert body["stderr_truncated"] is True
    assert len(body["stdout"].encode()) <= 1024
    assert body["stdout"].endswith(marker_tail), "tail must survive truncation"
    assert "truncated" in body["stdout"]

    with session_factory() as session:
        row = session.scalars(select(Execution)).all()[0]
        assert len(row.stdout.encode()) <= 1024
