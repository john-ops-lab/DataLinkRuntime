"""Durable local safety facts for Worker-owned version caches.

These records deliberately live outside ``entries``.  The verified content
manifest remains the immutable, backward-compatible content contract; this
module records only ownership, lifecycle and live-use facts needed by cache
governance.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dlr.worker.cache import CacheError

SCHEMA_VERSION = 1
_STATE_DIR = ".dlr-cache-lifecycle"
_OWNER_FIELDS = frozenset({"schema", "store_id", "worker_id"})
_LIFECYCLE_FIELDS = frozenset(
    {
        "schema",
        "key",
        "identity",
        "digest",
        "bytes",
        "first_observed_at",
        "last_used_at",
        "pinned",
        "rebuildability",
        "policy_revision",
    }
)
_LIFECYCLE_POLICY_FIELDS = frozenset({"pin_audit", "rebuild_proof", "source_unavailable"})
_LIFECYCLE_MANAGEMENT_FIELDS = frozenset({"management_applied"})
_SOURCE_FAILURE_FIELDS = frozenset({"schema", "source_scope", "error_code", "observed_at"})
_USE_FIELDS = frozenset(
    {"schema", "key", "worker_id", "execution_id", "attempt_id", "fencing_token", "started_at"}
)
_SAFE_KEY = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_ATTEMPT_JOURNAL_FIELDS = frozenset(
    {
        "execution_id",
        "attempt_id",
        "attempt_no",
        "fencing_token",
        "lease_expires_at",
        "protocol_version",
        "workspace_path",
        "claim_token",
        "cleanup_token",
    }
)
_CLEANUP_JOURNAL_FIELDS = frozenset(
    {"execution_id", "protocol_version", "workspace_path", "cleanup_token", "attempt_id"}
)
_SANDBOX_JOURNAL_FIELDS = frozenset({"cgroup_name", "execution_id", "mount_name", "mount_path"})
_SANDBOX_NAMESPACE_FIELDS = frozenset({"namespace_identity", "cgroup_device", "cgroup_inode"})
_STATE_MAX_BYTES = 1024 * 1024
_JOURNAL_MAX_BYTES = 64 * 1024
_INSTANCE_LOCK_NAME = ".dlr-instance.lock"


def cache_key(adapter_id: int, version_id: int) -> str:
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in (adapter_id, version_id)
    ):
        raise CacheError("cache_key_invalid")
    return f"{adapter_id}-{version_id}"


def _private_directory(path: Path) -> Path:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise CacheError("cache_lifecycle_invalid")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(path, 0o700, follow_symlinks=False)
    except OSError as error:
        raise CacheError("cache_lifecycle_unavailable") from error
    return path


def _cache_directory(path: Path) -> Path:
    try:
        path.mkdir(mode=0o711, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as error:
        raise CacheError("cache_root_unavailable") from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise CacheError("cache_root_invalid")
    return path


def _safe_key(value: str) -> str:
    if not value or any(character not in _SAFE_KEY for character in value):
        raise CacheError("cache_key_invalid")
    return value


def _sync_directory(path: Path, *, strict: bool = False) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as error:
        if strict:
            raise CacheError("cache_lifecycle_sync_failed") from error
        return
    try:
        os.fsync(descriptor)
    except OSError as error:
        if strict:
            raise CacheError("cache_lifecycle_sync_failed") from error
    finally:
        os.close(descriptor)


def _write_json(path: Path, value: Mapping[str, Any], *, strict_sync: bool = False) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent, strict=strict_sync)
    except (OSError, TypeError, ValueError) as error:
        with suppress(OSError):
            temporary.unlink()
        raise CacheError("cache_lifecycle_write_failed") from error


def _read_bounded_json(path: Path, *, max_bytes: int) -> dict[str, Any] | None:
    descriptor = -1
    try:
        initial = path.lstat()
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_uid != os.geteuid()
            or stat.S_IMODE(initial.st_mode) != 0o600
            or initial.st_size > max_bytes
        ):
            raise CacheError("cache_lifecycle_invalid")
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > max_bytes
        ):
            raise CacheError("cache_lifecycle_invalid")
        chunks = bytearray()
        while len(chunks) <= max_bytes:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > max_bytes:
            raise CacheError("cache_lifecycle_invalid")
        value = json.loads(bytes(chunks).decode("ascii"))
    except FileNotFoundError:
        return None
    except (OSError, RecursionError, UnicodeError, ValueError) as error:
        raise CacheError("cache_lifecycle_invalid") from error
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
    if not isinstance(value, dict):
        raise CacheError("cache_lifecycle_invalid")
    return value


def _is_journal_instance_lock(
    root: Path,
    *,
    observed_root: os.stat_result,
    observed_file: os.stat_result,
) -> bool:
    """Recognize only the empty private lock created by ``lock_roots``."""

    def valid_root(info: os.stat_result) -> bool:
        return (
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700
        )

    def valid_file(info: os.stat_result) -> bool:
        return (
            stat.S_ISREG(info.st_mode)
            and not stat.S_ISLNK(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and info.st_size == 0
        )

    root_descriptor = -1
    file_descriptor = -1
    try:
        if not valid_root(observed_root) or not valid_file(observed_file):
            return False
        root_descriptor = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        opened_root = os.fstat(root_descriptor)
        if not valid_root(opened_root) or (opened_root.st_dev, opened_root.st_ino) != (
            observed_root.st_dev,
            observed_root.st_ino,
        ):
            return False
        file_descriptor = os.open(
            _INSTANCE_LOCK_NAME,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=root_descriptor,
        )
        opened_file = os.fstat(file_descriptor)
        if (
            not valid_file(opened_file)
            or (opened_file.st_dev, opened_file.st_ino)
            != (observed_file.st_dev, observed_file.st_ino)
            or os.read(file_descriptor, 1) != b""
        ):
            return False
        final_file = os.stat(
            _INSTANCE_LOCK_NAME,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        final_root = root.lstat()
        return (
            valid_file(final_file)
            and (final_file.st_dev, final_file.st_ino)
            == (observed_file.st_dev, observed_file.st_ino)
            and valid_root(final_root)
            and (final_root.st_dev, final_root.st_ino)
            == (observed_root.st_dev, observed_root.st_ino)
        )
    except (NotImplementedError, OSError):
        return False
    finally:
        if file_descriptor >= 0:
            with suppress(OSError):
                os.close(file_descriptor)
        if root_descriptor >= 0:
            with suppress(OSError):
                os.close(root_descriptor)


def _read_json(path: Path, fields: frozenset[str]) -> dict[str, Any] | None:
    value = _read_bounded_json(path, max_bytes=_STATE_MAX_BYTES)
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != fields or value.get("schema") != SCHEMA_VERSION:
        raise CacheError("cache_lifecycle_invalid")
    return value


def _validated_lifecycle(value: dict[str, Any]) -> dict[str, Any]:
    try:
        _safe_key(value["key"])
        first_observed = float(value["first_observed_at"])
        last_used = float(value["last_used_at"])
    except (CacheError, KeyError, OverflowError, TypeError, ValueError) as error:
        raise CacheError("cache_lifecycle_invalid") from error
    digest = value["digest"]
    bytes_used = value["bytes"]
    policy_revision = value["policy_revision"]
    if (
        not isinstance(value["identity"], dict)
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or not isinstance(bytes_used, int)
        or isinstance(bytes_used, bool)
        or bytes_used < 0
        or not math.isfinite(first_observed)
        or not math.isfinite(last_used)
        or first_observed <= 0
        or last_used <= 0
        or type(value["pinned"]) is not bool
        or not isinstance(value["rebuildability"], str)
        or value["rebuildability"] not in {"unknown", "confirmed"}
        or not isinstance(policy_revision, int)
        or isinstance(policy_revision, bool)
        or policy_revision < 0
    ):
        raise CacheError("cache_lifecycle_invalid")
    normalized = dict(value)
    for field in _LIFECYCLE_POLICY_FIELDS:
        normalized.setdefault(field, None)
    normalized.setdefault("management_applied", None)
    management = normalized["management_applied"]
    if management is not None:
        try:
            uuid.UUID(str(management["operation_id"]))
            applied_at = float(management["applied_at"])
        except (KeyError, OverflowError, TypeError, ValueError) as error:
            raise CacheError("cache_lifecycle_invalid") from error
        request_hash = management.get("request_hash")
        if (
            not isinstance(management, dict)
            or set(management) != {"operation_id", "request_hash", "applied_at"}
            or not isinstance(request_hash, str)
            or len(request_hash) != 64
            or any(character not in "0123456789abcdef" for character in request_hash)
            or not math.isfinite(applied_at)
            or applied_at <= 0
        ):
            raise CacheError("cache_lifecycle_invalid")
    pin_audit = normalized["pin_audit"]
    if pin_audit is not None:
        if (
            not isinstance(pin_audit, dict)
            or set(pin_audit) != {"actor", "reason", "changed_at"}
            or not isinstance(pin_audit.get("actor"), str)
            or not pin_audit["actor"]
            or len(pin_audit["actor"]) > 128
            or not isinstance(pin_audit.get("reason"), str)
            or len(pin_audit["reason"]) > 256
            or not isinstance(pin_audit.get("changed_at"), (int, float))
            or isinstance(pin_audit.get("changed_at"), bool)
        ):
            raise CacheError("cache_lifecycle_invalid")
        try:
            changed_at = float(pin_audit["changed_at"])
        except (OverflowError, TypeError, ValueError) as error:
            raise CacheError("cache_lifecycle_invalid") from error
        if not math.isfinite(changed_at):
            raise CacheError("cache_lifecycle_invalid")
    proof = normalized["rebuild_proof"]
    if proof is not None and (
        not isinstance(proof, dict)
        or set(proof)
        not in (
            {
                "identity",
                "digest",
                "source_policy",
                "evidence_note",
                "actor",
                "confirmed_at",
                "valid_until",
                "automatic",
            },
            {
                "identity",
                "digest",
                "source_policy",
                "evidence_note",
                "actor",
                "confirmed_at",
                "valid_until",
                "automatic",
                "source_scope",
            },
        )
        or not isinstance(proof.get("identity"), dict)
        or not isinstance(proof.get("digest"), str)
        or not isinstance(proof.get("source_policy"), str)
        or proof["source_policy"] not in {"verified_offline", "managed_online"}
        or not isinstance(proof.get("evidence_note"), str)
        or not proof["evidence_note"]
        or len(proof["evidence_note"]) > 256
        or not isinstance(proof.get("actor"), str)
        or not proof["actor"]
        or len(proof["actor"]) > 128
        or type(proof.get("automatic")) is not bool
        or (
            proof.get("source_scope") is not None
            and (
                not isinstance(proof.get("source_scope"), str)
                or not proof["source_scope"]
                or len(proof["source_scope"]) > 128
            )
        )
    ):
        raise CacheError("cache_lifecycle_invalid")
    if proof is not None:
        try:
            confirmed_at = float(proof["confirmed_at"])
            valid_until = float(proof["valid_until"])
        except (OverflowError, TypeError, ValueError) as error:
            raise CacheError("cache_lifecycle_invalid") from error
        if (
            not math.isfinite(confirmed_at)
            or not math.isfinite(valid_until)
            or valid_until <= confirmed_at
            or valid_until - confirmed_at > 86_400
        ):
            raise CacheError("cache_lifecycle_invalid")
    unavailable = normalized["source_unavailable"]
    if unavailable is not None and (
        not isinstance(unavailable, dict)
        or set(unavailable)
        not in (
            {"identity", "digest", "error_code", "observed_at"},
            {"identity", "digest", "error_code", "observed_at", "source_scope"},
        )
        or not isinstance(unavailable.get("identity"), dict)
        or not isinstance(unavailable.get("digest"), str)
        or not isinstance(unavailable.get("error_code"), str)
        or not unavailable["error_code"]
        or len(unavailable["error_code"]) > 128
        or not isinstance(unavailable.get("observed_at"), (int, float))
        or not math.isfinite(float(unavailable["observed_at"]))
        or (
            unavailable.get("source_scope") is not None
            and (
                not isinstance(unavailable.get("source_scope"), str)
                or not unavailable["source_scope"]
                or len(unavailable["source_scope"]) > 128
            )
        )
    ):
        raise CacheError("cache_lifecycle_invalid")
    return normalized


def _read_lifecycle(path: Path) -> dict[str, Any] | None:
    value = _read_bounded_json(path, max_bytes=_STATE_MAX_BYTES)
    if value is None:
        return None
    fields = set(value)
    if value.get("schema") != SCHEMA_VERSION or frozenset(fields) not in {
        _LIFECYCLE_FIELDS,
        _LIFECYCLE_FIELDS | _LIFECYCLE_POLICY_FIELDS,
        _LIFECYCLE_FIELDS | _LIFECYCLE_POLICY_FIELDS | _LIFECYCLE_MANAGEMENT_FIELDS,
    }:
        raise CacheError("cache_lifecycle_invalid")
    return _validated_lifecycle(value)


def _validated_use(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    try:
        key = _safe_key(value["key"])
        started_at = float(value["started_at"])
    except (CacheError, KeyError, OverflowError, TypeError, ValueError) as error:
        raise CacheError("cache_use_invalid") from error
    identifiers = ("worker_id", "execution_id", "attempt_id", "fencing_token")
    if (
        any(
            not isinstance(value[name], int) or isinstance(value[name], bool) or value[name] <= 0
            for name in identifiers
        )
        or not math.isfinite(started_at)
        or started_at <= 0
        or path.name != f"attempt-{value['attempt_id']}.json"
        or not key
    ):
        raise CacheError("cache_use_invalid")
    return value


def _journal_identity(path: Path, kind: str, value: dict[str, Any]) -> tuple[int, int | None]:
    fields = frozenset(value)
    expected = {
        "attempt": (_ATTEMPT_JOURNAL_FIELDS,),
        "cleanup": (_CLEANUP_JOURNAL_FIELDS,),
        "sandbox": (
            _SANDBOX_JOURNAL_FIELDS,
            _SANDBOX_JOURNAL_FIELDS | _SANDBOX_NAMESPACE_FIELDS,
        ),
    }[kind]
    if fields not in expected:
        raise CacheError("cache_journal_invalid")
    execution_id = value.get("execution_id")
    attempt_id = value.get("attempt_id")
    if not isinstance(execution_id, int) or isinstance(execution_id, bool) or execution_id <= 0:
        raise CacheError("cache_journal_invalid")
    if kind == "attempt":
        positive = ("attempt_id", "attempt_no", "fencing_token")
        strings = ("lease_expires_at", "workspace_path", "claim_token", "cleanup_token")
        if (
            any(
                not isinstance(value[name], int)
                or isinstance(value[name], bool)
                or value[name] <= 0
                for name in positive
            )
            or value["protocol_version"] != 3
            or any(not isinstance(value[name], str) or not value[name] for name in strings)
            or not Path(value["workspace_path"]).is_absolute()
            or path.name != f"attempt-{value['attempt_id']}.attempt.json"
        ):
            raise CacheError("cache_journal_invalid")
        attempt_id = value["attempt_id"]
    elif kind == "cleanup":
        if (
            not isinstance(attempt_id, int)
            or isinstance(attempt_id, bool)
            or attempt_id <= 0
            or value["protocol_version"] != 3
            or not isinstance(value["workspace_path"], str)
            or not Path(value["workspace_path"]).is_absolute()
            or not isinstance(value["cleanup_token"], str)
            or not value["cleanup_token"]
            or path.name != f"execution-{execution_id}-attempt-{attempt_id}.cleanup.json"
        ):
            raise CacheError("cache_journal_invalid")
    else:
        from dlr.worker import sandbox

        base_strings = ("cgroup_name", "mount_name", "mount_path")
        name = value["cgroup_name"]
        if (
            any(not isinstance(value[name], str) or not value[name] for name in base_strings)
            or not sandbox.ATTEMPT_NAME_PATTERN.fullmatch(name)
            or not Path(value["mount_path"]).is_absolute()
            or value["mount_name"] != ".dlr-sandbox-mount"
            or path.name != f"sandbox-{name}.json"
        ):
            raise CacheError("cache_journal_invalid")
        attempt_match = sandbox.ATTEMPT_CGROUP_NAME_PATTERN.fullmatch(name)
        if attempt_match is not None:
            if int(attempt_match.group("execution_id")) != execution_id:
                raise CacheError("cache_journal_invalid")
        elif execution_id != 1:
            raise CacheError("cache_journal_invalid")
        if fields != _SANDBOX_JOURNAL_FIELDS and (
            not isinstance(value["namespace_identity"], dict)
            or set(value["namespace_identity"])
            != {"boot_id", "parent_device", "parent_inode", "root_device", "root_inode"}
            or not isinstance(value["namespace_identity"].get("boot_id"), str)
            or not re.fullmatch(
                r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                value["namespace_identity"]["boot_id"],
            )
            or any(
                type(value["namespace_identity"][field]) is not int
                or value["namespace_identity"][field] < 0
                for field in ("parent_device", "parent_inode", "root_device", "root_inode")
            )
            or type(value["cgroup_device"]) is not int
            or value["cgroup_device"] < 0
            or type(value["cgroup_inode"]) is not int
            or value["cgroup_inode"] <= 0
        ):
            raise CacheError("cache_journal_invalid")
        attempt_id = None
    return execution_id, attempt_id


def _open_lock(path: Path) -> Any:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise CacheError("cache_lock_invalid")
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "a+", encoding="ascii")
        descriptor = -1
        return handle
    except OSError as error:
        raise CacheError("cache_lock_invalid") from error
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)


@dataclass
class _LockState:
    mutex: threading.RLock
    pid: int = 0
    depth: int = 0
    handle: Any = None


_locks_guard = threading.Lock()
_locks: dict[str, _LockState] = {}
_active_uses = threading.local()


def _use_token(store: CacheLifecycleStore, value: Mapping[str, Any]) -> tuple[object, ...]:
    return (
        str(store.state_root),
        value["key"],
        value["worker_id"],
        value["execution_id"],
        value["attempt_id"],
        value["fencing_token"],
    )


def current_thread_owns_all_uses(store: CacheLifecycleStore, records: list[dict[str, Any]]) -> bool:
    counts = getattr(_active_uses, "counts", {})
    return bool(records) and all(counts.get(_use_token(store, record), 0) > 0 for record in records)


class CacheEntryLock(AbstractContextManager["CacheEntryLock"]):
    """One process-safe, thread-reentrant exclusive lock for a cache key."""

    def __init__(self, lock_path: Path, *, blocking: bool = True) -> None:
        self._path = lock_path
        identity = str(lock_path)
        with _locks_guard:
            self._state = _locks.setdefault(identity, _LockState(threading.RLock()))
        self._entered = False
        self._blocking = blocking

    def __enter__(self) -> CacheEntryLock:
        if not self._state.mutex.acquire(blocking=self._blocking):
            raise CacheError("cache_lock_busy")
        try:
            pid = os.getpid()
            if self._state.pid != pid:
                self._state.pid = pid
                self._state.depth = 0
                self._state.handle = None
            if self._state.depth == 0:
                handle = _open_lock(self._path)
                try:
                    flags = fcntl.LOCK_EX
                    if not self._blocking:
                        flags |= fcntl.LOCK_NB
                    fcntl.flock(handle.fileno(), flags)
                except OSError as error:
                    handle.close()
                    code = "cache_lock_busy" if not self._blocking else "cache_lock_unavailable"
                    raise CacheError(code) from error
                self._state.handle = handle
            self._state.depth += 1
            self._entered = True
            return self
        except BaseException:
            self._state.mutex.release()
            raise

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if not self._entered:
            return
        try:
            self._state.depth -= 1
            if self._state.depth == 0:
                handle = self._state.handle
                self._state.handle = None
                if handle is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()
        finally:
            self._entered = False
            self._state.mutex.release()


@dataclass(frozen=True)
class JournalProtection:
    """Bounded local protection result for a future governance decision."""

    protected_keys: frozenset[str]
    block_all: bool
    reasons: tuple[str, ...]


class CacheLifecycleStore:
    """Versioned local sidecars and locks for one persistent cache root."""

    def __init__(self, cache_root: Path) -> None:
        root = Path(cache_root)
        if not root.is_absolute():
            raise CacheError("cache_root_invalid")
        self.cache_root = _cache_directory(root)
        self.state_root = _private_directory(root / _STATE_DIR)
        self.lifecycle_root = _private_directory(self.state_root / "items")
        self.use_root = _private_directory(self.state_root / "uses")
        self.lock_root = _private_directory(self.state_root / "locks")
        self.deletion_root = _private_directory(self.state_root / "deletions")
        self.owner_path = self.state_root / "owner.json"

    @classmethod
    def for_runtime(cls, runtime_root: Path) -> CacheLifecycleStore:
        return cls(Path(runtime_root) / "version-cache")

    def entry_lock(self, key: str, *, blocking: bool = True) -> CacheEntryLock:
        return CacheEntryLock(self.lock_root / f"{_safe_key(key)}.lock", blocking=blocking)

    def owner(self, worker_id: int) -> dict[str, Any]:
        value = _read_json(self.owner_path, _OWNER_FIELDS)
        if value is None or value.get("worker_id") != worker_id:
            raise CacheError("cache_owner_unconfirmed")
        store_id = value.get("store_id")
        if not isinstance(store_id, str) or not store_id:
            raise CacheError("cache_owner_invalid")
        return value

    def lifecycle(self, key: str) -> dict[str, Any]:
        path = self.lifecycle_root / f"{_safe_key(key)}.json"
        value = _read_lifecycle(path)
        if value is None:
            raise CacheError("cache_lifecycle_unknown")
        validated = _validated_lifecycle(value)
        if validated["key"] != key:
            raise CacheError("cache_lifecycle_invalid")
        return validated

    def bind_owner(self, worker_id: int, *, allow_legacy_nonempty: bool = False) -> str:
        """Bind a fresh/confirmed root without silently taking another owner's data."""
        if not isinstance(worker_id, int) or isinstance(worker_id, bool) or worker_id <= 0:
            raise CacheError("cache_owner_invalid")
        with CacheEntryLock(self.lock_root / "owner.lock"):
            return self._bind_owner_locked(worker_id, allow_legacy_nonempty=allow_legacy_nonempty)

    def _bind_owner_locked(self, worker_id: int, *, allow_legacy_nonempty: bool) -> str:
        owner = _read_json(self.owner_path, _OWNER_FIELDS)
        if owner is not None:
            if owner["worker_id"] != worker_id:
                raise CacheError("cache_owner_mismatch")
            store_id = owner["store_id"]
            if not isinstance(store_id, str) or not store_id:
                raise CacheError("cache_owner_invalid")
            return store_id
        entries = self.cache_root / "entries"
        nonempty = entries.is_dir() and any(entries.iterdir())
        if nonempty and not allow_legacy_nonempty:
            raise CacheError("cache_owner_unconfirmed")
        store_id = uuid.uuid4().hex
        _write_json(
            self.owner_path,
            {"schema": SCHEMA_VERSION, "store_id": store_id, "worker_id": worker_id},
        )
        return store_id

    def observe_verified(
        self,
        key: str,
        *,
        identity: Mapping[str, Any],
        digest: str,
        bytes_used: int,
        now: float | None = None,
    ) -> None:
        """Initialize/update lifecycle data after exact content verification."""
        timestamp = time.time() if now is None else now
        path = self.lifecycle_root / f"{_safe_key(key)}.json"
        existing = _read_lifecycle(path)
        first_observed = timestamp
        pinned = False
        rebuildability = "unknown"
        policy_revision = 0
        last_used = timestamp
        if existing is not None:
            if existing["key"] != key:
                raise CacheError("cache_lifecycle_invalid")
            if existing["identity"] != dict(identity) or existing["digest"] != digest:
                raise CacheError("cache_lifecycle_conflict")
            first_observed = float(existing["first_observed_at"])
            last_used = float(existing["last_used_at"])
            pinned = bool(existing["pinned"])
            rebuildability = str(existing["rebuildability"])
            policy_revision = int(existing["policy_revision"])
        updated = {
            "schema": SCHEMA_VERSION,
            "key": key,
            "identity": dict(identity),
            "digest": digest,
            "bytes": bytes_used,
            "first_observed_at": first_observed,
            "last_used_at": last_used,
            "pinned": pinned,
            "rebuildability": rebuildability,
            "policy_revision": policy_revision,
        }
        if existing is not None:
            for field in _LIFECYCLE_POLICY_FIELDS | _LIFECYCLE_MANAGEMENT_FIELDS:
                updated[field] = existing[field]
        _write_json(path, updated)

    def initialize_legacy_bound(
        self,
        key: str,
        *,
        identity: Mapping[str, Any],
        digest: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Bind a manifest-owned legacy root without claiming its content is verified."""

        path = self.lifecycle_root / f"{_safe_key(key)}.json"
        existing = _read_lifecycle(path)
        if existing is not None:
            validated = existing
            if (
                validated["key"] != key
                or validated["identity"] != dict(identity)
                or validated["digest"] != digest
            ):
                raise CacheError("cache_lifecycle_conflict")
            return validated
        timestamp = time.time() if now is None else now
        value = {
            "schema": SCHEMA_VERSION,
            "key": key,
            "identity": dict(identity),
            "digest": digest,
            "bytes": 0,
            "first_observed_at": timestamp,
            "last_used_at": timestamp,
            "pinned": False,
            "rebuildability": "unknown",
            "policy_revision": 0,
        }
        _write_json(path, value, strict_sync=True)
        return _validated_lifecycle(value)

    def touch_used(self, key: str, *, now: float | None = None) -> None:
        """Advance last-used only after a real use has completed preparation."""
        path = self.lifecycle_root / f"{_safe_key(key)}.json"
        existing = _read_lifecycle(path)
        if existing is None:
            return
        updated = dict(existing)
        updated["last_used_at"] = time.time() if now is None else now
        _write_json(path, updated)

    def set_pin(
        self,
        key: str,
        *,
        identity: Mapping[str, Any],
        digest: str,
        pinned: bool,
        actor: str,
        reason: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if now is None else now
        if not actor or len(actor) > 128 or len(reason) > 256 or not math.isfinite(timestamp):
            raise CacheError("cache_policy_invalid")
        with self.entry_lock(key):
            value = self.lifecycle(key)
            if value["identity"] != dict(identity) or value["digest"] != digest:
                raise CacheError("cache_identity_conflict")
            updated = dict(value)
            updated["pinned"] = pinned
            updated["pin_audit"] = {
                "actor": actor,
                "reason": reason,
                "changed_at": timestamp,
            }
            updated["policy_revision"] = int(value["policy_revision"]) + 1
            _write_json(self.lifecycle_root / f"{_safe_key(key)}.json", updated, strict_sync=True)
            return _validated_lifecycle(updated)

    def confirm_rebuildability(
        self,
        key: str,
        *,
        identity: Mapping[str, Any],
        digest: str,
        source_policy: str,
        evidence_note: str,
        actor: str,
        valid_until: float,
        now: float | None = None,
        automatic: bool = False,
        source_scope: str | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if now is None else now
        if (
            source_policy not in {"verified_offline", "managed_online"}
            or not evidence_note
            or len(evidence_note) > 256
            or not actor
            or len(actor) > 128
            or not math.isfinite(timestamp)
            or not math.isfinite(valid_until)
            or valid_until <= timestamp
            or valid_until - timestamp > 86_400
            or (source_scope is not None and (not source_scope or len(source_scope) > 128))
        ):
            raise CacheError("cache_rebuild_proof_invalid")
        with self.entry_lock(key):
            value = self.lifecycle(key)
            if value["identity"] != dict(identity) or value["digest"] != digest:
                raise CacheError("cache_identity_conflict")
            updated = dict(value)
            updated["rebuildability"] = "confirmed"
            updated["rebuild_proof"] = {
                "identity": dict(identity),
                "digest": digest,
                "source_policy": source_policy,
                "evidence_note": evidence_note,
                "actor": actor,
                "confirmed_at": timestamp,
                "valid_until": valid_until,
                "automatic": automatic,
                "source_scope": source_scope,
            }
            updated["source_unavailable"] = None
            updated["policy_revision"] = int(value["policy_revision"]) + 1
            _write_json(self.lifecycle_root / f"{_safe_key(key)}.json", updated, strict_sync=True)
            cleared_scopes: list[str] = []
            if source_scope is not None:
                cleared_scopes.append(source_scope)
            language = identity.get("language")
            if source_policy == "managed_online" and isinstance(language, str) and language:
                normalized = language.strip().lower()
                global_digest = hashlib.sha256(f"{normalized}\0unconfigured".encode()).hexdigest()
                cleared_scopes.append(f"{normalized}:{global_digest}")
            for cleared_scope in cleared_scopes:
                marker = hashlib.sha256(cleared_scope.encode("ascii")).hexdigest()
                try:
                    (self.state_root / f"source-unavailable-{marker}.json").unlink()
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise CacheError("cache_lifecycle_write_failed") from error
            if cleared_scopes:
                _sync_directory(self.state_root, strict=True)
            return _validated_lifecycle(updated)

    def apply_admin_protection(
        self,
        key: str,
        *,
        operation_id: uuid.UUID,
        request_hash: str,
        identity: Mapping[str, Any],
        digest: str,
        actor: str,
        pinned: bool | None = None,
        source_policy: str | None = None,
        evidence_note: str | None = None,
        valid_until: float | None = None,
        now: float | None = None,
    ) -> bool:
        """Apply one administrator action and its replay marker atomically.

        The caller owns the non-blocking key lock.  ``False`` means this exact
        operation was already applied, so later source-failure facts must stay.
        """

        timestamp = time.time() if now is None else now
        if (
            not isinstance(request_hash, str)
            or len(request_hash) != 64
            or any(character not in "0123456789abcdef" for character in request_hash)
            or not actor
            or len(actor) > 128
            or not math.isfinite(timestamp)
        ):
            raise CacheError("cache_policy_invalid")
        value = self.lifecycle(key)
        if value["identity"] != dict(identity) or value["digest"] != digest:
            raise CacheError("cache_identity_conflict")
        marker = value.get("management_applied")
        expected_marker = {
            "operation_id": str(operation_id),
            "request_hash": request_hash,
        }
        if isinstance(marker, dict) and all(
            marker.get(name) == item for name, item in expected_marker.items()
        ):
            return False
        updated = dict(value)
        if pinned is not None:
            updated["pinned"] = pinned
            updated["pin_audit"] = {
                "actor": actor,
                "reason": "administrator_cache_protection",
                "changed_at": timestamp,
            }
        if source_policy is not None:
            if (
                source_policy not in {"verified_offline", "managed_online"}
                or not evidence_note
                or len(evidence_note) > 256
                or valid_until is None
                or not math.isfinite(valid_until)
                or valid_until <= timestamp
                or valid_until - timestamp > 86_400
            ):
                raise CacheError("cache_rebuild_proof_invalid")
            updated["rebuildability"] = "confirmed"
            updated["rebuild_proof"] = {
                "identity": dict(identity),
                "digest": digest,
                "source_policy": source_policy,
                "evidence_note": evidence_note,
                "actor": actor,
                "confirmed_at": timestamp,
                "valid_until": valid_until,
                "automatic": False,
                "source_scope": None,
            }
            updated["source_unavailable"] = None
        updated["management_applied"] = {**expected_marker, "applied_at": timestamp}
        updated["policy_revision"] = int(value["policy_revision"]) + 1
        _write_json(self.lifecycle_root / f"{_safe_key(key)}.json", updated, strict_sync=True)
        return True

    def invalidate_automatic_rebuildability(
        self,
        key: str,
        *,
        identity: Mapping[str, Any],
        digest: str,
    ) -> None:
        """Revoke only a system proof after its material is observed invalid."""

        with self.entry_lock(key):
            value = self.lifecycle(key)
            proof = value.get("rebuild_proof")
            if (
                value["identity"] != dict(identity)
                or value["digest"] != digest
                or not isinstance(proof, dict)
                or proof.get("automatic") is not True
                or proof.get("identity") != dict(identity)
                or proof.get("digest") != digest
            ):
                return
            updated = dict(value)
            updated["rebuildability"] = "unknown"
            updated["rebuild_proof"] = None
            updated["policy_revision"] = int(value["policy_revision"]) + 1
            _write_json(self.lifecycle_root / f"{_safe_key(key)}.json", updated, strict_sync=True)

    def record_source_unavailable(
        self,
        key: str,
        *,
        identity: Mapping[str, Any],
        digest: str,
        error_code: str,
        source_scope: str | None = None,
        now: float | None = None,
    ) -> None:
        stable_codes = {
            "dependency_source_unavailable",
            "builtin_content_unavailable",
            "builtin_dependency_missing",
        }
        if error_code not in stable_codes:
            return
        if source_scope is not None and (not source_scope or len(source_scope) > 128):
            raise CacheError("cache_policy_invalid")
        timestamp = time.time() if now is None else now
        with self.entry_lock(key):
            value = self.lifecycle(key)
            if value["identity"] != dict(identity) or value["digest"] != digest:
                return
            updated = dict(value)
            updated["source_unavailable"] = {
                "identity": dict(identity),
                "digest": digest,
                "error_code": error_code,
                "observed_at": timestamp,
                "source_scope": source_scope,
            }
            updated["policy_revision"] = int(value["policy_revision"]) + 1
            _write_json(self.lifecycle_root / f"{_safe_key(key)}.json", updated, strict_sync=True)
        if source_scope is not None:
            self.record_source_scope_unavailable(
                source_scope=source_scope, error_code=error_code, now=timestamp
            )

    def record_source_scope_unavailable(
        self,
        *,
        source_scope: str,
        error_code: str,
        now: float | None = None,
    ) -> None:
        stable_codes = {
            "dependency_source_unavailable",
            "builtin_content_unavailable",
            "builtin_dependency_missing",
        }
        if error_code not in stable_codes or not source_scope or len(source_scope) > 128:
            raise CacheError("cache_policy_invalid")
        timestamp = time.time() if now is None else now
        if not math.isfinite(timestamp):
            raise CacheError("cache_policy_invalid")
        marker = hashlib.sha256(source_scope.encode("ascii")).hexdigest()
        _write_json(
            self.state_root / f"source-unavailable-{marker}.json",
            {
                "schema": SCHEMA_VERSION,
                "source_scope": source_scope,
                "error_code": error_code,
                "observed_at": timestamp,
            },
            strict_sync=True,
        )

    def reclamation_reason(
        self,
        key: str,
        *,
        identity: Mapping[str, Any],
        digest: str,
        offline_protection: bool,
        offline_mode: bool,
        now: float | None = None,
    ) -> str | None:
        timestamp = time.time() if now is None else now
        value = self.lifecycle(key)
        if value["identity"] != dict(identity) or value["digest"] != digest:
            return "cache_identity_conflict"
        if value["pinned"]:
            return "cache_pinned"
        proof = value["rebuild_proof"]
        if value["rebuildability"] != "confirmed" or not isinstance(proof, dict):
            return "cache_rebuild_unknown"
        if proof["identity"] != dict(identity) or proof["digest"] != digest:
            return "cache_rebuild_unknown"
        if float(proof["valid_until"]) <= timestamp:
            return "cache_rebuild_unknown"
        unavailable = value["source_unavailable"]
        if (
            isinstance(unavailable, dict)
            and unavailable["identity"] == dict(identity)
            and unavailable["digest"] == digest
        ):
            return "cache_rebuild_unavailable"
        source_scope = proof.get("source_scope")
        scopes: list[str] = []
        if isinstance(source_scope, str):
            scopes.append(source_scope)
        language = identity.get("language")
        if isinstance(language, str) and language:
            normalized = language.strip().lower()
            global_digest = hashlib.sha256(f"{normalized}\0unconfigured".encode()).hexdigest()
            scopes.append(f"{normalized}:{global_digest}")
        for candidate_scope in scopes:
            marker = hashlib.sha256(candidate_scope.encode("ascii")).hexdigest()
            source_failure = _read_json(
                self.state_root / f"source-unavailable-{marker}.json",
                _SOURCE_FAILURE_FIELDS,
            )
            if (
                source_failure is not None
                and source_failure["source_scope"] == candidate_scope
                and isinstance(source_failure.get("observed_at"), (int, float))
                and math.isfinite(float(source_failure["observed_at"]))
                and timestamp - float(source_failure["observed_at"]) <= 86_400
            ):
                return "cache_rebuild_unavailable"
        if offline_protection and offline_mode and proof["source_policy"] != "verified_offline":
            return "cache_rebuild_unavailable"
        return None

    def replace_verified(
        self,
        key: str,
        *,
        old_identity: Mapping[str, Any],
        old_digest: str,
        new_identity: Mapping[str, Any],
        new_digest: str,
        bytes_used: int,
    ) -> None:
        """Move a verified sidecar to a newly published replacement identity."""

        path = self.lifecycle_root / f"{_safe_key(key)}.json"
        existing = self.lifecycle(key)
        if (
            existing["identity"] == dict(new_identity)
            and existing["digest"] == new_digest
            and existing["bytes"] == bytes_used
            and existing["pinned"] is False
        ):
            return
        if (
            existing["identity"] != dict(old_identity)
            or existing["digest"] != old_digest
            or existing["pinned"] is not False
        ):
            raise CacheError("cache_not_eligible")
        _write_json(
            path,
            {
                "schema": SCHEMA_VERSION,
                "key": key,
                "identity": dict(new_identity),
                "digest": new_digest,
                "bytes": bytes_used,
                "first_observed_at": existing["first_observed_at"],
                "last_used_at": existing["last_used_at"],
                "pinned": False,
                "rebuildability": "unknown",
                "policy_revision": int(existing["policy_revision"]) + 1,
                "pin_audit": None,
                "rebuild_proof": None,
                "source_unavailable": None,
            },
            strict_sync=True,
        )

    def begin_use(
        self,
        key: str,
        *,
        worker_id: int,
        execution_id: int,
        attempt_id: int,
        fencing_token: int,
    ) -> CacheUse:
        return CacheUse(
            self,
            key,
            worker_id=worker_id,
            execution_id=execution_id,
            attempt_id=attempt_id,
            fencing_token=fencing_token,
        )

    def scan_journal_protections(
        self,
        *,
        attempt_journal_root: Path,
        cleanup_journal_root: Path,
        sandbox_recovery_root: Path | None = None,
        resolve: Callable[[int, int | None], str | None],
        max_records: int = 1024,
        excluded_attempt: tuple[int, int, int] | None = None,
    ) -> JournalProtection:
        """Map old exact journals conservatively without changing their schemas."""
        protected: set[str] = set()
        reasons: list[str] = []
        candidates: list[tuple[Path, str, Path, os.stat_result, os.stat_result]] = []
        inspected = 0
        sandbox_root = sandbox_recovery_root or cleanup_journal_root / "sandbox-recovery"
        roots = (
            (Path(attempt_journal_root), "attempt"),
            (Path(cleanup_journal_root), "cleanup"),
            (Path(sandbox_root), "sandbox"),
        )
        for root, kind in roots:
            try:
                root_info = root.lstat()
                if not stat.S_ISDIR(root_info.st_mode):
                    reasons.append(f"{kind}_journal_unreadable")
                    continue
                for path in root.iterdir():
                    inspected += 1
                    if inspected > max_records:
                        return JournalProtection(frozenset(), True, ("journal_scan_truncated",))
                    try:
                        info = path.lstat()
                    except OSError:
                        reasons.append(f"{kind}_journal_unknown")
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if kind == "cleanup" and path == sandbox_root:
                            continue
                        reasons.append(f"{kind}_journal_unknown")
                        continue
                    candidates.append((path, kind, root, root_info, info))
            except FileNotFoundError:
                continue
            except OSError:
                reasons.append(f"{kind}_journal_unreadable")
        for path, kind, root, root_info, observed_info in candidates:
            try:
                if path.name == _INSTANCE_LOCK_NAME:
                    if kind in {"attempt", "cleanup"} and _is_journal_instance_lock(
                        root,
                        observed_root=root_info,
                        observed_file=observed_info,
                    ):
                        continue
                    reasons.append(f"{kind}_journal_unknown")
                    continue
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise ValueError
                value = _read_bounded_json(path, max_bytes=_JOURNAL_MAX_BYTES)
                if value is None:
                    raise ValueError
                execution_id, attempt_id = _journal_identity(path, kind, value)
                if (
                    excluded_attempt is not None
                    and execution_id == excluded_attempt[0]
                    and attempt_id == excluded_attempt[1]
                    and (kind != "attempt" or value.get("fencing_token") == excluded_attempt[2])
                ):
                    continue
                try:
                    key = resolve(execution_id, attempt_id)
                except Exception:  # noqa: BLE001 - an unavailable resolver is unknown, never safe
                    reasons.append(f"{kind}_journal_unmapped")
                    continue
                if key is None:
                    reasons.append(f"{kind}_journal_unmapped")
                else:
                    self.entry_lock(key)  # validates the key without mutating journal data
                    protected.add(key)
            except (CacheError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
                reasons.append(f"{kind}_journal_unknown")
        return JournalProtection(frozenset(protected), bool(reasons), tuple(sorted(set(reasons))))

    def iter_use_records(self, *, max_records: int = 1024) -> Iterator[dict[str, Any]]:
        try:
            iterator = self.use_root.iterdir()
        except OSError as error:
            raise CacheError("cache_use_unavailable") from error
        inspected = 0
        for path in iterator:
            inspected += 1
            if inspected > max_records:
                raise CacheError("cache_use_scan_truncated")
            raw_value = _read_json(path, _USE_FIELDS)
            value = _validated_use(path, raw_value) if raw_value is not None else None
            if value is None:
                continue
            yield value

    def use_records_for_key(self, key: str) -> list[dict[str, Any]]:
        """Fail closed on malformed/truncated use state via ``CacheError``."""
        self.entry_lock(key)  # validate key
        return [record for record in self.iter_use_records() if record["key"] == key]


class CacheUse(AbstractContextManager["CacheUse"]):
    """A persistent use record held with the key lock until cleanup is proven."""

    def __init__(
        self,
        store: CacheLifecycleStore,
        key: str,
        *,
        worker_id: int,
        execution_id: int,
        attempt_id: int,
        fencing_token: int,
    ) -> None:
        values = (worker_id, execution_id, attempt_id, fencing_token)
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in values
        ):
            raise CacheError("cache_use_invalid")
        self.store = store
        self.key = key
        self.worker_id = worker_id
        self.execution_id = execution_id
        self.attempt_id = attempt_id
        self.fencing_token = fencing_token
        self._lock = store.entry_lock(key)
        self._path = store.use_root / f"attempt-{attempt_id}.json"
        self._entered = False

    def __enter__(self) -> CacheUse:
        self._lock.__enter__()
        try:
            raw_existing = _read_json(self._path, _USE_FIELDS)
            existing = (
                _validated_use(self._path, raw_existing) if raw_existing is not None else None
            )
            record = {
                "schema": SCHEMA_VERSION,
                "key": self.key,
                "worker_id": self.worker_id,
                "execution_id": self.execution_id,
                "attempt_id": self.attempt_id,
                "fencing_token": self.fencing_token,
                "started_at": time.time(),
            }
            if existing is not None:
                comparable = {key: value for key, value in record.items() if key != "started_at"}
                old_comparable = {
                    key: value for key, value in existing.items() if key != "started_at"
                }
                if old_comparable != comparable:
                    raise CacheError("cache_use_conflict")
            else:
                _write_json(self._path, record)
            counts = getattr(_active_uses, "counts", None)
            if counts is None:
                counts = {}
                _active_uses.counts = counts
            token = _use_token(self.store, record)
            counts[token] = counts.get(token, 0) + 1
            self._entered = True
            return self
        except BaseException:
            self._lock.__exit__(None, None, None)
            raise

    def release(self, *, cleanup_completed: bool) -> None:
        if not self._entered:
            return
        try:
            if cleanup_completed:
                self.store.touch_used(self.key)
                try:
                    self._path.unlink()
                    _sync_directory(self._path.parent)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise CacheError("cache_use_write_failed") from error
        finally:
            counts = getattr(_active_uses, "counts", {})
            token = _use_token(
                self.store,
                {
                    "key": self.key,
                    "worker_id": self.worker_id,
                    "execution_id": self.execution_id,
                    "attempt_id": self.attempt_id,
                    "fencing_token": self.fencing_token,
                },
            )
            depth = counts.get(token, 0)
            if depth <= 1:
                counts.pop(token, None)
            else:
                counts[token] = depth - 1
            self._entered = False
            self._lock.__exit__(None, None, None)

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.release(cleanup_completed=False)
