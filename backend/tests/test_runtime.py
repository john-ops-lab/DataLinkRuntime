"""Worker runtime tests: venv lifecycle, Runtime Contract, executor behavior.

All adapter code runs in real subprocesses inside real ``uv`` venvs, mirroring
production. Adapters only use the Python standard library, so no public PyPI
availability is required (except one deliberate dependency-failure test).
"""

import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from dlr.common.config import settings
from dlr.worker import cache, executor
from dlr.worker import venv as venv_manager
from dlr.worker.client import ControlClient, ControlUnavailableError
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


def test_executor_secrets_are_redacted_in_output(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Important 4: secrets returned by adapter must be redacted from output."""
    monkeypatch.setenv("DLR_SECRET_SMOKE", "s3cret-value")
    code = (
        "def handle(context, input):\n"
        "    return {'value': context.secrets.get('SMOKE'),"
        " 'missing': context.secrets.get('NOPE')}\n"
    )
    result = executor.run(make_payload(code=code), runtime_settings(tmp_path))
    assert result["status"] == "succeeded"
    # The plaintext secret must never appear in the output; the redaction marker takes its place.
    assert result["output"] == {"value": "[REDACTED]", "missing": None}
    assert "s3cret-value" not in str(result["output"])


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
    # M5.5.10: the traceback lives in the unified log stream (stdout channel).
    assert "ValueError" in result["stdout"]
    assert "boom" in result["stdout"]
    assert result["stderr"] == ""
    assert result["error"]


def test_executor_non_serializable_return_reports_failed(tmp_path: object) -> None:
    code = "def handle(context, input):\n    return object()\n"
    result = executor.run(make_payload(code=code), runtime_settings(tmp_path))
    assert result["status"] == "failed"
    assert "JSON" in result["stdout"]


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
    # Locale-less legacy payloads keep the zh-CN default; the error field
    # carries the localized DLR message, never the raw English instruction.
    error = result["error"] or ""
    assert "Python 依赖准备失败" in error
    assert "本地缓存中没有所需依赖" in error
    assert "dependency preparation failed" not in error
    assert "not available from the local cache" not in error


def test_executor_dependency_logs_use_live_channel_before_user_script(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    progress: list[str] = []

    def fake_prepare(
        _runtime_root: Path,
        _adapter_id: int,
        _version_id: int,
        _requirements: str,
        *,
        timeout_seconds: int,
        index_url: str | None = None,
        dependency_log: venv_manager.DependencyLogCallback | None = None,
    ) -> Path:
        assert timeout_seconds == 120
        assert index_url is None
        assert dependency_log is not None
        dependency_log("requests==2.32.3 未安装，开始安装")
        dependency_log("requests==2.32.3 安装成功")
        return Path(sys.executable)

    monkeypatch.setattr(venv_manager, "prepare_version_venv", fake_prepare)
    result = executor.run(
        make_payload(
            code=(
                "def handle(context, input):\n"
                "    print('user-script-started', flush=True)\n"
                "    return {}\n"
            ),
            requirements="requests==2.32.3",
        ),
        runtime_settings(tmp_path),
        progress_callback=lambda stdout, _stderr: progress.append(stdout) or False,
    )

    assert result["status"] == "succeeded"
    assert "[依赖检查] requests==2.32.3 未安装，开始安装" in result["stdout"]
    assert "[依赖检查] requests==2.32.3 安装成功" in result["stdout"]
    assert result["stdout"].index("安装成功") < result["stdout"].index("user-script-started")
    delivered = "".join(progress)
    assert "[依赖检查] requests==2.32.3 未安装，开始安装" in delivered
    assert "user-script-started" in delivered


def test_executor_dependency_failure_skips_user_script_and_reports_dependency(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = Path(str(tmp_path)) / "user-script-started"

    def fake_prepare(
        _runtime_root: Path,
        _adapter_id: int,
        _version_id: int,
        _requirements: str,
        *,
        timeout_seconds: int,
        index_url: str | None = None,
        dependency_log: venv_manager.DependencyLogCallback | None = None,
    ) -> Path:
        assert timeout_seconds == 120
        assert index_url is None
        assert dependency_log is not None
        dependency_log("missing-package==1.0 未安装，开始安装")
        raise venv_manager.DependencyPreparationError(
            "package source rejected",
            "token=private-value",
            dependency="missing-package==1.0",
        )

    monkeypatch.setattr(venv_manager, "prepare_version_venv", fake_prepare)
    result = executor.run(
        make_payload(
            code=(
                "from pathlib import Path\n"
                f"def handle(context, input):\n    Path({str(marker)!r}).write_text('started')\n"
                "    return {}\n"
            ),
            requirements="missing-package==1.0",
        ),
        runtime_settings(tmp_path),
    )

    assert result["status"] == "failed"
    assert not marker.exists(), "user code must not run after dependency preparation fails"
    assert "missing-package==1.0" in result["stdout"]
    assert "安装失败，停止本次运行" in result["stdout"]
    assert "本次执行未开始脚本逻辑" in result["stdout"]
    assert "user-script-started" not in result["stdout"]
    assert "private-value" not in result["stdout"]
    assert "missing-package==1.0" in (result["error"] or "")


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


def test_wait_with_progress_caps_the_physical_log_file(
    tmp_path: object,
) -> None:
    stream_path = Path(tmp_path) / "bounded.log"
    process = subprocess.Popen(
        [sys.executable, "-c", "import os; os.write(1, b'x' * 2000000)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        returncode, timed_out, cancelled, final_text = executor._wait_with_progress(
            process,
            stream_path,
            timeout=5,
            progress_callback=None,
            max_bytes=4096,
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert returncode == 0
    assert timed_out is False
    assert cancelled is False
    assert stream_path.stat().st_size <= 4096
    assert len(final_text.encode()) <= 4096
    assert "truncated" in final_text


def test_dependency_preparation_uses_attempt_cgroup_and_bounded_log(
    tmp_path: object,
) -> None:
    cgroup = Path(tmp_path) / "attempt"
    cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text("", encoding="ascii")
    (cgroup / "cgroup.kill").write_text("", encoding="ascii")
    dependency_tmp = Path(tmp_path) / "dependency-tmp"
    context = venv_manager.DependencyExecutionContext(
        cgroup_path=cgroup,
        tmpdir=dependency_tmp,
        nofile=64,
        log_max_bytes=4096,
    )
    command = [sys.executable, "-c", "import os; os.write(1, b'x' * 200000)"]

    log = venv_manager._run_logged(command, timeout_seconds=5, context=context)

    assert len(log.encode()) <= 4096
    assert "truncated dependency log" in log
    assert (cgroup / "cgroup.procs").read_text(encoding="ascii").strip()


def test_dependency_build_stages_inside_attempt_tmpfs_until_promotion(tmp_path: object) -> None:
    root = Path(tmp_path)
    dependency_tmp = root / "dependency-tmp"
    dependency_tmp.mkdir()
    version_cache, _target, build = venv_manager._begin_version_build(
        root / "runtime",
        20,
        21,
        identity={"language": "python", "version": "tmpfs"},
        dependency_context=venv_manager.DependencyExecutionContext(
            cgroup_path=root / "attempt-cgroup",
            tmpdir=dependency_tmp,
            nofile=64,
            log_max_bytes=4096,
        ),
    )
    assert build is not None
    assert build.staging.is_relative_to(dependency_tmp)
    assert not build.staging.is_relative_to(version_cache.entries)
    build.abort()
    assert not build.staging.exists()
    assert not any(dependency_tmp.iterdir())
    assert not any(version_cache.entries.iterdir())


def test_version_build_cleanup_is_idempotent_after_failed_tmpfs_promotion(
    tmp_path: object,
) -> None:
    root = Path(tmp_path)
    version_cache = cache.VerifiedVersionCache(
        root / "cache", max_bytes=4096, low_watermark_bytes=0
    )
    reservation = version_cache.reserve(32)
    staging_root = root / "attempt-tmpfs" / "version-builds"
    staging = staging_root / "build"
    staging.mkdir(mode=0o700, parents=True)
    (staging / "runtime.bin").write_bytes(b"x" * 33)
    build = venv_manager._VersionBuild(
        version_cache,
        staging,
        version_cache.entry_path("failed-promotion"),
        reservation,
        staging_root,
    )

    with pytest.raises(cache.CacheError) as error:
        build.finish({"language": "test", "version": "failed-promotion"})

    assert error.value.code == "cache_reservation_insufficient"
    build.abort()
    build.abort()
    assert not staging.exists()
    assert not staging_root.exists()
    assert version_cache._state() == {}


def test_version_build_preserves_promotion_error_when_finish_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    root = Path(tmp_path)
    version_cache = cache.VerifiedVersionCache(
        root / "cache", max_bytes=4096, low_watermark_bytes=0
    )
    reservation = version_cache.reserve(32)
    staging_root = root / "attempt-tmpfs" / "version-builds"
    staging = staging_root / "build"
    staging.mkdir(mode=0o700, parents=True)
    (staging / "runtime.bin").write_bytes(b"x" * 33)
    build = venv_manager._VersionBuild(
        version_cache,
        staging,
        version_cache.entry_path("cleanup-error"),
        reservation,
        staging_root,
    )
    original_cleanup = build._remove_tmpfs_staging
    cleanup_calls = 0

    def fail_once() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            raise cache.CacheError("cache_staging_cleanup_failed")
        original_cleanup()

    monkeypatch.setattr(build, "_remove_tmpfs_staging", fail_once)
    with pytest.raises(cache.CacheError) as error:
        build.finish({"language": "test", "version": "cleanup-error"})

    assert error.value.code == "cache_reservation_insufficient"
    build.abort()
    assert cleanup_calls == 2
    assert not staging.exists()
    assert version_cache._state() == {}


def test_live_version_build_renews_global_reservation_until_finish(tmp_path: object) -> None:
    root = Path(tmp_path)
    version_cache = cache.VerifiedVersionCache(
        root / "cache", max_bytes=4096, low_watermark_bytes=0
    )
    reservation = version_cache.reserve(3000, ttl_seconds=1)
    staging = version_cache.staging_path("long-build", reservation.token)
    staging.mkdir(mode=0o700)
    build = venv_manager._VersionBuild(
        version_cache,
        staging,
        version_cache.entry_path("long-build"),
        reservation,
    )
    try:
        time.sleep(1.2)
        with pytest.raises(cache.CacheError) as error:
            version_cache.reserve(1097)
        assert error.value.code == "cache_low_watermark"
    finally:
        build.abort()
    assert not staging.exists()
    assert version_cache._state() == {}


def test_dependency_command_stops_when_cache_reservation_is_lost(tmp_path: object) -> None:
    root = Path(tmp_path)
    version_cache = cache.VerifiedVersionCache(
        root / "cache", max_bytes=4096, low_watermark_bytes=0
    )
    reservation = version_cache.reserve(3000, ttl_seconds=60)
    staging = version_cache.staging_path("lease-loss", reservation.token)
    staging.mkdir(mode=0o700)
    build = venv_manager._VersionBuild(
        version_cache,
        staging,
        version_cache.entry_path("lease-loss"),
        reservation,
    )
    cgroup = root / "attempt-cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text("", encoding="ascii")
    (cgroup / "cgroup.kill").write_text("", encoding="ascii")
    checks = 0

    def release_after_start() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            reservation.release()
        build.assert_live()

    context = venv_manager.DependencyExecutionContext(
        cgroup_path=cgroup,
        tmpdir=root / "dependency-tmp",
        nofile=64,
        log_max_bytes=4096,
    ).with_reservation(release_after_start, build.lease_lost)
    try:
        with pytest.raises(venv_manager.DependencyPreparationError) as error:
            venv_manager._run_logged(
                [
                    sys.executable,
                    "-c",
                    "import time; print('started', flush=True); time.sleep(30)",
                ],
                timeout_seconds=5,
                context=context,
            )
        assert error.value.error_code == "dependency_cache_reservation_expired"
        assert (cgroup / "cgroup.kill").read_text(encoding="ascii") == "1\n"
    finally:
        build.abort()
    assert not staging.exists()
    assert version_cache._state() == {}


def test_sandbox_output_copy_is_prefix_bounded_and_preserves_original_size(
    tmp_path: object,
) -> None:
    source = Path(tmp_path) / "tmpfs-output.json"
    destination = Path(tmp_path) / "host-output.json"
    metadata = Path(tmp_path) / ".dlr-output-meta"
    source.write_bytes(b"0123456789" * 100)

    from dlr.worker import sandbox

    sandbox._copy_bounded_output(source, destination, metadata, 17)

    expected_prefix = (b"0123456789" * 100)[:17]
    assert destination.read_bytes() == expected_prefix
    assert destination.stat().st_size == 17
    assert executor._read_bounded_file(destination, 17) == (
        expected_prefix,
        17,
        False,
    )
    assert executor._read_output_metadata(metadata) == (1000, True)


def test_sandbox_output_copy_replaces_symlink_without_following_it(tmp_path: object) -> None:
    source = Path(tmp_path) / "tmpfs-output.json"
    destination = Path(tmp_path) / "host-output.json"
    metadata = Path(tmp_path) / ".dlr-output-meta"
    outside = Path(tmp_path) / "unrelated.txt"
    source.write_bytes(b"safe-output")
    outside.write_text("must-survive", encoding="ascii")
    destination.symlink_to(outside)

    from dlr.worker import sandbox

    sandbox._copy_bounded_output(source, destination, metadata, 1024)

    assert not destination.is_symlink()
    assert destination.read_bytes() == b"safe-output"
    assert outside.read_text(encoding="ascii") == "must-survive"


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


# --- Review Round 1 regression tests ------------------------------------------


def test_dependency_env_excludes_sensitive_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Important 3: _dependency_env() must only return whitelisted keys."""
    monkeypatch.setenv("DLR_WORKER_TOKEN", "worker-secret")
    monkeypatch.setenv("DLR_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret/dlr")
    monkeypatch.setenv("DLR_SECRET_SMOKE", "smoke-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/test")

    env = venv_manager._dependency_env()
    assert "DLR_WORKER_TOKEN" not in env
    assert "DLR_ADMIN_TOKEN" not in env
    assert "DATABASE_URL" not in env
    assert "DLR_SECRET_SMOKE" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/test"


def test_dependency_install_log_redacts_sensitive_patterns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Important 3: install logs must not leak tokens or secrets."""
    monkeypatch.setenv("DLR_SECRET_SMOKE", "smoke-secret-value")
    # _redact_sensitive is a defensive safety net on top of the minimal env.
    text = (
        "pip install failed\n"
        "Bearer abc123token was rejected\n"
        "token=should-be-hidden\n"
        "postgresql+psycopg://user:pass@host/db leaked\n"
        "https://uri-user:uri-password@mirror.example.com/simple/ failed\n"
        "secret value: smoke-secret-value\n"
    )
    redacted = venv_manager._redact_sensitive(text)
    assert "abc123token" not in redacted
    assert "Bearer [REDACTED]" in redacted
    assert "should-be-hidden" not in redacted
    assert "user:pass@host" not in redacted
    assert "uri-user" not in redacted
    assert "uri-password" not in redacted
    assert "https://[REDACTED]@mirror.example.com/simple/" in redacted
    assert "smoke-secret-value" not in redacted
    assert "[REDACTED]" in redacted


# --- Review Round 2 regression tests ------------------------------------------


def test_output_dict_keys_are_redacted(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """Round 2 Important 2: a Secret used as a JSON object key must be redacted."""
    monkeypatch.setenv("DLR_SECRET_SMOKE", "top-secret-key")
    # The adapter uses the Secret both as a dict key and as a nested value.
    code = (
        "def handle(context, input):\n"
        "    secret = context.secrets.get('SMOKE')\n"
        "    return {secret: {'nested': secret, 'plain': 'visible'}}\n"
    )
    result = executor.run(make_payload(code=code), runtime_settings(tmp_path))
    assert result["status"] == "succeeded"
    output = result["output"]
    # The raw Secret must appear neither as a key nor as any value.
    assert "top-secret-key" not in str(output)
    assert "[REDACTED]" in output, "the redacted key must be present"
    assert output["[REDACTED]"]["nested"] == "[REDACTED]"
    assert output["[REDACTED]"]["plain"] == "visible"


# --- M3 live progress callback ---------------------------------------------------


LIVE_LOG_CODE = (
    "import time\n\n\n"
    "def handle(context, input):\n"
    "    print('phase-1', flush=True)\n"
    "    time.sleep(0.7)\n"
    "    print('phase-2', flush=True)\n"
    "    return {}\n"
)


def test_executor_progress_callback_receives_live_chunks(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor, "PROGRESS_POLL_SECONDS", 0.2)
    chunks: list[tuple[str, str]] = []

    def callback(stdout_chunk: str, stderr_chunk: str) -> None:
        chunks.append((stdout_chunk, stderr_chunk))

    result = executor.run(
        make_payload(code=LIVE_LOG_CODE),
        runtime_settings(tmp_path),
        progress_callback=callback,
    )
    assert result["status"] == "succeeded"
    delivered = "".join(stdout for stdout, _ in chunks)
    assert "phase-1" in delivered
    assert "phase-2" in delivered
    assert len(chunks) >= 2, "progress must arrive in multiple waves, not one final dump"
    # Progress never changes the final report; M5.5.10 adds unified line
    # timestamps, so the exact text is no longer the raw stream.
    assert "phase-1" in result["stdout"]
    assert "phase-2" in result["stdout"]
    assert result["stderr"] == ""


def test_executor_progress_chunks_are_redacted(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor, "PROGRESS_POLL_SECONDS", 0.2)
    monkeypatch.setenv("DLR_SECRET_SMOKE", "live-leak-secret")
    code = (
        "def handle(context, input):\n"
        "    print('leak: ' + str(context.secrets.get('SMOKE')), flush=True)\n"
        "    return {}\n"
    )
    chunks: list[str] = []

    def callback(stdout_chunk: str, stderr_chunk: str) -> None:
        chunks.append(stdout_chunk)

    result = executor.run(
        make_payload(code=code), runtime_settings(tmp_path), progress_callback=callback
    )
    assert result["status"] == "succeeded"
    delivered = "".join(chunks)
    assert "live-leak-secret" not in delivered, "live chunks must be redacted before upload"
    assert "[REDACTED]" in delivered


def test_executor_progress_callback_failure_never_fails_run(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor, "PROGRESS_POLL_SECONDS", 0.2)

    def broken_callback(stdout_chunk: str, stderr_chunk: str) -> None:
        raise RuntimeError("control unreachable")

    result = executor.run(
        make_payload(code=LIVE_LOG_CODE),
        runtime_settings(tmp_path),
        progress_callback=broken_callback,
    )
    assert result["status"] == "succeeded", "progress is best effort and must not fail the run"
    assert "phase-1" in result["stdout"]
    assert "phase-2" in result["stdout"]


# --- M5.5.10 unified live log contract -----------------------------------------


def test_executor_unified_log_merges_streams_and_timestamps_lines(
    tmp_path: object,
) -> None:
    """stdout, stderr and logger land in one actual-order stream with one
    unified per-line time prefix; the stderr channel stays empty."""
    code = (
        "import sys\n"
        "def handle(context, input):\n"
        "    print('print-line', flush=True)\n"
        "    sys.stderr.write('stderr-line\\n')\n"
        "    context.logger.info('logger-line')\n"
        "    context.logger.error('logger-error')\n"
        "    return {}\n"
    )
    result = executor.run(make_payload(code=code), runtime_settings(tmp_path))
    assert result["status"] == "succeeded"
    assert result["stderr"] == ""
    assert result["stderr_truncated"] is False
    lines = result["stdout"].splitlines()
    assert len(lines) == 4
    for line in lines:
        assert line.startswith("[20"), f"every line needs a time prefix: {line!r}"
    assert any("print-line" in line for line in lines)
    assert any("stderr-line" in line for line in lines)
    # The logger keeps its level marker after the unified time prefix.
    assert any("[INFO] logger-line" in line for line in lines)
    assert any("[ERROR] logger-error" in line for line in lines)
    # Actual order: the print happens before the stderr write.
    assert lines.index(next(line for line in lines if "print-line" in line)) < lines.index(
        next(line for line in lines if "stderr-line" in line)
    )


def test_executor_unified_log_platform_message_on_failure(tmp_path: object) -> None:
    code = "def handle(context, input):\n    import sys\n    sys.exit(3)\n"
    result = executor.run(make_payload(code=code), runtime_settings(tmp_path))
    assert result["status"] == "failed"
    assert "adapter process exited with code 3" in result["stdout"]
    assert "adapter process exited with code 3" in (result["error"] or "")


def test_executor_unified_log_traceback_is_inline(tmp_path: object) -> None:
    """A Traceback is part of the unified stream, not a separate view."""
    code = "def handle(context, input):\n    raise ValueError('boom')\n"
    result = executor.run(make_payload(code=code), runtime_settings(tmp_path))
    assert result["status"] == "failed"
    traceback_lines = [line for line in result["stdout"].splitlines() if "Traceback" in line]
    assert traceback_lines, "the Traceback header must be in the unified log"
    assert any("ValueError" in line for line in result["stdout"].splitlines())


def test_line_timestamp_buffer_prefixes_complete_lines_only() -> None:
    buffer = executor._LineTimestampBuffer()
    first = buffer.push("lead\npartial")
    assert first.startswith("[20"), "the emitted text needs a time prefix"
    assert first.endswith("lead\n"), "only the complete line is emitted"
    second = buffer.push(" tail\n")
    assert "partial tail" in second
    assert buffer.flush() == ""
    assert buffer.push("") == ""


def test_executor_unified_log_live_chunks_match_final_report(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live-uploaded chunks concatenate to a prefix of the final report
    text, so SSE replay and the authoritative result stay consistent."""
    monkeypatch.setattr(executor, "PROGRESS_POLL_SECONDS", 0.2)
    chunks: list[str] = []

    def callback(stdout_chunk: str, stderr_chunk: str) -> None:
        chunks.append(stdout_chunk)

    result = executor.run(
        make_payload(code=LIVE_LOG_CODE),
        runtime_settings(tmp_path),
        progress_callback=callback,
    )
    assert result["status"] == "succeeded"
    live = "".join(chunks)
    assert live != ""
    assert result["stdout"].startswith(live), (
        "the final authoritative text must keep the exact live-uploaded prefix"
    )


def test_stream_tailer_keeps_split_utf8_boundaries(tmp_path: object) -> None:
    path = Path(tmp_path) / "split.log"
    path.write_bytes(b"")
    tailer = executor._StreamTailer(path)
    try:
        encoded = "héllo".encode()
        cut = 2  # split inside the two-byte sequence of "é"
        with path.open("ab") as handle:
            handle.write(encoded[:cut])
        assert tailer.read_new() == "h"
        with path.open("ab") as handle:
            handle.write(encoded[cut:])
        assert tailer.read_new() == "éllo"
        assert tailer.read_new() == ""
    finally:
        tailer.close()


# --- M3 cross-chunk Secret redaction (Important 2) -------------------------------


# Writes the Secret split across two flushes with at least one progress poll
# between them; neither chunk alone contains the full Secret.
SPLIT_SECRET_CODE = (
    "import sys\n"
    "import time\n\n\n"
    "def handle(context, input):\n"
    "    secret = str(context.secrets.get('SPLIT'))\n"
    "    print('lead', flush=True)\n"
    "    sys.stdout.write(secret[:6])\n"
    "    sys.stdout.flush()\n"
    "    time.sleep(0.7)\n"
    "    sys.stdout.write(secret[6:] + '\\n')\n"
    "    sys.stdout.flush()\n"
    "    return {}\n"
)


def test_secret_holdback_redacts_across_chunk_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DLR_SECRET_SPLIT", "abcdef123456")
    guard = executor._SecretHoldback()
    delivered = "".join([guard.push("lead\nabcdef"), guard.push("123456 tail\n"), guard.flush()])
    assert "abcdef123456" not in delivered, (
        "a Secret split across pushes must never be reassemblable downstream"
    )
    assert "[REDACTED]" in delivered
    assert "lead" in delivered and "tail" in delivered, "surrounding text must not be lost"


def test_secret_holdback_releases_stale_tail_after_grace_period() -> None:
    guard = executor._SecretHoldback(["supersecretvalue"])
    guard.HOLD_MAX_SECONDS = 0.05
    first = guard.push("production step 1: starting\n")
    assert guard.push("") == "", "inside the grace window the tail stays held"
    time.sleep(0.06)
    released = guard.push("")
    assert first + released == "production step 1: starting\n", (
        "a silent subprocess must not stall the live log behind the held tail"
    )
    assert guard.flush() == ""


def test_secret_holdback_keeps_secret_prefix_tail_past_grace_period() -> None:
    guard = executor._SecretHoldback(["abcdef123456"])
    guard.HOLD_MAX_SECONDS = 0.05
    guard.push("lead\nabcdef")  # held tail ends with a Secret prefix
    time.sleep(0.06)
    assert guard.push("") == "", (
        "a tail that could still complete a split Secret stays held to the end"
    )
    delivered = guard.push("123456 tail\n") + guard.flush()
    assert "abcdef123456" not in delivered
    assert "[REDACTED]" in delivered and "tail" in delivered


def test_executor_progress_redacts_secret_split_across_polls(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor, "PROGRESS_POLL_SECONDS", 0.2)
    monkeypatch.setenv("DLR_SECRET_SPLIT", "abcdef123456")
    chunks: list[str] = []

    def callback(stdout_chunk: str, stderr_chunk: str) -> None:
        chunks.append(stdout_chunk)

    result = executor.run(
        make_payload(code=SPLIT_SECRET_CODE),
        runtime_settings(tmp_path),
        progress_callback=callback,
    )
    assert result["status"] == "succeeded"
    delivered = "".join(chunks)
    assert "abcdef123456" not in delivered, (
        "a Secret written across two flushes must never reach progress payloads"
    )
    assert "[REDACTED]" in delivered
    # The final M2 report stays fully redacted as well.
    assert "abcdef123456" not in result["stdout"]


# --- M3 progress deadline isolation (Important 3) --------------------------------


class _StuckHandler(BaseHTTPRequestHandler):
    """Accepts the connection but never answers, simulating a stuck Control."""

    def do_POST(self) -> None:
        time.sleep(60)

    def log_message(self, *args: object) -> None:  # silence request logging
        pass


def _start_stuck_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StuckHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_report_progress_fails_fast_on_a_stuck_control() -> None:
    server = _start_stuck_server()
    try:
        client = ControlClient(f"http://127.0.0.1:{server.server_address[1]}", "test-worker-token")
        started = time.monotonic()
        with pytest.raises(ControlUnavailableError):
            client.report_progress(1, 2, "x", "")
        elapsed = time.monotonic() - started
        assert elapsed < 15, "progress must use its own short timeout, not the 60s API budget"
    finally:
        server.shutdown()


def test_stuck_progress_upload_does_not_stretch_execution_timeout(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor, "PROGRESS_POLL_SECONDS", 0.2)
    server = _start_stuck_server()
    try:
        client = ControlClient(f"http://127.0.0.1:{server.server_address[1]}", "test-worker-token")

        def callback(stdout_chunk: str, stderr_chunk: str) -> None:
            client.report_progress(1, 2, stdout_chunk, stderr_chunk)

        code = (
            "import time\n\n\n"
            "def handle(context, input):\n"
            "    for _ in range(60):\n"
            "        print('tick', flush=True)\n"
            "        time.sleep(1)\n"
            "    return {}\n"
        )
        started = time.monotonic()
        result = executor.run(
            make_payload(code=code, timeout=2),
            runtime_settings(tmp_path),
            progress_callback=callback,
        )
        elapsed = time.monotonic() - started
        assert result["status"] == "timeout"
        # Tight bound: uploads now run off-thread, so a stuck progress upload
        # adds nothing to the adapter deadline — only small CI tolerance on
        # top of the 2s timeout. A synchronous callback (even with the short
        # 5s HTTP timeout) would visibly exceed this.
        assert elapsed < 8, f"a stuck progress upload stretched a 2s timeout to {elapsed:.1f}s"
    finally:
        server.shutdown()


def test_progress_uploader_never_blocks_and_merges_overlapping_chunks() -> None:
    """submit() must return instantly and overlapping uploads must merge.

    Regression for the M3 second review (I1): the executor may never wait
    on a progress upload, and at most one upload may be in flight.
    """
    gate = threading.Event()
    started_first = threading.Event()
    uploads: list[tuple[str, str]] = []
    lock = threading.Lock()

    def slow_callback(stdout_chunk: str, stderr_chunk: str) -> None:
        started_first.set()
        gate.wait(timeout=5)
        with lock:
            uploads.append((stdout_chunk, stderr_chunk))

    uploader = executor._ProgressUploader(slow_callback)
    started = time.monotonic()
    uploader.submit("a", "")
    assert started_first.wait(timeout=5), "the first upload should start"
    uploader.submit("b", "")  # arrives while the first upload is stuck
    uploader.submit("c", "")
    assert time.monotonic() - started < 0.5, "submit() must never wait on uploads"

    gate.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with lock:
            if len(uploads) == 2:
                break
        time.sleep(0.01)
    with lock:
        assert len(uploads) == 2, "only one upload may be in flight at a time"
        assert uploads[0][0] == "a"
        assert uploads[1][0] == "bc", "chunks submitted during a stuck upload must merge"


def test_blocking_progress_callback_cannot_delay_the_deadline(
    tmp_path: object,
) -> None:
    """Even a callback that never returns cannot stretch the timeout.

    Regression for the M3 second review (I1): with a synchronous callback
    the executor would hang forever here; with the off-thread uploader the
    kill must land within a small tolerance of the configured deadline.
    """
    stdout_path = Path(str(tmp_path)) / "stream.log"
    stdout_path.write_text("")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # own process group, as the executor does
    )

    def blocking_callback(stdout_chunk: str, stderr_chunk: str) -> None:
        threading.Event().wait()  # wedge forever like a stuck HTTP upload

    started = time.monotonic()
    try:
        returncode, timed_out, cancelled, _final_text = executor._wait_with_progress(
            process,
            stdout_path,
            timeout=1.0,
            progress_callback=blocking_callback,
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    elapsed = time.monotonic() - started

    assert timed_out is True
    assert cancelled is False
    assert returncode == -1
    # 1s deadline plus the bounded final-drain wait (PROGRESS_DRAIN_SECONDS)
    # plus small tolerance; a synchronous callback would hang forever.
    assert elapsed < 3, f"a wedged callback delayed the 1s deadline to {elapsed:.1f}s"
