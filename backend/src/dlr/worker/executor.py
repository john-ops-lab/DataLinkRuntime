"""Execute one claimed task in a fresh subprocess inside the version venv.

Every Execution gets a brand-new process; stdout/stderr stream to temp
files (never unbounded in-memory pipes) and are capped per the big-field
contract before reporting. The adapter subprocess only inherits basic
platform variables plus ``DLR_SECRET_*`` — never worker/admin tokens or
``DATABASE_URL``.
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dlr.common.bigfields import truncate_utf8
from dlr.common.config import settings
from dlr.runtime import harness
from dlr.worker import venv as venv_manager

logger = logging.getLogger("dlr.worker.executor")

HARNESS_PATH = Path(harness.__file__)
REDACTED = "[REDACTED]"

# Basic platform variables the adapter subprocess may inherit. Everything
# else (DLR_WORKER_TOKEN, DLR_ADMIN_TOKEN, DATABASE_URL, Control settings)
# is intentionally not passed to user code.
_INHERITED_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ", "USER")


@dataclass(frozen=True)
class RuntimeSettings:
    """Worker-side execution parameters (from environment configuration)."""

    runtime_root: Path
    execution_timeout_seconds: int
    dep_install_timeout_seconds: int
    pypi_index_url: str | None = None


def child_env() -> dict[str, str]:
    """Environment for the adapter subprocess (see module docstring)."""
    env = {key: os.environ[key] for key in _INHERITED_ENV_KEYS if key in os.environ}
    for key, value in os.environ.items():
        if key.startswith("DLR_SECRET_"):
            env[key] = value
    return env


def redact_secrets(text: str) -> str:
    """Best-effort masking of current DLR_SECRET_* plaintext values."""
    for key, value in os.environ.items():
        if key.startswith("DLR_SECRET_") and value:
            text = text.replace(value, REDACTED)
    return text


def _redact_json_value(value: Any) -> Any:
    """Recursively redact DLR_SECRET_* plaintext values from a JSON structure.

    Strings are scanned for each secret value and replaced with [REDACTED].
    Dicts and lists are traversed recursively. Other types are returned as-is.
    """
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {k: _redact_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    return value


def _cap_stream(raw: bytes) -> tuple[str, bool]:
    capped, truncated = truncate_utf8(raw, settings.execution_stream_max_bytes)
    return capped.decode("utf-8", errors="replace"), truncated


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate the whole process group so adapter children cannot leak."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError:
        process.kill()
    process.wait()


def run(payload: dict[str, Any], config: RuntimeSettings) -> dict[str, Any]:
    """Run one task payload to completion; always returns a report dict."""
    execution_id = int(payload["execution_id"])
    adapter_id = int(payload["adapter_id"])
    version_id = int(payload["version_id"])
    timeout = int(payload.get("execution_timeout_seconds") or config.execution_timeout_seconds)

    try:
        python_path = venv_manager.prepare_version_venv(
            config.runtime_root,
            adapter_id,
            version_id,
            str(payload.get("requirements") or ""),
            timeout_seconds=config.dep_install_timeout_seconds,
            index_url=config.pypi_index_url,
        )
    except venv_manager.DependencyPreparationError as error:
        stderr, stderr_truncated = _cap_stream(error.install_log.encode())
        return {
            "status": "failed",
            "error": redact_secrets(f"dependency preparation failed: {error}"),
            "stdout": "",
            "stderr": redact_secrets(stderr),
            "stderr_truncated": stderr_truncated,
        }

    workspace = Path(tempfile.mkdtemp(prefix=f"dlr-exec-{execution_id}-"))
    output_raw: bytes | None = None
    timed_out = False
    returncode = 0
    try:
        (workspace / "adapter.py").write_text(str(payload["code"]), encoding="utf-8")
        (workspace / "input.json").write_text(
            json.dumps(payload.get("input"), ensure_ascii=False), encoding="utf-8"
        )
        (workspace / "runtime_config.json").write_text(
            json.dumps(payload.get("runtime_config") or {}), encoding="utf-8"
        )

        stdout_path = workspace / ".stdout"
        stderr_path = workspace / ".stderr"
        with stdout_path.open("wb") as out_file, stderr_path.open("wb") as err_file:
            process = subprocess.Popen(  # noqa: S603 - fixed harness command
                [str(python_path), str(HARNESS_PATH), str(workspace)],
                stdout=out_file,
                stderr=err_file,
                start_new_session=True,
                env=child_env(),
                cwd=str(workspace),
            )
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_process_group(process)
                timed_out = True

        stdout_raw = stdout_path.read_bytes()
        stderr_raw = stderr_path.read_bytes()
        if not timed_out and returncode == 0:
            output_file = workspace / "output.json"
            output_raw = output_file.read_bytes() if output_file.exists() else None
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    stdout, stdout_truncated = _cap_stream(stdout_raw)
    stderr, stderr_truncated = _cap_stream(stderr_raw)
    base: dict[str, Any] = {
        "stdout": redact_secrets(stdout),
        "stdout_truncated": stdout_truncated,
        "stderr": redact_secrets(stderr),
        "stderr_truncated": stderr_truncated,
    }

    if timed_out:
        return base | {
            "status": "timeout",
            "error": redact_secrets(f"execution timed out after {timeout}s"),
        }
    if returncode != 0:
        return base | {
            "status": "failed",
            "error": redact_secrets(f"adapter process exited with code {returncode}"),
        }
    if output_raw is None:
        return base | {"status": "failed", "error": "adapter produced no output.json"}

    try:
        output_value = json.loads(output_raw)
    except ValueError as error:
        return base | {"status": "failed", "error": redact_secrets(f"invalid output.json: {error}")}

    # Redact secrets from the output structure before size calculation so the
    # stored size/preview semantics stay consistent with the redacted form.
    output_value = _redact_json_value(output_value)

    serialized = json.dumps(output_value, separators=(",", ":"), ensure_ascii=False).encode()
    if len(serialized) <= settings.execution_output_max_bytes:
        return base | {
            "status": "succeeded",
            "output": output_value,
            "output_size": len(serialized),
        }
    # Oversized output is still a successful run; only the stored form changes.
    preview = serialized[: settings.execution_output_preview_max_bytes].decode(
        "utf-8", errors="ignore"
    )
    return base | {
        "status": "succeeded",
        "output_truncated": True,
        "output_size": len(serialized),
        "output_preview": redact_secrets(preview),
    }
