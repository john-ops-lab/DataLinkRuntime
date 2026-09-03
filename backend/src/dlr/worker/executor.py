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
import selectors
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from dlr.common.bigfields import truncate_utf8
from dlr.common.config import settings
from dlr.runtime import harness
from dlr.runtime.node_harness import SOURCE as NODE_HARNESS_SOURCE
from dlr.worker import i18n, javaenv, nodeenv, sandbox
from dlr.worker import venv as venv_manager
from dlr.worker import workspace as workspace_manager

logger = logging.getLogger("dlr.worker.executor")

HARNESS_PATH = Path(harness.__file__)
REDACTED = "[REDACTED]"

# Display labels for the supported Adapter languages; unknown identifiers fall
# back to the raw internal language key.
LANGUAGE_LABELS: dict[str, str] = {
    "python": "Python",
    "javascript": "JavaScript",
    "java": "Java",
}

# Live-log upload rhythm while the subprocess runs (M3 spec §6.1).
PROGRESS_POLL_SECONDS = 1.0
# Bounded wait for the final progress upload after the subprocess ends, so
# the persisted live log normally includes the tail; progress is best effort,
# so a wedged upload must never delay the final result for longer than this.
PROGRESS_DRAIN_SECONDS = 1.0

RESOURCE_ERROR_CODES = frozenset(
    {
        "resource_exceeded_memory",
        "resource_exceeded_pids",
        "resource_exceeded_disk",
    }
)

# M5.5.10: every unified-log line carries one capture-time prefix.
LOG_LINE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# A read from a regular file or pipe must never be allowed to turn one noisy
# Adapter into an in-memory queue.  The value is deliberately smaller than
# the protocol cap and is only a read chunk; the ring itself is bounded by the
# immutable Resource Profile.
STREAM_READ_CHUNK_BYTES = 64 * 1024

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
    workspace_cleanup_journal_root: Path | None = None
    # ``None`` is retained for the legacy/unit-test seam.  The production v3
    # Agent always supplies a real SandboxConfig after its startup preflight;
    # v3 execution never reaches the ordinary subprocess path there.
    sandbox_config: sandbox.SandboxConfig | None = None


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


class _BoundedByteRing:
    """Fixed-memory head/tail byte ring used for logs and progress payloads.

    The old executor accumulated every poll in a Python ``str`` and applied
    ``truncate_utf8`` only at the very end.  That made a newline-free flood
    unbounded even though the wire result was capped.  This ring stores at
    most ``max_bytes`` worth of head/tail data plus a small amount of metadata
    and retains the omitted-byte count for the stable truncation marker.
    """

    def __init__(self, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("bounded ring requires a positive byte limit")
        self.max_bytes = max_bytes
        self._data = bytearray()
        self._head = bytearray()
        self._tail = deque[bytes]()
        self._tail_bytes = 0
        self._total_bytes = 0
        self._truncated = False

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def add(self, value: str | bytes) -> None:
        raw = value if isinstance(value, bytes) else value.encode("utf-8")
        if not raw:
            return
        self._total_bytes += len(raw)
        if not self._truncated and len(self._data) + len(raw) <= self.max_bytes:
            self._data.extend(raw)
            return
        if not self._truncated:
            # Keep only a bounded prefix/suffix while transitioning.  Do not
            # concatenate a potentially huge caller-owned value.
            self._head = self._data[: self._head_budget()]
            self._tail = deque()
            self._tail_bytes = 0
            self._append_tail(bytes(self._data[self._head_budget() :]))
            self._data.clear()
            self._truncated = True
        self._append_tail(raw)

    def _head_budget(self) -> int:
        return max(0, (self.max_bytes - 96) // 2)

    def _tail_budget(self) -> int:
        return max(0, self.max_bytes - self._head_budget() - 96)

    def _append_tail(self, raw: bytes) -> None:
        budget = self._tail_budget()
        if budget <= 0:
            return
        if len(raw) >= budget:
            self._tail.clear()
            self._tail.append(raw[-budget:])
            self._tail_bytes = budget
            return
        self._tail.append(raw)
        self._tail_bytes += len(raw)
        while self._tail_bytes > budget and self._tail:
            first = self._tail.popleft()
            excess = self._tail_bytes - budget
            if len(first) <= excess:
                self._tail_bytes -= len(first)
            else:
                kept = first[excess:]
                self._tail.appendleft(kept)
                self._tail_bytes -= excess

    def bytes(self) -> bytes:
        if not self._truncated:
            return bytes(self._data)
        omitted = max(0, self._total_bytes - len(self._head) - self._tail_bytes)
        marker = f"\n...[truncated {omitted} bytes]...\n".encode("ascii")
        available = max(0, self.max_bytes - len(marker))
        head_length = min(len(self._head), available // 2)
        tail_length = min(self._tail_bytes, available - head_length)
        head = bytes(self._head[:head_length])
        tail = b"".join(self._tail)[-tail_length:] if tail_length else b""
        result = head + marker + tail
        if len(result) <= self.max_bytes:
            return result
        # Extremely large omitted counts can make the marker itself consume
        # more than the reserved budget.  Preserve the hard cap and retain a
        # valid UTF-8-ish marker prefix rather than spilling memory.
        return result[: self.max_bytes]

    def text(self) -> str:
        return self.bytes().decode("utf-8", errors="replace")


def _bounded_text(value: str, max_bytes: int) -> tuple[str, bool]:
    ring = _BoundedByteRing(max_bytes)
    ring.add(value)
    return ring.text(), ring.truncated


def _timestamped_text(text: str, max_bytes: int | None = None) -> str:
    """Timestamp text into a bounded ring (e.g. a dependency log)."""
    buffer = _LineTimestampBuffer()
    if max_bytes is None:
        return buffer.push(text) + buffer.flush()
    ring = _BoundedByteRing(max_bytes)
    for offset in range(0, len(text), STREAM_READ_CHUNK_BYTES):
        ring.add(buffer.push(text[offset : offset + STREAM_READ_CHUNK_BYTES]))
    ring.add(buffer.flush())
    return ring.text()


def _finalize_stream(
    path: Path,
    secret_values: Iterable[str],
    max_bytes: int | None = None,
) -> str:
    """Build a bounded timestamped stream from a finished log file.

    Whole-file reads are intentionally avoided.  The file may be much larger
    than the reported field cap when an Adapter writes without
    newlines; the ring and fixed read chunks keep Worker RSS bounded.
    """
    limit = settings.execution_stream_max_bytes if max_bytes is None else max_bytes
    ring = _BoundedByteRing(limit)
    guard = _SecretHoldback(secret_values)
    lines = _LineTimestampBuffer()
    try:
        with path.open("rb") as stream:
            while True:
                raw = stream.read(STREAM_READ_CHUNK_BYTES)
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace")
                formatted = lines.push(guard.push(text))
                ring.add(formatted)
        ring.add(lines.push(guard.flush()))
        ring.add(lines.flush())
    except OSError:
        return ""
    return ring.text()


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


def _cap_stream(raw: bytes, max_bytes: int | None = None) -> tuple[str, bool]:
    limit = settings.execution_stream_max_bytes if max_bytes is None else max_bytes
    capped, truncated = truncate_utf8(raw, limit)
    # A bounded ring has already inserted the canonical marker before this
    # final serialization pass.  Its rendered bytes can fit under ``limit``
    # even though the source file was larger, so preserve that fact.
    truncated = truncated or b"...[truncated " in raw
    return capped.decode("utf-8", errors="replace"), truncated


def _read_bounded_file(path: Path, limit: int) -> tuple[bytes, int, bool]:
    """Read at most ``limit + 1`` bytes and return ``(prefix, size, huge)``.

    ``stat`` is authoritative for the reported size, while the extra byte
    handles a file that grows between stat and open.  Callers must not parse
    ``prefix`` when ``huge`` is true.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return b"", 0, False
    try:
        with path.open("rb") as stream:
            prefix = stream.read(max(0, limit) + 1)
    except OSError:
        return b"", size, False
    observed_size = max(size, len(prefix))
    huge = observed_size > limit or len(prefix) > limit
    return prefix[:limit], observed_size, huge


def _read_output_metadata(path: Path) -> tuple[int | None, bool]:
    """Read the helper's tiny original-size record without unbounded input."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(256)
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return None, False
    if not isinstance(value, dict):
        return None, False
    size = value.get("size")
    truncated = value.get("truncated")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(truncated, bool)
    ):
        return None, False
    return size, truncated


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

    def __init__(self, callback: ProgressCallback, max_bytes: int | None = None) -> None:
        self._callback = callback
        self._max_bytes = settings.execution_stream_max_bytes if max_bytes is None else max_bytes
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._pending_stdout = _BoundedByteRing(self._max_bytes)
        self._pending_stderr = _BoundedByteRing(self._max_bytes)
        self._pending = False
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
            self._pending_stdout.add(stdout_chunk)
            self._pending_stderr.add(stderr_chunk)
            self._pending = True
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
            while self._in_flight or self._pending:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return
                self._idle.wait(remaining)

    def _run(self) -> None:
        while True:
            with self._lock:
                stdout_chunk = self._pending_stdout.text()
                stderr_chunk = self._pending_stderr.text()
                self._pending_stdout = _BoundedByteRing(self._max_bytes)
                self._pending_stderr = _BoundedByteRing(self._max_bytes)
                self._pending = False
            # Always invoke (even with empty chunks): the round trip is the
            # cancel channel, so a silent subprocess still gets polled.
            try:
                if self._callback(stdout_chunk, stderr_chunk):
                    self._cancel.set()
            except Exception:  # noqa: BLE001 - progress never fails a run
                # The callback may contain a transport exception whose body
                # came from an untrusted peer.  Keep the log stable and never
                # risk copying a delegated credential into it.
                logger.warning("progress upload failed; continuing execution")
            with self._lock:
                if not self._pending:
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
        raw = self._handle.read(STREAM_READ_CHUNK_BYTES)
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


class _BoundedLogWriter:
    """Write a raw subprocess stream while keeping the on-disk log capped.

    The subprocess is drained through a pipe, so a noisy Adapter cannot block
    on a full log file.  Only the first ``max_bytes`` raw bytes are persisted;
    the formatter below still keeps a bounded head/tail report and records the
    fact that bytes were omitted.
    """

    def __init__(self, path: Path, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("bounded log requires a positive byte limit")
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_TRUNC
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        self._handle = os.fdopen(descriptor, "wb")
        self.max_bytes = max_bytes
        self.written_bytes = 0
        self.total_bytes = 0
        self.truncated = False

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.total_bytes += len(chunk)
        remaining = self.max_bytes - self.written_bytes
        if remaining <= 0:
            self.truncated = True
            return
        data = chunk[:remaining]
        self._handle.write(data)
        self._handle.flush()
        self.written_bytes += len(data)
        if len(data) != len(chunk):
            self.truncated = True

    def close(self) -> None:
        self._handle.close()


def _pipe_chunk(stream: Any) -> bytes:
    """Read one bounded chunk from a real or test pipe."""
    reader = getattr(stream, "read1", None)
    if callable(reader):
        return cast(bytes, reader(STREAM_READ_CHUNK_BYTES))
    return cast(bytes, stream.read(STREAM_READ_CHUNK_BYTES))


def _wait_with_progress(
    process: subprocess.Popen[bytes],
    stream_path: Path,
    timeout: int,
    progress_callback: ProgressCallback | None,
    secret_values: Iterable[str] = (),
    kill_callback: Callable[[], None] | None = None,
    max_bytes: int | None = None,
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

    def terminate() -> None:
        if kill_callback is None:
            _kill_process_group(process)
            return
        try:
            kill_callback()
        except Exception:  # noqa: BLE001 - process-group fallback is bounded
            logger.warning("sandbox kill request failed; terminating its process group")
        if process.poll() is None:
            _kill_process_group(process)

    process_stdout = getattr(process, "stdout", None)
    if process_stdout is not None:
        stream_limit = settings.execution_stream_max_bytes if max_bytes is None else max_bytes
        writer = _BoundedLogWriter(stream_path, stream_limit)
        pipe_selector = selectors.DefaultSelector()
        pipe_selector.register(process_stdout, selectors.EVENT_READ)
        guard = _SecretHoldback(secret_values)
        line_buffer = _LineTimestampBuffer()
        final_ring = _BoundedByteRing(stream_limit)
        uploader = (
            _ProgressUploader(progress_callback, stream_limit)
            if progress_callback is not None
            else None
        )

        def emit(raw: bytes = b"", *, final: bool = False) -> None:
            if raw:
                writer.write(raw)
                text = guard.push(raw.decode("utf-8", errors="replace"))
            else:
                text = guard.push("")
            if final:
                text += guard.flush()
            text = line_buffer.push(text)
            if final:
                text += line_buffer.flush()
            final_ring.add(text)
            if uploader is not None:
                # Empty submissions are the cancel poll for silent payloads.
                uploader.submit(text, "")

        def drain_ready() -> None:
            while pipe_selector.get_map():
                events = pipe_selector.select(0)
                if not events:
                    return
                for key, _ in events:
                    chunk = _pipe_chunk(key.fileobj)
                    if chunk:
                        emit(chunk)
                    else:
                        pipe_selector.unregister(key.fileobj)

        def drain_after_terminate() -> None:
            drain_deadline = time.monotonic() + PROGRESS_DRAIN_SECONDS
            while pipe_selector.get_map() and time.monotonic() < drain_deadline:
                events = pipe_selector.select(
                    min(0.05, max(0.0, drain_deadline - time.monotonic()))
                )
                if not events:
                    continue
                for key, _ in events:
                    chunk = _pipe_chunk(key.fileobj)
                    if chunk:
                        emit(chunk)
                    else:
                        pipe_selector.unregister(key.fileobj)
            if process.poll() is None:
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=0.2)
            if process.poll() is None:
                _kill_process_group(process)
            drain_ready()

        deadline = time.monotonic() + timeout
        timed_out = False
        cancelled = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    terminate()
                    timed_out = True
                    drain_after_terminate()
                    emit(final=True)
                    if uploader is not None:
                        uploader.drain(PROGRESS_DRAIN_SECONDS)
                    return -1, timed_out, cancelled, final_ring.text()
                wait_slice = min(PROGRESS_POLL_SECONDS, remaining)
                events = pipe_selector.select(wait_slice)
                for key, _ in events:
                    chunk = _pipe_chunk(key.fileobj)
                    if chunk:
                        emit(chunk)
                    else:
                        pipe_selector.unregister(key.fileobj)
                drain_ready()
                if uploader is not None:
                    emit()
                    if uploader.cancel_requested:
                        terminate()
                        cancelled = True
                        drain_after_terminate()
                        emit(final=True)
                        uploader.drain(PROGRESS_DRAIN_SECONDS)
                        return -1, timed_out, cancelled, final_ring.text()
                if process.poll() is not None and not pipe_selector.get_map():
                    emit(final=True)
                    if uploader is not None:
                        uploader.drain(PROGRESS_DRAIN_SECONDS)
                    return process.returncode or 0, timed_out, cancelled, final_ring.text()
        finally:
            pipe_selector.close()
            with suppress(OSError):
                process_stdout.close()
            writer.close()

    if progress_callback is None:
        try:
            returncode = process.wait(timeout=timeout)
            return (
                returncode,
                False,
                False,
                _finalize_stream(stream_path, secret_values, max_bytes),
            )
        except subprocess.TimeoutExpired:
            terminate()
            return -1, True, False, ""

    deadline = time.monotonic() + timeout
    tailer = _StreamTailer(stream_path)
    guard = _SecretHoldback(secret_values)
    line_buffer = _LineTimestampBuffer()
    stream_limit = settings.execution_stream_max_bytes if max_bytes is None else max_bytes
    uploader = _ProgressUploader(progress_callback, stream_limit)
    final_ring = _BoundedByteRing(stream_limit)

    def emit_file(final: bool = False) -> None:
        text = guard.push(tailer.read_new())
        if final:
            # Process is gone: release the hold-back tails as well.
            text += guard.flush()
        text = line_buffer.push(text)
        if final:
            text += line_buffer.flush()
        final_ring.add(text)
        # Empty submissions are kept: they double as the cancel poll so a
        # subprocess without any output can still observe a cancel request.
        uploader.submit(text, "")

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate()
                emit_file(final=True)
                uploader.drain(PROGRESS_DRAIN_SECONDS)
                return -1, True, False, final_ring.text()
            # Wait at most one poll slice, and never past the execution
            # deadline; uploads run off-thread and can never delay this loop.
            wait_slice = min(PROGRESS_POLL_SECONDS, remaining)
            try:
                returncode = process.wait(timeout=wait_slice)
                emit_file(final=True)  # final drain so the live view matches the end state
                uploader.drain(PROGRESS_DRAIN_SECONDS)
                return returncode, False, False, final_ring.text()
            except subprocess.TimeoutExpired:
                pass
            emit_file()
            if uploader.cancel_requested:
                terminate()
                emit_file(final=True)
                uploader.drain(PROGRESS_DRAIN_SECONDS)
                return -1, False, True, final_ring.text()
    finally:
        tailer.close()


def _cleanup_budget(payload: Mapping[str, Any]) -> tuple[float, float]:
    """Read immutable cleanup snapshots, with legacy-safe defaults."""
    try:
        attempt = float(payload.get("workspace_cleanup_attempt_timeout_seconds_snapshot") or 5)
        total = float(payload.get("workspace_cleanup_total_timeout_seconds_snapshot") or 20)
    except (TypeError, ValueError):
        return 5.0, 20.0
    if attempt <= 0 or total <= 0 or attempt > total:
        return 5.0, 20.0
    return attempt, total


def _v2_cleanup_budget(payload: Mapping[str, Any]) -> tuple[float, float]:
    """Validate immutable v2 cleanup snapshots before any local side effect."""
    names = (
        "workspace_cleanup_attempt_timeout_seconds_snapshot",
        "workspace_cleanup_total_timeout_seconds_snapshot",
        "recovery_grace_seconds_snapshot",
    )
    values = [payload.get(name) for name in names]
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        raise ValueError("v2 cleanup snapshots are required integers")
    attempt, total, grace = values
    assert isinstance(attempt, int) and isinstance(total, int) and isinstance(grace, int)
    if not (1 <= attempt <= 60 and 5 <= total <= 300 and 10 <= grace <= 3600):
        raise ValueError("v2 cleanup snapshots are out of range")
    if not (attempt <= total < grace):
        raise ValueError("v2 cleanup snapshot ordering is invalid")
    return float(attempt), float(total)


@dataclass(frozen=True)
class _ValidatedV2Payload:
    execution_id: int
    attempt_id: int | None
    adapter_id: int
    version_id: int
    timeout_seconds: int
    cleanup_token: str
    input_files: list[dict[str, Any]]
    cleanup_budget: tuple[float, float]


def _required_v2_integer(
    payload: Mapping[str, Any], name: str, *, minimum: int = 1, maximum: int | None = None
) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"v2 {name} is invalid")
    if maximum is not None and value > maximum:
        raise ValueError(f"v2 {name} is invalid")
    return value


def _validated_v2_payload(payload: Mapping[str, Any]) -> _ValidatedV2Payload:
    """Validate the complete protocol-v2 envelope before local side effects."""
    execution_id = _required_v2_integer(payload, "execution_id")
    raw_attempt_id = payload.get("attempt_id")
    if raw_attempt_id is not None and (
        not isinstance(raw_attempt_id, int)
        or isinstance(raw_attempt_id, bool)
        or raw_attempt_id <= 0
    ):
        raise ValueError("v2 attempt_id is invalid")
    adapter_id = _required_v2_integer(payload, "adapter_id")
    version_id = _required_v2_integer(payload, "version_id")
    timeout_seconds = _required_v2_integer(payload, "execution_timeout_seconds", maximum=86_400)
    for name in ("language", "code", "requirements", "claim_token", "cleanup_token"):
        value = payload.get(name)
        if not isinstance(value, str):
            raise ValueError(f"v2 {name} is invalid")
        if name in {"language", "code"} and not value.strip():
            raise ValueError(f"v2 {name} is invalid")
        if name.endswith("token") and not value:
            raise ValueError(f"v2 {name} is invalid")
    if "input" not in payload or "latest_version_id" not in payload:
        raise ValueError("v2 required field is missing")
    latest_version_id = payload["latest_version_id"]
    if latest_version_id is not None and (
        not isinstance(latest_version_id, int)
        or isinstance(latest_version_id, bool)
        or latest_version_id <= 0
    ):
        raise ValueError("v2 latest_version_id is invalid")
    runtime_config = payload.get("runtime_config")
    secrets = payload.get("secrets")
    if not isinstance(runtime_config, Mapping) or not isinstance(secrets, Mapping):
        raise ValueError("v2 object field is invalid")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in secrets.items()):
        raise ValueError("v2 secrets are invalid")
    try:
        input_files = workspace_manager.validate_input_descriptors(payload.get("input_files"))
    except workspace_manager.InputPreparationError as error:
        raise ValueError("v2 input descriptors are invalid") from error
    cleanup_token = payload["cleanup_token"]
    assert isinstance(cleanup_token, str)
    return _ValidatedV2Payload(
        execution_id=execution_id,
        attempt_id=raw_attempt_id,
        adapter_id=adapter_id,
        version_id=version_id,
        timeout_seconds=timeout_seconds,
        cleanup_token=cleanup_token,
        input_files=input_files,
        cleanup_budget=_v2_cleanup_budget(payload),
    )


def _workspace_failure(
    locale: i18n.WorkerLocale,
    error_code: str,
    *,
    protocol_version: int,
) -> dict[str, Any]:
    """Build a stable, secret-free failure for pre-process errors."""
    result: dict[str, Any] = {
        "status": "failed",
        "error": i18n.text(locale, "runtime.worker_internal_error"),
        "error_code": error_code,
        "stdout": "",
        "stdout_truncated": False,
        "stderr": "",
        "stderr_truncated": False,
    }
    if protocol_version >= 2:
        result.update(
            {
                "workspace_cleanup_status": "completed",
                "workspace_cleanup_error_code": None,
            }
        )
    return result


def _write_workspace_text(path: Path, value: str) -> None:
    """Write task material as a private file; user code only reads it."""
    try:
        path.write_text(value, encoding="utf-8")
        path.chmod(0o600)
    except OSError as error:
        raise workspace_manager.WorkspaceError("workspace_cleanup_failed") from error


def run(
    payload: dict[str, Any],
    config: RuntimeSettings,
    progress_callback: ProgressCallback | None = None,
    *,
    input_downloader: workspace_manager.InputDownloader | None = None,
) -> dict[str, Any]:
    """Run one task payload to completion; always returns a report dict.

    ``progress_callback`` receives redacted, line-timestamped unified log
    chunks while the subprocess runs and returns the Control-side cancel
    flag; a cancel request kills the subprocess and yields a ``cancelled``
    report. It never influences any other part of the final report.
    """
    language = str(payload.get("language") or "python")
    locale = i18n.resolve_locale(payload.get("locale"))
    # Payloads created before M5.6 have no locale. Keep their terminal error
    # text compatible; Control-created payloads always carry the field.
    legacy_terminal_text = "locale" not in payload
    raw_protocol_version = payload.get("protocol_version", 1)
    if raw_protocol_version is None:
        # Payloads written before protocol negotiation had no version; an
        # explicit null followed the same legacy-v1 compatibility path.
        raw_protocol_version = 1
    if not isinstance(raw_protocol_version, int) or isinstance(raw_protocol_version, bool):
        return _workspace_failure(
            locale,
            "worker_protocol_payload_invalid",
            protocol_version=2,
        )
    protocol_version = raw_protocol_version
    if protocol_version not in {1, 2, 3}:
        return _workspace_failure(
            locale,
            "worker_protocol_payload_invalid",
            protocol_version=2,
        )
    # A production v3 Agent supplies a SandboxConfig only after the real
    # startup preflight.  Validate the immutable Resource Profile before the
    # Attempt journal, Workspace, dependency preparation, or Adapter side
    # effects.  Older direct executor callers keep the v1/v2 compatibility
    # seam; the Agent/Consumer integration never uses that seam for v3.
    sandbox_limits: sandbox.ResourceLimits | None = None
    if protocol_version == 3 and config.sandbox_config is not None:
        try:
            sandbox_limits = sandbox.validate_resource_profile(
                payload.get("resource_profile"), config.sandbox_config
            )
            sandbox.validate_v3_payload_snapshots(payload, sandbox_limits)
        except sandbox.SandboxError as error:
            return _workspace_failure(locale, error.code, protocol_version=3)
    if protocol_version >= 2:
        try:
            validated_v2 = _validated_v2_payload(payload)
        except (TypeError, ValueError):
            return _workspace_failure(
                locale,
                "worker_protocol_payload_invalid",
                protocol_version=protocol_version,
            )
        execution_id = validated_v2.execution_id
        attempt_id = validated_v2.attempt_id
        adapter_id = validated_v2.adapter_id
        version_id = validated_v2.version_id
        timeout = validated_v2.timeout_seconds
        input_files: list[dict[str, Any]] = validated_v2.input_files
        input_files_valid = True
        cleanup_budget = validated_v2.cleanup_budget
        if protocol_version == 3 and attempt_id is None:
            return _workspace_failure(
                locale,
                "worker_protocol_payload_invalid",
                protocol_version=protocol_version,
            )
        if protocol_version == 3 and sandbox_limits is not None:
            if timeout != sandbox_limits.execution_timeout_seconds:
                return _workspace_failure(
                    locale, "resource_profile_invalid", protocol_version=protocol_version
                )
            if validated_v2.cleanup_budget != (
                float(sandbox_limits.cleanup_attempt_seconds),
                float(sandbox_limits.cleanup_total_seconds),
            ):
                return _workspace_failure(
                    locale, "resource_profile_invalid", protocol_version=protocol_version
                )
            timeout = sandbox_limits.execution_timeout_seconds
            cleanup_budget = (
                float(sandbox_limits.cleanup_attempt_seconds),
                float(sandbox_limits.cleanup_total_seconds),
            )
    else:
        attempt_id = None
        execution_id = int(payload["execution_id"])
        adapter_id = int(payload["adapter_id"])
        version_id = int(payload["version_id"])
        timeout = int(payload.get("execution_timeout_seconds") or config.execution_timeout_seconds)
        raw_input_files = payload.get("input_files") or []
        input_files_valid = isinstance(raw_input_files, list)
        input_files = raw_input_files if input_files_valid else []
        cleanup_budget = _cleanup_budget(payload)

    if protocol_version >= 2 and language not in LANGUAGE_LABELS:
        return {
            **_workspace_failure(
                locale,
                "unsupported_language",
                protocol_version=protocol_version,
            ),
            "error": i18n.text(locale, "runtime.unsupported_language", language=language),
        }

    # The Cleanup Token is needed after a Worker crash, so the journal is
    # durable before dependency preparation, Workspace creation, or input
    # download.  It is deliberately never copied into the result dict.
    planned_workspace: Path | None = None
    cleanup_journal_root = config.workspace_cleanup_journal_root or (
        config.runtime_root / "cleanup-journal"
    )
    if protocol_version >= 2:
        try:
            cleanup_attempt_id = attempt_id if protocol_version == 3 else None
            planned_workspace = workspace_manager.workspace_path(
                config.runtime_root,
                execution_id,
                attempt_id=cleanup_attempt_id,
            )
            workspace_manager.write_cleanup_journal(
                cleanup_journal_root,
                execution_id,
                planned_workspace,
                validated_v2.cleanup_token,
                protocol_version=protocol_version,
                attempt_id=cleanup_attempt_id,
            )
        except (workspace_manager.WorkspaceError, OSError, ValueError) as error:
            logger.warning("cleanup journal unavailable for execution %s", execution_id)
            error_code = (
                error.code
                if isinstance(error, workspace_manager.WorkspaceError)
                else "workspace_cleanup_failed"
            )
            return _workspace_failure(
                locale,
                error_code,
                protocol_version=protocol_version,
            )

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

    stream_limit = (
        sandbox_limits.stream_max_bytes
        if sandbox_limits is not None
        else settings.execution_stream_max_bytes
    )
    dependency_log_ring = _BoundedByteRing(stream_limit)
    dependency_uploader = (
        _ProgressUploader(progress_callback, stream_limit) if progress_callback else None
    )

    # v3 dependency preparation is itself an Attempt workload.  Establish the
    # journaled workspace and delegated cgroup before invoking uv/npm/Maven so
    # a slow, noisy or fork-heavy build cannot run beside the Worker Agent.
    layout: workspace_manager.WorkspaceLayout | None = None
    workspace: Path | None = None
    sandbox_attempt: sandbox.AttemptSandbox | None = None
    dependency_context: venv_manager.DependencyExecutionContext | None = None
    attempt_timeout, total_timeout = cleanup_budget

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
        dependency_log_ring.add(line)
        if dependency_uploader is not None:
            dependency_uploader.submit(line, "")

    if protocol_version == 3 and sandbox_limits is not None:
        try:
            if planned_workspace is None:
                planned_workspace = workspace_manager.workspace_path(
                    config.runtime_root,
                    execution_id,
                    attempt_id=attempt_id,
                )
            layout = workspace_manager.create_workspace(
                config.runtime_root,
                execution_id,
                attempt_id=attempt_id,
                attempt_timeout_seconds=attempt_timeout,
                total_timeout_seconds=total_timeout,
            )
            workspace = layout.root
            assert attempt_id is not None
            sandbox_config = config.sandbox_config
            if sandbox_config is None:
                raise sandbox.SandboxError("sandbox_linux_target_required")
            sandbox_attempt = sandbox.AttemptSandbox(
                sandbox_config,
                sandbox_limits,
                execution_id=execution_id,
                attempt_id=attempt_id,
                workspace=workspace,
                recovery_root=cleanup_journal_root / "sandbox-recovery",
            )
            dependency_tmp = layout.temp / ".dependency-tmp"
            sandbox_attempt.mount_dependency_tmpfs(dependency_tmp)
            dependency_context = venv_manager.DependencyExecutionContext(
                cgroup_path=sandbox_attempt.cgroup,
                tmpdir=dependency_tmp,
                nofile=sandbox_limits.nofile,
                log_max_bytes=stream_limit,
            )
        except (sandbox.SandboxError, workspace_manager.WorkspaceError, OSError) as error:
            if sandbox_attempt is not None:
                sandbox_attempt.cleanup()
            if workspace is not None:
                workspace_manager.cleanup_workspace(
                    workspace,
                    attempt_timeout_seconds=attempt_timeout,
                    total_timeout_seconds=total_timeout,
                )
            error_code = (
                error.code
                if isinstance(error, (sandbox.SandboxError, workspace_manager.WorkspaceError))
                else "workspace_cleanup_failed"
            )
            return _workspace_failure(locale, error_code, protocol_version=3)

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
                    **(
                        {"dependency_context": dependency_context}
                        if dependency_context is not None
                        else {}
                    ),
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
                    **(
                        {"dependency_context": dependency_context}
                        if dependency_context is not None
                        else {}
                    ),
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
                    **(
                        {"dependency_context": dependency_context}
                        if dependency_context is not None
                        else {}
                    ),
                )
            else:
                result = {
                    "status": "failed",
                    "error": i18n.text(locale, "runtime.unsupported_language", language=language),
                    "error_code": "unsupported_language",
                    "stdout": "",
                    "stdout_truncated": False,
                    "stderr": "",
                    "stderr_truncated": False,
                }
                if protocol_version >= 2:
                    result.update(
                        {
                            "workspace_cleanup_status": "completed",
                            "workspace_cleanup_error_code": None,
                        }
                    )
                return result
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
        elif preparation.no_source:
            failure_message = i18n.text(locale, f"dependency.no_source_{language}")
        # The env managers raise DLR-generated English summaries ("uv pip
        # install failed") that are internal diagnostics, not user-facing
        # copy; the error field carries only Execution-locale DLR text. The
        # raw tool output stays in the unified log below, untouched.
        failure_detail = i18n.text(
            locale, "dependency.prepare_failed", language=LANGUAGE_LABELS.get(language, language)
        )
        if preparation.dependency is not None:
            safe_dependency = venv_manager.redact_package_index_log(
                preparation.dependency, index_url
            )
            failure_detail += i18n.text(
                locale, "dependency.prepare_failed_dependency", dependency=safe_dependency
            )
        failure_detail += f": {failure_message}"
        # M5.5.10: the dependency failure lives in the unified log stream
        # (stdout channel) with the same line format as every other source.
        unified_log_ring = _BoundedByteRing(stream_limit)
        unified_log_ring.add(dependency_log_ring.text())
        unified_log_ring.add(
            _timestamped_text(
                redact_secrets(safe_install_log, dependency_secret_values),
                stream_limit,
            )
        )
        result_error = failure_detail
        unified_log_ring.add(
            _platform_message(
                "ERROR",
                failure_message,
                dependency_secret_values,
            )
        )
        unified_log_ring.add(
            _platform_message(
                "ERROR",
                i18n.text(locale, "dependency.script_not_started"),
                dependency_secret_values,
            )
        )
        unified_log = unified_log_ring.text()
        stdout, stdout_truncated = _cap_stream(
            unified_log.encode(),
            sandbox_limits.stream_max_bytes if sandbox_limits is not None else None,
        )
        result = {
            "status": "failed",
            "error": redact_secrets(result_error, dependency_secret_values),
            "error_code": preparation.error_code,
            "stdout": stdout,
            "stdout_truncated": stdout_truncated,
            "stderr": "",
            "stderr_truncated": False,
            **(
                {
                    "workspace_cleanup_status": "completed",
                    "workspace_cleanup_error_code": None,
                }
                if protocol_version >= 2
                else {}
            ),
        }
        if sandbox_attempt is not None:
            preparation_usage = sandbox_attempt.resource_usage()
            preparation_cleanup = sandbox_attempt.cleanup()
            result["resource_usage"] = preparation_usage
            result["cleanup_summary"] = {
                "sandbox": {
                    "status": preparation_cleanup.status,
                    "error_code": preparation_cleanup.error_code,
                    "cgroup": preparation_cleanup.cgroup_name,
                    "mount": preparation_cleanup.mount_name,
                    "killed": preparation_cleanup.killed,
                    "unmounted": preparation_cleanup.unmounted,
                    "residue": preparation_cleanup.residue,
                    "limits": sandbox_attempt.limits_readback,
                }
            }
        if workspace is not None:
            preparation_workspace_cleanup = workspace_manager.cleanup_workspace(
                workspace,
                attempt_timeout_seconds=attempt_timeout,
                total_timeout_seconds=total_timeout,
            )
            result["workspace_cleanup_status"] = preparation_workspace_cleanup.status
            result["workspace_cleanup_error_code"] = preparation_workspace_cleanup.error_code
        return result

    assert runtime_path is not None
    dependency_log_text = dependency_log_ring.text()
    if layout is None:
        try:
            if planned_workspace is None:
                planned_workspace = workspace_manager.workspace_path(
                    config.runtime_root,
                    execution_id,
                    attempt_id=attempt_id if protocol_version == 3 else None,
                )
            layout = workspace_manager.create_workspace(
                config.runtime_root,
                execution_id,
                attempt_id=attempt_id if protocol_version == 3 else None,
                attempt_timeout_seconds=attempt_timeout,
                total_timeout_seconds=total_timeout,
            )
        except workspace_manager.WorkspaceError as error:
            logger.warning("controlled workspace unavailable for execution %s", execution_id)
            result = _workspace_failure(
                locale,
                error.code,
                protocol_version=protocol_version,
            )
            if protocol_version >= 2:
                cleanup_outcome = error.cleanup_outcome or workspace_manager.CleanupOutcome(
                    "completed"
                )
                result["workspace_cleanup_status"] = cleanup_outcome.status
                result["workspace_cleanup_error_code"] = cleanup_outcome.error_code
            return result
    assert layout is not None
    workspace = layout.root
    output_raw: bytes | None = None
    output_size_on_disk: int | None = None
    output_file_truncated = False
    timed_out = False
    cancelled = False
    returncode = 0
    cleanup_outcome = workspace_manager.CleanupOutcome("deferred", "workspace_cleanup_failed")
    cleanup_attempted = False
    sandbox_cleanup: sandbox.CleanupResult | None = None
    sandbox_error_code: str | None = None
    sandbox_diagnostic: sandbox.HelperDiagnostic | None = None
    resource_usage: dict[str, Any] | None = None
    unified_log_ring = _BoundedByteRing(stream_limit)
    unified_log_ring.add(dependency_log_text)
    try:
        try:
            if language == "python":
                _write_workspace_text(workspace / "adapter.py", str(payload["code"]))
                command = [str(runtime_path), str(HARNESS_PATH), str(workspace)]
            elif language == "javascript":
                _write_workspace_text(workspace / "harness.mjs", NODE_HARNESS_SOURCE)
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
            _write_workspace_text(
                workspace / "input.json",
                json.dumps(payload.get("input"), ensure_ascii=False),
            )
            _write_workspace_text(
                workspace / "runtime_config.json",
                json.dumps(payload.get("runtime_config") or {}),
            )
            if not input_files_valid:
                raise workspace_manager.InputPreparationError("input_artifact_not_ready")
            workspace_manager.prepare_input_files(layout, input_files, input_downloader)
            workspace_manager.validate_input_manifest(layout)
        except (workspace_manager.InputPreparationError, workspace_manager.WorkspaceError) as error:
            logger.warning("input preparation failed for execution %s", execution_id)
            cleanup_attempted = True
            cleanup_outcome = workspace_manager.cleanup_workspace(
                workspace,
                attempt_timeout_seconds=attempt_timeout,
                total_timeout_seconds=total_timeout,
            )
            result = _workspace_failure(
                locale,
                error.code,
                protocol_version=protocol_version,
            )
            if protocol_version >= 2:
                result["workspace_cleanup_status"] = cleanup_outcome.status
                result["workspace_cleanup_error_code"] = cleanup_outcome.error_code
            return result

        stdout_path = workspace / ".log"
        # Drain the child through a bounded parent-side pipe.  Passing an open
        # file directly to Popen lets an untrusted Adapter grow ``.log``
        # without a runtime write boundary; _wait_with_progress persists only
        # the configured prefix while continuing to drain the pipe.
        if protocol_version == 3 and config.sandbox_config is not None:
            assert sandbox_limits is not None and attempt_id is not None
            assert sandbox_attempt is not None
            process = sandbox_attempt.start(
                command,
                stdout=subprocess.PIPE,
                environment=child_env(payload_secrets),
            )
            returncode, timed_out, cancelled, runtime_log = _wait_with_progress(
                process,
                stdout_path,
                timeout,
                progress_callback,
                secret_values,
                kill_callback=lambda: sandbox_attempt.kill(process),
                max_bytes=sandbox_limits.stream_max_bytes,
            )
        else:
            process = subprocess.Popen(  # noqa: S603 - fixed harness command
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=child_env(payload_secrets),
                cwd=str(workspace),
            )
            returncode, timed_out, cancelled, runtime_log = _wait_with_progress(
                process,
                stdout_path,
                timeout,
                progress_callback,
                secret_values,
                max_bytes=(
                    sandbox_limits.stream_max_bytes
                    if sandbox_limits is not None
                    else settings.execution_stream_max_bytes
                ),
            )
        unified_log_ring = _BoundedByteRing(stream_limit)
        unified_log_ring.add(dependency_log_text)
        unified_log_ring.add(runtime_log)
        if sandbox_attempt is not None:
            sandbox_error_code = sandbox_attempt.resource_error_code()
            resource_usage = sandbox_attempt.resource_usage()
        if not timed_out and not cancelled and sandbox_error_code is None and returncode == 0:
            output_file = workspace / "output.json"
            output_limit = (
                sandbox_limits.output_max_bytes
                if sandbox_limits is not None
                else settings.execution_output_max_bytes
            )
            if output_file.exists():
                output_raw, output_size_on_disk, output_file_truncated = _read_bounded_file(
                    output_file, output_limit
                )
                metadata_size, metadata_truncated = _read_output_metadata(
                    workspace / ".dlr-output-meta"
                )
                if metadata_size is not None:
                    output_size_on_disk = max(output_size_on_disk or 0, metadata_size)
                    output_file_truncated = output_file_truncated or metadata_truncated
    except sandbox.SandboxError as error:
        # The staging Workspace is still cleaned by the outer finally.  The
        # Adapter was never execed when this boundary is reached.
        sandbox_error_code = error.code
        unified_log_ring.add(
            _platform_message("ERROR", "sandbox preparation failed", secret_values)
        )
        returncode = 125
    finally:
        if sandbox_attempt is not None:
            sandbox_diagnostic = sandbox_attempt.read_helper_diagnostic()
            sandbox_cleanup = sandbox_attempt.cleanup()
            if sandbox_diagnostic is None:
                sandbox_diagnostic = sandbox_attempt.read_helper_diagnostic()
            if sandbox_diagnostic is not None:
                sandbox_error_code = sandbox_diagnostic.error_code
            if sandbox_cleanup.status != "completed":
                cleanup_outcome = workspace_manager.CleanupOutcome(
                    "deferred", "workspace_cleanup_failed"
                )
        if not cleanup_attempted:
            workspace_outcome = workspace_manager.cleanup_workspace(
                workspace,
                attempt_timeout_seconds=attempt_timeout,
                total_timeout_seconds=total_timeout,
            )
            if sandbox_cleanup is None or sandbox_cleanup.status == "completed":
                cleanup_outcome = workspace_outcome

    sandbox_summary: dict[str, Any] | None = None
    if sandbox_cleanup is not None:
        sandbox_summary = {
            "status": sandbox_cleanup.status,
            "error_code": sandbox_cleanup.error_code,
            "cgroup": sandbox_cleanup.cgroup_name,
            "mount": sandbox_cleanup.mount_name,
            "killed": sandbox_cleanup.killed,
            "unmounted": sandbox_cleanup.unmounted,
            "residue": sandbox_cleanup.residue,
            "limits": sandbox_attempt.limits_readback if sandbox_attempt is not None else {},
        }
        if sandbox_diagnostic is not None:
            sandbox_summary["helper_diagnostic"] = sandbox_diagnostic.as_dict()
        if resource_usage is not None:
            sandbox_summary["resource_usage"] = resource_usage

    cleanup_fields = {
        "workspace_cleanup_status": cleanup_outcome.status,
        "workspace_cleanup_error_code": cleanup_outcome.error_code,
    }

    # M5.5.10: terminal platform messages are appended to the unified stream
    # (redacted, timestamped) so the log alone explains the outcome.
    if cancelled:
        unified_log_ring.add(
            _platform_message(
                "ERROR",
                "execution cancelled"
                if legacy_terminal_text
                else i18n.text(locale, "execution.cancelled"),
                secret_values,
            )
        )
    elif timed_out:
        unified_log_ring.add(
            _platform_message(
                "ERROR",
                f"execution timed out after {timeout}s"
                if legacy_terminal_text
                else i18n.text(locale, "execution.timed_out", timeout=timeout),
                secret_values,
            )
        )
    elif returncode != 0:
        if sandbox_diagnostic is not None:
            message = (
                "sandbox helper failed during "
                f"phase={sandbox_diagnostic.phase} "
                f"kind={sandbox_diagnostic.kind} errno={sandbox_diagnostic.errno}"
            )
        else:
            message = (
                f"adapter process exited with code {returncode}"
                if legacy_terminal_text
                else i18n.text(locale, "execution.process_exited", returncode=returncode)
            )
        unified_log_ring.add(
            _platform_message(
                "ERROR",
                message,
                secret_values,
            )
        )
    elif output_raw is None:
        unified_log_ring.add(
            _platform_message(
                "ERROR",
                "adapter produced no output.json"
                if legacy_terminal_text
                else i18n.text(locale, "execution.no_output"),
                secret_values,
            )
        )
    elif output_file_truncated:
        preview_max_bytes = (
            sandbox_limits.output_preview_max_bytes
            if sandbox_limits is not None
            else settings.execution_output_preview_max_bytes
        )
        preview_ring = _BoundedByteRing(preview_max_bytes)
        preview_ring.add(
            redact_secrets((output_raw or b"").decode("utf-8", errors="replace"), secret_values)
        )
        unified_log_ring.add(
            _platform_message(
                "ERROR",
                i18n.text(locale, "execution.output_too_large"),
                secret_values,
            )
        )
        base_output_too_large = {
            "status": "succeeded",
            "error_code": "output_too_large",
            "output_truncated": True,
            "output_size": output_size_on_disk or len(output_raw or b""),
            "output_preview": preview_ring.text(),
        }
    else:
        try:
            output_value = json.loads(output_raw)
        except ValueError as error:
            unified_log_ring.add(
                _platform_message(
                    "ERROR",
                    f"invalid output.json: {error}"
                    if legacy_terminal_text
                    else i18n.text(locale, "execution.invalid_output", detail=error),
                    secret_values,
                )
            )

    unified_log = unified_log_ring.text()

    stdout, stdout_truncated = _cap_stream(
        unified_log.encode(),
        sandbox_limits.stream_max_bytes if sandbox_limits is not None else None,
    )
    base: dict[str, Any] = {
        "stdout": stdout,
        "stdout_truncated": stdout_truncated,
        # New runs report the unified log through the stdout channel; the
        # stderr channel stays empty while the API/SSE contracts keep
        # accepting legacy per-stream chunks unchanged.
        "stderr": "",
        "stderr_truncated": False,
    }
    if protocol_version >= 2:
        base.update(cleanup_fields)
    if resource_usage is not None:
        base["resource_usage"] = resource_usage
    if sandbox_summary is not None:
        base["cleanup_summary"] = {"sandbox": sandbox_summary}

    if cancelled:
        return base | {
            "status": "cancelled",
            "error": "execution cancelled",
            "error_code": "execution_cancelled",
        }
    if timed_out:
        return base | {
            "status": "timeout",
            "error": redact_secrets(f"execution timed out after {timeout}s", secret_values),
            "error_code": "execution_timeout",
        }
    if sandbox_error_code is not None:
        resource_failure = sandbox_error_code in RESOURCE_ERROR_CODES
        return base | {
            "status": "resource_exceeded" if resource_failure else "failed",
            "error": (
                i18n.text(locale, "execution.resource_exceeded")
                if resource_failure
                else "Sandbox preparation failed"
            ),
            "error_code": sandbox_error_code,
        }
    if returncode != 0:
        failure_result: dict[str, Any] = {
            "status": "failed",
            "error": redact_secrets(
                f"adapter process exited with code {returncode}", secret_values
            ),
        }
        return base | failure_result
    if output_raw is None:
        return base | {"status": "failed", "error": "adapter produced no output.json"}

    if output_file_truncated:
        return base | base_output_too_large

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
    output_max_bytes = (
        sandbox_limits.output_max_bytes
        if sandbox_limits is not None
        else settings.execution_output_max_bytes
    )
    if len(serialized) <= output_max_bytes:
        return base | {
            "status": "succeeded",
            "output": output_value,
            "output_size": len(serialized),
        }
    # Oversized output is still a successful run; only the stored form changes.
    preview_max_bytes = (
        sandbox_limits.output_preview_max_bytes
        if sandbox_limits is not None
        else settings.execution_output_preview_max_bytes
    )
    preview = serialized[:preview_max_bytes].decode("utf-8", errors="ignore")
    return base | {
        "status": "succeeded",
        "error_code": "output_too_large",
        "output_truncated": True,
        "output_size": len(serialized),
        "output_preview": redact_secrets(preview, secret_values),
    }
