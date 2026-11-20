"""Provision and verify one private POSIX namespace for two Control hosts."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

StoragePurpose = Literal["managed_files", "builtin_packages", "ai_attachments"]
PURPOSES = ("managed_files", "builtin_packages", "ai_attachments")
_MARKER = ".dlr-namespace.json"
_PROOF = ".dlr-preflight.json"
_REVOKED = ".dlr-revoked.json"
_LOCK = ".dlr-preflight.lock"
_DIRECTORY = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_DOCUMENT_LIMIT = 4096


class SharedStorageUnavailable(OSError):
    """The expected namespace or its storage guarantees could not be verified."""


def linux_boot_id() -> UUID:
    try:
        return UUID(Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        raise SharedStorageUnavailable("Linux boot identity unavailable") from None


def _canonical(value: dict[str, str]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def _document(data: bytes) -> dict[str, str]:
    value = json.loads(data)
    if (
        type(value) is not dict
        or not all(type(key) is str and type(item) is str for key, item in value.items())
        or _canonical(value) != data
    ):
        raise SharedStorageUnavailable("Invalid namespace record")
    return value


def _private_directory(info: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o700
    )


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _mount(directory: int) -> str:
    device, inode = _identity(os.fstat(directory))
    return f"{device}:{inode}"


def runtime_mount_id(directory: int) -> int:
    facts = Path(f"/proc/self/fdinfo/{directory}").read_text(encoding="ascii").splitlines()
    for entry in facts:
        name, _, value = entry.partition(":")
        if name == "mnt_id":
            return int(value.strip())
    raise SharedStorageUnavailable("Linux mount identity unavailable")


def preflight_id(directory: int) -> UUID:
    return UUID(read_record(directory, _PROOF)["proof_id"])


@contextmanager
def open_root(path: Path, *, private: bool = True) -> Iterator[int]:
    """Anchor operations at private directories and reject symlinks on the path."""
    if not path.is_absolute() or len(path.parts) < 2 or ".." in path.parts:
        raise SharedStorageUnavailable("Invalid shared root")
    fd = os.open("/", os.O_RDONLY | _DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if (
            info.st_uid != os.geteuid()
            or info.st_mode & 0o022
            or (private and not _private_directory(info))
        ):
            raise SharedStorageUnavailable("Shared root is not private")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def open_directory(parent: int, name: str, *, create: bool = False) -> Iterator[int]:
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        else:
            os.fsync(parent)
    descriptor = os.open(name, os.O_RDONLY | _DIRECTORY, dir_fd=parent)
    try:
        info = os.fstat(descriptor)
        visible = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not _private_directory(info)
            or info.st_dev != os.fstat(parent).st_dev
            or _identity(info) != _identity(visible)
        ):
            raise SharedStorageUnavailable("Invalid shared directory")
        yield descriptor
    finally:
        os.close(descriptor)


def open_regular(directory: int, name: str, flags: int) -> int:
    """Open a private regular file relative to an already checked directory."""
    descriptor = os.open(name, flags | _FILE, 0o600, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        visible = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or _identity(info) != _identity(visible)
        ):
            raise SharedStorageUnavailable("Invalid shared file")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def read_record(directory: int, name: str) -> dict[str, str]:
    with os.fdopen(open_regular(directory, name, os.O_RDONLY), "rb") as stream:
        saved = stream.read(_DOCUMENT_LIMIT + 1)
    if len(saved) > _DOCUMENT_LIMIT:
        raise SharedStorageUnavailable("Invalid namespace record size")
    try:
        return _document(saved)
    except (ValueError, UnicodeError, TypeError):
        raise SharedStorageUnavailable("Invalid namespace record") from None


def publish_record(
    directory: int, name: str, value: dict[str, str], *, replace: bool = False
) -> None:
    temporary = f".dlr-{uuid4().hex}.part"
    with os.fdopen(
        open_regular(directory, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb"
    ) as stream:
        stream.write(_canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if replace:
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        else:
            # link is exclusive even when two provisioners run concurrently.
            os.link(
                temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False
            )
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)
    os.fsync(directory)


@dataclass(frozen=True)
class SharedNamespace:
    root: Path
    purpose: StoragePurpose
    deployment_id: UUID
    namespace_id: UUID

    def marker(self) -> dict[str, str]:
        return {
            "version": "1",
            "backend": "shared_posix",
            "purpose": self.purpose,
            "deployment": str(self.deployment_id),
            "namespace": str(self.namespace_id),
        }

    @contextmanager
    def open(self, *, slot: Literal["a", "b"] | None = None) -> Iterator[int]:
        with open_root(self.root) as directory:
            try:
                if read_record(directory, _MARKER) != self.marker():
                    raise SharedStorageUnavailable("Shared namespace marker mismatched")
                if slot is not None:
                    try:
                        os.stat(_REVOKED, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise SharedStorageUnavailable("Shared namespace revoked")
                    proof = read_record(directory, _PROOF)
                    marker = self.marker()
                    if (
                        set(proof)
                        != {*marker, "boot_a", "boot_b", "mount_a", "mount_b", "proof_id"}
                        or any(proof[key] != value for key, value in marker.items())
                        or proof[f"boot_{slot}"] != str(linux_boot_id())
                        or proof[f"mount_{slot}"] != _mount(directory)
                        or proof["boot_a"] == proof["boot_b"]
                    ):
                        raise SharedStorageUnavailable("Shared preflight required")
            except FileNotFoundError:
                raise SharedStorageUnavailable("Shared storage unavailable") from None
            yield directory

    def initialize(self) -> None:
        with open_root(self.root) as directory:
            lock = open_regular(directory, _LOCK, os.O_RDWR | os.O_CREAT)
            os.close(lock)
            publish_record(directory, _MARKER, self.marker())

    def revoke_temporary(self) -> None:
        if self.purpose != "ai_attachments":
            raise SharedStorageUnavailable("Only temporary namespaces can be revoked here")
        with self.open() as directory:
            try:
                publish_record(directory, _REVOKED, self.marker())
            except FileExistsError:
                self.verify_revoked(directory)
                os.fsync(directory)

    def verify_revoked(self, directory: int) -> None:
        if self.purpose != "ai_attachments" or read_record(directory, _REVOKED) != self.marker():
            raise SharedStorageUnavailable("Temporary namespace has not been revoked")


def _wait_record(directory: int, name: str, deadline: float) -> dict[str, str]:
    while time.monotonic() < deadline:
        try:
            return read_record(directory, name)
        except FileNotFoundError:
            time.sleep(0.1)
    raise SharedStorageUnavailable("Peer preflight did not respond")


def prove(namespace: SharedNamespace, slot: Literal["a", "b"], probe: UUID, seconds: int) -> None:
    """Run one side of a fresh visibility, publication and cross-host lock test."""
    if not 1 <= seconds <= 120:
        raise SharedStorageUnavailable("Invalid preflight deadline")
    deadline = time.monotonic() + seconds
    boot = str(linux_boot_id())
    prefix = f".dlr-probe-{probe.hex}"
    challenge, answer, complete = (prefix + suffix for suffix in (".a", ".b", ".complete"))
    marker = namespace.marker()
    with namespace.open() as directory:
        lock = open_regular(directory, _LOCK, os.O_RDWR)
        try:
            if slot == "a":
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                first = {
                    **marker,
                    "boot_a": boot,
                    "mount_a": _mount(directory),
                    "challenge": uuid4().hex,
                }
                publish_record(directory, challenge, first, replace=True)
                second = _wait_record(directory, answer, deadline)
                if (
                    set(second) != {*first, "boot_b", "mount_b"}
                    or any(second[key] != value for key, value in first.items())
                    or second["boot_b"] == boot
                ):
                    raise SharedStorageUnavailable("Shared preflight response mismatched")
                fcntl.flock(lock, fcntl.LOCK_UN)
                if _wait_record(directory, complete, deadline) != second:
                    raise SharedStorageUnavailable("Shared lock handoff did not complete")
                publish_record(
                    directory,
                    _PROOF,
                    {key: value for key, value in second.items() if key != "challenge"}
                    | {"proof_id": str(UUID(first["challenge"]))},
                    replace=True,
                )
                for name in (challenge, answer, complete):
                    os.unlink(name, dir_fd=directory)
                os.fsync(directory)
            else:
                first = _wait_record(directory, challenge, deadline)
                if (
                    set(first) != {*marker, "boot_a", "mount_a", "challenge"}
                    or any(first[key] != value for key, value in marker.items())
                    or first["boot_a"] == boot
                ):
                    raise SharedStorageUnavailable("Shared preflight requires another host")
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise SharedStorageUnavailable("Shared lock failed to exclude the peer")
                second = {**first, "boot_b": boot, "mount_b": _mount(directory)}
                publish_record(directory, answer, second, replace=True)
                while time.monotonic() < deadline:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        time.sleep(0.1)
                    else:
                        publish_record(directory, complete, second, replace=True)
                        return
                raise SharedStorageUnavailable("Shared lock handoff timed out")
        finally:
            os.close(lock)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("initialize", "prove"))
    parser.add_argument("purpose", choices=PURPOSES)
    parser.add_argument("root", type=Path)
    parser.add_argument("deployment", type=UUID)
    parser.add_argument("namespace", type=UUID)
    parser.add_argument("--slot", choices=("a", "b"))
    parser.add_argument("--probe", type=UUID)
    parser.add_argument("--seconds", type=int, default=30)
    args = parser.parse_args()
    namespace = SharedNamespace(args.root, args.purpose, args.deployment, args.namespace)
    try:
        if args.action == "initialize":
            namespace.initialize()
        elif args.slot and args.probe:
            prove(namespace, args.slot, args.probe, args.seconds)
        else:
            parser.error("prove requires --slot and --probe")
    except OSError:
        print("Shared storage preflight unavailable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
