"""M3.2 tests: target Worker scheduling and the Execution cancel round trip.

Cancel semantics: pending becomes cancelled immediately (never claimable),
running gets ``cancel_requested`` which the owning Worker observes on its
next progress round trip (the ack carries the flag), and terminal state is
never rewritten. Claiming honors ``target_worker_id`` and never picks rows
flagged for cancellation.
"""

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.models import Execution
from dlr.worker import executor
from test_adapters import create_adapter, save_version
from test_executions import create_execution
from test_progress_sse import progress
from test_runtime import make_payload, runtime_settings
from test_workers import claim, register_worker, report, setup_claimed_execution

SLEEP_CODE = "import time\n\n\ndef handle(context, input):\n    time.sleep(30)\n    return {}\n"


def cancel(client: TestClient, execution_id: int):
    return client.post(f"/api/executions/{execution_id}/cancel")


def _set_fields(
    session_factory: sessionmaker[Session], execution_id: int, **fields: object
) -> None:
    """Directly mutate the Execution row, simulating M3.2 scheduling fields."""
    session = session_factory()
    try:
        row = session.get(Execution, execution_id)
        assert row is not None
        for key, value in fields.items():
            setattr(row, key, value)
        session.commit()
    finally:
        session.close()


# --- cancel API -----------------------------------------------------------------


def test_cancel_pending_is_immediate_and_idempotent(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="cancel-pending")
    save_version(api_client, adapter["id"])
    worker = register_worker(api_client)
    execution = create_execution(api_client, adapter["id"])
    assert execution["target_worker_id"] == worker["id"]
    assert execution["cancel_requested"] is False

    response = cancel(api_client, execution["id"])
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["ended_at"] is not None
    assert body["duration_ms"] is None, "a cancelled pending run never started"

    again = cancel(api_client, execution["id"])
    assert again.status_code == 200, "cancel is idempotent"
    assert again.json()["status"] == "cancelled"
    assert again.json()["ended_at"] == body["ended_at"], "terminal state never changes"

    # A cancelled Execution can never be claimed again.
    assert claim(api_client, worker["id"]).status_code == 204


def test_cancel_running_sets_flag_and_progress_ack_delivers_it(
    api_client: TestClient,
) -> None:
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="cancel-running")

    first = progress(api_client, worker["id"], execution["id"], stdout_chunk="boot\n")
    assert first.status_code == 200
    assert first.json()["cancel_requested"] is False

    response = cancel(api_client, execution["id"])
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "running", "running stays running until the Worker reports"
    assert body["cancel_requested"] is True

    # The progress round trip is the cancel channel.
    ack = progress(api_client, worker["id"], execution["id"], stdout_chunk="still up\n")
    assert ack.json()["cancel_requested"] is True

    # The Worker kills the subprocess and reports cancelled.
    done = report(
        api_client,
        worker["id"],
        execution["id"],
        {"status": "cancelled", "error": "execution cancelled", "stdout": "still up\n"},
    )
    assert done.status_code == 200
    assert done.json()["status"] == "cancelled"
    assert done.json()["ended_at"] is not None

    # Cancelling again is a terminal no-op.
    again = cancel(api_client, execution["id"])
    assert again.status_code == 200
    assert again.json()["status"] == "cancelled"


def test_cancel_terminal_is_noop(api_client: TestClient) -> None:
    worker, execution, _ = setup_claimed_execution(api_client, adapter_name="cancel-terminal")
    done = report(api_client, worker["id"], execution["id"], {"status": "succeeded"})
    assert done.status_code == 200

    response = cancel(api_client, execution["id"])
    assert response.status_code == 200, "cancel on terminal Executions is an idempotent no-op"
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["cancel_requested"] is False
    assert body["ended_at"] == done.json()["ended_at"]


def test_cancel_not_found(api_client: TestClient) -> None:
    response = cancel(api_client, 999999)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "execution_not_found"


# --- target Worker scheduling ----------------------------------------------------


def test_claim_respects_target_worker(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter = create_adapter(api_client, name="claim-target")
    save_version(api_client, adapter["id"])
    target = register_worker(api_client, name="claim-target-worker")
    updated = api_client.patch(
        f"/api/adapters/{adapter['id']}", json={"runtime_worker_id": target["id"]}
    )
    assert updated.status_code == 200, updated.text
    execution = create_execution(api_client, adapter["id"])
    other = register_worker(api_client, name="claim-other-worker")
    assert execution["target_worker_id"] == target["id"]

    assert claim(api_client, other["id"]).status_code == 204, (
        "a non-target Worker must never claim a targeted Execution"
    )
    response = claim(api_client, target["id"])
    assert response.status_code == 200
    assert response.json()["execution_id"] == execution["id"]


def test_claim_skips_cancel_requested_pending(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter = create_adapter(api_client, name="claim-cancel-skip")
    save_version(api_client, adapter["id"])
    worker = register_worker(api_client)
    first = create_execution(api_client, adapter["id"])
    _set_fields(session_factory, first["id"], cancel_requested=True)

    assert claim(api_client, worker["id"]).status_code == 204, (
        "Executions flagged for cancellation are never claimed"
    )


# --- Worker-side cancel enforcement -----------------------------------------------


def test_executor_kills_subprocess_on_cancel_flag(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cancel channel works even for a subprocess without any output."""
    monkeypatch.setattr(executor, "PROGRESS_POLL_SECONDS", 0.1)
    calls: list[tuple[str, str]] = []

    def callback(stdout_chunk: str, stderr_chunk: str) -> bool:
        calls.append((stdout_chunk, stderr_chunk))
        return True

    start = time.monotonic()
    result = executor.run(
        make_payload(code=SLEEP_CODE),
        runtime_settings(tmp_path),
        progress_callback=callback,
    )
    elapsed = time.monotonic() - start

    assert result["status"] == "cancelled"
    assert result["error"] == "execution cancelled"
    assert calls, "empty uploads must still poll the cancel channel"
    assert elapsed < 15, "cancel must kill the process group, not wait out the timeout"


def test_executor_without_callback_keeps_plain_wait(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No callback means no cancel channel: a normal run still succeeds."""
    monkeypatch.setattr(executor, "PROGRESS_POLL_SECONDS", 0.1)
    code = "def handle(context, input):\n    return {'ok': True}\n"
    result = executor.run(make_payload(code=code), runtime_settings(tmp_path))
    assert result["status"] == "succeeded"
    assert result["output"] == {"ok": True}
