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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from dlr.worker import sandbox
from dlr.worker import workspace as workspace_manager


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
