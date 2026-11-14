"""Descriptor-bound workspace operations for a private Worker mount namespace."""

import json
import os
import re
import stat
from pathlib import Path


def _mounts_safe(root: Path, parent: Path) -> bool:
    """Reject nested mounts and shared/slave propagation in this namespace only."""
    try:
        entries = []
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            before, separator, after = line.partition(" - ")
            fields = before.split()
            if not separator or len(fields) < 6 or not after.split():
                return False
            mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4]))
            entries.append((mount, fields[6:]))
        ancestors = [
            (path, flags) for path, flags in entries if path == root or path in root.parents
        ]
        if not ancestors:
            return False
        _, flags = max(ancestors, key=lambda item: len(item[0].parts))
        if any(flag.startswith(("shared:", "master:", "propagate_from:")) for flag in flags):
            return False
        return not any(
            path == parent.parent or path == parent or parent in path.parents for path, _ in entries
        )
    except (OSError, UnicodeError, ValueError):
        return False


def _open_private_child(parent: int, name: str) -> int:
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        actual = os.fstat(fd)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(actual.st_mode)
            or actual.st_uid != os.geteuid()
            or stat.S_IMODE(actual.st_mode) != 0o700
            or (actual.st_dev, actual.st_ino) != (named.st_dev, named.st_ino)
            or actual.st_dev != os.fstat(parent).st_dev
        ):
            raise ValueError("untrusted directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _directory_is_empty(fd: int) -> bool:
    """Check one entry without first materializing an unbounded directory."""
    with os.scandir(fd) as entries:
        return next(entries, None) is None


def _check_child(parent: int, name: str, child: int) -> None:
    actual = os.fstat(child)
    named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (actual.st_dev, actual.st_ino) != (named.st_dev, named.st_ino):
        raise ValueError("directory binding changed")


def _write_new(directory: int, name: str, value: dict[str, object]) -> None:
    """Publish once; an incomplete file is an unresolved record, never retried."""
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory,
    )
    try:
        offset = 0
        while offset < len(encoded):
            written = os.write(fd, encoded[offset:])
            if written <= 0:
                raise OSError("short intent write")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(directory)
