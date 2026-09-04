"""Issue #127 C1 red/green contract tests.

These tests start with the highest-risk Worker boundaries: a v2 request must
carry the correct delegated credential, input bytes must be verified before a
subprocess starts, and the private cleanup journal must exist before the
Workspace is touched.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
import time
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import Execution, ManagedInputArtifact
from dlr.control.services.artifact_store import LocalFileArtifactStore
from dlr.worker import agent as agent_module
from dlr.worker import client as client_module
from dlr.worker import executor
from dlr.worker import venv as venv_manager
from dlr.worker import workspace as workspace_manager
from dlr.worker.client import ClientError, ControlClient
from dlr.worker.executor import RuntimeSettings
from test_adapters import create_adapter, save_version
from test_issue127_c0 import (
    WORKER_HEADERS,
    _bind_artifact,
    _claim,
    _create_staged_artifact,
    _materialize_blob,
    _register_worker,
)


def _runtime_settings(runtime_root: Path, journal_root: Path) -> RuntimeSettings:
    return RuntimeSettings(
        runtime_root=runtime_root,
        execution_timeout_seconds=30,
        dep_install_timeout_seconds=120,
        workspace_cleanup_journal_root=journal_root,
    )


def _v2_payload(*, code: str, input_files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "execution_id": 1,
        "adapter_id": 7,
        "version_id": 42,
        "language": "python",
        "code": code,
        "requirements": "",
        "runtime_config": {},
        "input": None,
        "latest_version_id": 42,
        "execution_timeout_seconds": 30,
        "secrets": {},
        "protocol_version": 2,
        "claim_token": "claim-token-for-test",
        "cleanup_token": "cleanup-token-for-test",
        "recovery_grace_seconds_snapshot": 60,
        "workspace_cleanup_attempt_timeout_seconds_snapshot": 5,
        "workspace_cleanup_total_timeout_seconds_snapshot": 20,
        "input_files": input_files or [],
    }


def test_c1_v2_credentials_are_sent_in_their_designated_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    client = ControlClient("http://control.example", "worker-token")

    def fake_request(
        method: str,
        path: str,
        payload: Any = None,
        timeout: float | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        calls.append(
            {
                "method": method,
                "path": path,
                "payload": payload,
                "timeout": timeout,
                "headers": headers or {},
            }
        )
        return 200, b'{"cancel_requested":false}'

    monkeypatch.setattr(client, "_request", fake_request)
    client.report_result(7, 1, {"status": "succeeded"}, claim_token="claim-token")
    client.report_progress(7, 1, "", "", claim_token="claim-token")
    client.report_cleanup_receipt(7, 1, cleanup_token="cleanup-token")
    client.report_result(7, 1, {"status": "succeeded"})
    client.report_progress(7, 1, "", "")

    assert calls[0]["headers"] == {"X-DLR-Claim-Token": "claim-token"}
    assert calls[1]["headers"] == {"X-DLR-Claim-Token": "claim-token"}
    assert calls[2]["headers"] == {"X-DLR-Cleanup-Token": "cleanup-token"}
    assert calls[2]["path"] == "/api/workers/executions/1/workspace-cleanup"
    assert calls[3]["headers"] == {}
    assert calls[4]["headers"] == {}
    for call in calls:
        assert "claim-token" not in call["path"]
        assert "cleanup-token" not in call["path"]
        assert "claim-token" not in str(call["payload"])
        assert "cleanup-token" not in str(call["payload"])


def test_c1_agent_forwards_claim_only_and_removes_journal_after_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DLR_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("DLR_WORKSPACE_CLEANUP_JOURNAL_ROOT", str(tmp_path / "journal"))
    config = agent_module.WorkerConfig()
    execution_id = 1
    workspace = workspace_manager.workspace_path(config.runtime_root, execution_id)
    workspace_manager.write_cleanup_journal(
        config.workspace_cleanup_journal_root,
        execution_id,
        workspace,
        "cleanup-token",
    )
    calls: dict[str, Any] = {}

    class FakeClient:
        def report_progress(
            self,
            worker_id: int,
            received_execution_id: int,
            stdout: str,
            stderr: str,
            *,
            claim_token: str,
        ) -> bool:
            calls["progress"] = (worker_id, received_execution_id, stdout, stderr, claim_token)
            return False

        def download_input_artifact(
            self,
            worker_id: int,
            received_execution_id: int,
            artifact_id: int,
            *,
            claim_token: str,
            destination: Any,
        ) -> int:
            calls["download"] = (worker_id, received_execution_id, artifact_id, claim_token)
            return destination.write(b"data")

        def report_result(
            self,
            worker_id: int,
            received_execution_id: int,
            result: dict[str, Any],
            *,
            claim_token: str,
        ) -> dict[str, Any]:
            calls["result"] = (worker_id, received_execution_id, result, claim_token)
            return result

    def fake_run(
        task: dict[str, Any],
        _settings: RuntimeSettings,
        *,
        progress_callback: Any,
        input_downloader: Any,
    ) -> dict[str, Any]:
        progress_callback("log", "")
        destination = io.BytesIO()
        input_downloader(task["input_files"][0], destination)
        return {"status": "succeeded", "workspace_cleanup_status": "completed"}

    monkeypatch.setattr(executor, "run", fake_run)
    monkeypatch.setattr(agent_module.venv_manager, "cleanup_stale_venvs", lambda *args: None)
    task = _v2_payload(
        code="def handle(context, input):\n    return {}\n",
        input_files=[
            {
                "id": 9,
                "ordinal": 0,
                "mount_name": "input-00",
                "original_filename": "input.txt",
                "content_type": "text/plain",
                "size_bytes": 4,
                "sha256": hashlib.sha256(b"data").hexdigest(),
            }
        ],
    )
    agent = agent_module.Agent(config, FakeClient())  # type: ignore[arg-type]
    agent._execute_task(7, task)

    assert calls["progress"][-1] == "claim-token-for-test"
    assert calls["download"][-1] == "claim-token-for-test"
    assert calls["result"][-1] == "claim-token-for-test"
    assert "claim-token-for-test" not in str(calls["result"][2])
    assert not workspace_manager.journal_path(
        config.workspace_cleanup_journal_root, execution_id
    ).exists()


def test_c1_client_streams_download_chunks_with_claim_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        status = 200

        def __init__(self) -> None:
            self.chunks = [b"first", b"second", b""]
            self.requested_sizes: list[int] = []

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            self.requested_sizes.append(size)
            return self.chunks.pop(0)

    response = FakeResponse()
    request_facts: dict[str, Any] = {}

    def fake_urlopen(request: Any, *, timeout: float) -> FakeResponse:
        request_facts["headers"] = dict(request.headers)
        request_facts["timeout"] = timeout
        return response

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fake_urlopen)
    destination = io.BytesIO()
    total = ControlClient("http://control.example", "worker-token").download_input_artifact(
        7,
        1,
        9,
        claim_token="claim-token",
        destination=destination,
    )
    assert total == len(b"firstsecond")
    assert destination.getvalue() == b"firstsecond"
    assert request_facts["headers"]["X-dlr-claim-token"] == "claim-token"
    assert "claim-token" not in request_facts["headers"].get("Authorization", "")
    assert response.requested_sizes == [64 * 1024, 64 * 1024, 64 * 1024]


def test_c1_agent_recovery_sends_cleanup_receipt_before_journal_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DLR_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("DLR_WORKSPACE_CLEANUP_JOURNAL_ROOT", str(tmp_path / "journal"))
    config = agent_module.WorkerConfig()
    layout = workspace_manager.create_workspace(config.runtime_root, 2)
    workspace_manager.prepare_input_files(layout, [], None)
    workspace_manager.write_cleanup_journal(
        config.workspace_cleanup_journal_root,
        2,
        layout.root,
        "cleanup-token-recovery",
    )
    calls: list[tuple[int, int, str]] = []

    class FakeClient:
        def report_cleanup_receipt(
            self,
            worker_id: int,
            execution_id: int,
            *,
            cleanup_token: str,
        ) -> dict[str, Any]:
            calls.append((worker_id, execution_id, cleanup_token))
            return {}

    agent = agent_module.Agent(config, FakeClient())  # type: ignore[arg-type]
    agent._recover_cleanup_journals(7)
    assert calls == [(7, 2, "cleanup-token-recovery")]
    assert not layout.root.exists()
    assert not workspace_manager.journal_path(config.workspace_cleanup_journal_root, 2).exists()


def test_c1_progress_transport_error_does_not_log_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        venv_manager,
        "prepare_version_venv",
        lambda *args, **kwargs: Path(sys.executable),
    )

    def fail_progress(_stdout: str, _stderr: str) -> bool:
        raise ClientError(401, "claim-token-must-not-be-logged")

    caplog.set_level("WARNING", logger="dlr.worker.executor")
    result = executor.run(
        _v2_payload(code="def handle(context, input):\n    return {}\n"),
        _runtime_settings(tmp_path / "runtime", tmp_path / "journal"),
        progress_callback=fail_progress,
    )
    assert result["status"] == "succeeded"
    assert "claim-token-must-not-be-logged" not in caplog.text


def test_c1_bad_download_never_starts_adapter(tmp_path: Path) -> None:
    started = tmp_path / "adapter-started"
    expected = b"payload"
    input_file = {
        "id": 9,
        "ordinal": 0,
        "mount_name": "input-00",
        "original_filename": "input.txt",
        "content_type": "text/plain",
        "size_bytes": len(expected),
        "sha256": hashlib.sha256(b"different").hexdigest(),
    }
    payload = _v2_payload(
        code=(
            "from pathlib import Path\n"
            f"def handle(context, input):\n    Path({str(started)!r}).write_text('started')\n"
            "    return {}\n"
        ),
        input_files=[input_file],
    )

    def download(_file: dict[str, Any], destination: Any) -> int:
        destination.write(expected)
        return len(expected)

    result = executor.run(
        payload,
        _runtime_settings(tmp_path, tmp_path / "journal"),
        input_downloader=download,
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "input_artifact_checksum_mismatch"
    assert not started.exists()


def test_c1_oversized_stream_stops_at_declared_size_and_never_starts_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = tmp_path / "adapter-started"
    expected = b"payload"
    payload = _v2_payload(
        code=(
            "from pathlib import Path\n"
            f"def handle(context, input):\n    Path({str(started)!r}).write_text('started')\n"
            "    return {}\n"
        ),
        input_files=[
            {
                "id": 9,
                "ordinal": 0,
                "mount_name": "input-00",
                "original_filename": "input.txt",
                "content_type": "text/plain",
                "size_bytes": len(expected),
                "sha256": hashlib.sha256(expected).hexdigest(),
            }
        ],
    )
    overflow_seen = False

    def oversized(_file: dict[str, Any], destination: Any) -> int:
        nonlocal overflow_seen
        destination.write(expected)
        with pytest.raises(workspace_manager._InputSizeExceeded):
            destination.write(b"x" * (1024 * 1024))
        overflow_seen = True
        return len(expected)

    monkeypatch.setattr(
        venv_manager,
        "prepare_version_venv",
        lambda *args, **kwargs: Path(sys.executable),
    )
    result = executor.run(
        payload,
        _runtime_settings(tmp_path / "runtime", tmp_path / "journal"),
        input_downloader=oversized,
    )

    assert overflow_seen
    assert result["status"] == "failed"
    assert result["error_code"] == "input_artifact_checksum_mismatch"
    assert not started.exists()


@pytest.mark.parametrize("protocol_version", [1, None, "missing"])
def test_c1_v1_executor_report_keeps_legacy_cleanup_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protocol_version: int | str | None,
) -> None:
    monkeypatch.setattr(
        venv_manager,
        "prepare_version_venv",
        lambda *args, **kwargs: Path(sys.executable),
    )
    payload = _v2_payload(code="def handle(context, input):\n    return {}\n")
    payload.pop("claim_token")
    payload.pop("cleanup_token")
    if protocol_version == "missing":
        payload.pop("protocol_version")
    else:
        payload["protocol_version"] = protocol_version
    result = executor.run(payload, _runtime_settings(tmp_path, tmp_path / "journal"))
    assert result["status"] == "succeeded"
    assert "workspace_cleanup_status" not in result
    assert "workspace_cleanup_error_code" not in result


def test_c1_journal_and_workspace_are_ready_before_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal_root = tmp_path / "journal"
    runtime_root = tmp_path / "runtime"
    workspace = runtime_root / "workspaces" / "dlr-exec-1"
    journal_path = journal_root / "1.json"
    observed: dict[str, Any] = {}

    monkeypatch.setattr(
        venv_manager,
        "prepare_version_venv",
        lambda *args, **kwargs: Path(sys.executable),
    )
    original_wait = executor._wait_with_progress

    def observe_wait(*args: Any, **kwargs: Any) -> tuple[int, bool, bool, str]:
        observed["workspace_exists"] = workspace.is_dir()
        observed["journal_exists"] = journal_path.is_file()
        if journal_path.is_file():
            observed["journal"] = json.loads(journal_path.read_text(encoding="utf-8"))
        return original_wait(*args, **kwargs)

    monkeypatch.setattr(executor, "_wait_with_progress", observe_wait)
    result = executor.run(
        _v2_payload(code="def handle(context, input):\n    return {}\n"),
        _runtime_settings(runtime_root, journal_root),
    )

    assert result["status"] == "succeeded"
    assert observed == {
        "workspace_exists": True,
        "journal_exists": True,
        "journal": {
            "execution_id": 1,
            "protocol_version": 2,
            "workspace_path": str(workspace),
            "cleanup_token": "cleanup-token-for-test",
        },
    }
    assert journal_path.exists(), "Result confirmation belongs to the Agent, not executor.run"


def test_c1_download_requires_running_lease_and_verified_artifact(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", True)
    monkeypatch.setattr(settings, "artifact_store_root", str(tmp_path / "artifacts"))

    worker = _register_worker(api_client, "c1-download-worker", protocol_version=2)
    adapter = create_adapter(api_client, name="c1-download")
    save_version(api_client, adapter["id"])
    artifact_id = _create_staged_artifact(session_factory, adapter["id"], "download.txt")
    store = LocalFileArtifactStore(tmp_path / "artifacts")
    storage_key = _materialize_blob(session_factory, artifact_id, store)
    with session_factory.begin() as session:
        artifact = session.get(ManagedInputArtifact, artifact_id)
        assert artifact is not None
        artifact.sha256 = hashlib.sha256(b"payload!").hexdigest()
    _bind_artifact(api_client, adapter["id"], artifact_id)
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text
    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text
    claim_token = claimed.json()["claim_token"]
    path = (
        f"/api/workers/{worker['id']}/executions/{execution.json()['id']}"
        f"/input-artifacts/{artifact_id}/content"
    )

    response = api_client.get(
        path,
        headers={**WORKER_HEADERS, "X-DLR-Claim-Token": claim_token},
    )
    assert response.status_code == 200, response.text
    assert response.content == b"payload!"
    assert response.headers["content-length"] == "8"
    assert "download.txt" not in response.text
    assert storage_key not in response.text
    assert str(tmp_path) not in response.text

    unleased_path = (
        f"/api/workers/{worker['id']}/executions/{execution.json()['id']}"
        f"/input-artifacts/{artifact_id + 999}/content"
    )
    unleased = api_client.get(
        unleased_path,
        headers={**WORKER_HEADERS, "X-DLR-Claim-Token": claim_token},
    )
    assert unleased.status_code == 422
    assert unleased.json()["detail"]["code"] == "input_artifact_not_ready"
    assert storage_key not in unleased.text
    assert str(tmp_path) not in unleased.text

    store.object_path(storage_key).write_bytes(b"tampered")
    tampered = api_client.get(
        path,
        headers={**WORKER_HEADERS, "X-DLR-Claim-Token": claim_token},
    )
    assert tampered.status_code == 422
    assert tampered.json()["detail"]["code"] == "input_artifact_checksum_mismatch"
    assert storage_key not in tampered.text
    assert str(tmp_path) not in tampered.text

    finished = api_client.post(
        f"/api/workers/{worker['id']}/executions/{execution.json()['id']}/result",
        json={"status": "failed"},
        headers={**WORKER_HEADERS, "X-DLR-Claim-Token": claim_token},
    )
    assert finished.status_code == 200, finished.text
    after_terminal = api_client.get(
        path,
        headers={**WORKER_HEADERS, "X-DLR-Claim-Token": claim_token},
    )
    assert after_terminal.status_code == 422
    assert after_terminal.json()["detail"]["code"] == "execution_claim_token_invalid"


def test_c1_cleanup_receipt_is_terminal_only_and_idempotent(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker = _register_worker(api_client, "c1-receipt-worker", protocol_version=2)
    adapter = create_adapter(api_client, name="c1-receipt")
    save_version(api_client, adapter["id"])
    execution = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert execution.status_code == 202, execution.text
    claimed = _claim(api_client, worker["id"])
    assert claimed.status_code == 200, claimed.text
    execution_id = execution.json()["id"]
    cleanup_token = claimed.json()["cleanup_token"]
    receipt_path = f"/api/workers/executions/{execution_id}/workspace-cleanup"

    before_terminal = api_client.post(
        receipt_path,
        json={"status": "completed"},
        headers={**WORKER_HEADERS, "X-DLR-Cleanup-Token": cleanup_token},
    )
    assert before_terminal.status_code == 422
    assert before_terminal.json()["detail"]["code"] == "workspace_cleanup_transition_invalid"

    result = api_client.post(
        f"/api/workers/{worker['id']}/executions/{execution_id}/result",
        json={
            "status": "succeeded",
            "output": {"unchanged": True},
            "workspace_cleanup_status": "deferred",
            "workspace_cleanup_error_code": "workspace_cleanup_failed",
        },
        headers={**WORKER_HEADERS, "X-DLR-Claim-Token": claimed.json()["claim_token"]},
    )
    assert result.status_code == 200, result.text
    ended_at = result.json()["ended_at"]

    accepted = api_client.post(
        receipt_path,
        json={"status": "completed"},
        headers={**WORKER_HEADERS, "X-DLR-Cleanup-Token": cleanup_token},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["workspace_cleanup_status"] == "completed"
    assert accepted.json()["output"] == {"unchanged": True}
    assert accepted.json()["ended_at"] == ended_at

    retry = api_client.post(
        receipt_path,
        json={"status": "completed"},
        headers={**WORKER_HEADERS, "X-DLR-Cleanup-Token": cleanup_token},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["workspace_cleanup_status"] == "completed"
    with session_factory() as session:
        row = session.get(Execution, execution_id)
        assert row is not None
        assert row.workspace_cleanup_status == "completed"
        assert row.output == {"unchanged": True}


def test_c1_journal_failure_prevents_workspace_download_and_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = tmp_path / "started"
    downloaded = False

    def fail_journal(*args: Any, **kwargs: Any) -> Path:
        raise workspace_manager.WorkspaceError("workspace_cleanup_failed")

    def download(_file: dict[str, Any], _destination: Any) -> int:
        nonlocal downloaded
        downloaded = True
        return 0

    monkeypatch.setattr(workspace_manager, "write_cleanup_journal", fail_journal)
    result = executor.run(
        _v2_payload(
            code=(
                "from pathlib import Path\n"
                f"def handle(context, input):\n    Path({str(started)!r}).write_text('started')\n"
                "    return {}\n"
            ),
            input_files=[
                {
                    "id": 9,
                    "ordinal": 0,
                    "mount_name": "input-00",
                    "original_filename": "input.txt",
                    "content_type": "text/plain",
                    "size_bytes": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                }
            ],
        ),
        _runtime_settings(tmp_path / "runtime", tmp_path / "journal"),
        input_downloader=download,
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "workspace_cleanup_failed"
    assert not downloaded
    assert not started.exists()
    assert not (tmp_path / "runtime" / "workspaces").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workspace_cleanup_attempt_timeout_seconds_snapshot", None),
        ("workspace_cleanup_attempt_timeout_seconds_snapshot", 0),
        ("workspace_cleanup_total_timeout_seconds_snapshot", "20"),
        ("workspace_cleanup_total_timeout_seconds_snapshot", 61),
        ("recovery_grace_seconds_snapshot", 20),
    ],
)
def test_c1_invalid_v2_cleanup_snapshot_rejects_before_local_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    prepared = False

    def prepare(*_args: object, **_kwargs: object) -> Path:
        nonlocal prepared
        prepared = True
        return Path(sys.executable)

    monkeypatch.setattr(venv_manager, "prepare_version_venv", prepare)
    payload = _v2_payload(code="def handle(context, input):\n    return {}\n")
    payload[field] = value
    result = executor.run(
        payload,
        _runtime_settings(tmp_path / "runtime", tmp_path / "journal"),
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "worker_protocol_payload_invalid"
    assert result["workspace_cleanup_status"] == "completed"
    assert prepared is False
    assert not (tmp_path / "journal").exists()
    assert not (tmp_path / "runtime" / "workspaces").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("execution_id", "__missing__"),
        ("adapter_id", None),
        ("version_id", 0),
        ("claim_token", ""),
        ("cleanup_token", None),
        ("execution_timeout_seconds", None),
        ("language", ""),
        ("language", "   "),
        ("code", ""),
        ("code", " \n"),
        ("requirements", None),
        ("requirements", "__missing__"),
        ("code", "__missing__"),
        ("latest_version_id", "42"),
        (
            "input_files",
            [
                {
                    "id": "9",
                    "ordinal": 0,
                    "mount_name": "input-00.xlsx",
                    "original_filename": "customer.xlsx",
                    "content_type": (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    ),
                    "size_bytes": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                }
            ],
        ),
        (
            "input_files",
            [
                {
                    "id": 9,
                    "ordinal": 0,
                    "mount_name": "../customer.xlsx",
                    "original_filename": "customer.xlsx",
                    "content_type": (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    ),
                    "size_bytes": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                }
            ],
        ),
    ],
)
def test_c1_invalid_v2_envelope_rejects_before_all_local_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    calls = {"dependency": 0, "journal": 0, "workspace": 0, "process": 0}

    def count(name: str) -> Any:
        def called(*_args: object, **_kwargs: object) -> Path:
            calls[name] += 1
            return Path(sys.executable)

        return called

    monkeypatch.setattr(venv_manager, "prepare_version_venv", count("dependency"))
    monkeypatch.setattr(workspace_manager, "write_cleanup_journal", count("journal"))
    monkeypatch.setattr(workspace_manager, "create_workspace", count("workspace"))
    monkeypatch.setattr(executor.subprocess, "Popen", count("process"))
    payload = _v2_payload(code="def handle(context, input):\n    return {}\n")
    if value == "__missing__":
        payload.pop(field)
    else:
        payload[field] = value

    result = executor.run(
        payload,
        _runtime_settings(tmp_path / "runtime", tmp_path / "journal"),
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "worker_protocol_payload_invalid"
    assert result["workspace_cleanup_status"] == "completed"
    assert calls == {"dependency": 0, "journal": 0, "workspace": 0, "process": 0}
    assert not (tmp_path / "journal").exists()
    assert not (tmp_path / "runtime" / "workspaces").exists()


@pytest.mark.parametrize(
    "protocol_version",
    [0, 4, "1", "2", "not-a-version", 1.0, 1.5, 2.0, 2.9, True],
)
def test_c1_unknown_protocol_rejects_before_all_local_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protocol_version: object,
) -> None:
    calls = {"dependency": 0, "journal": 0, "workspace": 0, "process": 0}

    def count(name: str) -> Any:
        def called(*_args: object, **_kwargs: object) -> Path:
            calls[name] += 1
            return Path(sys.executable)

        return called

    monkeypatch.setattr(venv_manager, "prepare_version_venv", count("dependency"))
    monkeypatch.setattr(workspace_manager, "write_cleanup_journal", count("journal"))
    monkeypatch.setattr(workspace_manager, "create_workspace", count("workspace"))
    monkeypatch.setattr(executor.subprocess, "Popen", count("process"))
    payload = _v2_payload(code="def handle(context, input):\n    return {}\n")
    payload["protocol_version"] = protocol_version

    result = executor.run(
        payload,
        _runtime_settings(tmp_path / "runtime", tmp_path / "journal"),
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "worker_protocol_payload_invalid"
    assert result["workspace_cleanup_status"] == "completed"
    assert calls == {"dependency": 0, "journal": 0, "workspace": 0, "process": 0}
    assert not (tmp_path / "journal").exists()
    assert not (tmp_path / "runtime" / "workspaces").exists()


def test_c1_interrupted_download_never_starts_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = tmp_path / "started"
    payload = _v2_payload(
        code=(
            "from pathlib import Path\n"
            f"def handle(context, input):\n    Path({str(started)!r}).write_text('started')\n"
            "    return {}\n"
        ),
        input_files=[
            {
                "id": 9,
                "ordinal": 0,
                "mount_name": "input-00",
                "original_filename": "input.txt",
                "content_type": "text/plain",
                "size_bytes": 7,
                "sha256": hashlib.sha256(b"payload").hexdigest(),
            }
        ],
    )

    def interrupted(_file: dict[str, Any], destination: Any) -> int:
        destination.write(b"part")
        raise OSError("connection interrupted")

    cleanup_calls = 0
    original_cleanup = workspace_manager.cleanup_workspace

    def count_cleanup(path: Path, **kwargs: Any) -> workspace_manager.CleanupOutcome:
        nonlocal cleanup_calls
        cleanup_calls += 1
        return original_cleanup(path, **kwargs)

    monkeypatch.setattr(workspace_manager, "cleanup_workspace", count_cleanup)
    result = executor.run(
        payload,
        _runtime_settings(tmp_path / "runtime", tmp_path / "journal"),
        input_downloader=interrupted,
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "input_artifact_not_ready"
    assert result["workspace_cleanup_status"] == "completed"
    assert cleanup_calls == 1
    assert not started.exists()
    assert not (tmp_path / "runtime" / "workspaces" / "dlr-exec-1").exists()


def test_c1_input_files_are_streamed_before_start_and_permissions_are_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"verified input"
    journal_root = tmp_path / "journal"
    runtime_root = tmp_path / "runtime"
    input_path = runtime_root / "workspaces" / "dlr-exec-1" / "input" / "input-00"
    input_dir = input_path.parent
    manifest_path = runtime_root / "workspaces" / "dlr-exec-1" / "input_manifest.json"
    observed: dict[str, Any] = {}
    payload = _v2_payload(
        code="def handle(context, input):\n    return {}\n",
        input_files=[
            {
                "id": 9,
                "ordinal": 0,
                "mount_name": "input-00",
                "original_filename": "report.txt",
                "content_type": "text/plain",
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ],
    )
    monkeypatch.setattr(
        venv_manager,
        "prepare_version_venv",
        lambda *args, **kwargs: Path(sys.executable),
    )
    original_wait = executor._wait_with_progress

    def observe_wait(*args: Any, **kwargs: Any) -> tuple[int, bool, bool, str]:
        observed["file_mode"] = stat.S_IMODE(input_path.stat().st_mode)
        observed["input_mode"] = stat.S_IMODE(input_dir.stat().st_mode)
        observed["content"] = input_path.read_bytes()
        observed["manifest"] = json.loads(manifest_path.read_text(encoding="utf-8"))
        return original_wait(*args, **kwargs)

    monkeypatch.setattr(executor, "_wait_with_progress", observe_wait)
    result = executor.run(
        payload,
        _runtime_settings(runtime_root, journal_root),
        input_downloader=lambda _file, destination: destination.write(data),
    )
    assert result["status"] == "succeeded"
    assert result["workspace_cleanup_status"] == "completed"
    assert not input_path.exists()
    assert observed == {
        "file_mode": 0o444,
        "input_mode": 0o555,
        "content": data,
        "manifest": {
            "execution_id": 1,
            "files": [
                {
                    "artifact_id": 9,
                    "content_type": "text/plain",
                    "mount_name": "input-00",
                    "ordinal": 0,
                    "original_filename": "report.txt",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                }
            ],
        },
    }


def test_c1_cleanup_failure_is_deferred_without_changing_business_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        venv_manager,
        "prepare_version_venv",
        lambda *args, **kwargs: Path(sys.executable),
    )
    real_cleanup = workspace_manager.cleanup_workspace

    def report_deferred(path: Path, **kwargs: Any) -> workspace_manager.CleanupOutcome:
        outcome = real_cleanup(path, **kwargs)
        assert outcome.status == "completed"
        return workspace_manager.CleanupOutcome("deferred", "workspace_cleanup_failed")

    monkeypatch.setattr(workspace_manager, "cleanup_workspace", report_deferred)
    result = executor.run(
        _v2_payload(code="def handle(context, input):\n    return {'ok': True}\n"),
        _runtime_settings(tmp_path / "runtime", tmp_path / "journal"),
    )
    assert result["status"] == "succeeded"
    assert result["output"] == {"ok": True}
    assert result["workspace_cleanup_status"] == "deferred"
    assert result["workspace_cleanup_error_code"] == "workspace_cleanup_failed"


def test_c1_cleanup_budget_bounds_a_hung_delete_and_includes_backoff(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "payload").write_text("x", encoding="utf-8")
    entered = Event()
    release = Event()
    finished = Event()
    original_rmtree = workspace_manager.shutil.rmtree

    def stuck_delete(path: Path, **kwargs: Any) -> None:
        entered.set()
        release.wait(timeout=2)
        original_rmtree(path, **kwargs)
        finished.set()

    original = workspace_manager.shutil.rmtree
    workspace_manager.shutil.rmtree = stuck_delete
    returned_while_blocked = False
    try:
        started = time.monotonic()
        outcome = workspace_manager.cleanup_workspace(
            workspace,
            attempt_timeout_seconds=0.05,
            total_timeout_seconds=0.12,
        )
        elapsed = time.monotonic() - started
        returned_while_blocked = not finished.is_set()
    finally:
        release.set()
        finished.wait(timeout=2)
        workspace_manager.shutil.rmtree = original
    assert entered.is_set()
    assert outcome.status == "deferred"
    assert outcome.error_code == "workspace_cleanup_failed"
    assert returned_while_blocked
    assert elapsed < 1.0


def test_c1_recovery_requires_name_marker_manifest_triple_and_receipt(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    journal_root = tmp_path / "journal"
    valid_layout = workspace_manager.create_workspace(runtime_root, 1)
    workspace_manager.prepare_input_files(valid_layout, [], None)
    workspace_manager.write_cleanup_journal(
        journal_root,
        1,
        valid_layout.root,
        "cleanup-token-1",
    )
    unknown = workspace_manager.workspace_path(runtime_root, 2)
    unknown.mkdir(parents=True)
    workspace_manager.write_cleanup_journal(
        journal_root,
        2,
        unknown,
        "cleanup-token-2",
    )
    version_cache = runtime_root / "versions" / "adapter-7" / "42"
    version_cache.mkdir(parents=True)
    (version_cache / "keep").write_text("dependency", encoding="utf-8")
    receipts: list[tuple[int, str]] = []

    def receipt(execution_id: int, token: str) -> bool:
        receipts.append((execution_id, token))
        return True

    counts = workspace_manager.recover_cleanup_journals(
        journal_root,
        runtime_root,
        report_cleanup=receipt,
    )
    assert counts == {"inspected": 2, "completed": 1, "deferred": 0, "retained": 1}
    assert receipts == [(1, "cleanup-token-1")]
    assert not valid_layout.root.exists()
    assert workspace_manager.journal_path(journal_root, 1).exists() is False
    assert unknown.exists()
    assert workspace_manager.journal_path(journal_root, 2).exists()
    assert (version_cache / "keep").exists()


def test_c1_recovery_scan_budget_bounds_claim_delay_and_keeps_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DLR_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("DLR_WORKSPACE_CLEANUP_JOURNAL_ROOT", str(tmp_path / "journal"))
    config = agent_module.WorkerConfig()
    layout = workspace_manager.create_workspace(config.runtime_root, 1)
    workspace_manager.prepare_input_files(layout, [], None)
    journal = workspace_manager.write_cleanup_journal(
        config.workspace_cleanup_journal_root,
        1,
        layout.root,
        "cleanup-token-scan-budget",
    )
    entered = Event()
    release = Event()
    finished = Event()

    def stuck_cleanup(path: Path, **kwargs: Any) -> workspace_manager.CleanupOutcome:
        entered.set()
        release.wait(timeout=2)
        finished.set()
        return workspace_manager.CleanupOutcome("deferred", "workspace_cleanup_failed")

    monkeypatch.setattr(workspace_manager, "cleanup_workspace", stuck_cleanup)
    monkeypatch.setattr(workspace_manager, "RECOVERY_SCAN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(workspace_manager, "RECOVERY_RETRY_BACKOFF_SECONDS", 0.2)
    claim_times: list[float] = []
    agent_holder: dict[str, agent_module.Agent] = {}

    class FakeClient:
        def claim(self, _worker_id: int, _wait_seconds: int) -> None:
            claim_times.append(time.monotonic())
            agent_holder["agent"].request_stop()
            return None

    agent = agent_module.Agent(config, FakeClient())  # type: ignore[arg-type]
    agent_holder["agent"] = agent
    started = time.monotonic()
    agent._claim_loop(7)
    elapsed = time.monotonic() - started
    release.set()

    assert entered.wait(timeout=1)
    assert finished.wait(timeout=1)
    assert claim_times
    assert claim_times[0] - started < 1.0
    assert elapsed < 1.0
    assert journal.exists()


def test_c1_in_flight_execution_survives_periodic_recovery_scans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("DLR_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("DLR_WORKSPACE_CLEANUP_JOURNAL_ROOT", str(tmp_path / "journal"))
    config = agent_module.WorkerConfig()
    execution_id = 91
    data = b"long-running-input"
    payload = _v2_payload(
        code="def handle(context, input):\n    return {'ok': True}\n",
        input_files=[
            {
                "id": 9,
                "ordinal": 0,
                "mount_name": "input-00",
                "original_filename": "long-running.txt",
                "content_type": "text/plain",
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ],
    )
    payload["execution_id"] = execution_id
    started = Event()
    release = Event()
    finished = Event()
    reports: list[tuple[int, dict[str, Any], str]] = []
    cleanup_receipts: list[tuple[int, str]] = []

    class FakeClient:
        def download_input_artifact(
            self,
            _worker_id: int,
            _execution_id: int,
            _artifact_id: int,
            *,
            claim_token: str,
            destination: Any,
        ) -> int:
            assert claim_token == "claim-token-for-test"
            return destination.write(data)

        def report_result(
            self,
            _worker_id: int,
            received_execution_id: int,
            result: dict[str, Any],
            *,
            claim_token: str,
        ) -> None:
            reports.append((received_execution_id, dict(result), claim_token))

        def report_cleanup_receipt(
            self,
            _worker_id: int,
            received_execution_id: int,
            *,
            cleanup_token: str,
        ) -> None:
            cleanup_receipts.append((received_execution_id, cleanup_token))

    def long_running_run(
        task: dict[str, Any],
        runtime_settings: RuntimeSettings,
        *,
        progress_callback: Any,
        input_downloader: Any,
    ) -> dict[str, Any]:
        del progress_callback
        received_execution_id = int(task["execution_id"])
        planned_workspace = workspace_manager.workspace_path(
            runtime_settings.runtime_root, received_execution_id
        )
        workspace_manager.write_cleanup_journal(
            runtime_settings.workspace_cleanup_journal_root,
            received_execution_id,
            planned_workspace,
            str(task["cleanup_token"]),
        )
        layout = workspace_manager.create_workspace(
            runtime_settings.runtime_root,
            received_execution_id,
        )
        workspace_manager.prepare_input_files(layout, task["input_files"], input_downloader)
        started.set()
        if not release.wait(timeout=2):
            raise RuntimeError("test execution was not released")
        outcome = workspace_manager.cleanup_workspace(
            layout.root,
            attempt_timeout_seconds=0.2,
            total_timeout_seconds=1.0,
        )
        finished.set()
        return {
            "status": "succeeded",
            "workspace_cleanup_status": outcome.status,
            "workspace_cleanup_error_code": outcome.error_code,
        }

    monkeypatch.setattr(agent_module.executor, "run", long_running_run)
    monkeypatch.setattr(agent_module.venv_manager, "cleanup_stale_venvs", lambda *args: None)
    agent = agent_module.Agent(config, FakeClient())  # type: ignore[arg-type]
    caplog.set_level("INFO", logger="dlr.worker.workspace")
    workspace = workspace_manager.workspace_path(config.runtime_root, execution_id)
    journal = workspace_manager.journal_path(config.workspace_cleanup_journal_root, execution_id)
    marker = workspace / workspace_manager.MARKER_FILENAME
    manifest = workspace / workspace_manager.MANIFEST_FILENAME
    input_path = workspace / workspace_manager.INPUT_DIRNAME / "input-00"
    thread = Thread(target=agent._execute_task, args=(7, payload), daemon=True)
    thread.start()
    assert started.wait(timeout=1)

    try:
        for _ in range(3):
            agent._recover_cleanup_journals(7)
            assert workspace.is_dir()
            assert journal.is_file()
            assert marker.is_file()
            assert manifest.is_file()
            assert input_path.read_bytes() == data
        assert reports == []
        assert cleanup_receipts == []
        assert "skipped cleanup journal for in-flight execution 91" in caplog.text
    finally:
        release.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert finished.is_set()
    assert reports and reports[0][0] == execution_id
    assert reports[0][1]["status"] == "succeeded"
    assert reports[0][2] == "claim-token-for-test"
    assert cleanup_receipts == []
    assert not workspace.exists()
    assert not journal.exists()
    with agent._state_lock:
        assert agent._in_flight_execution_ids == set()


def test_c1_in_flight_execution_set_clears_on_exception_and_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DLR_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("DLR_WORKSPACE_CLEANUP_JOURNAL_ROOT", str(tmp_path / "journal"))
    config = agent_module.WorkerConfig()
    reports: list[tuple[int, str]] = []

    class FakeClient:
        def report_result(
            self,
            _worker_id: int,
            execution_id: int,
            result: dict[str, Any],
            *,
            claim_token: str,
        ) -> None:
            reports.append((execution_id, str(result["status"])))
            assert claim_token == "claim-token-for-test"
            if execution_id == 101:
                assert result["workspace_cleanup_status"] == "deferred"
                assert result["workspace_cleanup_error_code"] == "workspace_cleanup_failed"

    monkeypatch.setattr(agent_module.venv_manager, "cleanup_stale_venvs", lambda *args: None)
    agent = agent_module.Agent(config, FakeClient())  # type: ignore[arg-type]

    def raise_from_executor(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("executor failed")

    monkeypatch.setattr(agent_module.executor, "run", raise_from_executor)
    exception_task = _v2_payload(code="def handle(context, input):\n    return {}\n")
    exception_task["execution_id"] = 101
    agent._execute_task(7, exception_task)
    with agent._state_lock:
        assert agent._in_flight_execution_ids == set()

    def return_cancelled(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": "cancelled",
            "workspace_cleanup_status": "completed",
            "workspace_cleanup_error_code": None,
        }

    monkeypatch.setattr(agent_module.executor, "run", return_cancelled)
    cancelled_task = _v2_payload(code="def handle(context, input):\n    return {}\n")
    cancelled_task["execution_id"] = 102
    agent._execute_task(7, cancelled_task)
    with agent._state_lock:
        assert agent._in_flight_execution_ids == set()
    assert reports == [(101, "failed"), (102, "cancelled")]


def test_c1_submit_failure_clears_execution_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DLR_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("DLR_WORKSPACE_CLEANUP_JOURNAL_ROOT", str(tmp_path / "journal"))
    config = agent_module.WorkerConfig()
    task = _v2_payload(code="def handle(context, input):\n    return {}\n")

    class FakeClient:
        def claim(self, _worker_id: int, _wait_seconds: int) -> dict[str, Any]:
            return task

    class FailingPool:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        def __enter__(self) -> FailingPool:
            return self

        def __exit__(self, *_args: Any) -> bool:
            return False

        def submit(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise RuntimeError("submit failed")

    monkeypatch.setattr(agent_module, "ThreadPoolExecutor", FailingPool)
    agent = agent_module.Agent(config, FakeClient())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="submit failed"):
        agent._claim_loop(7)
    with agent._state_lock:
        assert agent._in_flight == 0
        assert agent._active_versions == {}
        assert agent._in_flight_execution_ids == set()


def test_c1_recovery_cleanup_timeout_deduplicates_journal_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime_root = tmp_path / "runtime"
    journal_root = tmp_path / "journal"
    layout = workspace_manager.create_workspace(runtime_root, 1)
    workspace_manager.prepare_input_files(layout, [], None)
    journal = workspace_manager.write_cleanup_journal(
        journal_root,
        1,
        layout.root,
        "cleanup-token-cleanup-timeout",
    )
    entered = Event()
    release = Event()
    finished = Event()
    calls = 0

    def stuck_cleanup(_path: Path, **kwargs: Any) -> workspace_manager.CleanupOutcome:
        del kwargs
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(timeout=2)
        finished.set()
        return workspace_manager.CleanupOutcome("deferred", "workspace_cleanup_failed")

    monkeypatch.setattr(workspace_manager, "cleanup_workspace", stuck_cleanup)
    caplog.set_level("INFO", logger="dlr.worker.workspace")
    try:
        first = workspace_manager.recover_cleanup_journals(
            journal_root,
            runtime_root,
            report_cleanup=lambda _execution_id, _token: True,
            scan_timeout_seconds=0.05,
            retry_backoff_seconds=0.2,
        )
        assert entered.wait(timeout=1)
        assert first == {"inspected": 1, "completed": 0, "deferred": 1, "retained": 0}

        os.utime(journal, (time.time() - 1, time.time() - 1), follow_symlinks=False)
        started = time.monotonic()
        second = workspace_manager.recover_cleanup_journals(
            journal_root,
            runtime_root,
            report_cleanup=lambda _execution_id, _token: True,
            scan_timeout_seconds=0.05,
            retry_backoff_seconds=0.2,
        )
        elapsed = time.monotonic() - started
        assert second == {"inspected": 1, "completed": 0, "deferred": 1, "retained": 0}
        assert elapsed < 1.0
        assert calls == 1
        assert not finished.is_set()
        assert journal.exists()
        assert layout.root.exists()
        assert "workspace cleanup already in progress for execution 1" in caplog.text
    finally:
        release.set()
        assert finished.wait(timeout=1)

    with workspace_manager._RECOVERY_WORK_LOCK:
        assert (journal, "cleanup") not in workspace_manager._RECOVERY_WORK_IN_PROGRESS


def test_c1_recovery_receipt_rejection_has_persistent_bounded_backoff(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    journal_root = tmp_path / "journal"
    layout = workspace_manager.create_workspace(runtime_root, 1)
    workspace_manager.prepare_input_files(layout, [], None)
    journal = workspace_manager.write_cleanup_journal(
        journal_root,
        1,
        layout.root,
        "cleanup-token-retry",
    )
    receipts: list[tuple[int, str]] = []

    def reject(execution_id: int, token: str) -> bool:
        receipts.append((execution_id, token))
        return False

    first = workspace_manager.recover_cleanup_journals(
        journal_root,
        runtime_root,
        report_cleanup=reject,
        scan_timeout_seconds=1,
        retry_backoff_seconds=0.2,
    )
    second = workspace_manager.recover_cleanup_journals(
        journal_root,
        runtime_root,
        report_cleanup=reject,
        scan_timeout_seconds=1,
        retry_backoff_seconds=0.2,
    )
    time.sleep(0.25)
    third = workspace_manager.recover_cleanup_journals(
        journal_root,
        runtime_root,
        report_cleanup=reject,
        scan_timeout_seconds=1,
        retry_backoff_seconds=0.2,
    )

    assert first == {"inspected": 1, "completed": 0, "deferred": 1, "retained": 0}
    assert second == {"inspected": 1, "completed": 0, "deferred": 0, "retained": 0}
    assert third == {"inspected": 1, "completed": 0, "deferred": 1, "retained": 0}
    assert receipts == [(1, "cleanup-token-retry"), (1, "cleanup-token-retry")]
    assert journal.exists()
    assert not layout.root.exists()


def test_c1_recovery_receipt_timeout_keeps_journal_and_scan_bounded(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime_root = tmp_path / "runtime"
    journal_root = tmp_path / "journal"
    layout = workspace_manager.create_workspace(runtime_root, 1)
    workspace_manager.prepare_input_files(layout, [], None)
    journal = workspace_manager.write_cleanup_journal(
        journal_root,
        1,
        layout.root,
        "cleanup-token-timeout",
    )
    entered = Event()
    release = Event()
    finished = Event()
    calls = 0

    def stuck_receipt(_execution_id: int, _token: str) -> bool:
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(timeout=2)
        finished.set()
        return False

    started = time.monotonic()
    caplog.set_level("INFO", logger="dlr.worker.workspace")
    counts = workspace_manager.recover_cleanup_journals(
        journal_root,
        runtime_root,
        report_cleanup=stuck_receipt,
        scan_timeout_seconds=0.05,
        retry_backoff_seconds=0.2,
    )
    elapsed = time.monotonic() - started

    assert entered.wait(timeout=1)
    assert counts == {"inspected": 1, "completed": 0, "deferred": 1, "retained": 0}
    assert not finished.is_set()
    assert elapsed < 1.0

    os.utime(journal, (time.time() - 1, time.time() - 1), follow_symlinks=False)
    started = time.monotonic()
    retry = workspace_manager.recover_cleanup_journals(
        journal_root,
        runtime_root,
        report_cleanup=stuck_receipt,
        scan_timeout_seconds=0.05,
        retry_backoff_seconds=0.2,
    )
    retry_elapsed = time.monotonic() - started

    assert retry == {"inspected": 1, "completed": 0, "deferred": 1, "retained": 0}
    assert retry_elapsed < 1.0
    assert calls == 1
    assert "cleanup receipt already in progress for execution 1" in caplog.text
    assert journal.exists()
    assert not layout.root.exists()

    release.set()
    assert finished.wait(timeout=1)
    with workspace_manager._RECOVERY_WORK_LOCK:
        assert (journal, "receipt") not in workspace_manager._RECOVERY_WORK_IN_PROGRESS


def test_c1_journal_contains_only_recovery_fields_and_no_claim_or_user_data(
    tmp_path: Path,
) -> None:
    journal = workspace_manager.write_cleanup_journal(
        tmp_path / "journal",
        11,
        tmp_path / "runtime" / "workspaces" / "dlr-exec-11",
        "cleanup-token-only",
    )
    record = json.loads(journal.read_text(encoding="utf-8"))
    assert set(record) == {
        "execution_id",
        "protocol_version",
        "workspace_path",
        "cleanup_token",
    }
    serialized = journal.read_text(encoding="utf-8")
    for sensitive in ("claim-token", "customer.csv", '{"input"', "secret-value", "adapter-output"):
        assert sensitive not in serialized
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.parent.stat().st_mode) == 0o700
