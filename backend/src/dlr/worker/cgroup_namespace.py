"""Prepare the container-owned namespace before the Agent starts any threads.

The deployment binds exactly one delegated host parent. We identify Docker's
namespace root by kernel inode, check each container's finite allocation, then
cover that ancestor bind with the namespace root. No ancestor control surface
remains reachable through the configured management mount.
"""

from __future__ import annotations

import fcntl
import math
import os
import re
import stat
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from dlr.worker import sandbox
from dlr.worker import workspace as workspace_manager

if TYPE_CHECKING:
    from dlr.worker.journal_anchor_v4 import AnchorHandle


@dataclass(frozen=True)
class _LockedRoot:
    path: Path
    directory_fd: int
    lock_fd: int
    guard_fd: int
    directory_identity: tuple[int, int]
    directory_mode: int
    lock_identity: tuple[int, int]
    mount_identity: int | None


_HANDLE_FACTORY = object()
_HANDLE_REGISTRY: dict[int, RootLockHandle] = {}
_HANDLE_REGISTRY_LOCK = threading.Lock()


def _reset_handles_after_fork() -> None:
    """Do not retain the parent's lock descriptions in a forked child."""
    global _HANDLE_REGISTRY, _HANDLE_REGISTRY_LOCK
    for handle in tuple(_HANDLE_REGISTRY.values()):
        handle._active = False
        for entry in handle._entries:
            for descriptor, identity in (
                (entry.lock_fd, entry.lock_identity),
                (entry.guard_fd, entry.lock_identity),
                (entry.directory_fd, entry.directory_identity),
            ):
                try:
                    info = os.fstat(descriptor)
                    if (info.st_dev, info.st_ino) == identity:
                        os.close(descriptor)
                except OSError:
                    pass
        if handle._v4_anchor is not None:
            anchor = handle._v4_anchor
            descriptors = [(anchor.fd, anchor.identity)] + [
                (fd, anchor.journal_identities[path]) for path, fd in anchor.journal_fds.items()
            ]
            for descriptor, identity in descriptors:
                try:
                    info = os.fstat(descriptor)
                    if (info.st_dev, info.st_ino) == identity:
                        os.close(descriptor)
                except OSError:
                    pass
    _HANDLE_REGISTRY = {}
    _HANDLE_REGISTRY_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_handles_after_fork)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _mount_id(fd: int) -> int | None:
    """Live Linux mount identity; inode/device alone miss same-inode bind mounts."""
    if sys.platform != "linux":
        return None
    try:
        lines = Path(f"/proc/self/fdinfo/{fd}").read_text(encoding="ascii").splitlines()
        values = [line.partition(":")[2].strip() for line in lines if line.startswith("mnt_id:")]
        if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal():
            raise ValueError
        value = int(values[0])
        if value <= 0:
            raise ValueError
        return value
    except (OSError, UnicodeError, ValueError):
        raise sandbox.SandboxError("sandbox_instance_root_invalid") from None


class RootLockHandle:
    """Opaque process-local capability for the still-held root locks.

    This protects against accidental reuse, not malicious code in this trusted
    Worker process: Python code with arbitrary execution can alter process FDs.
    """

    __slots__ = (
        "_active",
        "_entries",
        "_journal_roots",
        "_mutex",
        "_pid",
        "_runtime_root",
        "_v4_anchor",
    )

    def __init__(
        self,
        key: object,
        runtime_root: Path,
        journal_roots: frozenset[Path],
        entries: tuple[_LockedRoot, ...],
    ) -> None:
        if key is not _HANDLE_FACTORY:
            raise TypeError("root lock handles are issued only by lock_roots")
        self._pid = os.getpid()
        self._runtime_root = runtime_root
        self._journal_roots = journal_roots
        self._entries = entries
        self._mutex = threading.RLock()
        self._active = True
        self._v4_anchor: AnchorHandle | None = None
        with _HANDLE_REGISTRY_LOCK:
            _HANDLE_REGISTRY[id(self)] = self

    def _validate(self, runtime_root: Path, journal_roots: list[Path]) -> None:
        if getattr(self, "_pid", None) != os.getpid():
            raise sandbox.SandboxError("sandbox_instance_root_invalid")
        with _HANDLE_REGISTRY_LOCK:
            registered = _HANDLE_REGISTRY.get(id(self)) is self
        if (
            not registered
            or not self._active
            or self._pid != os.getpid()
            or _absolute(runtime_root) != self._runtime_root
            or not {_absolute(root) for root in journal_roots}.issubset(self._journal_roots)
        ):
            raise sandbox.SandboxError("sandbox_instance_root_invalid")
        try:
            if self._v4_anchor is not None:
                try:
                    self._v4_anchor.verify()
                except Exception:
                    raise sandbox.SandboxError("sandbox_journal_anchor_invalid") from None
            for entry in self._entries:
                directory = os.fstat(entry.directory_fd)
                path_info = entry.path.lstat()
                visible_fd = os.open(
                    entry.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                )
                try:
                    if (
                        _mount_id(entry.directory_fd) != entry.mount_identity
                        or _mount_id(visible_fd) != entry.mount_identity
                    ):
                        raise sandbox.SandboxError("sandbox_instance_root_invalid")
                finally:
                    os.close(visible_fd)
                lock_info = os.fstat(entry.lock_fd)
                guard_info = os.fstat(entry.guard_fd)
                lock_path_info = os.stat(
                    ".dlr-instance.lock", dir_fd=entry.directory_fd, follow_symlinks=False
                )
                if (
                    not stat.S_ISDIR(directory.st_mode)
                    or not stat.S_ISDIR(path_info.st_mode)
                    or (directory.st_dev, directory.st_ino) != entry.directory_identity
                    or (path_info.st_dev, path_info.st_ino) != entry.directory_identity
                    or directory.st_uid != os.geteuid()
                    or stat.S_IMODE(directory.st_mode) != entry.directory_mode
                    or stat.S_IMODE(path_info.st_mode) != entry.directory_mode
                    or not stat.S_ISREG(lock_info.st_mode)
                    or not stat.S_ISREG(lock_path_info.st_mode)
                    or (lock_info.st_dev, lock_info.st_ino) != entry.lock_identity
                    or (guard_info.st_dev, guard_info.st_ino) != entry.lock_identity
                    or (lock_path_info.st_dev, lock_path_info.st_ino) != entry.lock_identity
                    or lock_info.st_uid != os.geteuid()
                    or stat.S_IMODE(lock_info.st_mode) != 0o600
                    or lock_info.st_nlink != 1
                ):
                    raise sandbox.SandboxError("sandbox_instance_root_invalid")
                # A live descriptor alone does not prove its flock is still
                # held. A separately opened description must be blocked,
                # while our original description must still own the lock.
                probe = os.open(
                    ".dlr-instance.lock",
                    os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=entry.directory_fd,
                )
                try:
                    probe_info = os.fstat(probe)
                    if (probe_info.st_dev, probe_info.st_ino) != entry.lock_identity:
                        raise sandbox.SandboxError("sandbox_instance_root_invalid")
                    try:
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        fcntl.flock(probe, fcntl.LOCK_UN)
                        raise sandbox.SandboxError("sandbox_instance_root_invalid")
                    try:
                        fcntl.flock(entry.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise sandbox.SandboxError("sandbox_instance_root_invalid") from None
                finally:
                    os.close(probe)
        except OSError:
            raise sandbox.SandboxError("sandbox_instance_root_invalid") from None

    def require_v4_journal(self, runtime_root: Path, journal_root: Path) -> None:
        """Require the explicitly bound, live journal anchor for v4 writes."""
        anchor = self._v4_anchor
        if (
            anchor is None
            or Path(runtime_root) != _absolute(runtime_root)
            or Path(journal_root) != _absolute(journal_root)
            or Path(journal_root) not in anchor.journal_roots
            or Path(runtime_root) != anchor.runtime_root
        ):
            raise sandbox.SandboxError("sandbox_journal_anchor_invalid")
        self._validate(runtime_root, [journal_root, anchor.path])

    @contextmanager
    def borrow(self, runtime_root: Path, journal_roots: list[Path]) -> Iterator[None]:
        """Validate and hold the capability while one in-process operation runs."""
        # Check before acquiring inherited Python locks after fork.
        if getattr(self, "_pid", None) != os.getpid():
            raise sandbox.SandboxError("sandbox_instance_root_invalid")
        with self._mutex:
            self._validate(runtime_root, journal_roots)
            try:
                yield
            finally:
                self._validate(runtime_root, journal_roots)

    def _close(self) -> None:
        if self._pid != os.getpid():
            self._active = False
            return
        with self._mutex:
            self._active = False
            with _HANDLE_REGISTRY_LOCK:
                _HANDLE_REGISTRY.pop(id(self), None)


@contextmanager
def lock_roots_v4(runtime_root: Path, journal_roots: list[Path]) -> Iterator[RootLockHandle]:
    """Prevent local double ownership and yield a lifetime-scoped capability."""
    descriptors: list[tuple[int, tuple[int, int]]] = []
    entries: list[_LockedRoot] = []
    handle: RootLockHandle | None = None
    try:
        runtime = _absolute(runtime_root)
        journals = frozenset(_absolute(root) for root in journal_roots)
        workspace_manager.ensure_runtime_root(runtime)
        for root in sorted(journals):
            workspace_manager.ensure_private_directory(root)
        for root in sorted({runtime, *journals}):
            directory_fd = os.open(
                root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            directory_info = os.fstat(directory_fd)
            descriptors.append((directory_fd, (directory_info.st_dev, directory_info.st_ino)))
            path_info = root.lstat()
            if (
                not stat.S_ISDIR(directory_info.st_mode)
                or (directory_info.st_dev, directory_info.st_ino)
                != (path_info.st_dev, path_info.st_ino)
                or directory_info.st_uid != os.geteuid()
            ):
                raise sandbox.SandboxError("sandbox_instance_root_invalid")
            descriptor = os.open(
                ".dlr-instance.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory_fd,
            )
            info = os.fstat(descriptor)
            descriptors.append((descriptor, (info.st_dev, info.st_ino)))
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise sandbox.SandboxError("sandbox_instance_root_invalid")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise sandbox.SandboxError("sandbox_instance_root_in_use") from error
            guard_fd = os.dup(descriptor)
            descriptors.append((guard_fd, (info.st_dev, info.st_ino)))
            entries.append(
                _LockedRoot(
                    root,
                    directory_fd,
                    descriptor,
                    guard_fd,
                    (directory_info.st_dev, directory_info.st_ino),
                    stat.S_IMODE(directory_info.st_mode),
                    (info.st_dev, info.st_ino),
                    _mount_id(directory_fd),
                )
            )
        handle = RootLockHandle(_HANDLE_FACTORY, runtime, journals, tuple(entries))
        handle._validate(runtime, list(journals))
        yield handle
    finally:
        if handle is not None:
            handle._close()
        for descriptor, identity in reversed(descriptors):
            try:
                info = os.fstat(descriptor)
                if (info.st_dev, info.st_ino) == identity:
                    os.close(descriptor)
            except OSError:
                pass


@contextmanager
def lock_roots(runtime_root: Path, journal_roots: list[Path]) -> Iterator[None]:
    """Prevent two local instances from concurrently owning shared state."""
    descriptors: list[int] = []
    try:
        workspace_manager.ensure_runtime_root(runtime_root)
        for root in journal_roots:
            workspace_manager.ensure_private_directory(root)
        for root in dict.fromkeys([runtime_root, *journal_roots]):
            descriptor = os.open(
                root / ".dlr-instance.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise sandbox.SandboxError("sandbox_instance_root_invalid")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise sandbox.SandboxError("sandbox_instance_root_in_use") from error
        yield
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _finite_cpu(path: Path) -> float:
    try:
        quota, period = sandbox._read(path / "cpu.max").split()
        value = int(quota) / int(period)
        if int(quota) <= 0 or int(period) <= 0 or not math.isfinite(value):
            raise ValueError
        return value
    except (ValueError, ZeroDivisionError) as error:
        raise sandbox.SandboxError("sandbox_resource_envelope_unavailable") from error


def _finite_memory(path: Path) -> int:
    value = sandbox._read_positive_integer(path / "memory.max")
    if value is None:
        raise sandbox.SandboxError("sandbox_resource_envelope_unavailable")
    return value


def _verify_allocations(parent: Path, children: list[Path]) -> None:
    """Account each Docker root once, leaving capacity for the host keeper."""
    cpu = 0.0
    memory = 0
    pids = 0
    for child in children:
        if child.name == "agent":
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", child.name):
            raise sandbox.SandboxError("sandbox_cgroup_foreign_child")
        cpu += _finite_cpu(child)
        memory += _finite_memory(child)
        child_pids = sandbox._read_positive_integer(child / "pids.max")
        if child_pids is None or sandbox._read(child / "memory.swap.max") != "0":
            raise sandbox.SandboxError("sandbox_resource_envelope_unavailable")
        pids += child_pids
    parent_pids = sandbox._read_positive_integer(parent / "pids.max", allow_max=True)
    if (
        cpu + 0.05 > _finite_cpu(parent) + 1e-9
        or memory + 16 * 1024 * 1024 > _finite_memory(parent)
        or (parent_pids is not None and pids + 8 > parent_pids)
    ):
        raise sandbox.SandboxError("sandbox_shared_parent_overcommitted")


def prepare(config: sandbox.SandboxConfig) -> sandbox.SandboxConfig:
    """Bootstrap only the exact Docker namespace owned by this init process."""
    if sys.platform != "linux" or config.cgroup_path is None:
        raise sandbox.SandboxError("sandbox_linux_target_required")
    parent = config.cgroup_path
    if os.getpid() != 1 or sandbox._pid_cgroup(1) != "/":
        raise sandbox.SandboxError("sandbox_namespace_bootstrap_requires_init")
    if len(list(Path("/proc/self/task").iterdir())) != 1:
        raise sandbox.SandboxError("sandbox_namespace_bootstrap_requires_single_thread")
    # Validate the still-empty delegated parent and its kernel ancestor mount,
    # not a caller-provided container id or a guessed path from /proc/cgroup.
    sandbox.validate_delegated_parent(config)
    sandbox.validate_cgroup_mount(parent, root="/..", writable=True)
    canonical = Path("/sys/fs/cgroup")
    sandbox.validate_cgroup_mount(canonical, root="/")
    canonical_info = canonical.stat()
    parent_info = parent.stat()
    children: list[Path] = []
    for path in parent.iterdir():
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            children.append(path)
    roots = [
        path
        for path in children
        if (path.stat().st_dev, path.stat().st_ino)
        == (canonical_info.st_dev, canonical_info.st_ino)
    ]
    if len(roots) != 1 or roots[0].name == "agent":
        raise sandbox.SandboxError("sandbox_private_cgroup_namespace_required")
    root = roots[0]
    if sandbox._read(root / "cgroup.procs").split() != ["1"]:
        raise sandbox.SandboxError("sandbox_namespace_bootstrap_foreign_process")
    _verify_allocations(parent, children)
    identity = sandbox.NamespaceIdentity(
        boot_id=sandbox._read(Path("/proc/sys/kernel/random/boot_id")),
        parent_device=parent_info.st_dev,
        parent_inode=parent_info.st_ino,
        root_device=canonical_info.st_dev,
        root_inode=canonical_info.st_ino,
        ancestor_children=frozenset((path.stat().st_dev, path.stat().st_ino) for path in children),
    )
    # Bind only C over P. The namespace root's cpu/memory/pids remain Docker's
    # responsibility, even on hosts without nsdelegate enforcing the boundary.
    sandbox._mount(str(root), str(parent), None, sandbox.MS_BIND, None)
    sandbox.validate_cgroup_mount(parent, root="/", writable=True)
    if not parent.samefile(canonical):
        raise sandbox.SandboxError("sandbox_private_cgroup_namespace_required")
    management = parent / "agent"
    management.mkdir(mode=0o755, exist_ok=True)
    if management.is_symlink() or not sandbox._is_empty(management / "cgroup.procs"):
        raise sandbox.SandboxError("sandbox_namespace_bootstrap_foreign_process")
    sandbox._write(management / "cgroup.procs", "1\n")
    if not sandbox._is_empty(parent / "cgroup.procs"):
        raise sandbox.SandboxError("sandbox_cgroup_parent_has_internal_process")
    sandbox._write(parent / "cgroup.subtree_control", "+cpu +memory +pids\n")
    sandbox.validate_private_cgroup_namespace(parent)
    sandbox.validate_delegated_parent(config)
    return replace(config, namespace_identity=identity)
