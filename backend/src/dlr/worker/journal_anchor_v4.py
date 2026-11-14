"""Pre-provisioned local journal anchor for the isolated v4 Worker.

The deployment is responsible for placing this existing directory on the
intended persistent filesystem. No identity, fsync or mount observation can
prove the storage provider's crash durability or exclude another host.
"""

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from dlr.worker import cgroup_namespace


class AnchorError(Exception):
    pass


@dataclass(frozen=True)
class AnchorHandle:
    owner_pid: int
    path: Path
    fd: int
    identity: tuple[int, int]
    mount_id: int | None
    runtime_root: Path
    journal_roots: tuple[Path, ...]
    journal_identities: dict[Path, tuple[int, int]]
    journal_fds: dict[Path, int]

    def bind(self, root_lock: cgroup_namespace.RootLockHandle) -> None:
        """Bind this still-open anchor to the same process's held root locks."""
        if type(root_lock) is not cgroup_namespace.RootLockHandle:
            raise AnchorError("journal_anchor_invalid")
        if root_lock._v4_anchor is not None and root_lock._v4_anchor is not self:
            raise AnchorError("journal_anchor_invalid")
        with root_lock.borrow(self.runtime_root, [self.path, *self.journal_roots]):
            self.verify()
            expected = {self.path, *self.journal_roots}
            identities = {self.path: self.identity, **self.journal_identities}
            found = {entry.path for entry in root_lock._entries if entry.path in expected}
            if found != expected or any(
                entry.mount_identity != self.mount_id
                or entry.directory_identity != identities[entry.path]
                for entry in root_lock._entries
                if entry.path in expected
            ):
                raise AnchorError("journal_anchor_changed")
            root_lock._v4_anchor = self

    def only_journal_roots(self, roots: tuple[Path, ...]) -> bool:
        """Do not overlook old records outside separately configured roots."""
        normalized = tuple(Path(os.path.abspath(path)) for path in roots)
        if self.path in normalized:
            # The startup gate scans that whole root separately.
            return True
        allowed = {path.name for path in normalized} | {".dlr-instance.lock"}
        with os.scandir(self.fd) as entries:
            for count, entry in enumerate(entries, start=1):
                if count > 1024 or entry.name not in allowed:
                    return False
        return True

    def verify(self) -> None:
        current = os.fstat(self.fd)
        named = self.path.lstat()
        if (
            self.owner_pid != os.getpid()
            or not stat.S_ISDIR(current.st_mode)
            or current.st_uid != os.geteuid()
            or stat.S_IMODE(current.st_mode) != 0o700
            or (current.st_dev, current.st_ino) != self.identity
            or (named.st_dev, named.st_ino) != self.identity
            or self.path.resolve(strict=True) != self.path
        ):
            raise AnchorError("journal_anchor_changed")
        visible = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if cgroup_namespace._mount_id(visible) != self.mount_id:
                raise AnchorError("journal_anchor_changed")
        finally:
            os.close(visible)
        for path, identity in self.journal_identities.items():
            info = path.lstat()
            held = os.fstat(self.journal_fds[path])
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700
                or (info.st_dev, info.st_ino) != identity
                or (held.st_dev, held.st_ino) != identity
                or cgroup_namespace._mount_id(self.journal_fds[path]) != self.mount_id
            ):
                raise AnchorError("journal_root_changed")
            visible = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                if cgroup_namespace._mount_id(visible) != self.mount_id:
                    raise AnchorError("journal_root_changed")
            finally:
                os.close(visible)


@contextmanager
def prepare_roots(
    anchor: Path, *, runtime_root: Path, journal_roots: tuple[Path, ...]
) -> Iterator[AnchorHandle]:
    """Open an existing private anchor and durably create only its descendants."""
    if not 1 <= len(journal_roots) <= 2 or not all(
        Path(path).is_absolute() and Path(path) == Path(os.path.abspath(path))
        for path in (anchor, runtime_root, *journal_roots)
    ):
        raise AnchorError("journal_anchor_invalid")
    root = Path(os.path.abspath(anchor))
    runtime = Path(os.path.abspath(runtime_root))
    journals = tuple(Path(os.path.abspath(path)) for path in journal_roots)
    if (
        len(set(journals)) != len(journals)
        or runtime == root
        or runtime in root.parents
        or root in runtime.parents
        or any(path != root and root not in path.parents for path in journals)
        or any(len(path.relative_to(root).parts) > 1 for path in journals)
    ):
        raise AnchorError("journal_anchor_invalid")
    fd = -1
    handle: AnchorHandle | None = None
    try:
        # Anchor is explicitly pre-provisioned, never created by this code.
        if root.resolve(strict=True) != root:
            raise AnchorError("journal_anchor_invalid")
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(fd)
        handle = AnchorHandle(
            os.getpid(),
            root,
            fd,
            (info.st_dev, info.st_ino),
            cgroup_namespace._mount_id(fd),
            runtime,
            journals,
            {},
            {},
        )
        handle.verify()
        os.fsync(fd)
        for path in journals:
            current = os.dup(fd)
            try:
                for name in path.relative_to(root).parts:
                    handle.verify()
                    with suppress(FileExistsError):
                        os.mkdir(name, mode=0o700, dir_fd=current)
                    child = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=current,
                    )
                    try:
                        actual = os.fstat(child)
                        named = os.stat(name, dir_fd=current, follow_symlinks=False)
                        if (
                            not stat.S_ISDIR(actual.st_mode)
                            or actual.st_uid != os.geteuid()
                            or stat.S_IMODE(actual.st_mode) != 0o700
                            or (actual.st_dev, actual.st_ino) != (named.st_dev, named.st_ino)
                            or cgroup_namespace._mount_id(child) != handle.mount_id
                        ):
                            raise AnchorError("journal_root_invalid")
                        os.fsync(child)
                        os.fsync(current)
                        handle.verify()
                    except BaseException:
                        os.close(child)
                        raise
                    os.close(current)
                    current = child
                final = os.fstat(current)
                handle.journal_identities[path] = (final.st_dev, final.st_ino)
                handle.journal_fds[path] = os.dup(current)
                handle.verify()
            finally:
                os.close(current)
        handle.verify()
        yield handle
        handle.verify()
    except AnchorError:
        raise
    except (OSError, ValueError) as error:
        raise AnchorError("journal_anchor_unavailable") from error
    finally:
        if handle is not None:
            for journal_fd in handle.journal_fds.values():
                with suppress(OSError):
                    os.close(journal_fd)
        if fd >= 0:
            os.close(fd)
