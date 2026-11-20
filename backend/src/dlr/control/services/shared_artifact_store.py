"""The Managed Files layout on a verified shared POSIX mount."""

from __future__ import annotations

import fcntl
import hashlib
import io
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from typing import BinaryIO, Literal

from dlr.common import config
from dlr.control.services.artifact_store import (
    STORAGE_KEY_BYTES,
    ArtifactObjectStat,
    ArtifactStoreBusyError,
    ArtifactStoreObjectExistsError,
    LocalFileArtifactStore,
    _ObjectReader,
    _utc_from_timestamp,
)
from dlr.control.services.shared_posix import (
    SharedNamespace,
    SharedStorageUnavailable,
    open_directory,
    open_regular,
)


class SharedFileArtifactStore:
    """Use the existing keys and lock stripes, anchored at verified descriptors."""

    def __init__(self, namespace: SharedNamespace, slot: Literal["a", "b"]) -> None:
        self.root = namespace.root
        self.namespace = namespace
        self.slot = slot

    validate_storage_key = staticmethod(LocalFileArtifactStore.validate_storage_key)

    @contextmanager
    def _entry(self, root: int, area: str, key: str) -> Iterator[tuple[int, str]]:
        key = self.validate_storage_key(key)
        with (
            open_directory(root, area, create=True) as directory,
            open_directory(directory, key[:2], create=True) as prefix,
        ):
            yield prefix, f"{key}.part" if area in {"parts", "orphan-parts"} else key

    def _stripe(self, root: int, key: str, *, shared: bool) -> int:
        key = self.validate_storage_key(key)
        name = hashlib.sha256(key.encode("ascii")).hexdigest()[:2]
        with open_directory(root, ".read-locks", create=True) as directory:
            descriptor = open_regular(directory, name, os.O_RDWR | os.O_CREAT)
            try:
                try:
                    fcntl.flock(
                        descriptor, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
                    )
                except BlockingIOError:
                    raise ArtifactStoreBusyError("ArtifactStore object is busy") from None
                actual = os.fstat(descriptor)
                visible = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (actual.st_dev, actual.st_ino) != (visible.st_dev, visible.st_ino):
                    raise SharedStorageUnavailable("Shared lock was replaced")
            except BaseException:
                os.close(descriptor)
                raise
        return descriptor

    @contextmanager
    def custody(self, storage_key: str) -> Iterator[int]:
        with self.namespace.open(slot=self.slot) as root:
            descriptor = self._stripe(root, storage_key, shared=False)
            try:
                yield root
            finally:
                os.close(descriptor)

    def new_storage_key(self) -> str:
        # The writer uses O_EXCL and publication refuses an existing object.
        return secrets.token_hex(STORAGE_KEY_BYTES)

    def free_bytes(self) -> int:
        with self.namespace.open(slot=self.slot) as root:
            capacity = os.fstatvfs(root)
            return capacity.f_bavail * capacity.f_frsize

    @contextmanager
    def put_part(self, storage_key: str) -> Iterator[BinaryIO]:
        with self.namespace.open(slot=self.slot) as root:
            lock = self._stripe(root, storage_key, shared=False)
            release = True
            try:
                with self._entry(root, "parts", storage_key) as (directory, name):
                    try:
                        handle = os.fdopen(
                            open_regular(directory, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                            "wb",
                        )
                    except FileExistsError:
                        raise ArtifactStoreObjectExistsError(
                            "ArtifactStore part already exists"
                        ) from None
                    release = False
                    try:
                        yield handle
                    finally:
                        handle.close()
                        release = True
            finally:
                if release:
                    os.close(lock)

    def commit(self, storage_key: str) -> None:
        with (
            self.custody(storage_key) as root,
            self._entry(root, "parts", storage_key) as (source, temporary),
            self._entry(root, "objects", storage_key) as (destination, name),
        ):
            try:
                os.stat(name, dir_fd=destination, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ArtifactStoreObjectExistsError("ArtifactStore object already exists")
            with os.fdopen(open_regular(source, temporary, os.O_RDONLY), "rb") as handle:
                os.fsync(handle.fileno())
            os.rename(temporary, name, src_dir_fd=source, dst_dir_fd=destination)
            os.fsync(destination)
            os.fsync(source)

    def open(self, storage_key: str) -> BinaryIO:
        with self.namespace.open(slot=self.slot) as root:
            lock = self._stripe(root, storage_key, shared=True)
            try:
                with self._entry(root, "objects", storage_key) as (directory, name):
                    stream = io.FileIO(
                        open_regular(directory, name, os.O_RDONLY), "rb", closefd=True
                    )
            except BaseException:
                os.close(lock)
                raise
        return _ObjectReader(stream, lock)

    def _stat_in(self, area: str, storage_key: str) -> ArtifactObjectStat | None:
        with (
            self.namespace.open(slot=self.slot) as root,
            self._entry(root, area, storage_key) as (directory, name),
        ):
            try:
                descriptor = open_regular(directory, name, os.O_RDONLY)
            except FileNotFoundError:
                return None
            try:
                info = os.fstat(descriptor)
            finally:
                os.close(descriptor)
        return ArtifactObjectStat(storage_key, info.st_size, _utc_from_timestamp(info.st_mtime))

    def stat(self, storage_key: str) -> ArtifactObjectStat | None:
        return self._stat_in("objects", storage_key)

    def stat_part(self, storage_key: str) -> ArtifactObjectStat | None:
        return self._stat_in("parts", storage_key)

    def discard(self, root: int, storage_key: str, *, part: bool = False) -> None:
        """Delete a single name while the caller holds its exclusive custody."""
        with self._entry(root, "parts" if part else "objects", storage_key) as (directory, name):
            try:
                descriptor = open_regular(directory, name, os.O_RDONLY)
            except FileNotFoundError:
                pass
            else:
                os.close(descriptor)
                os.unlink(name, dir_fd=directory)
            # An absent path after a failed previous attempt still needs fsync.
            os.fsync(directory)

    def delete(self, storage_key: str) -> bool:
        with self.custody(storage_key) as root:
            self.discard(root, storage_key)
        return True

    def delete_part(self, storage_key: str) -> bool:
        with self.custody(storage_key) as root:
            self.discard(root, storage_key, part=True)
        return True

    def quarantine_orphan(self, root: int, storage_key: str, *, part: bool) -> bool:
        with (
            self._entry(root, "parts" if part else "objects", storage_key) as (source, name),
            self._entry(root, "orphan-parts" if part else "quarantine", storage_key) as (
                destination,
                target,
            ),
        ):
            try:
                descriptor = open_regular(source, name, os.O_RDONLY)
            except FileNotFoundError:
                return False
            os.close(descriptor)
            try:
                os.stat(target, dir_fd=destination, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ArtifactStoreObjectExistsError("ArtifactStore quarantine already exists")
            os.rename(name, target, src_dir_fd=source, dst_dir_fd=destination)
            os.fsync(destination)
            os.fsync(source)
        return True

    def scan(self, *, part: bool, shard: int) -> Iterator[ArtifactObjectStat]:
        """Yield safely named files from one shard without a directory-wide snapshot."""
        prefix = f"{shard:02x}"
        with (
            self.namespace.open(slot=self.slot) as root,
            open_directory(root, "parts" if part else "objects", create=True) as area,
            open_directory(area, prefix, create=True) as directory,
            os.scandir(directory) as entries,
        ):
            for entry in entries:
                if part and not entry.name.endswith(".part"):
                    continue
                key = entry.name.removesuffix(".part") if part else entry.name
                if not key.startswith(prefix) or len(key) != 64:
                    continue
                self.validate_storage_key(key)
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SharedStorageUnavailable("Invalid shared object")
                yield ArtifactObjectStat(key, info.st_size, _utc_from_timestamp(info.st_mtime))


type ArtifactStore = LocalFileArtifactStore | SharedFileArtifactStore


def configured_store() -> ArtifactStore:
    slot = config.settings.control_ha_slot
    if slot is None:
        return LocalFileArtifactStore(config.settings.artifact_store_root)
    from dlr.control.services.storage_backends import active_namespace

    return SharedFileArtifactStore(active_namespace("managed_files", config.settings), slot)
