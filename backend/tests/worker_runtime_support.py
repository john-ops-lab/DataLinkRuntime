"""Explicit Sandbox doubles for runtime unit tests, never production configuration.

These tests still execute the real language harnesses and dependency preparation.
Linux resource isolation is verified separately by the real-target Sandbox suite.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dlr.common.config import settings
from dlr.worker import executor, sandbox
from dlr.worker import venv as venv_manager


def unit_sandbox_config() -> sandbox.SandboxConfig:
    return sandbox.SandboxConfig(
        cgroup_path=Path("/test-only/delegated-cgroup"),
        execution_timeout_seconds=86_400,
        stream_max_bytes=settings.execution_stream_max_bytes,
        output_max_bytes=settings.execution_output_max_bytes,
        output_preview_max_bytes=settings.execution_output_preview_max_bytes,
    )


def unit_resource_envelope() -> sandbox.ResourceEnvelope:
    return sandbox.ResourceEnvelope(16.0, 8 << 30, 2048, 16 << 30, "unit-test-double")


def attempt_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build a complete current envelope from a test's business input fields."""
    value: dict[str, Any] = {
        "protocol_version": 3,
        "dispatch_backend": "rabbitmq",
        "attempt_id": int(payload.get("execution_id", 1)) + 1000,
        "attempt_no": 1,
        "fencing_token": 1,
        "claim_token": "unit-test-claim-token",
        "cleanup_token": "unit-test-cleanup-token",
        "lease_expires_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
        "lease_seconds": 60,
        "renew_seconds": 15,
        "execution_timeout_seconds": 30,
        "language": "python",
        "requirements": "",
        "input": None,
        "runtime_config": {},
        "secrets": {},
        "input_files": [],
        "input_source_type": "none",
        "input_snapshot": {"source_type": "none"},
        "credential_bindings": [],
        "latest_version_id": payload.get("version_id"),
        "locale": "zh-CN",
        "recovery_grace_seconds_snapshot": 60,
        "workspace_cleanup_attempt_timeout_seconds_snapshot": 5,
        "workspace_cleanup_total_timeout_seconds_snapshot": 20,
        **payload,
    }
    config = unit_sandbox_config()
    limits = sandbox.ResourceLimits(
        cpu_cores=config.cpu_cores,
        memory_bytes=config.memory_bytes,
        pids=config.pids,
        tmp_bytes=config.tmp_bytes,
        nofile=config.nofile,
        execution_timeout_seconds=value["execution_timeout_seconds"],
        claim_timeout_seconds=config.claim_timeout_seconds,
        recovery_grace_seconds=value["recovery_grace_seconds_snapshot"],
        cleanup_attempt_seconds=value["workspace_cleanup_attempt_timeout_seconds_snapshot"],
        cleanup_total_seconds=value["workspace_cleanup_total_timeout_seconds_snapshot"],
        stream_max_bytes=config.stream_max_bytes,
        output_max_bytes=config.output_max_bytes,
        output_preview_max_bytes=config.output_preview_max_bytes,
    )
    value.setdefault(
        "resource_profile",
        {"schema_version": 1, "resource_class": "standard", "backend": "cgroup_v2"}
        | limits.as_dict(),
    )
    return value


class UnitAttemptSandbox:
    """Only the kernel isolation boundary is replaced with a subprocess double."""

    def __init__(self, _config: Any, limits: Any, *, workspace: Path, **_kwargs: Any) -> None:
        self.workspace = workspace
        self.limits = limits
        self.limits_readback: dict[str, Any] = {}
        self.cgroup = workspace / ".test-cgroup"
        self._dependency_tmp: Path | None = None

    def mount_dependency_tmpfs(self, path: Path, *, max_bytes: int) -> None:
        self._dependency_tmp = path
        path.mkdir(parents=True, exist_ok=True)

    def unmount_dependency_tmpfs(self) -> None:
        pass

    def start(
        self, command: list[str], *, stdout: Any, environment: Mapping[str, str]
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(  # noqa: S603 - real harness inside explicit unit-test double
            command,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=dict(environment),
            cwd=str(self.workspace),
        )

    def kill(self, process: subprocess.Popen[bytes]) -> None:
        executor._kill_process_group(process)

    def resource_error_code(self) -> None:
        return None

    def resource_usage(self) -> dict[str, Any]:
        return {}

    def read_helper_diagnostic(self) -> None:
        return None

    def cleanup(self) -> sandbox.CleanupResult:
        return sandbox.CleanupResult("completed")


def install_test_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "AttemptSandbox", UnitAttemptSandbox)
    # Dependency commands remain real but do not write to the host cgroup in unit tests.
    monkeypatch.setattr(venv_manager, "DependencyExecutionContext", lambda **_kwargs: None)


def run_with_test_sandbox(
    payload: dict[str, Any], config: executor.RuntimeSettings, *args: Any, **kwargs: Any
) -> dict[str, Any]:
    with pytest.MonkeyPatch.context() as patch:
        install_test_sandbox(patch)
        return executor.run(
            attempt_payload(
                {"execution_timeout_seconds": config.execution_timeout_seconds} | payload
            ),
            replace(
                config,
                sandbox_config=unit_sandbox_config(),
                resource_envelope=unit_resource_envelope(),
            ),
            *args,
            **kwargs,
        )
