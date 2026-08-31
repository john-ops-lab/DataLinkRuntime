"""Small, dependency-free filesystem port for Managed Input uploads.

The store owns only the private mapping from an opaque storage key to a local
file. Adapter ownership, database state, and capacity accounting remain in the
service layer. User supplied file names never participate in path creation.
"""

from __future__ import annotations

import errno
import os
import re
import secrets
import stat as stat_module
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from dlr.common.config import settings

STORAGE_KEY_BYTES = 32
STORAGE_KEY_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
PREFIX_PATTERN = re.compile(r"[0-9a-f]{2}\Z")
ORPHAN_GRACE_SECONDS = 300


class ArtifactStoreError(Exception):
    """Base class for controlled local store failures."""


class ArtifactStoreSecurityError(ArtifactStoreError, ValueError):
    """Raised when a key or path would escape the controlled store."""


class ArtifactStoreObjectExistsError(ArtifactStoreError, FileExistsError):
    """Raised when a publish or quarantine destination already exists."""


class ArtifactStoreAtomicityError(ArtifactStoreError, OSError):
    """Raised when the local store cannot provide an atomic same-device move."""


@dataclass(frozen=True)
class ArtifactObjectStat:
    """Internal object facts used by upload and bounded orphan auditing."""

    storage_key: str
    size_bytes: int
    modified_at: datetime


@dataclass(frozen=True)
class ArtifactAuditResult:
    """Counts from one bounded audit; object names are never returned."""

    inspected_objects: int = 0
    inspected_parts: int = 0
    quarantined_objects: int = 0
    quarantined_parts: int = 0


def _utc_from_timestamp(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)


class LocalFileArtifactStore:
    """A private, single-Control local ArtifactStore implementation.

    ``parts`` and ``objects`` are children of one configured root and their
    device numbers are checked at startup. Publishing therefore uses one
    same-filesystem rename rather than copy-and-delete semantics.
    """

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        raw_root = os.fsdecode(
            os.fspath(root if root is not None else settings.artifact_store_root)
        )
        if "\x00" in raw_root:
            raise ArtifactStoreSecurityError("ArtifactStore root is invalid")
        configured = Path(raw_root)
        if not configured.is_absolute():
            raise ArtifactStoreSecurityError("ArtifactStore root must be absolute")

        self.root = configured
        self.objects_root = self.root / "objects"
        self.parts_root = self.root / "parts"
        self.quarantine_root = self.root / "quarantine"
        self._ensure_directory(self.root)
        self._ensure_directory(self.objects_root)
        self._ensure_directory(self.parts_root)
        self._ensure_directory(self.quarantine_root)
        self._ensure_same_filesystem()

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        """Create one private directory and reject symlink substitution."""
        try:
            info = path.lstat()
        except FileNotFoundError:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                info = path.lstat()
            else:
                return
        if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISDIR(info.st_mode):
            raise ArtifactStoreSecurityError("ArtifactStore path is not a directory")
        # Never broaden permissions. Tightening a store-owned directory is safe
        # and prevents an accidentally broad local mode from persisting.
        if info.st_mode & 0o077:
            path.chmod(0o700)

    def _ensure_same_filesystem(self) -> None:
        try:
            devices = {
                os.stat(path, follow_symlinks=False).st_dev
                for path in (self.objects_root, self.parts_root, self.quarantine_root)
            }
        except OSError as exc:
            raise ArtifactStoreAtomicityError("ArtifactStore filesystem is unavailable") from exc
        if len(devices) != 1:
            raise ArtifactStoreAtomicityError(
                "ArtifactStore namespaces are on different filesystems"
            )

    @staticmethod
    def validate_storage_key(storage_key: str) -> str:
        """Validate the only caller-controlled value that reaches a path."""
        if not isinstance(storage_key, str) or STORAGE_KEY_PATTERN.fullmatch(storage_key) is None:
            raise ArtifactStoreSecurityError("Invalid ArtifactStore storage key")
        return storage_key

    def _prefix_directory(self, namespace: Path, storage_key: str, *, create: bool) -> Path:
        key = self.validate_storage_key(storage_key)
        # Re-check on every operation so a namespace replaced after startup is
        # never silently followed.
        self._ensure_directory(namespace)
        prefix = namespace / key[:2]
        try:
            info = prefix.lstat()
        except FileNotFoundError:
            if not create:
                return prefix
            self._ensure_directory(prefix)
            return prefix
        if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISDIR(info.st_mode):
            raise ArtifactStoreSecurityError("ArtifactStore prefix is not a directory")
        return prefix

    def _path(self, namespace: Path, storage_key: str, *, create: bool) -> Path:
        key = self.validate_storage_key(storage_key)
        prefix = self._prefix_directory(namespace, key, create=create)
        return prefix / key

    def object_path(self, storage_key: str) -> Path:
        """Return an internal object path after validating the opaque key."""
        path = self._path(self.objects_root, storage_key, create=True)
        self._regular_info(path)
        return path

    def part_path(self, storage_key: str) -> Path:
        """Return an internal upload-part path after validating the key."""
        return self._part_path(storage_key, create=True)

    def _part_path(self, storage_key: str, *, create: bool) -> Path:
        key = self.validate_storage_key(storage_key)
        prefix = self._prefix_directory(self.parts_root, key, create=create)
        path = prefix / f"{key}.part"
        if create:
            self._validate_existing_path(path, "ArtifactStore part")
        return path

    def quarantine_path(self, storage_key: str) -> Path:
        """Return the private quarantine destination for one object."""
        return self._path(self.quarantine_root, storage_key, create=True)

    @staticmethod
    def _validate_existing_path(path: Path, label: str) -> os.stat_result | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISREG(info.st_mode):
            raise ArtifactStoreSecurityError(f"{label} is not a regular file")
        return info

    @classmethod
    def _regular_info(cls, path: Path) -> os.stat_result | None:
        return cls._validate_existing_path(path, "ArtifactStore object")

    def _open_exclusive(self, path: Path) -> BinaryIO:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            raise ArtifactStoreObjectExistsError("ArtifactStore part already exists") from exc
        return os.fdopen(descriptor, "wb")

    @contextmanager
    def put_part(self, storage_key: str) -> Iterator[BinaryIO]:
        """Open a new private ``.part`` file without following symlinks."""
        handle = self._open_exclusive(self.part_path(storage_key))
        try:
            yield handle
        finally:
            handle.close()

    def new_storage_key(self) -> str:
        """Return a cryptographically random, path-free object key."""
        for _ in range(100):
            key = secrets.token_hex(STORAGE_KEY_BYTES)
            part_prefix = self._prefix_directory(self.parts_root, key, create=False)
            part_candidate = part_prefix / f"{key}.part"
            if (
                self._regular_info(self._path(self.objects_root, key, create=False)) is None
                and self._validate_existing_path(part_candidate, "ArtifactStore part") is None
                and self._regular_info(self._path(self.quarantine_root, key, create=False)) is None
            ):
                return key
        raise ArtifactStoreError("Could not allocate an ArtifactStore key")

    @staticmethod
    def _sync_directory(path: Path) -> None:
        """Best-effort directory durability after a namespace change."""
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

    @staticmethod
    def _remove_empty_prefix(path: Path) -> None:
        """Drop an empty random-key prefix left by a failed upload."""
        try:
            path.rmdir()
        except OSError:
            return

    def commit(self, storage_key: str) -> None:
        """Publish a part with one atomic same-filesystem rename."""
        self._ensure_same_filesystem()
        part = self.part_path(storage_key)
        object_path = self.object_path(storage_key)
        if self._regular_info(part) is None:
            raise FileNotFoundError("ArtifactStore part is missing")
        if self._regular_info(object_path) is not None:
            raise ArtifactStoreObjectExistsError("ArtifactStore object already exists")
        descriptor = os.open(part, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.rename(part, object_path)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise ArtifactStoreAtomicityError(
                    "ArtifactStore part and object are on different filesystems"
                ) from exc
            raise
        self._sync_directory(object_path.parent)

    def open(self, storage_key: str) -> BinaryIO:
        """Open a published object read-only without following symlinks."""
        path = self._path(self.objects_root, storage_key, create=False)
        if self._regular_info(path) is None:
            raise FileNotFoundError("ArtifactStore object is missing")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        return os.fdopen(descriptor, "rb")

    def stat(self, storage_key: str) -> ArtifactObjectStat | None:
        """Return safe size/time facts, or ``None`` for a missing object."""
        path = self._path(self.objects_root, storage_key, create=False)
        info = self._regular_info(path)
        if info is None:
            return None
        return ArtifactObjectStat(
            storage_key=self.validate_storage_key(storage_key),
            size_bytes=info.st_size,
            modified_at=_utc_from_timestamp(info.st_mtime),
        )

    def stat_part(self, storage_key: str) -> ArtifactObjectStat | None:
        """Return one partial-upload stat without creating a prefix directory."""
        path = self._part_path(storage_key, create=False)
        info = self._validate_existing_path(path, "ArtifactStore part")
        if info is None:
            return None
        return ArtifactObjectStat(
            storage_key=self.validate_storage_key(storage_key),
            size_bytes=info.st_size,
            modified_at=_utc_from_timestamp(info.st_mtime),
        )

    def delete(self, storage_key: str) -> bool:
        """Delete an object idempotently; a missing object counts as success."""
        path = self._path(self.objects_root, storage_key, create=False)
        if self._regular_info(path) is None:
            self._remove_empty_prefix(path.parent)
            return True
        try:
            path.unlink()
        except FileNotFoundError:
            self._remove_empty_prefix(path.parent)
            return True
        self._sync_directory(path.parent)
        self._remove_empty_prefix(path.parent)
        return True

    def delete_part(self, storage_key: str) -> bool:
        """Delete a partial upload idempotently."""
        path = self.part_path(storage_key)
        if self._validate_existing_path(path, "ArtifactStore part") is None:
            self._remove_empty_prefix(path.parent)
            return True
        try:
            path.unlink()
        except FileNotFoundError:
            self._remove_empty_prefix(path.parent)
            return True
        self._sync_directory(path.parent)
        self._remove_empty_prefix(path.parent)
        return True

    def _quarantine(self, source: Path, destination: Path) -> bool:
        if self._regular_info(source) is None:
            return True
        if self._regular_info(destination) is not None:
            raise ArtifactStoreObjectExistsError("ArtifactStore quarantine object exists")
        try:
            os.rename(source, destination)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise ArtifactStoreAtomicityError("ArtifactStore quarantine is not atomic") from exc
            raise
        self._sync_directory(source.parent)
        self._sync_directory(destination.parent)
        return True

    def quarantine(self, storage_key: str) -> bool:
        """Move one unreferenced object into quarantine, idempotently."""
        self._ensure_same_filesystem()
        source = self._path(self.objects_root, storage_key, create=False)
        destination = self.quarantine_path(storage_key)
        return self._quarantine(source, destination)

    def quarantine_part(self, storage_key: str) -> bool:
        """Move one stale part into quarantine, idempotently."""
        self._ensure_same_filesystem()
        key = self.validate_storage_key(storage_key)
        source = self.part_path(key)
        destination = self.quarantine_path(key).with_name(f"{key}.part")
        return self._quarantine(source, destination)

    def _iter_files(self, namespace: Path, *, suffix: str = "") -> Iterator[ArtifactObjectStat]:
        """Yield only legal random-key files without traversing unknown paths."""
        self._ensure_directory(namespace)
        try:
            entries = list(namespace.iterdir())
        except FileNotFoundError:
            return
        for prefix in entries:
            prefix_info = prefix.lstat()
            if stat_module.S_ISLNK(prefix_info.st_mode):
                raise ArtifactStoreSecurityError("ArtifactStore prefix cannot be a symlink")
            if (
                not stat_module.S_ISDIR(prefix_info.st_mode)
                or PREFIX_PATTERN.fullmatch(prefix.name) is None
            ):
                continue
            for candidate in prefix.iterdir():
                candidate_info = candidate.lstat()
                if stat_module.S_ISLNK(candidate_info.st_mode):
                    raise ArtifactStoreSecurityError("ArtifactStore object cannot be a symlink")
                expected = rf"[0-9a-f]{{64}}{re.escape(suffix)}"
                if (
                    not stat_module.S_ISREG(candidate_info.st_mode)
                    or re.fullmatch(expected, candidate.name) is None
                ):
                    continue
                key = candidate.name.removesuffix(suffix) if suffix else candidate.name
                yield ArtifactObjectStat(
                    storage_key=key,
                    size_bytes=candidate_info.st_size,
                    modified_at=_utc_from_timestamp(candidate_info.st_mtime),
                )

    def iter_objects(self) -> Iterator[ArtifactObjectStat]:
        """Yield legal object metadata without traversing unknown directories."""
        yield from self._iter_files(self.objects_root)

    def iter_parts(self) -> Iterator[ArtifactObjectStat]:
        """Yield legal partial-upload metadata without following symlinks."""
        yield from self._iter_files(self.parts_root, suffix=".part")

    def audit_orphans(
        self,
        known_storage_keys: set[str],
        *,
        older_than: datetime | None = None,
    ) -> ArtifactAuditResult:
        """Quarantine only old legal random files absent from DB metadata."""
        cutoff = older_than or datetime.now(UTC) - timedelta(seconds=ORPHAN_GRACE_SECONDS)
        cutoff = cutoff.replace(tzinfo=UTC) if cutoff.tzinfo is None else cutoff.astimezone(UTC)
        inspected_objects = inspected_parts = quarantined_objects = quarantined_parts = 0
        for item in list(self.iter_objects()):
            inspected_objects += 1
            if (
                item.storage_key not in known_storage_keys
                and item.modified_at < cutoff
                and self.quarantine(item.storage_key)
            ):
                quarantined_objects += 1
        for item in list(self.iter_parts()):
            inspected_parts += 1
            if (
                item.storage_key not in known_storage_keys
                and item.modified_at < cutoff
                and self.quarantine_part(item.storage_key)
            ):
                quarantined_parts += 1
        return ArtifactAuditResult(
            inspected_objects=inspected_objects,
            inspected_parts=inspected_parts,
            quarantined_objects=quarantined_objects,
            quarantined_parts=quarantined_parts,
        )


__all__ = [
    "ArtifactAuditResult",
    "ArtifactObjectStat",
    "ArtifactStoreAtomicityError",
    "ArtifactStoreError",
    "ArtifactStoreObjectExistsError",
    "ArtifactStoreSecurityError",
    "LocalFileArtifactStore",
    "ORPHAN_GRACE_SECONDS",
    "PREFIX_PATTERN",
    "STORAGE_KEY_BYTES",
    "STORAGE_KEY_PATTERN",
]
