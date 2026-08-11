"""Worker runtime tests: venv lifecycle, Runtime Contract, executor behavior.

All adapter code runs in real subprocesses inside real ``uv`` venvs, mirroring
production. Adapters only use the Python standard library, so no public PyPI
availability is required (except one deliberate dependency-failure test).
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from dlr.common.config import settings
from dlr.worker import executor
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
        "secret value: smoke-secret-value\n"
    )
    redacted = venv_manager._redact_sensitive(text)
    assert "abc123token" not in redacted
    assert "Bearer [REDACTED]" in redacted
    assert "should-be-hidden" not in redacted
    assert "user:pass@host" not in redacted
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
    # Progress never changes the final report.
    assert result["stdout"] == "phase-1\nphase-2\n"


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
    assert result["stdout"] == "phase-1\nphase-2\n"


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
        # Without the dedicated short progress timeout the first stuck upload
        # would block ~60s before the adapter deadline is checked again.
        assert elapsed < 20, f"a stuck progress upload stretched a 2s timeout to {elapsed:.1f}s"
    finally:
        server.shutdown()
