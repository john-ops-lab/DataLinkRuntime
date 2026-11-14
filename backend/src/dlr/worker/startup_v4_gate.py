"""Read-only v4 startup observation; no recovery or readiness authorization.

The caller owns a lifetime RootLockHandle. This conservative gate accepts only
empty recognized directories and the root lock files; it does not prove a host,
physical stop, storage provenance, or absence of Control-side responsibility.
It is intentionally not wired into the legacy Agent.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dlr.worker import cgroup_namespace, sandbox


@dataclass(frozen=True)
class StartupObservation:
    status: Literal["local_empty", "blocked"]
    reason: Literal["no_local_records", "records_or_unknown", "scan_limit", "root_invalid"]


class _Blocked(Exception):
    def __init__(self, reason: Literal["records_or_unknown", "scan_limit", "root_invalid"]):
        self.reason = reason


# These names describe only directories, not permission to trust their contents.
# Any other item remains a reason to pause for reconciliation.
_ALLOWED = {
    (): frozenset({"workspaces", "version-cache", "cleanup-journal", "attempt-journal"}),
    ("version-cache",): frozenset({"entries", ".dlr-cache-lifecycle"}),
    ("version-cache", ".dlr-cache-lifecycle"): frozenset({"items", "uses", "locks", "deletions"}),
}
_MAX_ENTRIES = 1024
_MAX_DEPTH = 8


def _mount_id(fd: int) -> int | None:
    """Identify a live descriptor's mount in this process's namespace only."""
    if sys.platform != "linux":
        # The production gate is Linux; retain portable unit-test behavior.
        return None
    try:
        lines = Path(f"/proc/self/fdinfo/{fd}").read_text(encoding="ascii").splitlines()
        values = [line.partition(":")[2].strip() for line in lines if line.startswith("mnt_id:")]
        if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal():
            raise ValueError
        result = int(values[0])
        if result <= 0:
            raise ValueError
        return result
    except (OSError, UnicodeError, ValueError):
        raise _Blocked("root_invalid") from None


def inspect_local_startup(
    *,
    runtime_root: Path,
    cleanup_journal_root: Path,
    attempt_journal_root: Path,
    root_lock: cgroup_namespace.RootLockHandle,
    max_entries: int = _MAX_ENTRIES,
    max_depth: int = _MAX_DEPTH,
) -> StartupObservation:
    """Report only local emptiness while the caller owns all three root locks.

    The fixed ceilings cannot be raised by a caller. Open directories through
    no-follow descriptors and recheck their identity; no paths are created,
    removed, repaired, followed through symlinks, or included in the result.
    """
    if (
        type(root_lock) is not cgroup_namespace.RootLockHandle
        or type(max_entries) is not int
        or not 0 < max_entries <= _MAX_ENTRIES
        or type(max_depth) is not int
        or not 0 < max_depth <= _MAX_DEPTH
    ):
        return StartupObservation("blocked", "root_invalid")
    try:
        roots = tuple(
            Path(os.path.abspath(os.fspath(path)))
            for path in (runtime_root, cleanup_journal_root, attempt_journal_root)
        )
    except (TypeError, ValueError, OSError):
        return StartupObservation("blocked", "root_invalid")
    if len(set(roots)) != 3:
        return StartupObservation("blocked", "root_invalid")
    remaining = [max_entries]

    def scan(fd: int, path: Path, relative: tuple[str, ...], *, root_kind: int, depth: int) -> None:
        if depth > max_depth:
            raise _Blocked("scan_limit")
        directory_mount = _mount_id(fd)
        with os.scandir(fd) as entries:
            for entry in entries:
                remaining[0] -= 1
                if remaining[0] < 0:
                    raise _Blocked("scan_limit")
                name = entry.name
                info = entry.stat(follow_symlinks=False)
                if name == ".dlr-instance.lock" and not relative:
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                        raise _Blocked("root_invalid")
                    continue  # RootLockHandle validates the exact lock inode and mode.
                child = path / name
                if child in roots[1:]:
                    if not stat.S_ISDIR(info.st_mode):
                        raise _Blocked("root_invalid")
                    continue  # Independently scanned and locked below.
                allowed = (root_kind == 1 and not relative and name == "sandbox-recovery") or (
                    root_kind == 0 and name in _ALLOWED.get(relative, frozenset())
                )
                if (
                    not allowed
                    or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o022
                ):
                    raise _Blocked("records_or_unknown")
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                child_fd = os.open(name, flags, dir_fd=fd)
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise _Blocked("root_invalid")
                    if opened.st_dev != os.fstat(fd).st_dev:
                        raise _Blocked("root_invalid")
                    # st_dev alone misses bind mounts of the same filesystem.
                    # Both descriptors are live in the same mount namespace.
                    child_mount = _mount_id(child_fd)
                    if directory_mount != child_mount:
                        raise _Blocked("root_invalid")
                    scan(child_fd, child, (*relative, name), root_kind=root_kind, depth=depth + 1)
                    current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                        raise _Blocked("root_invalid")
                    # A bind mount can appear without changing device/inode.
                    # Reopen the name and compare mount identity while both
                    # descriptions are live; this is not an exclusion lock.
                    visible_fd = os.open(name, flags, dir_fd=fd)
                    try:
                        visible = os.fstat(visible_fd)
                        if (visible.st_dev, visible.st_ino) != (
                            opened.st_dev,
                            opened.st_ino,
                        ) or _mount_id(visible_fd) != child_mount:
                            raise _Blocked("root_invalid")
                    finally:
                        os.close(visible_fd)
                finally:
                    os.close(child_fd)

    try:
        with root_lock.borrow(roots[0], [roots[1], roots[2]]):
            for index, path in enumerate(roots):
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    before = os.fstat(fd)
                    visible = path.lstat()
                    if (before.st_dev, before.st_ino) != (visible.st_dev, visible.st_ino):
                        raise _Blocked("root_invalid")
                    scan(fd, path, (), root_kind=index, depth=0)
                finally:
                    os.close(fd)
        return StartupObservation("local_empty", "no_local_records")
    except _Blocked as error:
        return StartupObservation("blocked", error.reason)
    except (OSError, ValueError, sandbox.SandboxError):
        return StartupObservation("blocked", "root_invalid")
