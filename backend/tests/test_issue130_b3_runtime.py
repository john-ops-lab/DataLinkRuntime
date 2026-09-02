"""Focused Batch 3 Worker sandbox and fail-closed contract tests.

The real-target cases are opt-in because this macOS checkout cannot prove a
Linux delegated cgroup.  The same file is executed inside the task-owned
Colima Worker container with ``DLR_B3_REAL_TARGET=1`` for the runtime receipt.
"""

from __future__ import annotations

import os
import sys
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dlr.control.schemas.worker import REQUIRED_ISOLATION_CAPABILITIES, isolation_capabilities_ready
from dlr.worker import agent as worker_agent
from dlr.worker import executor, sandbox
from dlr.worker.consumer import ConsumerConfig, V3Consumer

MiB = 1024 * 1024


def _worker_config(**overrides: Any) -> sandbox.SandboxConfig:
    values: dict[str, Any] = {
        "cgroup_path": None,
        "cpu_cores": 1.0,
        "memory_bytes": 512 * MiB,
        "pids": 128,
        "tmp_bytes": 1 * 1024 * MiB,
        "nofile": 1024,
        "execution_timeout_seconds": 300,
        "claim_timeout_seconds": 300,
        "recovery_grace_seconds": 60,
        "cleanup_attempt_seconds": 5,
        "cleanup_total_seconds": 20,
        "stream_max_bytes": 1 * MiB,
        "output_max_bytes": 512 * 1024,
        "output_preview_max_bytes": 16 * 1024,
        "payload_uid": 501,
        "payload_gid": 1000,
    }
    values.update(overrides)
    return sandbox.SandboxConfig(**values)


def _profile(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 1,
        "resource_class": "standard",
        "backend": "cgroup_v2",
        "cpu_cores": 1.0,
        "memory_bytes": 256 * MiB,
        "pids": 64,
        "tmp_bytes": 16 * MiB,
        "nofile": 64,
        "execution_timeout_seconds": 20,
        "claim_timeout_seconds": 300,
        "recovery_grace_seconds": 60,
        "workspace_cleanup_attempt_timeout_seconds": 5,
        "workspace_cleanup_total_timeout_seconds": 20,
        "stream_max_bytes": 1 * MiB,
        "output_max_bytes": 512 * 1024,
        "output_preview_max_bytes": 16 * 1024,
    }
    value.update(overrides)
    return value


def _v3_payload(
    language: str,
    code: str,
    *,
    execution_id: int = 7001,
    attempt_id: int = 8001,
    timeout: int = 20,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "dispatch_backend": "rabbitmq",
        "protocol_version": 3,
        "execution_id": execution_id,
        "attempt_id": attempt_id,
        "attempt_no": 1,
        "fencing_token": 1,
        "lease_expires_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
        "lease_seconds": 60,
        "renew_seconds": 15,
        "claim_token": "test-claim-token",
        "cleanup_token": "test-cleanup-token",
        "adapter_id": 9001,
        "version_id": 9002,
        "language": language,
        "code": code,
        "requirements": "",
        "runtime_config": {},
        "input": {"case": "b3"},
        "latest_version_id": 9002,
        "execution_timeout_seconds": timeout,
        "secrets": {},
        "locale": "en",
        "resource_profile": profile or _profile(execution_timeout_seconds=timeout),
        "credential_bindings": [],
        "input_source_type": "none",
        "input_snapshot": {"source_type": "none"},
        "input_files": [],
        "recovery_grace_seconds_snapshot": 60,
        "workspace_cleanup_attempt_timeout_seconds_snapshot": 5,
        "workspace_cleanup_total_timeout_seconds_snapshot": 20,
    }


def test_resource_profile_is_complete_immutable_and_bounded() -> None:
    config = _worker_config()
    limits = sandbox.validate_resource_profile(_profile(), config)
    assert limits.memory_bytes == 256 * MiB
    assert limits.as_dict()["workspace_cleanup_total_timeout_seconds"] == 20
    with pytest.raises(FrozenInstanceError):
        limits.memory_bytes = 128 * MiB  # type: ignore[misc]

    for missing in (
        "memory_bytes",
        "pids",
        "tmp_bytes",
        "nofile",
        "output_preview_max_bytes",
    ):
        candidate = _profile()
        del candidate[missing]
        with pytest.raises(sandbox.SandboxError, match="Sandbox"):
            sandbox.validate_resource_profile(candidate, config)

    with pytest.raises(sandbox.SandboxError) as error:
        sandbox.validate_resource_profile(_profile(memory_bytes=513 * MiB), config)
    assert error.value.code == "resource_profile_exceeds_worker_capability"

    with pytest.raises(sandbox.SandboxError) as error:
        sandbox.validate_resource_profile(_profile(schema_version=True), config)
    assert error.value.code == "resource_profile_invalid"

    with pytest.raises(sandbox.SandboxError) as error:
        sandbox.validate_resource_profile(
            _profile(
                workspace_cleanup_total_timeout_seconds=20,
                recovery_grace_seconds=20,
            ),
            config,
        )
    assert error.value.code == "resource_profile_invalid"


def test_v3_payload_snapshots_are_required_to_match_the_queued_profile() -> None:
    config = _worker_config()
    profile = _profile()
    limits = sandbox.validate_resource_profile(profile, config)
    payload = _v3_payload("python", "def handle(context, input): return {}", profile=profile)
    sandbox.validate_v3_payload_snapshots(payload, limits)
    for name in (
        "execution_timeout_seconds",
        "recovery_grace_seconds_snapshot",
        "workspace_cleanup_attempt_timeout_seconds_snapshot",
        "workspace_cleanup_total_timeout_seconds_snapshot",
    ):
        changed = dict(payload)
        changed[name] = int(changed[name]) + 1
        with pytest.raises(sandbox.SandboxError) as error:
            sandbox.validate_v3_payload_snapshots(changed, limits)
        assert error.value.code == "resource_profile_invalid"


def test_adapter_environment_keeps_platform_credentials_outside_the_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "DLR_WORKER_TOKEN",
        "DLR_ADMIN_TOKEN",
        "DLR_CONTROL_URL",
        "DLR_RABBITMQ_URL",
        "DATABASE_URL",
    ):
        monkeypatch.setenv(key, "EXAMPLE_NOT_FOR_ADAPTER")
    environment = executor.child_env({"TASK_API_TOKEN": "task-secret"})
    assert all(
        key not in environment
        for key in (
            "DLR_WORKER_TOKEN",
            "DLR_ADMIN_TOKEN",
            "DLR_CONTROL_URL",
            "DLR_RABBITMQ_URL",
            "DATABASE_URL",
        )
    )
    assert environment["DLR_SECRET_TASK_API_TOKEN"] == "task-secret"


def test_recovery_marker_cannot_authorize_a_mount_outside_runtime_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    parent = tmp_path / "delegated"
    parent.mkdir(mode=0o700)
    (parent / "cgroup.controllers").write_text("cpu memory pids\n", encoding="ascii")
    (parent / "cgroup.subtree_control").write_text("cpu memory pids\n", encoding="ascii")
    (parent / "cgroup.procs").write_text("", encoding="ascii")
    (parent / "cgroup.kill").write_text("", encoding="ascii")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    recovery_root = runtime_root / "sandbox-recovery"
    recovery_root.mkdir(mode=0o700)
    outside_mount = tmp_path / "outside" / ".dlr-sandbox-mount"
    marker = recovery_root / "sandbox-attempt-7001-8001.json"
    marker.write_text(
        '{"cgroup_name":"attempt-7001-8001","execution_id":7001,'
        f'"mount_name":".dlr-sandbox-mount","mount_path":"{outside_mount}"}}\n',
        encoding="ascii",
    )
    marker.chmod(0o600)
    result = sandbox.recover(
        _worker_config(cgroup_path=parent),
        recovery_root,
        runtime_root=runtime_root,
    )
    assert result == {"inspected": 1, "completed": 0, "retained": 1}
    assert marker.exists()
    assert not outside_mount.exists()


def test_preflight_without_linux_delegation_fails_closed_without_side_effects(
    tmp_path: Path,
) -> None:
    recovery = tmp_path / "recovery"
    result = sandbox.run_preflight(_worker_config(), recovery_root=recovery)
    assert result["details"]["status"] == "failed"
    assert result["details"]["error_code"] == "sandbox_linux_target_required"
    assert result["capabilities"]["preflight_passed"] is False
    assert not recovery.exists()


def test_control_requires_the_complete_b3_capability_matrix() -> None:
    matrix = {key: True for key in REQUIRED_ISOLATION_CAPABILITIES}
    assert isolation_capabilities_ready(matrix)
    for key in REQUIRED_ISOLATION_CAPABILITIES:
        incomplete = dict(matrix)
        incomplete[key] = False
        assert not isolation_capabilities_ready(incomplete)


def test_agent_registration_uses_real_preflight_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DLR_WORKER_PROTOCOL_VERSION", "3")
    monkeypatch.setenv("DLR_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("DLR_WORKSPACE_CLEANUP_JOURNAL_ROOT", str(tmp_path / "journal"))
    config = worker_agent.WorkerConfig()
    config.capabilities = lambda: ["python"]  # type: ignore[method-assign]
    submitted: dict[str, Any] = {}

    def failed_preflight(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            # Even an inconsistent/malformed receipt must not turn an
            # environment claim into a v3-ready registration.
            "capabilities": {key: True for key in worker_agent.ISOLATION_CAPABILITY_KEYS},
            "details": {"status": "failed", "error_code": "sandbox_probe_failed"},
        }

    class Client:
        def register(self, _name: str, _capabilities: list[str], **kwargs: Any) -> dict[str, Any]:
            submitted.update(kwargs)
            return {"id": 42}

    monkeypatch.setattr(worker_agent.sandbox, "run_preflight", failed_preflight)
    worker = worker_agent.Agent(config, Client())  # type: ignore[arg-type]
    assert worker._register() == 42
    assert submitted["protocol_version"] == 3
    assert submitted["isolation_capabilities"]["preflight_passed"] is False
    assert submitted["isolation_capabilities"]["sandbox_cleanup"] is False


class _PrepareFailureClient:
    def __init__(self) -> None:
        self.body: dict[str, Any] | None = None

    def prepare_failed_attempt(
        self, _worker_id: int, _attempt_id: int, body: dict[str, Any]
    ) -> None:
        self.body = body


class _AckChannel:
    def __init__(self) -> None:
        self.acks: list[int] = []

    def basic_ack(self, *, delivery_tag: int) -> None:
        self.acks.append(delivery_tag)


class _ImmediateConnection:
    @staticmethod
    def add_callback_threadsafe(callback: Any) -> None:
        callback()


def test_consumer_rejects_profile_before_attempt_journal(tmp_path: Path) -> None:
    client = _PrepareFailureClient()
    consumer = V3Consumer(
        ConsumerConfig(
            worker_id=42,
            queue="dlr.worker.42.q",
            execution_slots=1,
            runtime_root=tmp_path / "runtime",
            attempt_journal_root=tmp_path / "journal",
        ),
        client,  # type: ignore[arg-type]
        connection_factory=lambda: object(),  # type: ignore[return-value]
        runtime_settings=SimpleNamespace(
            sandbox_config=_worker_config(memory_bytes=64 * MiB),
        ),
    )
    channel = _AckChannel()
    try:
        accepted = consumer._prepare_execute(
            _ImmediateConnection(),  # type: ignore[arg-type]
            channel,
            17,
            {
                "payload": _v3_payload("python", "def handle(context, input): return {}")
                | {"resource_profile": _profile(memory_bytes=128 * MiB)}
            },
        )
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)
    assert accepted is False
    assert channel.acks == [17]
    assert client.body is not None
    assert client.body["error_code"] == "resource_profile_exceeds_worker_capability"
    assert not (tmp_path / "journal").exists()
    assert not (tmp_path / "runtime").exists()


def test_consumer_rejects_stale_profile_snapshot_before_attempt_journal(tmp_path: Path) -> None:
    client = _PrepareFailureClient()
    consumer = V3Consumer(
        ConsumerConfig(
            worker_id=42,
            queue="dlr.worker.42.q",
            execution_slots=1,
            runtime_root=tmp_path / "runtime",
            attempt_journal_root=tmp_path / "journal",
        ),
        client,  # type: ignore[arg-type]
        connection_factory=lambda: object(),  # type: ignore[return-value]
        runtime_settings=SimpleNamespace(sandbox_config=_worker_config()),
    )
    raw_payload = _v3_payload("python", "def handle(context, input): return {}")
    raw_payload["execution_timeout_seconds"] = 21
    channel = _AckChannel()
    try:
        accepted = consumer._prepare_execute(
            _ImmediateConnection(),  # type: ignore[arg-type]
            channel,
            18,
            {"payload": raw_payload},
        )
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)
    assert accepted is False
    assert channel.acks == [18]
    assert client.body is not None
    assert client.body["error_code"] == "resource_profile_invalid"
    assert not (tmp_path / "journal").exists()
    assert not (tmp_path / "runtime").exists()


def _real_target_config() -> sandbox.SandboxConfig:
    if os.environ.get("DLR_B3_REAL_TARGET") != "1":
        pytest.skip("real delegated Linux target is opt-in")
    if sys.platform != "linux":
        pytest.fail("DLR_B3_REAL_TARGET requires Linux")
    config = sandbox.SandboxConfig.from_environment()
    if config.cgroup_path is None:
        pytest.fail("real target omitted DLR_SANDBOX_CGROUP_PATH")
    return config


def test_real_linux_preflight_receipt(tmp_path: Path) -> None:
    config = _real_target_config()
    result = sandbox.run_preflight(config, recovery_root=tmp_path / "sandbox-recovery")
    details = result["details"]
    assert details["status"] == "passed", details
    assert result["capabilities"]["preflight_passed"] is True
    assert details["agent_outside_attempt"] is True
    assert details["probe_in_attempt"] is True
    assert details["limits_readback"] == {
        "cpu.max": "100000 100000",
        "memory.max": "67108864",
        "memory.swap.max": "0",
        "pids.max": "64",
    }
    assert details["cleanup"]["status"] == "completed"
    assert details["cleanup"]["residue"] is False
    assert details["workspace_residue"] is False
    assert list((tmp_path / "sandbox-recovery").iterdir()) == []


@pytest.mark.parametrize(
    ("language", "code"),
    [
        (
            "python",
            "def handle(context, input):\n"
            "    from pathlib import Path\n"
            "    hidden = not Path('/sys/fs/cgroup/cgroup.controllers').exists()\n"
            "    return {'language': 'python', 'hidden_cgroup': hidden}\n",
        ),
        (
            "javascript",
            "export function handle(context, input) { return {language: 'javascript'}; }",
        ),
        (
            "java",
            "import java.util.LinkedHashMap;\n"
            "import java.util.Map;\n"
            "public class Adapter {\n"
            "  public Object handle(Context context, Object input) {\n"
            "    Map<String, Object> result = new LinkedHashMap<>();\n"
            '    result.put("language", "java");\n'
            "    return result;\n"
            "  }\n"
            "}\n",
        ),
    ],
    ids=["python", "javascript", "java"],
)
def test_real_linux_three_languages_are_sandboxed(tmp_path: Path, language: str, code: str) -> None:
    config = _real_target_config()
    payload = _v3_payload(
        language, code, execution_id=7100 + len(language), attempt_id=8100 + len(language)
    )
    result = executor.run(
        payload,
        executor.RuntimeSettings(
            runtime_root=tmp_path / language,
            execution_timeout_seconds=300,
            dep_install_timeout_seconds=120,
            workspace_cleanup_journal_root=tmp_path / language / "cleanup-journal",
            sandbox_config=config,
        ),
    )
    assert result["status"] == "succeeded", result
    assert result["cleanup_summary"]["sandbox"]["status"] == "completed"
    assert result["cleanup_summary"]["sandbox"]["residue"] is False
    assert result["workspace_cleanup_status"] == "completed"
    if language == "python":
        assert result["output"]["hidden_cgroup"] is True
    assert not list((tmp_path / language / "workspaces").glob("**/.dlr-sandbox-mount"))


@pytest.mark.parametrize("outcome", ["cancel", "timeout", "crash"])
def test_real_linux_cancel_timeout_crash_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    config = _real_target_config()
    monkeypatch.setattr(executor, "PROGRESS_POLL_SECONDS", 0.1)
    if outcome == "crash":
        code = "def handle(context, input):\n    raise RuntimeError('b3 crash')\n"
        expected = "failed"
        callback = None
        timeout = 20
    else:
        code = "import time\n"
        code += "def handle(context, input):\n    time.sleep(30)\n"
        expected = "cancelled" if outcome == "cancel" else "timeout"
        callback = (lambda _stdout, _stderr: True) if outcome == "cancel" else None
        timeout = 1 if outcome == "timeout" else 20
    payload = _v3_payload(
        "python",
        code,
        execution_id=7200 + ["cancel", "timeout", "crash"].index(outcome),
        attempt_id=8200 + ["cancel", "timeout", "crash"].index(outcome),
        timeout=timeout,
    )
    result = executor.run(
        payload,
        executor.RuntimeSettings(
            runtime_root=tmp_path / outcome,
            execution_timeout_seconds=300,
            dep_install_timeout_seconds=120,
            workspace_cleanup_journal_root=tmp_path / outcome / "cleanup-journal",
            sandbox_config=config,
        ),
        progress_callback=callback,
    )
    assert result["status"] == expected, result
    assert result["cleanup_summary"]["sandbox"]["status"] == "completed"
    assert result["cleanup_summary"]["sandbox"]["residue"] is False
    assert result["workspace_cleanup_status"] == "completed"
