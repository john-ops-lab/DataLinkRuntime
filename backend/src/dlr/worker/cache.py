"""Bounded, verified version-cache primitives for the v3 Worker.

The cache is a Worker-owned platform resource.  Adapters receive a runtime
path only through the private sandbox and never receive the cache reservation
or staging handles.  Reservations are recorded under an inter-process lock so
two Workers cannot both spend the same byte budget before an atomic promote.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CACHE_MAX_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_CACHE_LOW_WATERMARK_BYTES = 128 * 1024 * 1024
_MANIFEST_NAME = ".dlr-cache-manifest.json"
_READY_NAME = ".ready"
_RESERVATIONS_NAME = ".dlr-cache-reservations.json"
_LOCK_NAME = ".dlr-cache-reservations.lock"
_STATE_FIELDS = frozenset({"reservations"})
_MANIFEST_FIELDS = frozenset({"bytes", "digest", "files", "identity"})
_SAFE_KEY = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


class CacheError(Exception):
    """Stable cache failure without exposing local paths to callers."""

    def __init__(self, code: str, message: str = "Version cache operation failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class CacheReservation:
    """One idempotent byte reservation held until promote or release."""

    cache: VerifiedVersionCache
    token: str
    amount: int
    _released: bool = False

    def release(self) -> None:
        if not self._released:
            self.cache.release(self)
            self._released = True

    def __enter__(self) -> CacheReservation:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.release()


_thread_lock = threading.RLock()


def _safe_key(value: str) -> str:
    if not value or any(character not in _SAFE_KEY for character in value):
        raise CacheError("cache_key_invalid")
    return value


def _directory(path: Path, *, mode: int = 0o700) -> Path:
    if not path.is_absolute():
        raise CacheError("cache_root_invalid")
    try:
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as error:
        raise CacheError("cache_root_unavailable") from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise CacheError("cache_root_invalid")
    if stat.S_IMODE(info.st_mode) != mode:
        try:
            os.chmod(path, mode, follow_symlinks=False)
        except OSError as error:
            raise CacheError("cache_root_invalid") from error
    return path


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="ascii"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return default
    return value if isinstance(value, dict) else default


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
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
        _fsync_directory(path.parent)
    except OSError as error:
        with suppress(OSError):
            temporary.unlink()
        raise CacheError("cache_state_write_failed") from error


def _write_ready(path: Path) -> None:
    """Create the ready marker without following a pre-existing symlink."""

    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            descriptor = -1
            stream.write("ready\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        raise CacheError("cache_ready_write_failed") from error


def _tree_facts(root: Path) -> tuple[str, int, int]:
    """Hash regular files and links without following an untrusted symlink."""
    digest = hashlib.sha256()
    total = 0
    files = 0
    try:
        entries = sorted(root.rglob("*"), key=lambda path: path.as_posix())
    except OSError as error:
        raise CacheError("cache_verify_failed") from error
    for entry in entries:
        relative = entry.relative_to(root).as_posix().encode("utf-8")
        try:
            info = entry.lstat()
        except OSError as error:
            raise CacheError("cache_verify_failed") from error
        if entry.name in {_MANIFEST_NAME, _READY_NAME}:
            continue
        if stat.S_ISLNK(info.st_mode):
            # Linux reports symlink mode bits as 0777; they are not DAC
            # permissions and must not make a normal venv link (for example
            # bin/python) fail the cache contract. Hash the link text only;
            # never resolve or open it while verifying the cache.
            digest.update(b"L")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(os.readlink(entry).encode("utf-8"))
            files += 1
            continue
        if info.st_mode & 0o002:
            raise CacheError("cache_permissions_invalid")
        digest.update(b"D" if stat.S_ISDIR(info.st_mode) else b"F")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        if not stat.S_ISREG(info.st_mode):
            if not stat.S_ISDIR(info.st_mode):
                raise CacheError("cache_entry_invalid")
            continue
        files += 1
        total += info.st_size
        try:
            with entry.open("rb") as stream:
                while chunk := stream.read(64 * 1024):
                    digest.update(chunk)
        except OSError as error:
            raise CacheError("cache_verify_failed") from error
    return digest.hexdigest(), total, files


def _make_read_only(root: Path, *, public: bool = False) -> None:
    """Remove write bits without following symlinks.

    Promoted runtime entries are read-only Worker views.  Their directories
    and regular files need read/search permission for the non-root Adapter,
    while the cache and entries parents remain non-listable (0711).
    """
    try:
        entries = sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    except OSError as error:
        raise CacheError("cache_permissions_invalid") from error
    for entry in entries + [root]:
        try:
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            if public:
                if stat.S_ISDIR(info.st_mode):
                    mode = 0o555
                elif stat.S_ISREG(info.st_mode):
                    mode = 0o555 if info.st_mode & 0o111 else 0o444
                else:
                    raise CacheError("cache_entry_invalid")
            else:
                mode = stat.S_IMODE(info.st_mode) & ~0o222
            os.chmod(entry, mode, follow_symlinks=False)
        except OSError as error:
            raise CacheError("cache_permissions_invalid") from error


def _make_writable_for_removal(root: Path) -> None:
    """Restore directory write/search bits without following symlinks."""
    try:
        entries = sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    except OSError as error:
        raise CacheError("cache_staging_cleanup_failed") from error
    for entry in entries + [root]:
        try:
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            extra_bits = 0o300 if stat.S_ISDIR(info.st_mode) else 0o200
            os.chmod(entry, info.st_mode | extra_bits, follow_symlinks=False)
        except OSError as error:
            raise CacheError("cache_staging_cleanup_failed") from error


class VerifiedVersionCache:
    """A bounded cache with read-only verified entries and atomic promotion."""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = DEFAULT_CACHE_MAX_BYTES,
        low_watermark_bytes: int = DEFAULT_CACHE_LOW_WATERMARK_BYTES,
    ) -> None:
        if max_bytes <= 0 or low_watermark_bytes < 0 or low_watermark_bytes >= max_bytes:
            raise CacheError("cache_budget_invalid")
        # The parent chain is traversable but deliberately not listable by a
        # payload.  Only an already-known promoted runtime path can be read.
        self.root = _directory(Path(root), mode=0o711)
        self.entries = _directory(self.root / "entries", mode=0o711)
        self.max_bytes = max_bytes
        self.low_watermark_bytes = low_watermark_bytes
        self._state_path = self.root / _RESERVATIONS_NAME
        self._lock_path = self.root / _LOCK_NAME

    def entry_path(self, key: str) -> Path:
        return self.entries / _safe_key(key)

    def staging_path(self, key: str, token: str | None = None) -> Path:
        safe = _safe_key(key)
        suffix = token or uuid.uuid4().hex
        _safe_key(suffix)
        return self.entries / f".{safe}.staging-{suffix}"

    def _direct_entry(self, path: Path, *, staging: bool, allow_missing: bool) -> Path:
        """Return one direct cache child without following its leaf symlink."""
        candidate = Path(path)
        entries = self.entries.resolve(strict=True)
        try:
            if candidate.parent.resolve(strict=True) != entries:
                raise CacheError("cache_path_invalid")
            info = candidate.lstat()
        except FileNotFoundError:
            if not allow_missing:
                raise CacheError("cache_path_invalid") from None
            return candidate
        except OSError as error:
            raise CacheError("cache_path_invalid") from error
        if stat.S_ISLNK(info.st_mode):
            raise CacheError("cache_path_invalid")
        if staging and not candidate.name.startswith("."):
            raise CacheError("cache_path_invalid")
        if not staging and candidate.name.startswith("."):
            raise CacheError("cache_path_invalid")
        return candidate

    def _locked(self) -> Any:
        class Lock:
            def __init__(self, owner: VerifiedVersionCache) -> None:
                self.owner = owner
                self.handle: Any = None

            def __enter__(self) -> Lock:
                self.handle = open(self.owner._lock_path, "a+", encoding="ascii")
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
                return self

            def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                self.handle.close()

        return Lock(self)

    def _state(self) -> dict[str, dict[str, int | float]]:
        value = _read_json(self._state_path, {"reservations": {}})
        reservations = value.get("reservations")
        if not isinstance(reservations, dict):
            return {}
        now = time.time()
        valid: dict[str, dict[str, int | float]] = {}
        for token, item in reservations.items():
            if not isinstance(token, str) or not isinstance(item, dict):
                continue
            amount = item.get("amount")
            expires = item.get("expires")
            if (
                isinstance(amount, int)
                and not isinstance(amount, bool)
                and amount > 0
                and isinstance(expires, (int, float))
                and expires > now
            ):
                valid[token] = {"amount": amount, "expires": expires}
        return valid

    def _committed_bytes(self) -> int:
        total = 0
        for entry in self.entries.iterdir():
            try:
                entry_info = entry.lstat()
                if entry.name.startswith(".") or not stat.S_ISDIR(entry_info.st_mode):
                    continue
                for path in entry.rglob("*"):
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode):
                        total += info.st_size
            except OSError:
                continue
        return total

    def reserve(self, amount: int, *, ttl_seconds: int = 900) -> CacheReservation:
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise CacheError("cache_reservation_invalid")
        if ttl_seconds < 1:
            raise CacheError("cache_reservation_invalid")
        with _thread_lock, self._locked():
            state = self._state()
            reserved = sum(int(item["amount"]) for item in state.values())
            disk_free = shutil.disk_usage(self.root).free
            available = min(
                self.max_bytes - self._committed_bytes() - reserved,
                disk_free - self.low_watermark_bytes,
            )
            if amount > available:
                raise CacheError("cache_low_watermark")
            token = uuid.uuid4().hex
            state[token] = {"amount": amount, "expires": time.time() + ttl_seconds}
            _write_json(self._state_path, {"reservations": state})
        return CacheReservation(self, token, amount)

    def release(self, reservation: CacheReservation) -> None:
        with _thread_lock, self._locked():
            state = self._state()
            state.pop(reservation.token, None)
            _write_json(self._state_path, {"reservations": state})

    def verify(self, path: Path, identity: Mapping[str, Any]) -> bool:
        """Verify a ready entry's exact identity, digest, and byte count."""
        try:
            entry = self._direct_entry(path, staging=False, allow_missing=False)
            if not entry.is_dir():
                return False
            ready = entry / _READY_NAME
            manifest_path = entry / _MANIFEST_NAME
            ready_info = ready.lstat()
            manifest_info = manifest_path.lstat()
            if (
                not stat.S_ISREG(ready_info.st_mode)
                or ready_info.st_mode & 0o222
                or not stat.S_ISREG(manifest_info.st_mode)
                or manifest_info.st_mode & 0o222
            ):
                return False
            manifest = json.loads(manifest_path.read_text(encoding="ascii"))
            if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
                return False
            if manifest["identity"] != dict(identity):
                return False
            digest, total, files = _tree_facts(entry)
            return bool(
                manifest["digest"] == digest
                and manifest["bytes"] == total
                and manifest["files"] == files
            )
        except (CacheError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return False

    def promote(
        self,
        staging: Path,
        target: Path,
        *,
        identity: Mapping[str, Any],
        reservation: CacheReservation,
    ) -> Path:
        """Verify staging, publish one ready directory, then release bytes."""
        try:
            staging_path = self._direct_entry(staging, staging=True, allow_missing=False)
            target_path = self._direct_entry(target, staging=False, allow_missing=True)
            entries = self.entries.resolve(strict=True)
            if not staging_path.is_dir():
                raise CacheError("cache_staging_invalid")
            digest, total, files = _tree_facts(staging_path)
            if total > reservation.amount:
                raise CacheError("cache_reservation_insufficient")
            _write_json(
                staging_path / _MANIFEST_NAME,
                {"bytes": total, "digest": digest, "files": files, "identity": dict(identity)},
            )
            _write_ready(staging_path / _READY_NAME)
            _make_read_only(staging_path, public=True)
            if target_path.exists():
                if self.verify(target_path, identity):
                    self.remove_staging(staging_path)
                    return target_path
                raise CacheError("cache_target_conflict")
            os.replace(staging_path, target_path)
            _fsync_directory(entries)
            return target_path
        except OSError as error:
            raise CacheError("cache_promote_failed") from error
        finally:
            reservation.release()

    def remove_staging(self, staging: Path) -> None:
        """Remove one exact staging child; never accepts a cache root."""
        try:
            candidate = self._direct_entry(staging, staging=True, allow_missing=True)
            if not candidate.exists():
                return
            if not candidate.is_dir():
                raise CacheError("cache_staging_invalid")
            _make_writable_for_removal(candidate)
            shutil.rmtree(candidate)
        except OSError as error:
            raise CacheError("cache_staging_cleanup_failed") from error

    def remove_entry(self, entry: Path) -> None:
        """Remove one exact verified entry before a version rebuild."""
        try:
            candidate = self._direct_entry(entry, staging=False, allow_missing=True)
            if not candidate.exists():
                return
            if not candidate.is_dir():
                raise CacheError("cache_entry_invalid")
            _make_writable_for_removal(candidate)
            shutil.rmtree(candidate)
        except CacheError:
            raise
        except OSError as error:
            raise CacheError("cache_entry_cleanup_failed") from error
