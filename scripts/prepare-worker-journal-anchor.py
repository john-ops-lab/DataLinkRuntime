#!/usr/bin/env python3
"""Explicitly prepare or verify one host-local Worker journal anchor.

The deployment chooses the persistent filesystem and owns the parent path.
Successful fsync calls do not attest the storage provider, old-host fencing,
or the absence of previously outstanding Worker responsibilities.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from contextlib import suppress
from pathlib import Path
from typing import NoReturn

_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class AnchorSetupError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _mount_id(fd: int) -> int:
    try:
        lines = Path(f"/proc/self/fdinfo/{fd}").read_text(encoding="ascii").splitlines()
        values = [
            line.partition(":")[2].strip()
            for line in lines
            if line.startswith("mnt_id:")
        ]
        if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal():
            raise ValueError
        value = int(values[0])
        if value <= 0:
            raise ValueError
        return value
    except (OSError, UnicodeError, ValueError):
        raise AnchorSetupError("anchor_unconfirmed") from None


def _identity(fd: int) -> tuple[int, int, int]:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise AnchorSetupError("anchor_invalid")
    return info.st_dev, info.st_ino, _mount_id(fd)


def _open_directory(path: Path) -> int:
    """Open each path component without following symlinks."""
    current = os.open("/", _FLAGS)
    try:
        for name in path.parts[1:]:
            child = os.open(name, _FLAGS, dir_fd=current)
            try:
                opened = os.fstat(child)
                named = os.stat(name, dir_fd=current, follow_symlinks=False)
                if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                    raise AnchorSetupError("anchor_unconfirmed")
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _sync(fd: int) -> None:
    os.fsync(fd)


def prepare_anchor(anchor: Path, *, create: bool) -> str:
    """Create only for an explicit fresh install or sync an existing anchor.

    Never create parents, change permissions on existing data, or remove a
    directory on uncertainty. Reopening after syncing detects path or mount
    replacement during this operation; it does not exclude later changes.
    """
    if type(create) is not bool or not isinstance(anchor, Path):
        raise AnchorSetupError("anchor_invalid")
    if not anchor.is_absolute() or anchor == Path("/") or ".." in anchor.parts:
        raise AnchorSetupError("anchor_invalid")
    parent_fd = child_fd = visible_parent = visible_child = -1
    created = False
    try:
        parent_fd = _open_directory(anchor.parent)
        parent_identity = _identity(parent_fd)
        if create:
            try:
                os.mkdir(anchor.name, 0o700, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                raise AnchorSetupError("anchor_exists") from None
        child_fd = os.open(anchor.name, _FLAGS, dir_fd=parent_fd)
        info = os.fstat(child_fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise AnchorSetupError("anchor_invalid")
        child_identity = _identity(child_fd)
        _sync(child_fd)
        _sync(parent_fd)
        visible_parent = _open_directory(anchor.parent)
        if _identity(visible_parent) != parent_identity:
            raise AnchorSetupError("anchor_unconfirmed")
        visible_child = os.open(anchor.name, _FLAGS, dir_fd=visible_parent)
        if _identity(visible_child) != child_identity:
            raise AnchorSetupError("anchor_unconfirmed")
        confirmed = os.fstat(visible_child)
        if confirmed.st_uid != os.geteuid() or stat.S_IMODE(confirmed.st_mode) != 0o700:
            raise AnchorSetupError("anchor_unconfirmed")
        return "created_and_synced" if created else "verified_and_synced"
    except AnchorSetupError as error:
        if created and error.code != "anchor_unconfirmed":
            raise AnchorSetupError("anchor_unconfirmed") from None
        raise
    except Exception:  # noqa: BLE001 - a partial filesystem operation remains unknown
        # mkdir, fsync or a path observation may have taken effect even when
        # the caller did not receive confirmation. Never remove that path.
        raise AnchorSetupError("anchor_unconfirmed") from None
    finally:
        for fd in (visible_child, visible_parent, child_fd, parent_fd):
            if fd >= 0:
                with suppress(OSError):
                    os.close(fd)


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        raise AnchorSetupError("anchor_invalid")


def main() -> int:
    parser = _Parser(description="Explicitly prepare a Worker journal anchor")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--create-fresh", action="store_true")
    mode.add_argument("--verify-existing", action="store_true")
    parser.add_argument("--anchor", required=True, type=Path)
    try:
        args = parser.parse_args()
        if not sys.platform.startswith("linux"):
            raise AnchorSetupError("anchor_platform_unsupported")
        result = prepare_anchor(args.anchor, create=args.create_fresh)
    except AnchorSetupError as error:
        print(error.code, file=sys.stderr)
        return 2 if error.code in {"anchor_invalid", "anchor_exists"} else 1
    except KeyboardInterrupt:
        print("anchor_unconfirmed", file=sys.stderr)
        return 130
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
