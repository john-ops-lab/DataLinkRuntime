"""Tests for the M2 worker-internal API against real PostgreSQL."""

import threading
from pathlib import Path

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.common.config import settings
from dlr.control.models import Execution, Worker, WorkerCleanupRequest
from dlr.control.services import worker as worker_service
from dlr.worker import venv as venv_manager
from dlr.worker.agent import Agent, WorkerConfig
from dlr.worker.client import ControlClient
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
    """Create an adapter/version, register its target Worker and claim the run."""
    adapter = create_adapter(api_client, name=adapter_name)
    save_version(api_client, adapter["id"], runtime_config={"stage": "s1"})
    worker = register_worker(api_client)
    execution = create_execution(api_client, adapter["id"], {"input": {"n": 7}})
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
    # M5.5.11: the Adapter-level timeout (default 300s) is authoritative.
    adapter = api_client.get(f"/api/adapters/{execution['adapter_id']}").json()
    assert payload["execution_timeout_seconds"] == adapter["timeout_seconds"]

    fetched = api_client.get(f"/api/executions/{execution['id']}").json()
    assert fetched["status"] == "running"
    assert fetched["worker_id"] == worker["id"]
    assert fetched["started_at"] is not None
    assert fetched["ended_at"] is None


def test_claim_payload_uses_custom_adapter_timeout(api_client: TestClient) -> None:
    """M5.5.11: the claimed payload carries the Adapter's timeout, not the
    platform-wide default."""
    adapter = create_adapter(api_client, name="timeout-payload", timeout_seconds=7200)
    save_version(api_client, adapter["id"])
    worker = register_worker(api_client)
    execution = create_execution(api_client, adapter["id"], {"input": {}})

    payload = claim(api_client, worker["id"]).json()

    assert payload["execution_id"] == execution["id"]
    assert payload["execution_timeout_seconds"] == 7200


def test_claim_without_pending_task_returns_204(api_client: TestClient) -> None:
    worker = register_worker(api_client)
    assert claim(api_client, worker["id"]).status_code == 204


def test_claim_delivers_adapter_cleanup_and_accepts_secret_free_result(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker = register_worker(api_client, name="worker-cleanup-task-node")
    adapter = create_adapter(api_client, name="worker-cleanup-task")
    save_version(api_client, adapter["id"])
    deleted = api_client.delete(f"/api/adapters/{adapter['id']}")
    assert deleted.status_code == 204, deleted.text

    cleanup = claim(api_client, worker["id"])
    assert cleanup.status_code == 200, cleanup.text
    payload = cleanup.json()
    assert payload["kind"] == "adapter_cleanup"
    assert payload["adapter_id"] == adapter["id"]
    assert "secret" not in cleanup.text.lower()

    completed = api_client.post(
        f"/api/workers/{worker['id']}/cleanups/{payload['cleanup_id']}/result",
        json={"success": True},
        headers=WORKER_HEADERS,
    )
    assert completed.status_code == 204, completed.text
    with session_factory() as session:
        row = session.get(WorkerCleanupRequest, payload["cleanup_id"])
        assert row is not None and row.status == "completed"


def test_claim_serves_sequential_executions(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="worker-order")
    save_version(api_client, adapter["id"])
    worker = register_worker(api_client)
    first = create_execution(api_client, adapter["id"], {"input": 1})

    payload = claim(api_client, worker["id"]).json()
    assert payload["execution_id"] == first["id"]
    assert report(api_client, worker["id"], first["id"], {"status": "succeeded"}).status_code == 200

    second = create_execution(api_client, adapter["id"], {"input": 2})
    payload = claim(api_client, worker["id"]).json()
    assert payload["execution_id"] == second["id"]
    assert claim(api_client, worker["id"]).status_code == 204


def test_concurrent_claims_never_claim_same_execution_twice(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter = create_adapter(api_client, name="worker-race")
    save_version(api_client, adapter["id"])
    w1 = register_worker(api_client, name="racer-1")
    execution = create_execution(api_client, adapter["id"])
    w2 = register_worker(api_client, name="racer-2")
    # New M4.1 Manual Tests always have a live target.  Clear it explicitly
    # to model a historical NULL-target row and retain the any-Worker
    # concurrent-claim regression coverage.
    with session_factory.begin() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        row.target_worker_id = None

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
    assert response.status_code == 422, "only succeeded/failed/timeout/cancelled are accepted"


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


# --- M3 admin-facing worker list -------------------------------------------------


def test_list_workers_requires_admin_token(api_client: TestClient) -> None:
    register_worker(api_client, name="worker-list-auth")
    assert api_client.get("/api/workers", headers={"Authorization": ""}).status_code == 401
    assert api_client.get("/api/workers", headers=WORKER_HEADERS).status_code == 401


def test_list_workers_returns_status_fields(api_client: TestClient) -> None:
    register_worker(api_client, name="worker-a")
    second = register_worker(api_client, name="worker-b")
    api_client.post(f"/api/workers/{second['id']}/offline", headers=WORKER_HEADERS)

    response = api_client.get("/api/workers")
    assert response.status_code == 200
    body = response.json()
    assert [worker["name"] for worker in body] == ["worker-a", "worker-b"]
    by_name = {worker["name"]: worker for worker in body}
    assert by_name["worker-a"]["status"] == "online"
    assert by_name["worker-b"]["status"] == "offline"
    assert by_name["worker-a"]["last_heartbeat"] is not None
    assert by_name["worker-a"]["capabilities"] == ["python"]


# --- Review Round 1 regression tests ------------------------------------------


def test_result_non_owning_worker_rejected_after_terminal(
    api_client: TestClient,
) -> None:
    """Important 1: ownership check must happen before terminal idempotency."""
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="worker-terminal-owner")
    intruder = register_worker(api_client, name="worker-terminal-intruder")

    # Owner finishes the execution.
    first = report(
        api_client,
        worker["id"],
        execution["id"],
        {"status": "succeeded", "output": {"done": True}},
    )
    assert first.status_code == 200
    assert first.json()["status"] == "succeeded"

    # A non-owning Worker re-reporting must still get 409, not 200.
    retry = report(
        api_client,
        intruder["id"],
        execution["id"],
        {"status": "failed", "output": {"hijacked": True}},
    )
    assert retry.status_code == 409
    assert retry.json()["detail"]["code"] == "execution_not_owned"

    # The original terminal state is unchanged.
    fetched = api_client.get(f"/api/executions/{execution['id']}").json()
    assert fetched["status"] == "succeeded"
    assert fetched["output"] == {"done": True}


def test_result_concurrent_reports_only_first_wins(
    api_client: TestClient,
) -> None:
    """Important 1: two concurrent result reports must not race."""
    worker, execution, _ = setup_claimed_execution(
        api_client, adapter_name="worker-concurrent-result"
    )

    results = []
    errors = []

    def report_result(status: str, output: dict) -> None:
        try:
            response = report(
                api_client,
                worker["id"],
                execution["id"],
                {"status": status, "output": output},
            )
            results.append(response.json())
        except Exception as e:
            errors.append(e)

    thread1 = threading.Thread(target=report_result, args=("succeeded", {"winner": 1}))
    thread2 = threading.Thread(target=report_result, args=("failed", {"winner": 2}))
    thread1.start()
    thread2.start()
    thread1.join()
    thread2.join()

    assert not errors, f"unexpected errors: {errors}"
    assert len(results) == 2
    # Both reports must see the same terminal state (the first one to commit).
    assert results[0]["status"] == results[1]["status"]
    assert results[0]["output"] == results[1]["output"]
    assert results[0]["ended_at"] == results[1]["ended_at"]


def test_control_preserves_worker_truncated_flag(
    api_client: TestClient,
) -> None:
    """Important 2: Worker-reported truncated=true must not be overwritten to false."""
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="worker-truncated-flag")
    # Worker already truncated stdout/stderr to under the Control limit, but flagged it.
    response = report(
        api_client,
        worker["id"],
        execution["id"],
        {
            "status": "succeeded",
            "stdout": "short stdout",
            "stdout_truncated": True,
            "stderr": "short stderr",
            "stderr_truncated": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    # The persisted truncated flags must reflect the Worker's report, not the Control-side cap.
    assert body["stdout_truncated"] is True
    assert body["stderr_truncated"] is True


def test_control_trusts_worker_output_truncated_flag(
    api_client: TestClient,
) -> None:
    """Important 2: output_truncated=true must be trusted even if output is None."""
    worker, execution, _ = setup_claimed_execution(
        api_client, adapter_name="worker-output-truncated"
    )
    response = report(
        api_client,
        worker["id"],
        execution["id"],
        {
            "status": "succeeded",
            "output": None,
            "output_truncated": True,
            "output_size": 999999,
            "output_preview": "preview of truncated output",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["output"] is None
    assert body["output_truncated"] is True
    assert body["output_size"] == 999999
    assert body["output_preview"] == "preview of truncated output"


# --- Review Round 2: output truncation contract re-validation ------------------


def test_result_rejects_output_truncated_with_non_null_output(
    api_client: TestClient,
) -> None:
    """Round 2 Important 1: output_truncated=true with a complete output is a contract violation."""
    worker, execution, _ = setup_claimed_execution(
        api_client, adapter_name="worker-r2-contradict-a"
    )
    response = report(
        api_client,
        worker["id"],
        execution["id"],
        {
            "status": "succeeded",
            "output": {"actually": "complete"},
            "output_truncated": True,
            "output_size": 100,
            "output_preview": "anything",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "output_contract_violation"


def test_result_rejects_output_truncated_with_missing_size(
    api_client: TestClient,
) -> None:
    """Round 2 Important 1: output_truncated=true without a size is invalid."""
    worker, execution, _ = setup_claimed_execution(
        api_client, adapter_name="worker-r2-contradict-b"
    )
    response = report(
        api_client,
        worker["id"],
        execution["id"],
        {
            "status": "succeeded",
            "output": None,
            "output_truncated": True,
            "output_size": None,
            "output_preview": "preview",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "output_contract_violation"


def test_result_rejects_output_truncated_with_zero_size(
    api_client: TestClient,
) -> None:
    """Round 2 Important 1: output_truncated=true with size<=0 is invalid."""
    worker, execution, _ = setup_claimed_execution(
        api_client, adapter_name="worker-r2-contradict-c"
    )
    response = report(
        api_client,
        worker["id"],
        execution["id"],
        {
            "status": "succeeded",
            "output": None,
            "output_truncated": True,
            "output_size": 0,
            "output_preview": "preview",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "output_contract_violation"


# --- Important 5: reference counting for active versions -----------------------


def test_active_versions_reference_counting_prevents_premature_cleanup(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    """Important 5: two concurrent Executions on the same version must keep the venv alive."""
    adapter = create_adapter(api_client, name="worker-refcount")
    v1 = save_version(api_client, adapter["id"])
    v2 = save_version(api_client, adapter["id"])

    config = WorkerConfig()
    monkeypatch.setattr(config, "runtime_root", Path(tmp_path))
    monkeypatch.setattr(config, "max_concurrency", 4)
    agent = Agent(config, ControlClient("http://fake:8000", "fake"))

    task_v1_a = {"adapter_id": adapter["id"], "version_id": v1["id"], "execution_id": 1}
    task_v1_b = {"adapter_id": adapter["id"], "version_id": v1["id"], "execution_id": 2}
    task_v2 = {"adapter_id": adapter["id"], "version_id": v2["id"], "execution_id": 3}

    # Two Executions start on v1.
    agent._track_start(task_v1_a)
    agent._track_start(task_v1_b)
    assert agent._active_versions[(adapter["id"], v1["id"])] == 2

    # First v1 finishes; refcount drops to 1.
    agent._track_end(task_v1_a)
    assert agent._active_versions[(adapter["id"], v1["id"])] == 1

    # v2 starts and finishes; cleanup must keep v1 because its count > 0.
    agent._track_start(task_v2)
    agent._track_end(task_v2)

    # Build the keep set the same way _cleanup_venvs does.
    keep: set[int] = {v2["id"]}
    with agent._state_lock:
        for (aid, vid), count in agent._active_versions.items():
            if aid == adapter["id"] and count > 0:
                keep.add(vid)
    assert v1["id"] in keep, "v1 must be kept while a second Execution is still running"

    # Second v1 finishes; refcount drops to 0 and the key is removed.
    agent._track_end(task_v1_b)
    assert (adapter["id"], v1["id"]) not in agent._active_versions


def test_cleanup_preserves_versions_in_keep_set(
    tmp_path: object,
) -> None:
    """Important 5: cleanup_stale_venvs only removes versions NOT in the keep set.

    The Agent's _cleanup_venvs() builds the keep set from active version refcounts;
    this test verifies the filesystem-level contract: kept versions survive, others don't.
    """
    runtime_root = Path(tmp_path)
    # Create venv directories with .ready markers for v1 and v2.
    for vid in (1, 2):
        d = venv_manager.version_dir(runtime_root, 1, vid)
        d.mkdir(parents=True, exist_ok=True)
        (d / ".ready").write_text("ready", encoding="utf-8")

    # Both v1 and v2 are in the keep set (as the Agent would do when v1 is active).
    venv_manager.cleanup_stale_venvs(runtime_root, 1, keep_version_ids={1, 2})

    assert (venv_manager.version_dir(runtime_root, 1, 1) / ".ready").exists()
    assert (venv_manager.version_dir(runtime_root, 1, 2) / ".ready").exists()

    # A version NOT in the keep set is removed.
    v3_dir = venv_manager.version_dir(runtime_root, 1, 3)
    v3_dir.mkdir(parents=True, exist_ok=True)
    (v3_dir / ".ready").write_text("ready", encoding="utf-8")

    venv_manager.cleanup_stale_venvs(runtime_root, 1, keep_version_ids={1, 2})

    assert (venv_manager.version_dir(runtime_root, 1, 1) / ".ready").exists()
    assert not (venv_manager.version_dir(runtime_root, 1, 3) / ".ready").exists()


def test_cleanup_adapter_environment_removes_only_private_adapter_tree(
    tmp_path: object,
) -> None:
    runtime_root = Path(tmp_path)
    private = runtime_root / "adapters" / "42" / "versions" / "7"
    private.mkdir(parents=True)
    (private / ".ready").write_text("ready", encoding="utf-8")
    shared = runtime_root / "uv-cache"
    shared.mkdir()
    (shared / "keep.txt").write_text("shared", encoding="utf-8")

    venv_manager.cleanup_adapter_environment(runtime_root, 42)

    assert not (runtime_root / "adapters" / "42").exists()
    assert (shared / "keep.txt").exists()
