"""Controlled Worker workspaces and crash-recovery cleanup journals.

The journal is intentionally a small private hand-off between the Worker
process that claimed an Execution and a later Worker process.  It is the only
place where the raw Cleanup Token is persisted.  Workspace contents are
never used as a source of authorization; a recovery scan deletes a directory
only after its controlled name, ownership marker, and manifest all agree.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import stat as stat_module
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast

logger = logging.getLogger("dlr.worker.workspace")

WORKSPACES_DIRNAME = "workspaces"
JOURNAL_SUFFIX = ".json"
MARKER_FILENAME = ".dlr-execution-workspace"
MANIFEST_FILENAME = "input_manifest.json"
INPUT_DIRNAME = "input"
TEMP_DIRNAME = "temp"
OUTPUT_DIRNAME = "output"
WORKSPACE_NAME_PATTERN = re.compile(r"dlr-exec-([1-9][0-9]*)\Z")
JOURNAL_NAME_PATTERN = re.compile(r"([1-9][0-9]*)\.json\Z")
MOUNT_NAME_PATTERN = re.compile(r"input-([0-9]{2})\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")

# Recovery runs in the claim loop, so one scan must not monopolize the loop
# behind a slow filesystem or Control response.  Retry timing is kept in the
# journal inode metadata rather than its schema, which must remain the exact
# four recovery fields below.
RECOVERY_SCAN_TIMEOUT_SECONDS = 5.0
RECOVERY_RETRY_BACKOFF_SECONDS = 60.0

# Keep this list deliberately small.  A journal must never become a general
# task snapshot or a convenient place to retain user data.
JOURNAL_FIELDS = frozenset({"execution_id", "protocol_version", "workspace_path", "cleanup_token"})


@dataclass(frozen=True)
class CleanupOutcome:
    """Local cleanup facts safe to include in a Result report."""

    status: str
    error_code: str | None = None


class WorkspaceError(Exception):
    """Controlled Worker preparation or cleanup failure."""

    def __init__(self, code: str, message: str = "Workspace operation failed") -> None:
        super().__init__(message)
        self.code = code
        self.cleanup_outcome: CleanupOutcome | None = None


class InputPreparationError(WorkspaceError):
    """A leased input could not be downloaded and verified."""


@dataclass(frozen=True)
class WorkspaceLayout:
    """The controlled paths created for one Execution."""

    root: Path
    input: Path
    temp: Path
    output: Path


CleanupReporter = Callable[[int, str], bool]


class WritableBinary(Protocol):
    """Small writer interface needed by streamed input downloaders."""

    def write(self, data: bytes) -> int: ...


InputDownloader = Callable[[Mapping[str, Any], WritableBinary], int]

_CLEANUP_LOCK = threading.Lock()
_CLEANUP_IN_PROGRESS: set[Path] = set()


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_regular(path: Path) -> bool:
    info = _lstat(path)
    return info is not None and stat_module.S_ISREG(info.st_mode)


def _is_directory(path: Path) -> bool:
    info = _lstat(path)
    return info is not None and stat_module.S_ISDIR(info.st_mode)


def _ensure_private_directory(path: Path) -> None:
    """Create/check one controlled directory without following a leaf link."""
    info = _lstat(path)
    if info is None:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError:
            info = _lstat(path)
        except OSError as error:
            raise WorkspaceError("workspace_cleanup_failed") from error
        else:
            return
    if info is None or stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISDIR(info.st_mode):
        raise WorkspaceError("workspace_cleanup_unknown")
    if info.st_mode & 0o077:
        try:
            path.chmod(0o700)
        except OSError as error:
            raise WorkspaceError("workspace_cleanup_failed") from error


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_json(path: Path, value: Mapping[str, Any], *, mode: int) -> None:
    """Write a small private JSON file with a durable same-directory rename."""
    parent = path.parent
    _ensure_private_directory(parent)
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
        )
    except OSError as error:
        raise WorkspaceError("workspace_cleanup_failed") from error
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(encoded)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(mode)
        except OSError as error:
            raise WorkspaceError("workspace_cleanup_failed") from error
        _sync_directory(parent)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise


def workspace_path(runtime_root: Path, execution_id: int) -> Path:
    """Return the only Workspace path accepted for an Execution."""
    if execution_id <= 0:
        raise WorkspaceError("workspace_cleanup_unknown")
    root = Path(runtime_root)
    if not root.is_absolute():
        raise WorkspaceError("workspace_cleanup_unknown")
    return root / WORKSPACES_DIRNAME / f"dlr-exec-{execution_id}"


def journal_path(journal_root: Path, execution_id: int) -> Path:
    """Return the only journal filename accepted for an Execution."""
    if execution_id <= 0 or not Path(journal_root).is_absolute():
        raise WorkspaceError("workspace_cleanup_unknown")
    return Path(journal_root) / f"{execution_id}{JOURNAL_SUFFIX}"


def write_cleanup_journal(
    journal_root: Path,
    execution_id: int,
    planned_workspace: Path,
    cleanup_token: str,
    *,
    protocol_version: int = 2,
) -> Path:
    """Persist the v2 cleanup hand-off before touching a Workspace."""
    if protocol_version < 2 or not isinstance(cleanup_token, str) or not cleanup_token:
        raise WorkspaceError("workspace_cleanup_failed")
    if not Path(journal_root).is_absolute():
        raise WorkspaceError("workspace_cleanup_unknown")
    if not planned_workspace.is_absolute():
        raise WorkspaceError("workspace_cleanup_unknown")
    root = Path(journal_root)
    if root == planned_workspace or planned_workspace in root.parents:
        raise WorkspaceError("workspace_cleanup_unknown")
    _ensure_private_directory(root)
    target = journal_path(root, execution_id)
    if _lstat(target) is not None:
        raise WorkspaceError("workspace_cleanup_unknown")
    try:
        _write_json(
            target,
            {
                "cleanup_token": cleanup_token,
                "execution_id": execution_id,
                "protocol_version": protocol_version,
                "workspace_path": str(planned_workspace),
            },
            mode=0o600,
        )
    except OSError as error:
        raise WorkspaceError("workspace_cleanup_failed") from error
    # A newly-created journal is immediately eligible for its first recovery
    # attempt.  Later deferred attempts move the mtime forward and therefore
    # retain the retry backoff across Worker restarts without adding fields to
    # the private journal contract.
    try:
        initial_mtime = time.time() - RECOVERY_RETRY_BACKOFF_SECONDS
        os.utime(target, (initial_mtime, initial_mtime), follow_symlinks=False)
        _sync_directory(root)
    except OSError:
        # The durable journal is still safe to retry early if mtime updates
        # are unavailable on the mounted filesystem.
        pass
    return target


def remove_cleanup_journal(journal_root: Path, execution_id: int) -> bool:
    """Remove one journal only after Control accepted its cleanup receipt."""
    target = journal_path(Path(journal_root), execution_id)
    info = _lstat(target)
    if info is None:
        return True
    if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISREG(info.st_mode):
        return False
    try:
        target.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    _sync_directory(target.parent)
    return True


def create_workspace(
    runtime_root: Path,
    execution_id: int,
    *,
    attempt_timeout_seconds: float = 5.0,
    total_timeout_seconds: float = 20.0,
) -> WorkspaceLayout:
    """Create a new controlled Workspace after the journal is durable."""
    root = workspace_path(runtime_root, execution_id)
    _ensure_private_directory(root.parent)
    if _lstat(root) is not None:
        raise WorkspaceError("workspace_cleanup_unknown")
    try:
        root.mkdir(mode=0o700)
    except FileExistsError as error:
        raise WorkspaceError("workspace_cleanup_unknown") from error
    except OSError as error:
        raise WorkspaceError("workspace_cleanup_failed") from error
    try:
        input_path = root / INPUT_DIRNAME
        temp_path = root / TEMP_DIRNAME
        output_path = root / OUTPUT_DIRNAME
        for child in (input_path, temp_path, output_path):
            child.mkdir(mode=0o700)
        _write_json(root / MARKER_FILENAME, {"execution_id": execution_id, "format": 1}, mode=0o600)
    except Exception as error:
        # The partial tree is never an authorization path and cleanup never
        # follows a symlink leaf.  Preserve the outcome for the caller so it
        # can report deferred cleanup instead of claiming local completion.
        cleanup_outcome = cleanup_workspace(
            root,
            attempt_timeout_seconds=attempt_timeout_seconds,
            total_timeout_seconds=total_timeout_seconds,
        )
        if isinstance(error, WorkspaceError):
            error.cleanup_outcome = cleanup_outcome
            raise
        wrapped = WorkspaceError("workspace_cleanup_failed")
        wrapped.cleanup_outcome = cleanup_outcome
        raise wrapped from error
    return WorkspaceLayout(root, input_path, temp_path, output_path)


class _HashingWriter:
    """Bounded adapter around a binary file used during input streaming."""

    def __init__(self, stream: Any, expected_size: int) -> None:
        self._stream = stream
        self._expected_size = expected_size
        self.size = 0
        self.digest = hashlib.sha256()
        self.exceeded = False

    def write(self, data: bytes) -> int:
        if not isinstance(data, bytes):
            raise TypeError("input downloader must write bytes")
        if self.size + len(data) > self._expected_size:
            self.exceeded = True
            raise _InputSizeExceeded
        written = self._stream.write(data)
        if written is None:
            written = len(data)
        if written != len(data):
            raise OSError("input download write was incomplete")
        self.size += written
        self.digest.update(data)
        return int(written)


class _InputSizeExceeded(Exception):
    """The downloader attempted to write beyond its declared byte budget."""


def _input_descriptor(value: object, expected_ordinal: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InputPreparationError("input_artifact_not_ready")
    try:
        artifact_id = int(value["id"])
        ordinal = int(value["ordinal"])
        mount_name = str(value["mount_name"])
        expected_size = int(value["size_bytes"])
        expected_sha = str(value["sha256"])
    except (KeyError, TypeError, ValueError) as error:
        raise InputPreparationError("input_artifact_not_ready") from error
    match = MOUNT_NAME_PATTERN.fullmatch(mount_name)
    if (
        artifact_id <= 0
        or ordinal != expected_ordinal
        or match is None
        or int(match.group(1)) != expected_ordinal
        or expected_size < 0
        or SHA256_PATTERN.fullmatch(expected_sha) is None
    ):
        raise InputPreparationError("input_artifact_not_ready")
    return dict(value)


def _open_input_destination(path: Path) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise InputPreparationError("input_artifact_not_ready") from error
    return os.fdopen(descriptor, "wb")


def prepare_input_files(
    layout: WorkspaceLayout,
    input_files: Sequence[object],
    downloader: InputDownloader | None,
) -> None:
    """Stream, hash, and permission all Lease files before process start."""
    if input_files and downloader is None:
        raise InputPreparationError("input_artifact_not_ready")
    manifest_files: list[dict[str, Any]] = []
    for expected_ordinal, raw_descriptor in enumerate(input_files):
        descriptor = _input_descriptor(raw_descriptor, expected_ordinal)
        target = layout.input / str(descriptor["mount_name"])
        stream: BinaryIO | None = None
        try:
            stream = _open_input_destination(target)
            hashing = _HashingWriter(stream, int(descriptor["size_bytes"]))
            assert downloader is not None
            try:
                downloader(descriptor, hashing)
            except _InputSizeExceeded:
                # The writer has already stopped the over-limit write.  Flush
                # and hash the bytes accepted so the final size/SHA check
                # below still determines the stable failure code.
                pass
            except InputPreparationError:
                raise
            except Exception as error:
                raise InputPreparationError("input_artifact_not_ready") from error
            stream.flush()
            os.fsync(stream.fileno())
            actual_size = hashing.size
            actual_sha = hashing.digest.hexdigest()
        except InputPreparationError:
            if stream is not None:
                stream.close()
            target.unlink(missing_ok=True)
            raise
        except (OSError, TypeError) as error:
            if stream is not None:
                stream.close()
            target.unlink(missing_ok=True)
            raise InputPreparationError("input_artifact_not_ready") from error
        else:
            stream.close()
        if (
            hashing.exceeded
            or actual_size != int(descriptor["size_bytes"])
            or actual_sha != descriptor["sha256"]
        ):
            target.unlink(missing_ok=True)
            raise InputPreparationError("input_artifact_checksum_mismatch")
        try:
            target.chmod(0o444)
        except OSError as error:
            target.unlink(missing_ok=True)
            raise InputPreparationError("input_artifact_not_ready") from error
        # The manifest is deliberately metadata-only.  It is not the journal,
        # and it is never used as a source of Cleanup authorization.
        manifest_files.append(
            {
                "artifact_id": int(descriptor["id"]),
                "ordinal": expected_ordinal,
                "mount_name": str(descriptor["mount_name"]),
                "original_filename": str(descriptor.get("original_filename") or ""),
                "content_type": str(descriptor.get("content_type") or ""),
                "size_bytes": actual_size,
                "sha256": actual_sha,
            }
        )
    try:
        layout.input.chmod(0o555)
        match = WORKSPACE_NAME_PATTERN.fullmatch(layout.root.name)
        if match is None:
            raise InputPreparationError("input_artifact_not_ready")
        _write_json(
            layout.root / MANIFEST_FILENAME,
            {"execution_id": int(match.group(1)), "files": manifest_files},
            mode=0o600,
        )
    except (AttributeError, OSError, WorkspaceError) as error:
        raise InputPreparationError("input_artifact_not_ready") from error


def _path_exists(path: Path) -> bool:
    return _lstat(path) is not None


def _rmtree_on_error(func: Any, path: str | bytes, error: BaseException) -> None:
    """Retry a failed removal after restoring private workspace write bits.

    Input files and their directory are deliberately made read-only before
    the Adapter starts.  That protects against accidental writes by the same
    OS user, but it also means their parent cannot unlink the files during
    cleanup.  Only the already-controlled Workspace tree reaches this
    handler; symlinks are never chmod-ed or traversed.
    """
    candidate = Path(os.fsdecode(path))
    parent = candidate.parent
    parent.chmod(0o700)
    info = _lstat(candidate)
    if info is not None and stat_module.S_ISDIR(info.st_mode):
        candidate.chmod(0o700)
    elif info is not None and not stat_module.S_ISLNK(info.st_mode):
        candidate.chmod(0o600)
    func(path)


def _bounded_delete(path: Path, timeout_seconds: float) -> bool:
    """Run rmtree on a daemon thread so a hung filesystem call is bounded."""
    if not _path_exists(path):
        return True
    info = _lstat(path)
    if info is None:
        return True
    if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISDIR(info.st_mode):
        return False
    with _CLEANUP_LOCK:
        if path in _CLEANUP_IN_PROGRESS:
            return False
        _CLEANUP_IN_PROGRESS.add(path)
    error_holder: list[BaseException] = []

    def delete() -> None:
        try:
            shutil.rmtree(path, onexc=_rmtree_on_error)
        except BaseException as error:  # pragma: no cover - thread handoff
            error_holder.append(error)
        finally:
            with _CLEANUP_LOCK:
                _CLEANUP_IN_PROGRESS.discard(path)

    thread = threading.Thread(target=delete, name="dlr-workspace-cleanup", daemon=True)
    try:
        thread.start()
    except BaseException:
        with _CLEANUP_LOCK:
            _CLEANUP_IN_PROGRESS.discard(path)
        raise
    thread.join(max(0.0, timeout_seconds))
    if thread.is_alive() or error_holder:
        return False
    return not _path_exists(path)


def cleanup_workspace(
    workspace: Path,
    *,
    attempt_timeout_seconds: float,
    total_timeout_seconds: float,
) -> CleanupOutcome:
    """Delete a Workspace with an inclusive hard budget and confirmation."""
    if not _path_exists(workspace):
        return CleanupOutcome("completed")
    if (
        not all(math.isfinite(value) for value in (attempt_timeout_seconds, total_timeout_seconds))
        or attempt_timeout_seconds <= 0
        or total_timeout_seconds <= 0
    ):
        return CleanupOutcome("deferred", "workspace_cleanup_failed")
    deadline = time.monotonic() + total_timeout_seconds
    attempt = 0
    backoff = 0.05
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        attempt += 1
        if _bounded_delete(workspace, min(attempt_timeout_seconds, remaining)):
            return CleanupOutcome("completed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        delay = min(backoff, remaining)
        time.sleep(delay)
        backoff = min(backoff * 2, 0.5)
    return CleanupOutcome("deferred", "workspace_cleanup_failed")


def _bounded_workspace_cleanup(
    workspace: Path,
    *,
    attempt_timeout_seconds: float,
    total_timeout_seconds: float,
    timeout_seconds: float,
) -> CleanupOutcome:
    """Keep one recovery item from exceeding the scan's remaining budget."""
    outcome_holder: list[CleanupOutcome] = []
    error_holder: list[BaseException] = []

    def run_cleanup() -> None:
        try:
            outcome_holder.append(
                cleanup_workspace(
                    workspace,
                    attempt_timeout_seconds=attempt_timeout_seconds,
                    total_timeout_seconds=total_timeout_seconds,
                )
            )
        except BaseException as error:  # pragma: no cover - thread handoff
            error_holder.append(error)

    thread = threading.Thread(target=run_cleanup, name="dlr-recovery-cleanup", daemon=True)
    try:
        thread.start()
    except BaseException:
        return CleanupOutcome("deferred", "workspace_cleanup_failed")
    thread.join(max(0.0, timeout_seconds))
    if thread.is_alive() or error_holder or not outcome_holder:
        return CleanupOutcome("deferred", "workspace_cleanup_failed")
    return outcome_holder[0]


def _bounded_cleanup_receipt(
    report_cleanup: CleanupReporter,
    execution_id: int,
    cleanup_token: str,
    timeout_seconds: float,
) -> bool:
    """Bound a recovery receipt call without dropping its journal."""
    result_holder: list[bool] = []

    def report() -> None:
        try:
            result_holder.append(bool(report_cleanup(execution_id, cleanup_token)))
        except Exception:  # noqa: BLE001 - retryable recovery transport failure
            result_holder.append(False)

    thread = threading.Thread(target=report, name="dlr-recovery-receipt", daemon=True)
    try:
        thread.start()
    except BaseException:
        return False
    thread.join(max(0.0, timeout_seconds))
    return not thread.is_alive() and bool(result_holder and result_holder[0])


def _journal_retry_due(path: Path, now: float, backoff_seconds: float) -> bool:
    info = _lstat(path)
    return info is not None and now >= info.st_mtime + backoff_seconds


def _defer_journal_retry(path: Path, execution_id: int) -> None:
    """Persist the next retry boundary without changing journal contents."""
    try:
        now = time.time()
        os.utime(path, (now, now), follow_symlinks=False)
        _sync_directory(path.parent)
        logger.info(
            "cleanup journal deferred for execution %s; retry backoff persisted",
            execution_id,
        )
    except OSError:
        logger.warning(
            "cleanup journal deferred for execution %s; retry backoff persistence unavailable",
            execution_id,
        )


def _read_json(path: Path) -> object | None:
    if not _is_regular(path):
        return None
    try:
        return cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError):
        return None


def _valid_workspace_triple(workspace: Path, execution_id: int) -> bool:
    match = WORKSPACE_NAME_PATTERN.fullmatch(workspace.name)
    info = _lstat(workspace)
    if (
        match is None
        or int(match.group(1)) != execution_id
        or info is None
        or not stat_module.S_ISDIR(info.st_mode)
        or info.st_mode & 0o077
    ):
        return False
    marker = _read_json(workspace / MARKER_FILENAME)
    manifest = _read_json(workspace / MANIFEST_FILENAME)
    if not isinstance(marker, Mapping) or not isinstance(manifest, Mapping):
        return False
    marker_execution_id = marker.get("execution_id")
    if (
        not isinstance(marker_execution_id, int)
        or isinstance(marker_execution_id, bool)
        or marker_execution_id != execution_id
    ):
        return False
    if marker.get("format") != 1:
        return False
    manifest_execution_id = manifest.get("execution_id")
    if (
        not isinstance(manifest_execution_id, int)
        or isinstance(manifest_execution_id, bool)
        or manifest_execution_id != execution_id
    ):
        return False
    files = manifest.get("files")
    return isinstance(files, list)


def _load_journal(path: Path, expected_execution_id: int) -> dict[str, Any] | None:
    info = _lstat(path)
    if (
        info is None
        or stat_module.S_ISLNK(info.st_mode)
        or not stat_module.S_ISREG(info.st_mode)
        or stat_module.S_IMODE(info.st_mode) != 0o600
    ):
        return None
    value = _read_json(path)
    if not isinstance(value, Mapping) or set(value) != JOURNAL_FIELDS:
        return None
    if not all(isinstance(value[field], str) for field in ("workspace_path", "cleanup_token")):
        return None
    if not isinstance(value["execution_id"], int) or isinstance(value["execution_id"], bool):
        return None
    if not isinstance(value["protocol_version"], int) or isinstance(
        value["protocol_version"], bool
    ):
        return None
    try:
        execution_id = int(value["execution_id"])
        protocol_version = int(value["protocol_version"])
        workspace = value["workspace_path"]
        cleanup_token = value["cleanup_token"]
    except (TypeError, ValueError, KeyError):
        return None
    if (
        execution_id != expected_execution_id
        or protocol_version != 2
        or not cleanup_token
        or not Path(workspace).is_absolute()
    ):
        return None
    return {
        "execution_id": execution_id,
        "protocol_version": protocol_version,
        "workspace_path": workspace,
        "cleanup_token": cleanup_token,
    }


def recover_cleanup_journals(
    journal_root: Path,
    runtime_root: Path,
    *,
    report_cleanup: CleanupReporter | None = None,
    attempt_timeout_seconds: float = 5.0,
    total_timeout_seconds: float = 20.0,
    scan_timeout_seconds: float = RECOVERY_SCAN_TIMEOUT_SECONDS,
    retry_backoff_seconds: float = RECOVERY_RETRY_BACKOFF_SECONDS,
) -> dict[str, int]:
    """Recover only journaled, triple-matched workspaces.

    Unknown directories and malformed journals are retained.  Counts are safe
    operational facts and contain no filenames, tokens, or file contents.
    """
    try:
        scan_budget = (
            float(scan_timeout_seconds)
            if math.isfinite(float(scan_timeout_seconds)) and float(scan_timeout_seconds) > 0
            else RECOVERY_SCAN_TIMEOUT_SECONDS
        )
    except (TypeError, ValueError):
        scan_budget = RECOVERY_SCAN_TIMEOUT_SECONDS
    try:
        retry_backoff = (
            float(retry_backoff_seconds)
            if math.isfinite(float(retry_backoff_seconds)) and float(retry_backoff_seconds) > 0
            else RECOVERY_RETRY_BACKOFF_SECONDS
        )
    except (TypeError, ValueError):
        retry_backoff = RECOVERY_RETRY_BACKOFF_SECONDS
    deadline = time.monotonic() + scan_budget
    budget_exhausted = False
    root = Path(journal_root)
    if not root.is_absolute() or not _is_directory(root):
        return {"inspected": 0, "completed": 0, "deferred": 0, "retained": 0}
    try:
        _ensure_private_directory(root)
    except WorkspaceError:
        return {"inspected": 0, "completed": 0, "deferred": 0, "retained": 0}
    counts = {"inspected": 0, "completed": 0, "deferred": 0, "retained": 0}
    try:
        candidates = list(root.iterdir())
    except OSError:
        return counts
    for candidate in candidates:
        if time.monotonic() >= deadline:
            budget_exhausted = True
            break
        match = JOURNAL_NAME_PATTERN.fullmatch(candidate.name)
        if match is None or not _is_regular(candidate):
            continue
        execution_id = int(match.group(1))
        counts["inspected"] += 1
        record = _load_journal(candidate, execution_id)
        try:
            expected_workspace = workspace_path(Path(runtime_root), execution_id)
        except WorkspaceError:
            counts["retained"] += 1
            logger.warning("retained invalid cleanup journal for execution %s", execution_id)
            continue
        if record is None or Path(record["workspace_path"]) != expected_workspace:
            counts["retained"] += 1
            logger.warning("retained invalid cleanup journal for execution %s", execution_id)
            continue
        workspace = expected_workspace
        if _path_exists(workspace) and not _valid_workspace_triple(workspace, execution_id):
            counts["retained"] += 1
            logger.warning("retained unverified workspace for execution %s", execution_id)
            continue
        if not _journal_retry_due(candidate, time.time(), retry_backoff):
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            budget_exhausted = True
            break
        outcome = _bounded_workspace_cleanup(
            workspace,
            attempt_timeout_seconds=min(attempt_timeout_seconds, remaining),
            total_timeout_seconds=min(total_timeout_seconds, remaining),
            timeout_seconds=remaining,
        )
        if outcome.status != "completed":
            counts["deferred"] += 1
            _defer_journal_retry(candidate, execution_id)
            if time.monotonic() >= deadline:
                budget_exhausted = True
                break
            continue
        if report_cleanup is None:
            counts["retained"] += 1
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            counts["deferred"] += 1
            _defer_journal_retry(candidate, execution_id)
            budget_exhausted = True
            break
        receipt_accepted = _bounded_cleanup_receipt(
            report_cleanup,
            execution_id,
            str(record["cleanup_token"]),
            remaining,
        )
        if receipt_accepted and remove_cleanup_journal(root, execution_id):
            counts["completed"] += 1
        else:
            counts["deferred"] += 1
            _defer_journal_retry(candidate, execution_id)
            if time.monotonic() >= deadline:
                budget_exhausted = True
                break
    if budget_exhausted:
        logger.info("workspace cleanup scan reached its bounded time budget")
    return counts
