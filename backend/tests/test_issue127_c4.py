"""C4 deployment checks for the real Worker protocol selection."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from dlr.common.config import Settings
from dlr.worker.agent import Agent, WorkerConfig

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_C4_ENV_NAMES = (
    "DLR_MIN_WORKER_PROTOCOL_VERSION",
    "DLR_EXECUTION_CLAIM_TIMEOUT_SECONDS",
    "DLR_EXECUTION_RECOVERY_GRACE_SECONDS",
    "DLR_WORKSPACE_CLEANUP_ATTEMPT_TIMEOUT_SECONDS",
    "DLR_WORKSPACE_CLEANUP_TOTAL_TIMEOUT_SECONDS",
    "DLR_MANAGED_FILES_ENABLED",
    "DLR_ARTIFACT_DELETE_ALERT_THRESHOLD",
)


def _clear_c4_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _C4_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def _set_c4_environment(monkeypatch: pytest.MonkeyPatch, **values: int | str) -> None:
    _clear_c4_environment(monkeypatch)
    for name, value in values.items():
        monkeypatch.setenv(name, str(value))


def test_control_c4_settings_default_to_enabled_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_c4_environment(monkeypatch)

    value = Settings()

    assert "min_worker_protocol_version" not in Settings.model_fields
    assert value.execution_claim_timeout_seconds == 300
    assert value.execution_recovery_grace_seconds == 60
    assert value.workspace_cleanup_attempt_timeout_seconds == 5
    assert value.workspace_cleanup_total_timeout_seconds == 20
    assert value.managed_files_enabled is True
    assert value.artifact_delete_alert_threshold == 5


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [(None, True), ("true", True), ("false", False)],
)
def test_control_c4_managed_files_environment_matrix(
    monkeypatch: pytest.MonkeyPatch,
    raw_value: str | None,
    expected: bool,
) -> None:
    _clear_c4_environment(monkeypatch)
    if raw_value is not None:
        monkeypatch.setenv("DLR_MANAGED_FILES_ENABLED", raw_value)

    assert Settings().managed_files_enabled is expected


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [(None, "true"), ("true", "true"), ("false", "false")],
)
def test_control_c4_compose_managed_files_environment_matrix(
    raw_value: str | None,
    expected: str,
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is needed for the configuration-only audit")
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("DLR_", "COMPOSE_"))
    }
    environment.update(
        DLR_RABBITMQ_USER="audit-user",
        DLR_RABBITMQ_PASSWORD="audit-password",
        DLR_ADMIN_TOKEN="audit-admin",
        DLR_WORKER_TOKEN="audit-worker",
    )
    if raw_value is not None:
        environment["DLR_MANAGED_FILES_ENABLED"] = raw_value

    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            "docker-compose.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=_REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    rendered = json.loads(completed.stdout)
    assert rendered["services"]["control"]["environment"]["DLR_MANAGED_FILES_ENABLED"] == expected


@pytest.mark.parametrize(
    ("name", "low", "high"),
    [
        ("DLR_EXECUTION_CLAIM_TIMEOUT_SECONDS", 30, 86_400),
        ("DLR_EXECUTION_RECOVERY_GRACE_SECONDS", 10, 3_600),
        ("DLR_WORKSPACE_CLEANUP_ATTEMPT_TIMEOUT_SECONDS", 1, 60),
        ("DLR_WORKSPACE_CLEANUP_TOTAL_TIMEOUT_SECONDS", 5, 300),
        ("DLR_ARTIFACT_DELETE_ALERT_THRESHOLD", 1, 100),
    ],
)
def test_control_c4_settings_accept_documented_boundaries(
    monkeypatch: pytest.MonkeyPatch, name: str, low: int, high: int
) -> None:
    # Keep the cross-field cleanup invariant valid while testing each field's
    # independent lower and upper boundary.
    values: dict[str, int] = {
        "DLR_EXECUTION_CLAIM_TIMEOUT_SECONDS": 300,
        "DLR_EXECUTION_RECOVERY_GRACE_SECONDS": 3_600,
        "DLR_WORKSPACE_CLEANUP_ATTEMPT_TIMEOUT_SECONDS": 1,
        "DLR_WORKSPACE_CLEANUP_TOTAL_TIMEOUT_SECONDS": 5,
    }
    if name == "DLR_WORKSPACE_CLEANUP_ATTEMPT_TIMEOUT_SECONDS":
        values["DLR_WORKSPACE_CLEANUP_TOTAL_TIMEOUT_SECONDS"] = high
    elif name == "DLR_WORKSPACE_CLEANUP_TOTAL_TIMEOUT_SECONDS":
        values["DLR_EXECUTION_RECOVERY_GRACE_SECONDS"] = max(high + 1, 10)
    _set_c4_environment(monkeypatch, **values)
    monkeypatch.setenv(name, str(low))
    assert getattr(Settings(), name.removeprefix("DLR_").lower()) == low
    monkeypatch.setenv(name, str(high))
    assert getattr(Settings(), name.removeprefix("DLR_").lower()) == high


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DLR_EXECUTION_CLAIM_TIMEOUT_SECONDS", "29"),
        ("DLR_EXECUTION_CLAIM_TIMEOUT_SECONDS", "86401"),
        ("DLR_EXECUTION_RECOVERY_GRACE_SECONDS", "9"),
        ("DLR_EXECUTION_RECOVERY_GRACE_SECONDS", "3601"),
        ("DLR_WORKSPACE_CLEANUP_ATTEMPT_TIMEOUT_SECONDS", "0"),
        ("DLR_WORKSPACE_CLEANUP_ATTEMPT_TIMEOUT_SECONDS", "61"),
        ("DLR_WORKSPACE_CLEANUP_TOTAL_TIMEOUT_SECONDS", "4"),
        ("DLR_WORKSPACE_CLEANUP_TOTAL_TIMEOUT_SECONDS", "301"),
        ("DLR_ARTIFACT_DELETE_ALERT_THRESHOLD", "0"),
        ("DLR_ARTIFACT_DELETE_ALERT_THRESHOLD", "101"),
    ],
)
def test_control_c4_settings_reject_out_of_range_values(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    _set_c4_environment(monkeypatch, **{name: value})

    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    ("attempt", "total", "grace"),
    [(6, 5, 10), (5, 5, 5)],
)
def test_control_c4_settings_reject_cleanup_order(
    monkeypatch: pytest.MonkeyPatch, attempt: int, total: int, grace: int
) -> None:
    _set_c4_environment(
        monkeypatch,
        DLR_WORKSPACE_CLEANUP_ATTEMPT_TIMEOUT_SECONDS=attempt,
        DLR_WORKSPACE_CLEANUP_TOTAL_TIMEOUT_SECONDS=total,
        DLR_EXECUTION_RECOVERY_GRACE_SECONDS=grace,
    )

    with pytest.raises((ValidationError, ValueError)):
        Settings()


def test_worker_protocol_version_is_current_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DLR_WORKER_PROTOCOL_VERSION", raising=False)

    assert WorkerConfig().protocol_version == 3


def test_worker_protocol_version_cannot_select_old_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DLR_WORKER_PROTOCOL_VERSION", "2")

    with pytest.raises(ValueError, match="Worker protocol is fixed"):
        WorkerConfig()


def test_worker_protocol_version_can_select_v3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DLR_WORKER_PROTOCOL_VERSION", "3")

    assert WorkerConfig().protocol_version == 3


@pytest.mark.parametrize("value", ["0", "1", "4", "not-a-version"])
def test_worker_protocol_version_rejects_unsupported_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DLR_WORKER_PROTOCOL_VERSION", value)

    with pytest.raises(ValueError, match="Worker protocol is fixed"):
        WorkerConfig()


def test_agent_registration_forwards_configured_protocol_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DLR_WORKER_PROTOCOL_VERSION", raising=False)
    config = WorkerConfig()
    monkeypatch.setattr(config, "capabilities", lambda: ["python"])
    monkeypatch.setattr(config, "run_preflight", lambda: None)
    calls: list[tuple[str, list[str], int]] = []

    class FakeClient:
        def register(
            self,
            name: str,
            capabilities: list[str],
            *,
            protocol_version: int,
            isolation_capabilities: dict[str, bool],
        ) -> dict[str, int]:
            calls.append((name, capabilities, protocol_version))
            return {"id": 7}

    assert Agent(config, FakeClient())._register() == 7  # type: ignore[arg-type]
    assert calls == [(config.name, ["python"], 3)]
