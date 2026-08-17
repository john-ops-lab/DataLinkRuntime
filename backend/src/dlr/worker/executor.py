"""Execute one claimed task in a fresh subprocess inside the version venv.

Every Execution gets a brand-new process; stdout/stderr stream to one shared
temp file (never unbounded in-memory pipes) and are capped per the big-field
contract before reporting. The adapter subprocess only inherits basic
platform variables plus ``DLR_SECRET_*`` — never worker/admin tokens or
``DATABASE_URL``.

M5.5.10 unified live log: the subprocess is started with
``stderr=subprocess.STDOUT``, so the captured stream is the actual byte-level
order of stdout, stderr, ``context.logger`` output, tracebacks and third-party
tooling. Every line is prefixed with a capture-time ``[YYYY-MM-DD HH:mm:ss]``
timestamp (plus an optional ``[LEVEL]`` marker) and reported through the
``stdout`` channel; the ``stderr`` channel stays empty for new runs while the
API/SSE contracts keep accepting legacy per-stream chunks unchanged.

M3 adds an optional progress callback: while the subprocess runs, newly
appended bytes are uploaded about once per second. Progress is best effort —
failures are logged and never change the Execution outcome, and the final
result report stays the authoritative source of truth.

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
from dlr.runtime.node_harness import SOURCE as NODE_HARNESS_SOURCE
from dlr.worker import i18n, javaenv, nodeenv
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

# M5.5.10: every unified-log line carries one capture-time prefix.
LOG_LINE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

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
    npm_registry_url: str | None = None
    maven_repository_url: str | None = None


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


# --- M5.5.10 unified log line formatting -----------------------------------
# One timestamped, optionally level-tagged line format shared by every output
# source (stdout / stderr / logger / traceback / platform messages), so the
# live view and the persisted stream use exactly the same contract.


def _timestamped_line(text: str) -> str:
    return f"[{time.strftime(LOG_LINE_TIME_FORMAT)}] {text}"


def _platform_message(
    level: str,
    message: str,
    secret_values: Iterable[str],
) -> str:
    """One redacted platform Runtime status line (e.g. timeout / cancel)."""
    return _timestamped_line(f"[{level}] {redact_secrets(message, secret_values)}") + "\n"


class _LineTimestampBuffer:
    """Prefix every complete line with a capture-time timestamp.

    The subprocess stream is read in ~1s slices, so a line only becomes
    complete at its trailing newline; the partial tail is buffered until the
    next read (or ``flush()`` at the end). A pathological single line without
    any newline cannot stall the live log forever: past ``MAX_PARTIAL_LINE``
    the buffer is released as-is and the next slice starts a fresh
    timestamped line.
    """

    MAX_PARTIAL_LINE = 64 * 1024

    def __init__(self) -> None:
        self._partial = ""

    def push(self, text: str) -> str:
        if not text:
            return ""
        lines = (self._partial + text).split("\n")
        self._partial = lines.pop()
        if len(self._partial) > self.MAX_PARTIAL_LINE:
            # Unreasonably long line: release it so the live log keeps
            # flowing; the next read simply starts a new timestamped line.
            lines.append(self._partial)
            self._partial = ""
        return "".join(_timestamped_line(line) + "\n" for line in lines)

    def flush(self) -> str:
        if not self._partial:
            return ""
        tail = self._partial
        self._partial = ""
        return _timestamped_line(tail)


def _timestamped_text(text: str) -> str:
    """Timestamp an already-redacted whole text (e.g. a dependency log)."""
    buffer = _LineTimestampBuffer()
    return buffer.push(text) + buffer.flush()


def _finalize_stream(path: Path, secret_values: Iterable[str]) -> str:
    """Build the full timestamped stream text from a finished log file."""
    raw = path.read_bytes().decode("utf-8", errors="replace")
    return _timestamped_text(redact_secrets(raw, secret_values))


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
    stream_path: Path,
    timeout: int,
    progress_callback: ProgressCallback | None,
    secret_values: Iterable[str] = (),
) -> tuple[int, bool, bool, str]:
    """Wait for the adapter subprocess, uploading unified live-log chunks.

    Returns ``(returncode, timed_out, cancelled, final_text)``. The subprocess
    writes stdout and stderr into the same ``stream_path`` (M5.5.10), so the
    captured text is the actual byte-level order of every output source;
    ``final_text`` is the complete redacted, line-timestamped stream text and
    equals the concatenation of every live chunk uploaded via the callback.
    Without a callback this degrades to the plain M2 blocking wait (no cancel
    channel); the final text is still built from the file. Callback
    invocations receive redacted text and happen on a background uploader
    thread: a slow or failing upload can never delay the deadline checks, and
    progress never fails the Execution. When the callback reports a cancel
    request the process group is killed at the next poll slice.
    """
    if progress_callback is None:
        try:
            returncode = process.wait(timeout=timeout)
            return (
                returncode,
                False,
                False,
                _finalize_stream(stream_path, secret_values),
            )
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            return -1, True, False, ""

    deadline = time.monotonic() + timeout
    tailer = _StreamTailer(stream_path)
    guard = _SecretHoldback(secret_values)
    line_buffer = _LineTimestampBuffer()
    uploader = _ProgressUploader(progress_callback)
    final_text = ""

    def emit(final: bool = False) -> None:
        nonlocal final_text
        text = guard.push(tailer.read_new())
        if final:
            # Process is gone: release the hold-back tails as well.
            text += guard.flush()
        text = line_buffer.push(text)
        if final:
            text += line_buffer.flush()
        if text:
            final_text += text
        # Empty submissions are kept: they double as the cancel poll so a
        # subprocess without any output can still observe a cancel request.
        uploader.submit(text, "")

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_process_group(process)
                emit(final=True)
                uploader.drain(PROGRESS_DRAIN_SECONDS)
                return -1, True, False, final_text
            # Wait at most one poll slice, and never past the execution
            # deadline; uploads run off-thread and can never delay this loop.
            wait_slice = min(PROGRESS_POLL_SECONDS, remaining)
            try:
                returncode = process.wait(timeout=wait_slice)
                emit(final=True)  # final drain so the live view matches the end state
                uploader.drain(PROGRESS_DRAIN_SECONDS)
                return returncode, False, False, final_text
            except subprocess.TimeoutExpired:
                pass
            emit()
            if uploader.cancel_requested:
                _kill_process_group(process)
                emit(final=True)
                uploader.drain(PROGRESS_DRAIN_SECONDS)
                return -1, False, True, final_text
    finally:
        tailer.close()


def run(
    payload: dict[str, Any],
    config: RuntimeSettings,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Run one task payload to completion; always returns a report dict.

    ``progress_callback`` receives redacted, line-timestamped unified log
    chunks while the subprocess runs and returns the Control-side cancel
    flag; a cancel request kills the subprocess and yields a ``cancelled``
    report. It never influences any other part of the final report.
    """
    execution_id = int(payload["execution_id"])
    adapter_id = int(payload["adapter_id"])
    version_id = int(payload["version_id"])
    language = str(payload.get("language") or "python")
    timeout = int(payload.get("execution_timeout_seconds") or config.execution_timeout_seconds)
    locale = i18n.resolve_locale(payload.get("locale"))
    # Payloads created before M5.6 have no locale. Keep their terminal error
    # text compatible; Control-created payloads always carry the field.
    legacy_terminal_text = "locale" not in payload

    # M3.2: bound credentials from the TaskPayload, injected as DLR_SECRET_*
    # and added to the redaction set alongside the platform DLR_SECRET_*.
    payload_secrets: dict[str, str] = {
        str(env_key): str(value) for env_key, value in (payload.get("secrets") or {}).items()
    }
    # The language-specific platform default source (resolved at claim time)
    # wins; Worker environment variables remain compatibility fallbacks. Test
    # and triggered runs share this exact strategy.
    fallback_source = {
        "python": config.pypi_index_url,
        "javascript": config.npm_registry_url,
        "java": config.maven_repository_url,
    }.get(language)
    index_url = payload.get("index_url") or fallback_source
    index_url = str(index_url) if index_url else None
    # Package-source credentials are only used by the dependency subprocess.
    # Keep them in that error path's explicit redaction set, but do not apply
    # them to normal Adapter stdout/output where a short username could cause
    # unrelated business data to be over-redacted.
    secret_values = _env_secret_values() + [value for value in payload_secrets.values() if value]
    dependency_secret_values = secret_values + venv_manager.package_index_secret_values(index_url)

    dependency_log: list[str] = []
    dependency_uploader = _ProgressUploader(progress_callback) if progress_callback else None

    def emit_dependency_log(message: str, level: str = "INFO") -> None:
        """Format and upload one redacted dependency-stage line."""
        safe_message = venv_manager.redact_package_index_log(message, index_url)
        safe_message = redact_secrets(safe_message, dependency_secret_values)
        safe_message = i18n.localize_dependency_event(safe_message, locale)
        line = _platform_message(
            level,
            f"{i18n.text(locale, 'dependency.log_prefix')} {safe_message}",
            dependency_secret_values,
        )
        dependency_log.append(line)
        if dependency_uploader is not None:
            dependency_uploader.submit(line, "")

    runtime_path: Path | None = None
    preparation_error: venv_manager.DependencyPreparationError | None = None
    try:
        try:
            if language == "python":
                runtime_path = venv_manager.prepare_version_venv(
                    config.runtime_root,
                    adapter_id,
                    version_id,
                    str(payload.get("requirements") or ""),
                    timeout_seconds=config.dep_install_timeout_seconds,
                    index_url=index_url,
                    dependency_log=emit_dependency_log,
                )
            elif language == "javascript":
                runtime_path = nodeenv.prepare_version_node(
                    config.runtime_root,
                    adapter_id,
                    version_id,
                    str(payload["code"]),
                    str(payload.get("requirements") or ""),
                    timeout_seconds=config.dep_install_timeout_seconds,
                    registry_url=index_url,
                    dependency_log=emit_dependency_log,
                )
            elif language == "java":
                runtime_path = javaenv.prepare_version_java(
                    config.runtime_root,
                    adapter_id,
                    version_id,
                    str(payload["code"]),
                    str(payload.get("requirements") or ""),
                    timeout_seconds=config.dep_install_timeout_seconds,
                    repository_url=index_url,
                    dependency_log=emit_dependency_log,
                )
            else:
                return {
                    "status": "failed",
                    "error": i18n.text(locale, "runtime.unsupported_language", language=language),
                    "stdout": "",
                    "stderr": "",
                }
        except venv_manager.DependencyPreparationError as error:
            preparation_error = error
            if error.dependency is not None:
                emit_dependency_log(
                    i18n.text(
                        locale,
                        "dependency.install_failed",
                        dependency=error.dependency,
                    ),
                    level="ERROR",
                )
            else:
                emit_dependency_log(
                    i18n.text(locale, "dependency.preparation_failed"), level="ERROR"
                )
    finally:
        if dependency_uploader is not None:
            dependency_uploader.drain(PROGRESS_DRAIN_SECONDS)

    if preparation_error is not None:
        preparation = preparation_error
        safe_install_log = venv_manager.redact_package_index_log(preparation.install_log, index_url)
        safe_install_log = i18n.localize_dependency_log_marker(safe_install_log, locale)
        safe_error = venv_manager.redact_package_index_log(str(preparation), index_url)
        failure_detail = f"{language} dependency preparation failed"
        if preparation.dependency is not None:
            safe_dependency = venv_manager.redact_package_index_log(
                preparation.dependency, index_url
            )
            failure_detail += f" (failed dependency: {safe_dependency})"
        failure_detail += f": {safe_error}"
        # M5.5.10: the dependency failure lives in the unified log stream
        # (stdout channel) with the same line format as every other source.
        unified_log = "".join(dependency_log)
        unified_log += _timestamped_text(redact_secrets(safe_install_log, dependency_secret_values))
        hint_code = preparation.hint_code or venv_manager.dependency_source_hint_code(
            preparation.install_log
        )
        hint = i18n.source_hint(locale, hint_code)
        failure_message = i18n.text(locale, "dependency.preparation_failed")
        runtime_name = next(
            (
                runtime
                for runtime in ("Node.js", "npm", "java", "javac", "Maven")
                if f"{runtime} Runtime is unavailable" in str(preparation)
            ),
            None,
        )
        if runtime_name is not None:
            failure_message = i18n.text(
                locale,
                "runtime.unavailable",
                runtime=runtime_name,
            )
        elif hint is not None:
            failure_message = f"{failure_message}: {hint}"
        elif not index_url and preparation.dependency is not None:
            failure_message = i18n.text(locale, f"dependency.no_source_{language}")
        result_error = failure_detail
        if "locale" in payload:
            if runtime_name is not None:
                runtime_error = i18n.text(
                    locale,
                    "runtime.unavailable",
                    runtime=runtime_name,
                )
                result_error = f"{result_error}: {runtime_error}"
            elif hint is not None:
                result_error = f"{result_error}: {hint}"
            elif not index_url and preparation.dependency is not None:
                result_error = f"{result_error}: {failure_message}"
        unified_log += _platform_message(
            "ERROR",
            failure_message,
            dependency_secret_values,
        )
        unified_log += _platform_message(
            "ERROR",
            i18n.text(locale, "dependency.script_not_started"),
            dependency_secret_values,
        )
        stdout, stdout_truncated = _cap_stream(unified_log.encode())
        return {
            "status": "failed",
            "error": redact_secrets(result_error, dependency_secret_values),
            "stdout": stdout,
            "stdout_truncated": stdout_truncated,
            "stderr": "",
            "stderr_truncated": False,
        }

    assert runtime_path is not None
    dependency_log_text = "".join(dependency_log)
    workspace = Path(tempfile.mkdtemp(prefix=f"dlr-exec-{execution_id}-"))
    output_raw: bytes | None = None
    timed_out = False
    cancelled = False
    returncode = 0
    try:
        if language == "python":
            (workspace / "adapter.py").write_text(str(payload["code"]), encoding="utf-8")
            command = [str(runtime_path), str(HARNESS_PATH), str(workspace)]
        elif language == "javascript":
            (workspace / "harness.mjs").write_text(NODE_HARNESS_SOURCE, encoding="utf-8")
            (workspace / "node_modules").symlink_to(runtime_path / "node_modules")
            command = [
                "node",
                str(workspace / "harness.mjs"),
                str(workspace),
                str(runtime_path / "adapter.mjs"),
            ]
        else:
            classpath = os.pathsep.join(
                [str(runtime_path / "classes"), str(runtime_path / "deps" / "*")]
            )
            command = ["java", "-cp", classpath, "DlrRuntime", str(workspace)]
        (workspace / "input.json").write_text(
            json.dumps(payload.get("input"), ensure_ascii=False), encoding="utf-8"
        )
        (workspace / "runtime_config.json").write_text(
            json.dumps(payload.get("runtime_config") or {}), encoding="utf-8"
        )

        stdout_path = workspace / ".log"
        with stdout_path.open("wb") as out_file:
            # M5.5.10 unified stream: stderr merges into the same file at the
            # OS level, so the captured text keeps the actual byte order of
            # stdout, stderr, logger and traceback output.
            process = subprocess.Popen(  # noqa: S603 - fixed harness command
                command,
                stdout=out_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=child_env(payload_secrets),
                cwd=str(workspace),
            )
            returncode, timed_out, cancelled, runtime_log = _wait_with_progress(
                process, stdout_path, timeout, progress_callback, secret_values
            )
            unified_log = dependency_log_text + runtime_log

        if not timed_out and not cancelled and returncode == 0:
            output_file = workspace / "output.json"
            output_raw = output_file.read_bytes() if output_file.exists() else None
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    # M5.5.10: terminal platform messages are appended to the unified stream
    # (redacted, timestamped) so the log alone explains the outcome.
    if cancelled:
        unified_log += _platform_message(
            "ERROR",
            "execution cancelled"
            if legacy_terminal_text
            else i18n.text(locale, "execution.cancelled"),
            secret_values,
        )
    elif timed_out:
        unified_log += _platform_message(
            "ERROR",
            f"execution timed out after {timeout}s"
            if legacy_terminal_text
            else i18n.text(locale, "execution.timed_out", timeout=timeout),
            secret_values,
        )
    elif returncode != 0:
        unified_log += _platform_message(
            "ERROR",
            f"adapter process exited with code {returncode}"
            if legacy_terminal_text
            else i18n.text(locale, "execution.process_exited", returncode=returncode),
            secret_values,
        )
    elif output_raw is None:
        unified_log += _platform_message(
            "ERROR",
            "adapter produced no output.json"
            if legacy_terminal_text
            else i18n.text(locale, "execution.no_output"),
            secret_values,
        )
    else:
        try:
            output_value = json.loads(output_raw)
        except ValueError as error:
            unified_log += _platform_message(
                "ERROR",
                f"invalid output.json: {error}"
                if legacy_terminal_text
                else i18n.text(locale, "execution.invalid_output", detail=error),
                secret_values,
            )

    stdout, stdout_truncated = _cap_stream(unified_log.encode())
    base: dict[str, Any] = {
        "stdout": stdout,
        "stdout_truncated": stdout_truncated,
        # New runs report the unified log through the stdout channel; the
        # stderr channel stays empty while the API/SSE contracts keep
        # accepting legacy per-stream chunks unchanged.
        "stderr": "",
        "stderr_truncated": False,
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
