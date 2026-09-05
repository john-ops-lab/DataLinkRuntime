"""The only Worker execution path requires current messages and a real Sandbox."""

import json
from pathlib import Path
from typing import Any

import pytest

from dlr.worker import agent, executor, sandbox
from dlr.worker.client import ClientError, ControlClient
from dlr.worker.consumer import ConsumerConfig, V3Consumer


def test_worker_defaults_to_current_consumer_and_ignores_asserted_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DLR_WORKER_PROTOCOL_VERSION", raising=False)
    monkeypatch.delenv("DLR_WORKER_EXECUTION_SLOTS", raising=False)
    monkeypatch.setenv(
        "DLR_WORKER_ISOLATION_CAPABILITIES_JSON",
        json.dumps(dict.fromkeys(agent.ISOLATION_CAPABILITY_KEYS, True)),
    )
    config = agent.WorkerConfig()
    assert config.protocol_version == 3
    assert config.execution_slots == 2
    assert not any(config.isolation_capabilities.values())
    assert not hasattr(agent.Agent, "_claim_loop")
    assert not hasattr(agent.Agent, "_execute_execution_task")
    for method in ("claim", "report_result", "report_progress"):
        assert not hasattr(ControlClient, method)


@pytest.mark.parametrize("protocol", [None, 1, 2, True, "3", 3.0])
def test_executor_rejects_noncurrent_protocol_before_local_side_effects(
    tmp_path: Path,
    protocol: Any,
) -> None:
    result = executor.run(
        {"protocol_version": protocol},
        executor.RuntimeSettings(tmp_path / "runtime", 30, 30),
    )
    assert result["error_code"] == "worker_protocol_payload_invalid"
    assert not (tmp_path / "runtime").exists()


def test_executor_never_falls_back_to_unisolated_process(tmp_path: Path) -> None:
    result = executor.run(
        {"protocol_version": 3},
        executor.RuntimeSettings(tmp_path / "runtime", 30, 30),
    )
    assert result["error_code"] == "sandbox_linux_target_required"
    assert not (tmp_path / "runtime").exists()


def test_consumer_requires_resource_isolation_configuration(tmp_path: Path) -> None:
    with pytest.raises(sandbox.SandboxError, match="Sandbox operation failed") as caught:
        V3Consumer(
            ConsumerConfig(1, "worker-1", 1, tmp_path, tmp_path / "journal"),
            ControlClient("https://control.example", "unit-test-token"),
            connection_factory=lambda: None,  # type: ignore[arg-type,return-value]
            runtime_settings=executor.RuntimeSettings(tmp_path, 30, 30),
        )
    assert caught.value.code == "sandbox_linux_target_required"


def test_cleanup_client_has_no_execution_claim_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ControlClient("https://control.example", "unit-test-token")
    requests: list[str] = []

    def request(_method: str, path: str) -> tuple[int, bytes]:
        requests.append(path)
        return 200, b'{"kind":"adapter_cleanup","adapter_id":5,"cleanup_id":9}'

    monkeypatch.setattr(client, "_request", request)
    assert client.claim_cleanup(7) == {"kind": "adapter_cleanup", "adapter_id": 5, "cleanup_id": 9}
    assert requests == ["/api/workers/7/cleanups/claim"]
    monkeypatch.setattr(client, "_request", lambda *_args: (200, b'{"kind":"execution"}'))
    with pytest.raises(ClientError):
        client.claim_cleanup(7)


@pytest.mark.parametrize("preflight_passed", [True, False])
def test_agent_enters_consumer_only_after_local_isolation_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preflight_passed: bool,
) -> None:
    monkeypatch.setenv(agent.READY_FILE_ENV, str(tmp_path / "ready"))
    config = agent.WorkerConfig()
    config.isolation_capabilities = dict.fromkeys(agent.ISOLATION_CAPABILITY_KEYS, preflight_passed)
    worker = agent.Agent(config, ControlClient("https://control.example", "unit-test-token"))
    worker._registration_info = {"rabbitmq_execution_v3": True}
    calls: list[str] = []
    monkeypatch.setattr(worker, "_register", lambda: 7)
    monkeypatch.setattr(worker, "_recover_cleanup_journals", lambda _id: None)
    monkeypatch.setattr(worker, "_heartbeat_loop", lambda _id: None)
    monkeypatch.setattr(worker, "_cleanup_loop", lambda _id: None)
    monkeypatch.setattr(worker, "_run_consumer", lambda _id: calls.append("consumer"))
    monkeypatch.setattr(worker, "_graceful_offline", lambda _id: None)
    if not preflight_passed:
        worker.request_stop()
    worker.run()
    assert calls == (["consumer"] if preflight_passed else [])
    assert not (tmp_path / "ready").exists()
