"""Execute one claimed task in a fresh subprocess inside the version venv.

Every Execution gets a brand-new process; stdout/stderr stream to temp
files (never unbounded in-memory pipes) and are capped per the big-field
contract before reporting. The adapter subprocess only inherits basic
platform variables plus ``DLR_SECRET_*`` — never worker/admin tokens or
``DATABASE_URL``.

M3 adds an optional progress callback: while the subprocess runs, newly
appended stdout/stderr bytes are uploaded about once per second. Progress is
best effort — failures are logged and never change the Execution outcome,
and the final result report stays the authoritative source of truth.

M3.2 turns the callback into a cancel channel: it returns whether Control
requested cancellation, and is invoked once per poll slice even without new
output so a silent subprocess can still be cancelled. On cancel the whole
process group is killed and the final report uses status ``cancelled``.
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
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

# Live-log upload rhythm while the subprocess runs (M3 spec §6.1).
PROGRESS_POLL_SECONDS = 1.0
# Bounded wait for the final progress upload after the subprocess ends, so
# the persisted live log normally includes the tail; progress is best effort,
# so a wedged upload must never delay the final result for longer than this.
PROGRESS_DRAIN_SECONDS = 1.0

# Receives already redacted (stdout_chunk, stderr_chunk) and returns whether
# Control requested cancellation of this Execution (M3.2); see run().
ProgressCallback = Callable[[str, str], bool]

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


def child_env(secrets: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for the adapter subprocess (see module docstring).

    M3.2: bound credentials arrive as ``secrets`` (env_key -> value) and are
    injected as ``DLR_SECRET_<env_key>`` alongside the inherited
    ``DLR_SECRET_*`` platform variables, which stay as the compatibility
    path. Payload values win on key collisions.
    """
    env = {key: os.environ[key] for key in _INHERITED_ENV_KEYS if key in os.environ}
    for key, value in os.environ.items():
        if key.startswith("DLR_SECRET_"):
            env[key] = value
    if secrets:
        for env_key, value in secrets.items():
            env[f"DLR_SECRET_{env_key}"] = value
    return env


def _env_secret_values() -> list[str]:
    """Non-empty DLR_SECRET_* values from the Worker environment."""
    return [value for key, value in os.environ.items() if key.startswith("DLR_SECRET_") and value]


def redact_secrets(text: str, secret_values: Iterable[str] | None = None) -> str:
    """Best-effort masking of secret plaintext values.

    Without an explicit value list the current DLR_SECRET_* environment
    values are used (compatibility path); run() passes the union of those
    and the payload-bound secrets so every known value is masked.
    """
    values = _env_secret_values() if secret_values is None else secret_values
    for value in values:
        if value:
            text = text.replace(value, REDACTED)
    return text


def _max_secret_length(secret_values: Iterable[str]) -> int:
    """Length of the longest secret value (0 when none)."""
    return max((len(value) for value in secret_values if value), default=0)


class _SecretHoldback:
    """Rolling redaction buffer for live-log chunks.

    Redacting each progress chunk independently is unsafe: a Secret written
    across two flushes is complete in neither chunk, so both would pass
    unredacted and reassemble downstream. Every push therefore prepends a
    small held-back tail (at most ``max_secret_len - 1`` characters) before
    redacting, and only emits the part that can no longer complete a split
    Secret. The remainder is flushed once the subprocess exits.

    A held tail that stays unchanged past ``HOLD_MAX_SECONDS`` is released on
    an empty push: a split Secret can only complete via the next read, so
    holding longer just stalls the live log of a long-silent subprocess.
    Tails that are still a suffix of a Secret value stay held to the very
    end, because only they could genuinely complete a split Secret later.
    """

    # Progress reads happen about once per second; two poll slices of silence
    # already exceed any realistic split-Secret window.
    HOLD_MAX_SECONDS = 2.0

    def __init__(self, secret_values: Iterable[str] | None = None) -> None:
        self._secret_values = _env_secret_values() if secret_values is None else list(secret_values)
        self._held = ""
        self._hold = max(_max_secret_length(self._secret_values) - 1, 0)
        self._held_since: float | None = None

    def _could_complete_a_secret(self) -> bool:
        """True when a suffix of the held tail is a Secret prefix: only then
        could upcoming output complete a split Secret with the held text."""
        held = self._held
        for value in self._secret_values:
            if not value:
                continue
            for k in range(min(len(held), len(value) - 1), 0, -1):
                if value.startswith(held[len(held) - k :]):
                    return True
        return False

    def push(self, text: str) -> str:
        now = time.monotonic()
        if (
            not text
            and self._held
            and self._held_since is not None
            and now - self._held_since > self.HOLD_MAX_SECONDS
            and not self._could_complete_a_secret()
        ):
            released, self._held, self._held_since = self._held, "", None
            return released
        redacted = redact_secrets(self._held + text, self._secret_values)
        keep = min(self._hold, len(redacted))
        self._held = redacted[len(redacted) - keep :]
        if text and keep:
            self._held_since = now
        elif not keep:
            self._held_since = None
        return redacted[: len(redacted) - keep]

    def flush(self) -> str:
        text = redact_secrets(self._held, self._secret_values)
        self._held = ""
        self._held_since = None
        return text


def _redact_json_value(value: Any, secret_values: Iterable[str]) -> Any:
    """Recursively redact secret plaintext values from a JSON structure.

    Strings are scanned for each secret value and replaced with [REDACTED].
    Dicts and lists are traversed recursively; dict string keys are redacted
    too, so a Secret used as an object key cannot leak into Execution.output.
    Other types are returned as-is.
    """
    if isinstance(value, str):
        return redact_secrets(value, secret_values)
    if isinstance(value, dict):
        return {
            redact_secrets(k, secret_values) if isinstance(k, str) else k: _redact_json_value(
                v, secret_values
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_value(item, secret_values) for item in value]
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


class _ProgressUploader:
    """Best-effort live-log uploader running off the executor thread.

    ``submit`` never blocks the executor: at most one upload is in flight,
    and while one is running new chunks are merged into the pending payload.
    Progress may therefore lag behind or coalesce; that is safe because the
    final result carries the authoritative full logs. Uploading synchronously
    instead would let a stuck HTTP call block the executor's deadline checks
    and stretch the adapter timeout semantics.
    """

    def __init__(self, callback: ProgressCallback) -> None:
        self._callback = callback
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._pending: tuple[str, str] = ("", "")
        self._in_flight = False
        self._cancel = threading.Event()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def submit(self, stdout_chunk: str, stderr_chunk: str) -> None:
        """Non-blocking: merge into the pending payload, ensure one upload.

        Empty submissions are kept: they double as the cancel poll so a
        subprocess without any output can still observe a cancel request.
        """
        with self._lock:
            out, err = self._pending
            self._pending = (out + stdout_chunk, err + stderr_chunk)
            if self._in_flight:
                return
            self._in_flight = True
        threading.Thread(target=self._run, name="dlr-progress-upload", daemon=True).start()

    def drain(self, timeout: float) -> None:
        """Best-effort wait for in-flight/pending uploads to finish.

        Called once after the subprocess is gone so the persisted live log
        normally contains the final chunk; bounded because the final result
        carries the authoritative full logs anyway.
        """
        with self._idle:
            end = time.monotonic() + timeout
            while self._in_flight or self._pending != ("", ""):
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return
                self._idle.wait(remaining)

    def _run(self) -> None:
        while True:
            with self._lock:
                stdout_chunk, stderr_chunk = self._pending
                self._pending = ("", "")
            # Always invoke (even with empty chunks): the round trip is the
            # cancel channel, so a silent subprocess still gets polled.
            try:
                if self._callback(stdout_chunk, stderr_chunk):
                    self._cancel.set()
            except Exception:  # noqa: BLE001 - progress never fails a run
                logger.warning("progress upload failed; continuing execution", exc_info=True)
            with self._lock:
                if self._pending == ("", ""):
                    self._in_flight = False
                    self._idle.notify_all()
                    return


class _StreamTailer:
    """Read newly appended bytes from a file the subprocess writes to.

    Incomplete trailing UTF-8 sequences are kept for the next round so a
    multi-byte character split across chunk boundaries is never corrupted.
    """

    def __init__(self, path: Path) -> None:
        self._handle = path.open("rb")
        self._pending = bytearray()

    def read_new(self) -> str:
        raw = self._handle.read()
        if raw:
            self._pending.extend(raw)
        data = bytes(self._pending)
        if not data:
            return ""
        try:
            text = data.decode()
            self._pending.clear()
            return text
        except UnicodeDecodeError:
            # At most three trailing bytes can belong to an incomplete
            # UTF-8 sequence.
            for trim in range(1, 4):
                try:
                    text = data[:-trim].decode()
                except UnicodeDecodeError:
                    continue
                self._pending = bytearray(data[-trim:])
                return text
            return ""

    def close(self) -> None:
        self._handle.close()


def _wait_with_progress(
    process: subprocess.Popen[bytes],
    stdout_path: Path,
    stderr_path: Path,
    timeout: int,
    progress_callback: ProgressCallback | None,
    secret_values: Iterable[str] = (),
) -> tuple[int, bool, bool]:
    """Wait for the adapter subprocess, uploading live log chunks.

    Returns ``(returncode, timed_out, cancelled)``. Without a callback this
    degrades to the plain M2 blocking wait (no cancel channel). Callback
    invocations receive redacted text and happen on a background uploader
    thread: a slow or failing upload can never delay the deadline checks,
    and progress never fails the Execution. When the callback reports a
    cancel request the process group is killed at the next poll slice.
    """
    if progress_callback is None:
        try:
            return process.wait(timeout=timeout), False, False
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            return -1, True, False

    deadline = time.monotonic() + timeout
    stdout_tailer = _StreamTailer(stdout_path)
    stderr_tailer = _StreamTailer(stderr_path)
    stdout_guard = _SecretHoldback(secret_values)
    stderr_guard = _SecretHoldback(secret_values)
    uploader = _ProgressUploader(progress_callback)

    def emit(final: bool = False) -> None:
        stdout_chunk = stdout_guard.push(stdout_tailer.read_new())
        stderr_chunk = stderr_guard.push(stderr_tailer.read_new())
        if final:
            # Process is gone: release the hold-back tails as well.
            stdout_chunk += stdout_guard.flush()
            stderr_chunk += stderr_guard.flush()
        uploader.submit(stdout_chunk, stderr_chunk)

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_process_group(process)
                emit(final=True)
                uploader.drain(PROGRESS_DRAIN_SECONDS)
                return -1, True, False
            # Wait at most one poll slice, and never past the execution
            # deadline; uploads run off-thread and can never delay this loop.
            wait_slice = min(PROGRESS_POLL_SECONDS, remaining)
            try:
                returncode = process.wait(timeout=wait_slice)
                emit(final=True)  # final drain so the live view matches the end state
                uploader.drain(PROGRESS_DRAIN_SECONDS)
                return returncode, False, False
            except subprocess.TimeoutExpired:
                pass
            emit()
            if uploader.cancel_requested:
                _kill_process_group(process)
                emit(final=True)
                uploader.drain(PROGRESS_DRAIN_SECONDS)
                return -1, False, True
    finally:
        stdout_tailer.close()
        stderr_tailer.close()


def run(
    payload: dict[str, Any],
    config: RuntimeSettings,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Run one task payload to completion; always returns a report dict.

    ``progress_callback`` receives redacted live stdout/stderr chunks while
    the subprocess runs and returns the Control-side cancel flag; a cancel
    request kills the subprocess and yields a ``cancelled`` report. It never
    influences any other part of the final report.
    """
    execution_id = int(payload["execution_id"])
    adapter_id = int(payload["adapter_id"])
    version_id = int(payload["version_id"])
    timeout = int(payload.get("execution_timeout_seconds") or config.execution_timeout_seconds)

    # M3.2: bound credentials from the TaskPayload, injected as DLR_SECRET_*
    # and added to the redaction set alongside the platform DLR_SECRET_*.
    payload_secrets: dict[str, str] = {
        str(env_key): str(value) for env_key, value in (payload.get("secrets") or {}).items()
    }
    secret_values = _env_secret_values() + [value for value in payload_secrets.values() if value]

    # M3.2: the platform default package source (resolved by Control at claim
    # time) wins; the Worker's DLR_PYPI_INDEX_URL stays the compatibility
    # fallback. Test runs and production runs share this exact strategy.
    index_url = payload.get("index_url") or config.pypi_index_url

    try:
        python_path = venv_manager.prepare_version_venv(
            config.runtime_root,
            adapter_id,
            version_id,
            str(payload.get("requirements") or ""),
            timeout_seconds=config.dep_install_timeout_seconds,
            index_url=index_url,
        )
    except venv_manager.DependencyPreparationError as error:
        stderr, stderr_truncated = _cap_stream(error.install_log.encode())
        return {
            "status": "failed",
            "error": redact_secrets(f"dependency preparation failed: {error}", secret_values),
            "stdout": "",
            "stderr": redact_secrets(stderr, secret_values),
            "stderr_truncated": stderr_truncated,
        }

    workspace = Path(tempfile.mkdtemp(prefix=f"dlr-exec-{execution_id}-"))
    output_raw: bytes | None = None
    timed_out = False
    cancelled = False
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
                env=child_env(payload_secrets),
                cwd=str(workspace),
            )
            returncode, timed_out, cancelled = _wait_with_progress(
                process, stdout_path, stderr_path, timeout, progress_callback, secret_values
            )

        stdout_raw = stdout_path.read_bytes()
        stderr_raw = stderr_path.read_bytes()
        if not timed_out and not cancelled and returncode == 0:
            output_file = workspace / "output.json"
            output_raw = output_file.read_bytes() if output_file.exists() else None
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    stdout, stdout_truncated = _cap_stream(stdout_raw)
    stderr, stderr_truncated = _cap_stream(stderr_raw)
    base: dict[str, Any] = {
        "stdout": redact_secrets(stdout, secret_values),
        "stdout_truncated": stdout_truncated,
        "stderr": redact_secrets(stderr, secret_values),
        "stderr_truncated": stderr_truncated,
    }

    if cancelled:
        return base | {"status": "cancelled", "error": "execution cancelled"}
    if timed_out:
        return base | {
            "status": "timeout",
            "error": redact_secrets(f"execution timed out after {timeout}s", secret_values),
        }
    if returncode != 0:
        return base | {
            "status": "failed",
            "error": redact_secrets(
                f"adapter process exited with code {returncode}", secret_values
            ),
        }
    if output_raw is None:
        return base | {"status": "failed", "error": "adapter produced no output.json"}

    try:
        output_value = json.loads(output_raw)
    except ValueError as error:
        return base | {
            "status": "failed",
            "error": redact_secrets(f"invalid output.json: {error}", secret_values),
        }

    # Redact secrets from the output structure before size calculation so the
    # stored size/preview semantics stay consistent with the redacted form.
    output_value = _redact_json_value(output_value, secret_values)

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
        "output_preview": redact_secrets(preview, secret_values),
    }
