"""Worker runtime tests: venv lifecycle, Runtime Contract, executor behavior.

All adapter code runs in real subprocesses inside real ``uv`` venvs, mirroring
production. Adapters only use the Python standard library, so no public PyPI
availability is required (except one deliberate dependency-failure test).
"""

import time

import pytest

from dlr.common.config import settings
from dlr.worker import executor
from dlr.worker import venv as venv_manager
from dlr.worker.executor import RuntimeSettings

ECHO_CODE = """
def handle(context, input):
    return {"echo": input, "stage": context.config.get("stage")}
"""


def make_payload(
    *,
    code: str,
    requirements: str = "",
    runtime_config: dict | None = None,
    input_value: object = None,
    timeout: int = 30,
) -> dict:
    return {
        "execution_id": 1,
        "adapter_id": 7,
        "version_id": 42,
        "code": code,
        "requirements": requirements,
        "runtime_config": runtime_config or {},
        "input": input_value,
        "latest_version_id": 42,
        "published_version_id": None,
        "execution_timeout_seconds": timeout,
    }


def runtime_settings(runtime_root: object) -> RuntimeSettings:
    return RuntimeSettings(
        runtime_root=runtime_root,  # type: ignore[arg-type]
        execution_timeout_seconds=30,
        dep_install_timeout_seconds=120,
    )


# --- version-scoped venv lifecycle --------------------------------------------


def test_venv_empty_requirements_builds_independent_venv(tmp_path: object) -> None:
    python_path = venv_manager.prepare_version_venv(
        tmp_path,
        1,
        1,
        "",
        timeout_seconds=120,  # type: ignore[arg-type]
    )
    assert python_path.exists()
    directory = venv_manager.version_dir(tmp_path, 1, 1)  # type: ignore[arg-type]
    assert (directory / ".ready").exists()
    assert (directory / "requirements.txt").read_text(encoding="utf-8") == ""


def test_venv_ready_environment_is_reused(tmp_path: object) -> None:
    first = venv_manager.prepare_version_venv(
        tmp_path,
        1,
        2,
        "",
        timeout_seconds=120,  # type: ignore[arg-type]
    )
    ready = venv_manager.version_dir(tmp_path, 1, 2) / ".ready"  # type: ignore[arg-type]
    mtime = ready.stat().st_mtime_ns

    second = venv_manager.prepare_version_venv(
        tmp_path,
        1,
        2,
        "",
        timeout_seconds=120,  # type: ignore[arg-type]
    )
    assert second == first
    assert ready.stat().st_mtime_ns == mtime, "a ready venv must not be rebuilt"


def test_venv_incomplete_directory_is_rebuilt(tmp_path: object) -> None:
    directory = venv_manager.version_dir(tmp_path, 1, 3)  # type: ignore[arg-type]
    directory.mkdir(parents=True)
    (directory / "leftover.txt").write_text("partial build", encoding="utf-8")

    python_path = venv_manager.prepare_version_venv(
        tmp_path,
        1,
        3,
        "",
        timeout_seconds=120,  # type: ignore[arg-type]
    )
    assert python_path.exists()
    assert (directory / ".ready").exists()
    assert not (directory / "leftover.txt").exists()


def test_venv_dependency_failure_raises(tmp_path: object) -> None:
    with pytest.raises(venv_manager.DependencyPreparationError):
        venv_manager.prepare_version_venv(
            tmp_path,  # type: ignore[arg-type]
            1,
            4,
            "definitely-not-a-real-package-xyz==1.0",
            timeout_seconds=120,
        )


# --- harness / Runtime Contract ------------------------------------------------


def test_executor_passes_input_and_runtime_config(tmp_path: object) -> None:
    result = executor.run(
        make_payload(code=ECHO_CODE, runtime_config={"stage": "s1"}, input_value={"n": 1}),
        runtime_settings(tmp_path),
    )
    assert result["status"] == "succeeded"
    assert result["output"] == {"echo": {"n": 1}, "stage": "s1"}
    assert result.get("output_truncated", False) is False
    assert result["output_size"] == len(b'{"echo":{"n":1},"stage":"s1"}')


def test_executor_secrets_read_from_worker_env(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLR_SECRET_SMOKE", "s3cret-value")
    code = (
        "def handle(context, input):\n"
        "    return {'value': context.secrets.get('SMOKE'),"
        " 'missing': context.secrets.get('NOPE')}\n"
    )
    result = executor.run(make_payload(code=code), runtime_settings(tmp_path))
    assert result["status"] == "succeeded"
    assert result["output"] == {"value": "s3cret-value", "missing": None}


def test_executor_collects_logger_output(tmp_path: object) -> None:
    code = (
        "def handle(context, input):\n"
        "    context.logger.info('hello from adapter')\n"
        "    return {'ok': True}\n"
    )
    result = executor.run(make_payload(code=code), runtime_settings(tmp_path))
    assert result["status"] == "succeeded"
    assert "hello from adapter" in result["stdout"]
    assert result["stdout_truncated"] is False


def test_executor_adapter_exception_reports_failed(tmp_path: object) -> None:
    code = "def handle(context, input):\n    raise ValueError('boom')\n"
    result = executor.run(make_payload(code=code), runtime_settings(tmp_path))
    assert result["status"] == "failed"
    assert "ValueError" in result["stderr"]
    assert "boom" in result["stderr"]
    assert result["error"]


def test_executor_non_serializable_return_reports_failed(tmp_path: object) -> None:
    code = "def handle(context, input):\n    return object()\n"
    result = executor.run(make_payload(code=code), runtime_settings(tmp_path))
    assert result["status"] == "failed"
    assert "JSON" in result["stderr"]


def test_executor_timeout_kills_process(tmp_path: object) -> None:
    code = "import time\n\n\ndef handle(context, input):\n    time.sleep(30)\n    return {}\n"
    started = time.monotonic()
    result = executor.run(make_payload(code=code, timeout=1), runtime_settings(tmp_path))
    elapsed = time.monotonic() - started
    assert result["status"] == "timeout"
    assert "timed out" in (result["error"] or "")
    assert elapsed < 15, "timeout must terminate the process promptly"


def test_executor_dependency_failure_reports_failed(tmp_path: object) -> None:
    payload = make_payload(code=ECHO_CODE, requirements="definitely-not-a-real-package-xyz==1.0")
    result = executor.run(payload, runtime_settings(tmp_path))
    assert result["status"] == "failed"
    assert "dependency preparation failed" in (result["error"] or "")


# --- big-field strategy ---------------------------------------------------------


def test_executor_oversized_output_keeps_size_and_preview(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "execution_output_max_bytes", 1024)
    code = "def handle(context, input):\n    return 'x' * 4096\n"
    result = executor.run(make_payload(code=code), runtime_settings(tmp_path))
    assert result["status"] == "succeeded", "oversized output is not a failure"
    assert result.get("output") is None, "no broken JSON may be stored"
    assert result["output_truncated"] is True
    assert result["output_size"] > 1024
    assert len(result["output_preview"].encode()) <= settings.execution_output_preview_max_bytes


def test_executor_truncates_large_stdout_keeping_tail(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "execution_stream_max_bytes", 64 * 1024)
    code = (
        "def handle(context, input):\n"
        "    print('a' * 2_000_000)\n"
        "    print('END-MARKER')\n"
        "    return {}\n"
    )
    result = executor.run(make_payload(code=code), runtime_settings(tmp_path))
    assert result["status"] == "succeeded"
    assert result["stdout_truncated"] is True
    assert len(result["stdout"].encode()) <= 64 * 1024
    assert "END-MARKER" in result["stdout"], "traceback tails must stay visible"
    assert "truncated" in result["stdout"]


# --- credential isolation --------------------------------------------------------


def test_child_env_excludes_platform_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DLR_WORKER_TOKEN", "worker-secret")
    monkeypatch.setenv("DLR_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret-host/dlr")
    monkeypatch.setenv("DLR_SECRET_VISIBLE", "visible-value")

    env = executor.child_env()
    assert "DLR_WORKER_TOKEN" not in env
    assert "DLR_ADMIN_TOKEN" not in env
    assert "DATABASE_URL" not in env
    assert env["DLR_SECRET_VISIBLE"] == "visible-value"


def test_adapter_subprocess_never_sees_tokens(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLR_WORKER_TOKEN", "worker-secret")
    monkeypatch.setenv("DLR_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret-host/dlr")
    code = "import os\n\n\ndef handle(context, input):\n    return {'env': dict(os.environ)}\n"

    result = executor.run(make_payload(code=code), runtime_settings(tmp_path))
    assert result["status"] == "succeeded"
    seen = result["output"]["env"]
    assert "DLR_WORKER_TOKEN" not in seen
    assert "DLR_ADMIN_TOKEN" not in seen
    assert "DATABASE_URL" not in seen


def test_reported_streams_redact_secret_values(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DLR_SECRET_SMOKE", "hunter2-secret")
    code = (
        "def handle(context, input):\n"
        "    print('leak attempt: ' + str(context.secrets.get('SMOKE')))\n"
        "    return {}\n"
    )
    result = executor.run(make_payload(code=code), runtime_settings(tmp_path))
    assert result["status"] == "succeeded"
    assert "hunter2-secret" not in result["stdout"]
    assert "[REDACTED]" in result["stdout"]
