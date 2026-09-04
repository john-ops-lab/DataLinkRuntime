"""Focused Batch 3 Worker sandbox and fail-closed contract tests.

The real-target cases are opt-in because this macOS checkout cannot prove a
Linux delegated cgroup.  The same file is executed inside the task-owned
Colima Worker container with ``DLR_B3_REAL_TARGET=1`` for the runtime receipt.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from contextlib import nullcontext
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dlr.control.schemas.worker import REQUIRED_ISOLATION_CAPABILITIES, isolation_capabilities_ready
from dlr.worker import agent as worker_agent
from dlr.worker import executor, sandbox
from dlr.worker import venv as venv_manager
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
        sandbox.validate_resource_profile(
            _profile(
                memory_bytes=513 * MiB,
                workspace_cleanup_attempt_timeout_seconds=21,
            ),
            config,
        )
    assert error.value.code == "resource_profile_invalid"

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


def test_helper_diagnostic_keeps_syscall_phase_and_errno_path_free() -> None:
    diagnostic = sandbox._parse_helper_diagnostic(
        "DLR_SANDBOX_HELPER_DIAGNOSTIC phase=payload_setup kind=os_error errno=1"
    )
    assert diagnostic is not None
    assert diagnostic.as_dict() == {
        "phase": "payload_setup",
        "kind": "os_error",
        "errno": 1,
        "error_code": "sandbox_payload_setup_failed",
    }
    assert (
        sandbox._parse_helper_diagnostic(
            "DLR_SANDBOX_HELPER_DIAGNOSTIC phase=payload_setup kind=os_error errno=0"
        )
        is None
    )
    assert (
        sandbox._parse_helper_diagnostic(
            "DLR_SANDBOX_HELPER_DIAGNOSTIC phase=payload_setup kind=exception errno=1"
        )
        is None
    )


def test_tmpfs_exhaustion_reads_the_exact_open_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[int] = []

    def fake_fstatvfs(descriptor: int) -> SimpleNamespace:
        observed.append(descriptor)
        return SimpleNamespace(f_bavail=0)

    monkeypatch.setattr(sandbox.os, "fstatvfs", fake_fstatvfs)
    assert sandbox._tmpfs_has_no_available_blocks(17) is True
    assert observed == [17]

    def unavailable(_descriptor: int) -> SimpleNamespace:
        raise OSError("tmpfs stat unavailable")

    monkeypatch.setattr(sandbox.os, "fstatvfs", unavailable)
    assert sandbox._tmpfs_has_no_available_blocks(17) is False


def test_dependency_tmpfs_is_discarded_before_adapter_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / ".dependency-tmp"
    target.mkdir()
    attempt = object.__new__(sandbox.AttemptSandbox)
    attempt._dependency_tmpfs = target
    unmounted: list[str] = []
    monkeypatch.setattr(sandbox, "_unmount", unmounted.append)

    attempt.unmount_dependency_tmpfs()
    attempt.unmount_dependency_tmpfs()

    assert unmounted == [str(target)]
    assert not target.exists()
    assert attempt._dependency_tmpfs is None


def test_workspace_command_rewrites_descendants_without_prefix_collisions(tmp_path: Path) -> None:
    source = tmp_path / "dlr-exec-1"
    target = Path("/proc/self/fd/7/dlr-exec-1")
    command = [
        "node",
        str(source / "harness.mjs"),
        str(source),
        str(tmp_path / "dlr-exec-10"),
        "--workspace",
    ]
    assert sandbox._replace_workspace(command, source, target) == [
        "node",
        "/proc/self/fd/7/dlr-exec-1/harness.mjs",
        "/proc/self/fd/7/dlr-exec-1",
        str(tmp_path / "dlr-exec-10"),
        "--workspace",
    ]
    assert (
        sandbox._parse_helper_diagnostic(
            "DLR_SANDBOX_HELPER_DIAGNOSTIC phase=payload_setup kind=os_error errno=1 /host/secret"
        )
        is None
    )


def test_supervisor_capability_mask_is_exactly_the_approved_three() -> None:
    assert sandbox.SUPERVISOR_CAPABILITY_MASK == 0x2000C0
    assert (
        sum(
            1 << capability
            for capability in (sandbox.CAP_SYS_ADMIN, sandbox.CAP_SETUID, sandbox.CAP_SETGID)
        )
        == sandbox.SUPERVISOR_CAPABILITY_MASK
    )


def test_resource_budget_keeps_agent_reserve_when_all_slots_are_used() -> None:
    config = _worker_config()
    budget = sandbox.ResourceBudget.for_worker(config, slots=2)
    limits = sandbox.ResourceLimits(
        cpu_cores=1.0,
        memory_bytes=256 * MiB,
        pids=64,
        tmp_bytes=16 * MiB,
        nofile=64,
        execution_timeout_seconds=20,
    )

    first = budget.try_reserve(limits)
    second = budget.try_reserve(limits)

    assert first is not None
    assert second is not None
    snapshot = budget.snapshot()
    assert snapshot["active_reservations"] == 2
    assert snapshot["agent_reserve"]["memory"] > 0
    assert snapshot["agent_reserve"]["pids"] > 0
    assert budget.try_reserve(limits) is None

    budget.release(first)
    budget.release(second)
    assert budget.snapshot()["active_reservations"] == 0
    assert budget.snapshot()["used"] == {"cpu": 0, "memory": 0, "pids": 0, "tmp": 0}


def test_verified_resource_budget_uses_deployment_envelope_for_all_slots() -> None:
    config = _worker_config()
    envelope = sandbox.ResourceEnvelope(
        cpu_cores=2.25,
        memory_bytes=2 * 1024 * MiB,
        pids=300,
        tmp_bytes=3 * 1024 * MiB,
        source="delegated_cgroup_v2(cpu,memory);host_pid_max;tmp=memory.max",
    )
    budget = sandbox.ResourceBudget.from_verified_envelope(config, slots=2, envelope=envelope)
    limits = sandbox.ResourceLimits(
        cpu_cores=1.0,
        memory_bytes=512 * MiB,
        pids=128,
        tmp_bytes=1 * 1024 * MiB,
        nofile=64,
        execution_timeout_seconds=20,
    )

    first = budget.try_reserve(limits)
    second = budget.try_reserve(limits)
    assert first is not None and second is not None
    assert budget.try_reserve(limits) is None
    snapshot = budget.snapshot()
    assert snapshot["capacity"] == {
        "cpu": 2.25,
        "memory": 2 * 1024 * MiB,
        "pids": 300,
        "tmp": 3 * 1024 * MiB,
    }
    assert snapshot["envelope_source"] == envelope.source
    budget.release(first)
    budget.release(second)
    assert budget.snapshot()["active_reservations"] == 0


def test_verified_resource_budget_rejects_envelope_that_cannot_leave_all_slot_reserve() -> None:
    config = _worker_config()
    envelope = sandbox.ResourceEnvelope(
        cpu_cores=2.0,
        memory_bytes=2 * 1024 * MiB,
        pids=300,
        tmp_bytes=3 * 1024 * MiB,
        source="delegated_cgroup_v2",
    )
    with pytest.raises(sandbox.SandboxError) as error:
        sandbox.ResourceBudget.from_verified_envelope(config, slots=2, envelope=envelope)
    assert error.value.code == "sandbox_resource_envelope_insufficient"


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


def test_recovery_marker_cannot_authorize_an_unrelated_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(
        sandbox,
        "_filesystem_magic",
        lambda _path: sandbox.CGROUP2_SUPER_MAGIC,
    )
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
    unrelated_mount = runtime_root / "unrelated" / ".dlr-sandbox-mount"
    unrelated_mount.mkdir(mode=0o700, parents=True)
    sentinel = unrelated_mount / "must-survive"
    sentinel.write_text("keep", encoding="ascii")
    marker = recovery_root / "sandbox-attempt-7001-8001.json"
    marker.write_text(
        '{"cgroup_name":"attempt-7001-8001","execution_id":7001,'
        f'"mount_name":".dlr-sandbox-mount","mount_path":"{unrelated_mount}"}}\n',
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
    assert sentinel.read_text(encoding="ascii") == "keep"
    assert unrelated_mount.is_dir()


def test_attempt_recovery_marker_removes_only_derived_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(
        sandbox,
        "_filesystem_magic",
        lambda _path: sandbox.CGROUP2_SUPER_MAGIC,
    )
    parent = (tmp_path / "delegated").resolve()
    parent.mkdir(mode=0o700)
    (parent / "cgroup.controllers").write_text("cpu memory pids\n", encoding="ascii")
    (parent / "cgroup.subtree_control").write_text("cpu memory pids\n", encoding="ascii")
    (parent / "cgroup.procs").write_text("", encoding="ascii")
    (parent / "cgroup.kill").write_text("", encoding="ascii")
    runtime_root = (tmp_path / "runtime").resolve()
    runtime_root.mkdir(mode=0o700)
    workspaces = runtime_root / "workspaces"
    workspaces.mkdir(mode=0o700)
    attempt_parent = workspaces / "attempt-8001"
    mount = attempt_parent / ".dlr-sandbox-mount"
    mount.mkdir(mode=0o700, parents=True)
    attempt_parent.chmod(0o700)
    sentinel = mount / "recovery-owned"
    sentinel.write_text("remove", encoding="ascii")
    child = parent / "attempt-7001-8001"
    child.mkdir(mode=0o700)
    (child / "cgroup.procs").write_text("", encoding="ascii")
    (child / "cgroup.kill").write_text("", encoding="ascii")
    real_rmdir = Path.rmdir

    def rmdir_fake_cgroup(path: Path) -> None:
        if path == child:
            (path / "cgroup.procs").unlink()
            (path / "cgroup.kill").unlink()
        real_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", rmdir_fake_cgroup)
    recovery_root = runtime_root / "sandbox-recovery"
    recovery_root.mkdir(mode=0o700, parents=True)
    marker = recovery_root / "sandbox-attempt-7001-8001.json"
    marker.write_text(
        json.dumps(
            {
                "cgroup_name": "attempt-7001-8001",
                "execution_id": 7001,
                "mount_name": ".dlr-sandbox-mount",
                "mount_path": str(mount),
            }
        )
        + "\n",
        encoding="ascii",
    )
    marker.chmod(0o600)

    result = sandbox.recover(
        _worker_config(cgroup_path=parent),
        recovery_root,
        runtime_root=runtime_root,
    )

    assert result == {"inspected": 1, "completed": 1, "retained": 0}
    assert not marker.exists()
    assert not child.exists()
    assert not mount.exists()
    assert not sentinel.exists()
    assert attempt_parent.is_dir()


def test_preflight_recovery_marker_cannot_authorize_an_unrelated_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(
        sandbox,
        "_filesystem_magic",
        lambda _path: sandbox.CGROUP2_SUPER_MAGIC,
    )
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
    preflight_name = "dlr-preflight-" + "a" * 32
    (runtime_root / preflight_name).mkdir(mode=0o700)
    unrelated_mount = runtime_root / "sentinel_dir" / ".dlr-sandbox-mount"
    unrelated_mount.mkdir(mode=0o700, parents=True)
    sentinel = unrelated_mount / "must-survive"
    sentinel.write_text("keep", encoding="ascii")
    marker = recovery_root / f"sandbox-{preflight_name}.json"
    marker.write_text(
        json.dumps(
            {
                "cgroup_name": preflight_name,
                "execution_id": 1,
                "mount_name": ".dlr-sandbox-mount",
                "mount_path": str(unrelated_mount),
            }
        )
        + "\n",
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
    assert sentinel.read_text(encoding="ascii") == "keep"
    assert unrelated_mount.is_dir()


def test_preflight_recovery_marker_removes_derived_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(
        sandbox,
        "_filesystem_magic",
        lambda _path: sandbox.CGROUP2_SUPER_MAGIC,
    )
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
    preflight_name = "dlr-preflight-" + "c" * 32
    preflight_directory = runtime_root / preflight_name
    mount = preflight_directory / ".dlr-sandbox-mount"
    preflight_directory.mkdir(mode=0o700)
    mount.mkdir(mode=0o700)
    sentinel = mount / "recovery-owned"
    sentinel.write_text("remove", encoding="ascii")
    marker = recovery_root / f"sandbox-{preflight_name}.json"
    marker.write_text(
        json.dumps(
            {
                "cgroup_name": preflight_name,
                "execution_id": 1,
                "mount_name": ".dlr-sandbox-mount",
                "mount_path": str(mount),
            }
        )
        + "\n",
        encoding="ascii",
    )
    marker.chmod(0o600)
    result = sandbox.recover(
        _worker_config(cgroup_path=parent),
        recovery_root,
        runtime_root=runtime_root,
    )
    assert result == {"inspected": 1, "completed": 1, "retained": 0}
    assert not marker.exists()
    assert not mount.exists()
    assert not sentinel.exists()
    assert preflight_directory.is_dir()


def test_preflight_recovery_marker_requires_private_worker_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(
        sandbox,
        "_filesystem_magic",
        lambda _path: sandbox.CGROUP2_SUPER_MAGIC,
    )
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
    preflight_name = "dlr-preflight-" + "b" * 32
    preflight_directory = runtime_root / preflight_name
    mount = preflight_directory / ".dlr-sandbox-mount"
    mount.mkdir(mode=0o700, parents=True)
    sentinel = mount / "must-survive"
    sentinel.write_text("keep", encoding="ascii")
    preflight_directory.chmod(0o755)
    marker = recovery_root / f"sandbox-{preflight_name}.json"
    marker.write_text(
        json.dumps(
            {
                "cgroup_name": preflight_name,
                "execution_id": 1,
                "mount_name": ".dlr-sandbox-mount",
                "mount_path": str(mount),
            }
        )
        + "\n",
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
    assert sentinel.read_text(encoding="ascii") == "keep"
    assert mount.is_dir()


def test_timeout_cleanup_is_idempotent_after_helper_termination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout's later cleanup retry must not recreate a residue marker."""

    cgroup = tmp_path / "attempt"
    cgroup.mkdir(mode=0o700)
    stream_path = tmp_path / "stream.log"
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    attempt = object.__new__(sandbox.AttemptSandbox)
    attempt.limits = sandbox.ResourceLimits(
        cpu_cores=1.0,
        memory_bytes=64 * MiB,
        pids=64,
        tmp_bytes=1 * MiB,
        nofile=64,
        execution_timeout_seconds=1,
        cleanup_attempt_seconds=1,
        cleanup_total_seconds=2,
    )
    attempt.cgroup_name = "attempt-timeout"
    attempt.cgroup = cgroup
    attempt.mount_root = tmp_path / "mount"
    attempt._process = process
    attempt._killed = False
    attempt._unmounted = False
    attempt._dependency_tmpfs = None
    attempt._cleanup_lock = threading.Lock()
    attempt._cleanup_result = None

    def terminate_process(_process: subprocess.Popen[bytes] | None = None) -> None:
        if process.poll() is None:
            process.kill()
            process.wait()
        attempt._killed = True

    attempt.kill = terminate_process  # type: ignore[attr-defined]
    monkeypatch.setattr(sandbox, "_is_empty", lambda _path: process.poll() is not None)
    try:
        _returncode, timed_out, _cancelled, _text = executor._wait_with_progress(
            process,
            stream_path,
            timeout=0.05,
            progress_callback=None,
            kill_callback=lambda: attempt.kill(process),  # type: ignore[attr-defined]
        )
        first = attempt.cleanup()
        second = attempt.cleanup()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()

    assert timed_out is True
    assert first.status == "completed"
    assert first.residue is False
    assert second is first
    assert not cgroup.exists()
    assert not list(tmp_path.glob("**/*recovery*"))


def test_timeout_cleanup_serializes_concurrent_retries_without_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent timeout/error cleanup must publish one terminal result."""

    cgroup = tmp_path / "attempt"
    cgroup.mkdir(mode=0o700)
    attempt = object.__new__(sandbox.AttemptSandbox)
    attempt.limits = sandbox.ResourceLimits(
        cpu_cores=1.0,
        memory_bytes=64 * MiB,
        pids=64,
        tmp_bytes=1 * MiB,
        nofile=64,
        execution_timeout_seconds=1,
        cleanup_attempt_seconds=1,
        cleanup_total_seconds=2,
    )
    attempt.cgroup_name = "attempt-timeout-concurrent"
    attempt.cgroup = cgroup
    attempt.mount_root = tmp_path / "mount"
    attempt._process = None
    attempt._killed = False
    attempt._unmounted = False
    attempt._dependency_tmpfs = None
    attempt._cleanup_lock = threading.Lock()
    attempt._cleanup_result = None
    monkeypatch.setattr(sandbox, "_is_empty", lambda _path: True)

    real_rmdir = Path.rmdir
    first_rmdir_entered = threading.Event()
    release_first_rmdir = threading.Event()
    rmdir_calls = 0
    rmdir_lock = threading.Lock()

    def blocking_rmdir(path: Path) -> None:
        nonlocal rmdir_calls
        if path == cgroup:
            with rmdir_lock:
                rmdir_calls += 1
                first_call = rmdir_calls == 1
            if first_call:
                first_rmdir_entered.set()
                assert release_first_rmdir.wait(timeout=2)
        real_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", blocking_rmdir)
    results: list[sandbox.CleanupResult] = []

    def cleanup() -> None:
        results.append(attempt.cleanup())

    first = threading.Thread(target=cleanup)
    second = threading.Thread(target=cleanup)
    first.start()
    assert first_rmdir_entered.wait(timeout=2)
    second.start()
    release_first_rmdir.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert rmdir_calls == 1
    assert len(results) == 2
    assert all(result.status == "completed" and not result.residue for result in results)
    assert results[0] is results[1]
    assert not cgroup.exists()


def test_configured_cgroup_hide_target_is_cgroup2fs_and_disjoint_from_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    target = tmp_path / "delegated"
    target.mkdir(mode=0o700)
    workspace = tmp_path / "runtime" / "workspaces" / "dlr-exec-1"
    workspace.mkdir(mode=0o700, parents=True)
    mount_root = workspace.parent / ".dlr-sandbox-mount"

    monkeypatch.setattr(sandbox, "_filesystem_magic", lambda _path: 0)
    with pytest.raises(OSError, match="not cgroup2fs"):
        sandbox._validated_hidden_cgroup_path(str(target), mount_root, workspace)

    monkeypatch.setattr(sandbox, "_filesystem_magic", lambda _path: sandbox.CGROUP2_SUPER_MAGIC)
    assert sandbox._validated_hidden_cgroup_path(str(target), mount_root, workspace) == target
    with pytest.raises(OSError, match="overlaps workspace"):
        sandbox._validated_hidden_cgroup_path(str(workspace), mount_root, workspace)


def test_copied_tmpfs_workspace_allows_payload_output_but_not_managed_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creation-time ownership enables payload writes without relaxing input modes."""

    if os.name != "posix" or os.getuid() == 0:
        pytest.skip("requires a non-root POSIX payload identity")

    source = tmp_path / "staging"
    source.mkdir(mode=0o700)
    managed_input = source / "input" / "input-00.bin"
    managed_input.parent.mkdir(mode=0o700)
    managed_input.write_bytes(b"managed")
    managed_input.chmod(0o444)
    managed_input.parent.chmod(0o555)
    (source / "temp").mkdir(mode=0o700)
    (source / "output").mkdir(mode=0o700)
    (source / "adapter.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "adapter.py").chmod(0o600)
    (source / "input-link").symlink_to(managed_input)

    copied = tmp_path / "tmpfs" / source.name
    copied.mkdir(mode=0o700, parents=True)
    # The real Linux path uses setfsuid/setfsgid.  Keep this unit test portable
    # while exercising the same no-follow copy topology on the macOS checkout.
    monkeypatch.setattr(sandbox, "_filesystem_identity", lambda _uid, _gid: nullcontext())
    sandbox._copy_tree_as_owner(source, copied, os.getuid(), os.getgid())

    copied_input = copied / "input" / managed_input.name
    assert copied.stat().st_uid == os.getuid()
    assert copied_input.stat().st_uid == os.getuid()
    assert stat.S_IMODE((copied / "input").stat().st_mode) == 0o555
    assert stat.S_IMODE(copied_input.stat().st_mode) == 0o444
    assert (copied / "input-link").is_symlink()
    assert os.readlink(copied / "input-link") == os.readlink(source / "input-link")

    (copied / "output.json").write_text('{"ok":true}\n', encoding="utf-8")
    (copied / "temp" / "fill-1").write_bytes(b"x" * (1024 * 1024))
    with pytest.raises(PermissionError):
        copied_input.write_bytes(b"must remain read-only")


def test_bounded_output_reads_payload_owned_source_and_writes_host_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "tmpfs" / "output.json"
    destination = tmp_path / "host" / "output.json"
    metadata = tmp_path / "host" / ".dlr-output-meta"
    source.parent.mkdir()
    destination.parent.mkdir(mode=0o700)
    source.write_text('{"ok":true}\n', encoding="ascii")
    source.chmod(0o600)
    identities: list[tuple[int, int]] = []

    def fake_filesystem_identity(uid: int, gid: int) -> Any:
        identities.append((uid, gid))
        return nullcontext()

    monkeypatch.setattr(sandbox, "_filesystem_identity", fake_filesystem_identity)
    sandbox._copy_bounded_output(
        source,
        destination,
        metadata,
        1024,
        source_uid=501,
        source_gid=1000,
    )

    assert identities == [(501, 1000)]
    assert destination.read_text(encoding="ascii") == '{"ok":true}\n'
    assert json.loads(metadata.read_text(encoding="ascii")) == {
        "size": len('{"ok":true}\n'),
        "truncated": False,
    }


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
    assert "adapter_mount_blocked" in REQUIRED_ISOLATION_CAPABILITIES
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


def test_agent_verifies_finite_envelope_before_v3_registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DLR_WORKER_PROTOCOL_VERSION", "3")
    monkeypatch.setenv("DLR_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("DLR_WORKSPACE_CLEANUP_JOURNAL_ROOT", str(tmp_path / "journal"))
    config = worker_agent.WorkerConfig()
    config.capabilities = lambda: ["python"]  # type: ignore[method-assign]
    events: list[str] = []
    submitted: dict[str, Any] = {}

    def unavailable_envelope(*_args: Any, **_kwargs: Any) -> sandbox.ResourceEnvelope:
        events.append("envelope")
        raise sandbox.SandboxError("sandbox_resource_envelope_unavailable")

    def unexpected_probe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("the disposable probe must not run after envelope failure")

    class Client:
        def register(self, _name: str, _capabilities: list[str], **kwargs: Any) -> dict[str, Any]:
            events.append("register")
            submitted.update(kwargs)
            return {"id": 43}

    monkeypatch.setattr(
        worker_agent.sandbox, "read_verified_resource_envelope", unavailable_envelope
    )
    monkeypatch.setattr(worker_agent.sandbox, "run_preflight", unexpected_probe)
    worker = worker_agent.Agent(config, Client())  # type: ignore[arg-type]

    assert worker._register() == 43
    assert events == ["envelope", "register"]
    assert submitted["isolation_capabilities"]["resource_envelope_verified"] is False
    assert submitted["isolation_capabilities"]["preflight_passed"] is False


def test_agent_passes_the_verified_envelope_snapshot_to_the_consumer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DLR_WORKER_PROTOCOL_VERSION", "3")
    monkeypatch.setenv("DLR_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("DLR_WORKSPACE_CLEANUP_JOURNAL_ROOT", str(tmp_path / "journal"))
    config = worker_agent.WorkerConfig()
    envelope = sandbox.ResourceEnvelope(
        cpu_cores=8.0,
        memory_bytes=8 * 1024 * MiB,
        pids=1024,
        tmp_bytes=8 * 1024 * MiB,
        source="delegated_cgroup_v2(test)",
    )
    monkeypatch.setattr(
        worker_agent.sandbox,
        "read_verified_resource_envelope",
        lambda *_args, **_kwargs: envelope,
    )
    monkeypatch.setattr(
        worker_agent.sandbox,
        "run_preflight",
        lambda *_args, **_kwargs: {
            "capabilities": {key: True for key in worker_agent.ISOLATION_CAPABILITY_KEYS},
            "details": {"status": "passed"},
        },
    )

    config.run_preflight()

    assert config.isolation_capabilities["resource_envelope_verified"] is True
    assert config._verified_resource_envelope is envelope
    assert config.runtime_settings().resource_envelope is envelope


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


def test_consumer_reports_intrinsic_profile_error_before_ceiling_or_model_validation(
    tmp_path: Path,
) -> None:
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
    raw_payload = _v3_payload(
        "python",
        "def handle(context, input): return {}",
        profile=_profile(
            memory_bytes=128 * MiB,
            workspace_cleanup_attempt_timeout_seconds=21,
        ),
    )
    channel = _AckChannel()
    try:
        accepted = consumer._prepare_execute(
            _ImmediateConnection(),  # type: ignore[arg-type]
            channel,
            19,
            {"payload": raw_payload},
        )
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)
    assert accepted is False
    assert channel.acks == [19]
    assert client.body is not None
    assert client.body["error_code"] == "resource_profile_invalid"
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


def _allow_payload_traversal(path: Path) -> None:
    """Let uid 501 traverse pytest's root-owned temp parent directories."""

    temporary_root = Path("/tmp").resolve()
    candidate = path.resolve()
    if not candidate.is_relative_to(temporary_root):
        pytest.fail("real Linux B3 tests require a disposable /tmp workspace")
    while candidate != temporary_root:
        candidate.chmod(stat.S_IMODE(candidate.stat().st_mode) | 0o111)
        candidate = candidate.parent


def test_real_linux_preflight_receipt(tmp_path: Path) -> None:
    config = _real_target_config()
    result = sandbox.run_preflight(config, recovery_root=tmp_path / "sandbox-recovery")
    details = result["details"]
    assert details["status"] == "passed", details
    assert result["capabilities"]["preflight_passed"] is True
    assert details["agent_outside_attempt"] is True
    assert details["probe_in_attempt"] is True
    identity = details["adapter_identity"]
    assert identity == {
        "uid": 501,
        "gid": 1000,
        "Groups": "",
        "CapPrm": "0000000000000000",
        "CapEff": "0000000000000000",
        "CapInh": "0000000000000000",
        "CapBnd": identity["CapBnd"],
        "CapAmb": "0000000000000000",
        "NoNewPrivs": "1",
    }
    assert isinstance(identity["CapBnd"], str)
    assert int(identity["CapBnd"], 16) >= 0
    assert details["adapter_mount"]["blocked"] is True
    assert details["adapter_mount"]["errno"] in {1, 13}
    assert details["limits_readback"] == {
        "cpu.max": "100000 100000",
        "memory.max": "67108864",
        "memory.swap.max": "0",
        "pids.max": "64",
    }
    assert details["cleanup"]["status"] == "completed"
    assert details["cleanup"]["residue"] is False
    assert details["workspace_residue"] is False
    recovery_root = tmp_path / "sandbox-recovery"
    assert not recovery_root.exists() or list(recovery_root.iterdir()) == []


@pytest.mark.parametrize(
    ("language", "code"),
    [
        (
            "python",
            "from pathlib import Path\n"
            "def probe(root):\n"
            "    result = {'read_blocked': False, 'write_blocked': False}\n"
            "    try:\n"
            "        (root / 'cgroup.controllers').read_text()\n"
            "    except OSError:\n"
            "        result['read_blocked'] = True\n"
            "    try:\n"
            "        (root / 'dlr-write-probe').write_text('x')\n"
            "    except OSError:\n"
            "        result['write_blocked'] = True\n"
            "    if not all(result.values()):\n"
            "        raise RuntimeError('cgroup control plane visible')\n"
            "    return result\n"
            "def handle(context, input):\n"
            "    paths = ['/run/dlr-cgroup', '/sys/fs/cgroup']\n"
            "    return {'language': 'python', 'hidden_cgroup': {\n"
            "        path: probe(Path(path)) for path in paths\n"
            "    }}\n",
        ),
        (
            "javascript",
            "import fs from 'node:fs';\n"
            "import path from 'node:path';\n"
            "function probe(root) {\n"
            "  let readBlocked = false;\n"
            "  try { fs.readFileSync(path.join(root, 'cgroup.controllers'), 'utf8'); }\n"
            "  catch { readBlocked = true; }\n"
            "  let writeBlocked = false;\n"
            "  try { fs.writeFileSync(path.join(root, 'dlr-write-probe'), 'x'); }\n"
            "  catch { writeBlocked = true; }\n"
            "  if (!readBlocked || !writeBlocked)\n"
            "    throw new Error('cgroup control plane visible');\n"
            "  return {read_blocked: readBlocked, write_blocked: writeBlocked};\n"
            "}\n"
            "export function handle(context, input) {\n"
            "  const hidden = {};\n"
            "  for (const root of ['/run/dlr-cgroup', '/sys/fs/cgroup']) {\n"
            "    hidden[root] = probe(root);\n"
            "  }\n"
            "  return {language: 'javascript', hidden_cgroup: hidden};\n"
            "}\n",
        ),
        (
            "java",
            "import java.util.LinkedHashMap;\n"
            "import java.util.Map;\n"
            "import java.nio.file.Files;\n"
            "import java.nio.file.Path;\n"
            "public class Adapter {\n"
            "  private static Map<String, Boolean> probe(Path root) throws Exception {\n"
            "    boolean readBlocked = false;\n"
            '    try { Files.readString(root.resolve("cgroup.controllers")); }\n'
            "    catch (Exception error) { readBlocked = true; }\n"
            "    boolean writeBlocked = false;\n"
            '    try { Files.writeString(root.resolve("dlr-write-probe"), "x"); }\n'
            "    catch (Exception error) { writeBlocked = true; }\n"
            "    if (!readBlocked || !writeBlocked)\n"
            '      throw new Exception("cgroup control plane visible");\n'
            "    Map<String, Boolean> result = new LinkedHashMap<>();\n"
            '    result.put("read_blocked", readBlocked);\n'
            '    result.put("write_blocked", writeBlocked);\n'
            "    return result;\n"
            "  }\n"
            "  public Object handle(Context context, Object input) {\n"
            "    Map<String, Object> result = new LinkedHashMap<>();\n"
            "    Map<String, Object> hidden = new LinkedHashMap<>();\n"
            "    try {\n"
            '      hidden.put("/run/dlr-cgroup", probe(Path.of("/run/dlr-cgroup")));\n'
            '      hidden.put("/sys/fs/cgroup", probe(Path.of("/sys/fs/cgroup")));\n'
            "    } catch (Exception error) { throw new RuntimeException(error); }\n"
            '    result.put("language", "java");\n'
            '    result.put("hidden_cgroup", hidden);\n'
            "    return result;\n"
            "  }\n"
            "}\n",
        ),
    ],
    ids=["python", "javascript", "java"],
)
def test_real_linux_three_languages_are_sandboxed(tmp_path: Path, language: str, code: str) -> None:
    config = _real_target_config()
    _allow_payload_traversal(tmp_path)
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
    assert result["output"]["hidden_cgroup"] == {
        "/run/dlr-cgroup": {"read_blocked": True, "write_blocked": True},
        "/sys/fs/cgroup": {"read_blocked": True, "write_blocked": True},
    }
    assert not list((tmp_path / language / "workspaces").glob("**/.dlr-sandbox-mount"))


def test_real_linux_dependency_resource_failure_is_classified_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _real_target_config()
    _allow_payload_traversal(tmp_path)

    def fail_preparation(*_args: object, **kwargs: object) -> Path:
        context = kwargs.get("dependency_context")
        assert isinstance(context, venv_manager.DependencyExecutionContext)
        assert context.tmpdir.is_dir()
        raise venv_manager.DependencyPreparationError(
            "dependency process exceeded memory",
            "",
            error_code="resource_exceeded_memory",
        )

    monkeypatch.setattr(venv_manager, "prepare_version_venv", fail_preparation)
    result = executor.run(
        _v3_payload(
            "python",
            "def handle(context, input): return {}",
            execution_id=7198,
            attempt_id=8198,
        ),
        executor.RuntimeSettings(
            runtime_root=tmp_path / "dependency-resource",
            execution_timeout_seconds=300,
            dep_install_timeout_seconds=120,
            workspace_cleanup_journal_root=tmp_path / "dependency-resource-journal",
            sandbox_config=config,
        ),
    )

    assert result["status"] == "resource_exceeded", result
    assert result["error_code"] == "resource_exceeded_memory", result
    assert result["cleanup_summary"]["sandbox"]["status"] == "completed"
    assert result["cleanup_summary"]["sandbox"]["residue"] is False
    assert not list((tmp_path / "dependency-resource").glob("**/.dependency-tmp"))


@pytest.mark.parametrize("outcome", ["cancel", "timeout", "crash"])
def test_real_linux_cancel_timeout_crash_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    config = _real_target_config()
    _allow_payload_traversal(tmp_path)
    monkeypatch.setattr(executor, "PROGRESS_POLL_SECONDS", 0.1)
    if outcome == "crash":
        code = (
            "def handle(context, input):\n"
            "    print('DLR_TEST_CRASH_STARTED', flush=True)\n"
            "    raise RuntimeError('b3 crash')\n"
        )
        expected = "failed"
        callback = None
        timeout = 20
    else:
        code = (
            "import time\n"
            "def handle(context, input):\n"
            "    print('DLR_TEST_SLEEP_STARTED', flush=True)\n"
            "    time.sleep(30)\n"
        )
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
    expected_marker = "DLR_TEST_CRASH_STARTED" if outcome == "crash" else "DLR_TEST_SLEEP_STARTED"
    assert expected_marker in result["stdout"], result
    if outcome == "crash":
        assert "b3 crash" in result["stdout"], result
    assert result["cleanup_summary"]["sandbox"]["status"] == "completed"
    assert result["cleanup_summary"]["sandbox"]["residue"] is False
    assert result["workspace_cleanup_status"] == "completed"
