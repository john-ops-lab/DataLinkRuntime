"""Tests for the M3 progress API, SSE event stream and history enrichment.

Progress is best effort and must never disturb the M2 final-result contract:
only the owning Worker appends, terminal Executions ignore further chunks,
and status/output/error/timing are untouched by progress. The SSE stream is
a plain PostgreSQL poll: immediate snapshot, log deltas, terminal close.
"""

import json
import threading
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.common.config import settings
from dlr.control.models import Execution
from test_adapters import create_adapter, save_version
from test_executions import create_execution
from test_workers import register_worker, report, setup_claimed_execution

WORKER_AUTH = {"Authorization": f"Bearer {WORKER_TOKEN}"}


def progress(
    client: TestClient,
    worker_id: int,
    execution_id: int,
    *,
    stdout_chunk: str = "",
    stderr_chunk: str = "",
    headers: dict[str, str] = WORKER_AUTH,
):
    return client.post(
        f"/api/workers/{worker_id}/executions/{execution_id}/progress",
        json={"stdout_chunk": stdout_chunk, "stderr_chunk": stderr_chunk},
        headers=headers,
    )


def _set_fields(
    session_factory: sessionmaker[Session], execution_id: int, **fields: object
) -> None:
    """Directly mutate the Execution row, simulating a Worker-side change."""
    session = session_factory()
    try:
        row = session.get(Execution, execution_id)
        assert row is not None
        for key, value in fields.items():
            setattr(row, key, value)
        session.commit()
    finally:
        session.close()


def _parse_sse(raw: str) -> list[dict]:
    """Parse a raw SSE payload into ``{"event", "data"}`` records."""
    events = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        record = {"event": "message", "data": ""}
        for line in block.split("\n"):
            if line.startswith("event: "):
                record["event"] = line[len("event: ") :]
            elif line.startswith("data: "):
                record["data"] = line[len("data: ") :]
        events.append(record)
    return events


# --- progress API ---------------------------------------------------------------


def test_progress_appends_streams_without_disturbing_state(api_client: TestClient) -> None:
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="progress-append")

    first = progress(api_client, worker["id"], execution["id"], stdout_chunk="line 1\n")
    assert first.status_code == 204
    second = progress(
        api_client, worker["id"], execution["id"], stdout_chunk="line 2\n", stderr_chunk="warn\n"
    )
    assert second.status_code == 204

    fetched = api_client.get(f"/api/executions/{execution['id']}").json()
    assert fetched["status"] == "running"
    assert fetched["stdout"] == "line 1\nline 2\n"
    assert fetched["stderr"] == "warn\n"
    assert fetched["stdout_truncated"] is False
    assert fetched["stderr_truncated"] is False
    # Progress never touches output, error or timing.
    assert fetched["output"] is None
    assert fetched["error"] is None
    assert fetched["ended_at"] is None
    assert fetched["duration_ms"] is None
    assert fetched["started_at"] is not None


def test_progress_empty_chunks_are_a_noop(api_client: TestClient) -> None:
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="progress-empty")
    response = progress(api_client, worker["id"], execution["id"])
    assert response.status_code == 204
    fetched = api_client.get(f"/api/executions/{execution['id']}").json()
    assert fetched["stdout"] == ""
    assert fetched["stderr"] == ""


def test_progress_requires_owning_worker(api_client: TestClient) -> None:
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="progress-owner")
    intruder = register_worker(api_client, name="progress-intruder")

    response = progress(api_client, intruder["id"], execution["id"], stdout_chunk="hijack\n")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "execution_not_owned"

    fetched = api_client.get(f"/api/executions/{execution['id']}").json()
    assert fetched["stdout"] == "", "a non-owning Worker must not append logs"


def test_progress_unclaimed_execution_conflicts(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="progress-unclaimed")
    save_version(api_client, adapter["id"])
    execution = create_execution(api_client, adapter["id"])
    worker = register_worker(api_client, name="progress-unclaimed-worker")

    response = progress(api_client, worker["id"], execution["id"], stdout_chunk="early\n")
    assert response.status_code == 409, "a pending Execution has no owner yet"


def test_progress_not_found(api_client: TestClient) -> None:
    worker = register_worker(api_client, name="progress-missing")
    response = progress(api_client, worker["id"], 999999, stdout_chunk="x")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "execution_not_found"


def test_progress_requires_worker_token(api_client: TestClient) -> None:
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="progress-auth")
    no_token = progress(
        api_client, worker["id"], execution["id"], stdout_chunk="x", headers={"Authorization": ""}
    )
    assert no_token.status_code == 401
    admin_token = progress(
        api_client,
        worker["id"],
        execution["id"],
        stdout_chunk="x",
        headers={"Authorization": "Bearer test-admin-token"},
    )
    assert admin_token.status_code == 401, "progress is a worker-internal API"


def test_progress_after_terminal_is_noop(api_client: TestClient) -> None:
    """The M2 final result is authoritative; late progress is a 204 no-op."""
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="progress-terminal")
    done = report(
        api_client,
        worker["id"],
        execution["id"],
        {"status": "succeeded", "output": {"ok": True}, "stdout": "final\n"},
    )
    assert done.status_code == 200
    ended_at = done.json()["ended_at"]

    late = progress(api_client, worker["id"], execution["id"], stdout_chunk="late tail\n")
    assert late.status_code == 204

    fetched = api_client.get(f"/api/executions/{execution['id']}").json()
    assert fetched["status"] == "succeeded"
    assert fetched["stdout"] == "final\n", "progress must never overwrite the final result"
    assert fetched["output"] == {"ok": True}
    assert fetched["ended_at"] == ended_at


def test_progress_non_owner_still_409_after_terminal(api_client: TestClient) -> None:
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="progress-owner-term")
    intruder = register_worker(api_client, name="progress-term-intruder")
    report(api_client, worker["id"], execution["id"], {"status": "succeeded"})

    response = progress(api_client, intruder["id"], execution["id"], stdout_chunk="x")
    assert response.status_code == 409, "ownership is checked before the terminal no-op"


def test_progress_caps_streams(api_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "execution_stream_max_bytes", 256)
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="progress-cap")

    for index in range(10):
        response = progress(
            api_client, worker["id"], execution["id"], stdout_chunk=f"chunk-{index}\n" * 8
        )
        assert response.status_code == 204

    fetched = api_client.get(f"/api/executions/{execution['id']}").json()
    assert fetched["stdout_truncated"] is True
    assert len(fetched["stdout"].encode()) <= 256
    assert fetched["status"] == "running", "capping the stream never changes the status"


# --- SSE event stream -------------------------------------------------------------


def test_sse_requires_admin_token(api_client: TestClient) -> None:
    assert (
        api_client.get("/api/executions/1/events", headers={"Authorization": ""}).status_code == 401
    )
    worker_token = api_client.get(
        "/api/executions/1/events", headers={"Authorization": f"Bearer {WORKER_TOKEN}"}
    )
    assert worker_token.status_code == 401, "the event stream is admin-facing"


def test_sse_not_found(api_client: TestClient) -> None:
    response = api_client.get("/api/executions/999999/events")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "execution_not_found"


def test_sse_terminal_execution_sends_snapshot_and_closes(api_client: TestClient) -> None:
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="sse-terminal")
    done = report(
        api_client,
        worker["id"],
        execution["id"],
        {"status": "succeeded", "output": {"ok": True}, "stdout": "done\n"},
    )
    assert done.status_code == 200

    with api_client.stream("GET", f"/api/executions/{execution['id']}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        raw = b"".join(response.iter_bytes()).decode()

    events = _parse_sse(raw)
    assert len(events) == 1, "a terminal Execution sends one snapshot and closes"
    assert events[0]["event"] == "execution"
    data = json.loads(events[0]["data"])
    assert data["status"] == "succeeded"
    assert data["output"] == {"ok": True}
    assert data["stdout"] == "done\n"


def test_sse_streams_progress_and_closes_on_terminal(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "sse_poll_interval_seconds", 0.1)
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="sse-live")
    execution_id = execution["id"]

    def driver() -> None:
        time.sleep(0.3)
        _set_fields(session_factory, execution_id, stdout="step 1\n")
        time.sleep(0.4)
        _set_fields(
            session_factory,
            execution_id,
            stdout="step 1\nstep 2\n",
            status="succeeded",
            ended_at=datetime.now(UTC),
        )

    thread = threading.Thread(target=driver)
    thread.start()
    with api_client.stream("GET", f"/api/executions/{execution_id}/events") as response:
        assert response.status_code == 200
        raw = b"".join(response.iter_bytes()).decode()
    thread.join(timeout=5)

    events = _parse_sse(raw)
    kinds = [event["event"] for event in events]
    assert kinds[0] == "execution", "the stream opens with an immediate snapshot"
    assert json.loads(events[0]["data"])["status"] == "running"

    stdout_chunks = [
        json.loads(event["data"])["chunk"]
        for event in events
        if event["event"] == "log" and json.loads(event["data"])["stream"] == "stdout"
    ]
    assert "".join(stdout_chunks) == "step 1\nstep 2\n", (
        "appends arrive as prefix-preserving deltas"
    )

    assert kinds[-1] == "execution", "the terminal status sends a final execution event"
    assert json.loads(events[-1]["data"])["status"] == "succeeded"


def test_sse_sends_keepalive_while_idle(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "sse_poll_interval_seconds", 0.1)
    monkeypatch.setattr(settings, "sse_keepalive_seconds", 0.2)
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="sse-keepalive")
    execution_id = execution["id"]

    def driver() -> None:
        time.sleep(0.8)
        _set_fields(session_factory, execution_id, status="succeeded", ended_at=datetime.now(UTC))

    thread = threading.Thread(target=driver)
    thread.start()
    with api_client.stream("GET", f"/api/executions/{execution_id}/events") as response:
        raw = b"".join(response.iter_bytes()).decode()
    thread.join(timeout=5)

    assert ": keepalive" in raw, "idle streams must emit SSE comment keepalives"


# --- history enrichment -------------------------------------------------------------


def test_history_summary_carries_worker_and_version_after_claim(api_client: TestClient) -> None:
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="history-enriched")
    report(api_client, worker["id"], execution["id"], {"status": "succeeded"})

    page = api_client.get(f"/api/adapters/{execution['adapter_id']}/executions").json()
    assert len(page["items"]) == 1
    item = page["items"][0]
    assert item["version_seq"] == 1
    assert item["worker_id"] == worker["id"]
    assert item["worker_name"] == worker["name"]
    assert item["status"] == "succeeded"
    assert item["duration_ms"] is not None
