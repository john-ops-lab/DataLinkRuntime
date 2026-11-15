"""Same-process workspace binding and bounded cleanup for a fixed Worker.

Deployment must give this Worker exclusive control of its runtime root and a
private mount namespace. Descriptors and journal checks do not establish that
contract against an untrusted host writer, or grant authority after restart.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dlr.worker import (
    cgroup_intent_v4,
    cgroup_namespace,
    claim_preparation_v4,
    journal_v4,
    workspace_fs_v4,
)


@dataclass(frozen=True)
class WorkspaceObservation:
    state: Literal["blocked", "uncertain", "bound_observation", "removed_observation"]
    code: str


_KEY = object()


class LiveWorkspace:
    """Process-local binding; callers must not duplicate or leak borrowed fds."""

    def __init__(
        self,
        key: object,
        cgroup: cgroup_intent_v4.LiveCgroup,
        fds: tuple[int, int, int, int, int],
        names: tuple[str, str],
        mount_id: int,
        parent_record: dict[str, object],
        parent_bound: dict[str, object],
        record: dict[str, object],
        bound: dict[str, object],
    ) -> None:
        if key is not _KEY:
            raise TypeError("live workspaces are created by create_live_workspace")
        self._pid = os.getpid()
        self._cgroup = cgroup
        self._root, self._workspaces, self._parent, self._leaf, self._mountns = fds
        workspaces_info = os.fstat(self._workspaces)
        self._workspaces_identity = (workspaces_info.st_dev, workspaces_info.st_ino)
        self._mountns_identity = _namespace_identity(self._mountns)
        self._parent_name, self._leaf_name = names
        self._mount_id = mount_id
        self._parent_record = parent_record
        self._parent_bound = parent_bound
        self._record = record
        self._bound = bound
        self._lock = threading.RLock()
        self._borrows = 0
        self._scratch_inflight = False
        self._external_io_inflight = False
        # Retain a same-process owner if external broker shutdown is unclear.
        self._broker_owner: object | None = None
        self._active = True
        self._cleanup_confirmed = False
        # Set before the sandbox helper can create its sibling mountpoint.
        # It is meaningful only for this live, same-process workspace.
        self._sandbox_mount_expected = False

    def _close_locked(self) -> None:
        if self._active:
            self._active = False
            for fd in (self._leaf, self._parent, self._workspaces, self._root, self._mountns):
                os.close(fd)

    def close(self) -> None:
        if self._pid != os.getpid():
            return
        with self._lock:
            if self._borrows:
                raise ValueError("workspace borrowed")
            self._close_locked()

    @contextmanager
    def borrow(self) -> Iterator[int]:
        """Lend a directory fd to a trusted same-process test writer only."""
        if self._pid != os.getpid():
            raise ValueError("invalid workspace owner")
        fd: int | None = None
        registered = False
        try:
            with self._lock, self._cgroup._mutex:
                if (
                    not self._active
                    or self._scratch_inflight
                    or self._external_io_inflight
                    or self._cgroup._helper_inflight
                    or self._cgroup._limits_state == "uncertain"
                ):
                    raise ValueError("invalid workspace owner")
                if not self._cgroup._budget._v4_start_allowed(self._cgroup._prepared.reservation):
                    raise ValueError("workspace reservation unavailable")
                with self._cgroup._root_lock.borrow(
                    self._cgroup._runtime_root, [self._cgroup._attempt_journal_root]
                ):
                    _check_journal(self)
                    _verify_workspace(self)
                    if not self._cgroup._budget._v4_start_allowed(
                        self._cgroup._prepared.reservation
                    ):
                        raise ValueError("workspace reservation unavailable")
                    fd = os.dup(self._leaf)
                if not self._cgroup._budget._v4_start_allowed(self._cgroup._prepared.reservation):
                    raise ValueError("workspace reservation unavailable")
                # Set the cgroup-level exclusion first. If interrupted between
                # these mutations, an outstanding count conservatively blocks
                # the supervisor rather than letting it race an unknown writer.
                self._cgroup._workspace_borrows += 1
                self._borrows += 1
                registered = True
            assert fd is not None
            yield fd
            if self._pid == os.getpid():
                # A successful writer must not carry an old binding forward
                # if the directory, journal or reservation changed while it
                # held the descriptor. Body exceptions remain the primary error.
                with self._lock, self._cgroup._mutex:
                    if (
                        not self._active
                        or self._cgroup._helper_inflight
                        or self._cgroup._limits_state == "uncertain"
                    ):
                        raise ValueError("invalid workspace owner")
                    with self._cgroup._root_lock.borrow(
                        self._cgroup._runtime_root, [self._cgroup._attempt_journal_root]
                    ):
                        _check_journal(self)
                        _verify_workspace(self)
                    if not self._cgroup._budget._v4_start_allowed(
                        self._cgroup._prepared.reservation
                    ):
                        raise ValueError("workspace reservation unavailable")
        finally:
            if fd is not None:
                os.close(fd)
            # A forked child owns its duplicated fd but cannot safely enter
            # an inherited lock or settle the parent's borrow count.
            if registered and self._pid == os.getpid():
                with self._lock, self._cgroup._mutex:
                    self._borrows -= 1
                    self._cgroup._workspace_borrows -= 1


def _namespace_identity(fd: int) -> tuple[int, int]:
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _current_mount_namespace() -> int:
    return os.open("/proc/self/ns/mnt", os.O_RDONLY | os.O_CLOEXEC)


def _verify_workspace_parent(live: LiveWorkspace) -> None:
    cg = live._cgroup
    runtime = Path(os.path.abspath(cg._runtime_root))
    parent = runtime / "workspaces" / live._parent_name
    if runtime.resolve(strict=True) != runtime:
        raise ValueError("runtime ancestor changed")
    for fd in (live._workspaces, live._parent):
        info = os.fstat(fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError("workspace ownership or mode changed")
    if not workspace_fs_v4._mounts_safe(runtime, parent):
        raise ValueError("workspace mount changed")
    current_ns = _current_mount_namespace()
    try:
        if _namespace_identity(current_ns) != _namespace_identity(live._mountns):
            raise ValueError("mount namespace changed")
    finally:
        os.close(current_ns)
    if any(
        cgroup_namespace._mount_id(fd) != live._mount_id
        for fd in (live._root, live._workspaces, live._parent)
    ):
        raise ValueError("workspace mount changed")
    root_path = os.stat(runtime, follow_symlinks=False)
    root = os.fstat(live._root)
    if (root.st_dev, root.st_ino) != (root_path.st_dev, root_path.st_ino):
        raise ValueError("workspace root changed")
    workspace_fs_v4._check_child(live._root, "workspaces", live._workspaces)
    workspace_fs_v4._check_child(live._workspaces, live._parent_name, live._parent)


def _verify_workspace(live: LiveWorkspace) -> None:
    _verify_workspace_parent(live)
    info = os.fstat(live._leaf)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or cgroup_namespace._mount_id(live._leaf) != live._mount_id
    ):
        raise ValueError("workspace ownership, mode or mount changed")
    workspace_fs_v4._check_child(live._parent, live._leaf_name, live._leaf)


def _check_journal(live: LiveWorkspace, *, for_cleanup: bool = False) -> None:
    cg = live._cgroup
    claim_preparation_v4.check_binding(
        cg._prepared, cg._instance, cg._config, cg._runtime_root, cg._attempt_journal_root
    )
    reservation = cg._prepared.reservation
    if for_cleanup:
        permitted = cg._budget._v4_stop_allowed(reservation)
    else:
        permitted = cg._budget._v4_handle_state(reservation) == "in_use"
    if not permitted:
        raise ValueError("reservation changed")
    attempt = live._record["attempt_id"]
    with journal_v4._locked_root(cg._attempt_journal_root) as directory:
        if (
            journal_v4._read(directory, f"cgroup-{attempt}.intent.json") != cg._intent
            or journal_v4._read(directory, f"cgroup-{attempt}.bound.json") != cg._bound
            or journal_v4._read(directory, f"physical-parent-{attempt}.intent.json")
            != live._parent_record
            or journal_v4._read(directory, f"physical-parent-{attempt}.bound.json")
            != live._parent_bound
            or journal_v4._read(directory, f"physical-{attempt}.intent.json") != live._record
            or journal_v4._read(directory, f"physical-{attempt}.bound.json") != live._bound
        ):
            raise ValueError("workspace journal changed")


def create_live_workspace(
    cgroup: cgroup_intent_v4.LiveCgroup,
) -> tuple[WorkspaceObservation, LiveWorkspace | None]:
    """Create only under the exact live cgroup's already-reserved Attempt."""
    if (
        sys.platform != "linux"
        or type(cgroup) is not cgroup_intent_v4.LiveCgroup
        or cgroup._pid != os.getpid()
        or not cgroup._prepared.attempt.is_usable()
    ):
        return WorkspaceObservation("blocked", "test_premise_required"), None
    root_fd = workspaces_fd = parent_fd = leaf_fd = mountns_fd = -1
    started = False
    try:
        with cgroup._mutex:
            if not cgroup._active or cgroup._helper_inflight or cgroup._limits_state == "uncertain":
                raise ValueError("cgroup not quiescent")
            with cgroup._root_lock.borrow(cgroup._runtime_root, [cgroup._attempt_journal_root]):
                cgroup_intent_v4._configuration_binding(cgroup)
                if cgroup_intent_v4._populated(cgroup._child_fd):
                    raise ValueError("cgroup populated")
                task = cgroup._prepared.payload
                runtime = Path(os.path.abspath(cgroup._runtime_root))
                if runtime.resolve(strict=True) != runtime:
                    raise ValueError("runtime ancestor symlink")
                parent_name = f"attempt-{task.attempt_id}"
                parent_path = runtime / "workspaces" / parent_name
                if not workspace_fs_v4._mounts_safe(runtime, parent_path):
                    raise ValueError("mount visibility unknown")
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                mountns_fd = _current_mount_namespace()
                root_fd = os.open(runtime, flags)
                mount_id = cgroup_namespace._mount_id(root_fd)
                if mount_id is None:
                    raise ValueError("workspace mount unavailable")
                leaf_name = f"dlr-exec-{task.execution_id}"
                if cgroup._config.namespace_identity is None:
                    raise ValueError("cgroup namespace unavailable")
                parent_record: dict[str, object] = {
                    "version": 1,
                    "execution_id": task.execution_id,
                    "attempt_id": task.attempt_id,
                    "attempt_no": task.attempt_no,
                    "worker_identity_id": str(task.worker_identity_id),
                    "instance_id": str(task.instance_id),
                    "instance_generation": task.instance_generation,
                    "fencing_token": task.fencing_token,
                    "namespace_identity": cgroup._config.namespace_identity.marker(),
                    "root_device": os.fstat(root_fd).st_dev,
                    "root_inode": os.fstat(root_fd).st_ino,
                    "parent": parent_name,
                }
                with journal_v4._locked_root(cgroup._attempt_journal_root) as directory:
                    started = True
                    workspace_fs_v4._write_new(
                        directory, f"physical-parent-{task.attempt_id}.intent.json", parent_record
                    )
                    if not cgroup._budget._v4_start_allowed(cgroup._prepared.reservation):
                        raise ValueError("reservation accounting unknown")
                    try:
                        workspaces_fd = workspace_fs_v4._open_private_child(root_fd, "workspaces")
                    except FileNotFoundError:
                        os.mkdir("workspaces", mode=0o700, dir_fd=root_fd)
                        os.fsync(root_fd)
                        workspaces_fd = workspace_fs_v4._open_private_child(root_fd, "workspaces")
                    if cgroup_namespace._mount_id(workspaces_fd) != mount_id:
                        raise ValueError("mount identity changed")
                    os.mkdir(parent_name, mode=0o700, dir_fd=workspaces_fd)
                    parent_fd = workspace_fs_v4._open_private_child(workspaces_fd, parent_name)
                    if (
                        not workspace_fs_v4._directory_is_empty(parent_fd)
                        or cgroup_namespace._mount_id(parent_fd) != mount_id
                    ):
                        raise ValueError("workspace parent unavailable")
                    for fd in (parent_fd, workspaces_fd, root_fd):
                        os.fsync(fd)
                    parent_info = os.fstat(parent_fd)
                    parent_bound = dict(
                        parent_record,
                        parent_device=parent_info.st_dev,
                        parent_inode=parent_info.st_ino,
                    )
                    workspace_fs_v4._write_new(
                        directory, f"physical-parent-{task.attempt_id}.bound.json", parent_bound
                    )
                    if not cgroup._budget._v4_start_allowed(cgroup._prepared.reservation):
                        raise ValueError("reservation accounting unknown")
                    workspace_fs_v4._check_child(root_fd, "workspaces", workspaces_fd)
                    workspace_fs_v4._check_child(workspaces_fd, parent_name, parent_fd)
                    record: dict[str, object] = {
                        **parent_record,
                        "parent_device": parent_info.st_dev,
                        "parent_inode": parent_info.st_ino,
                        "leaf": leaf_name,
                    }
                    workspace_fs_v4._write_new(
                        directory, f"physical-{task.attempt_id}.intent.json", record
                    )
                    if not cgroup._budget._v4_start_allowed(cgroup._prepared.reservation):
                        raise ValueError("reservation accounting unknown")
                    os.mkdir(leaf_name, mode=0o700, dir_fd=parent_fd)
                    leaf_fd = workspace_fs_v4._open_private_child(parent_fd, leaf_name)
                    if (
                        not workspace_fs_v4._directory_is_empty(leaf_fd)
                        or cgroup_namespace._mount_id(leaf_fd) != mount_id
                    ):
                        raise ValueError("workspace not empty")
                    os.fsync(leaf_fd)
                    os.fsync(parent_fd)
                    info = os.fstat(leaf_fd)
                    bound = dict(record, workspace_device=info.st_dev, workspace_inode=info.st_ino)
                    live = LiveWorkspace(
                        _KEY,
                        cgroup,
                        (root_fd, workspaces_fd, parent_fd, leaf_fd, mountns_fd),
                        (parent_name, leaf_name),
                        mount_id,
                        parent_record,
                        parent_bound,
                        record,
                        bound,
                    )
                    _verify_workspace(live)
                    workspace_fs_v4._write_new(
                        directory, f"physical-{task.attempt_id}.bound.json", bound
                    )
                    _verify_workspace(live)
                    if (
                        journal_v4._read(directory, f"cgroup-{task.attempt_id}.intent.json")
                        != cgroup._intent
                        or journal_v4._read(directory, f"cgroup-{task.attempt_id}.bound.json")
                        != cgroup._bound
                        or journal_v4._read(
                            directory, f"physical-parent-{task.attempt_id}.intent.json"
                        )
                        != parent_record
                        or journal_v4._read(
                            directory, f"physical-parent-{task.attempt_id}.bound.json"
                        )
                        != parent_bound
                        or journal_v4._read(directory, f"physical-{task.attempt_id}.intent.json")
                        != record
                        or journal_v4._read(directory, f"physical-{task.attempt_id}.bound.json")
                        != bound
                        or not cgroup._budget._v4_start_allowed(cgroup._prepared.reservation)
                        or cgroup_intent_v4._populated(cgroup._child_fd)
                    ):
                        raise ValueError("workspace binding changed")
                    parent = cgroup_intent_v4._verify_live_kernel(cgroup._config, cgroup._budget)
                    cgroup_intent_v4._verify_visible(cgroup, parent, cgroup._config)
        root_fd = workspaces_fd = parent_fd = leaf_fd = mountns_fd = -1
        return WorkspaceObservation("bound_observation", "workspace_bound"), live
    except Exception:
        return WorkspaceObservation(
            "uncertain" if started else "blocked", "workspace_create_unverified"
        ), None
    finally:
        for fd in (leaf_fd, parent_fd, workspaces_fd, root_fd, mountns_fd):
            if fd >= 0:
                os.close(fd)


def _remove_contents(fd: int, mount_id: int, remaining: list[int], depth: int) -> None:
    if depth > 32:
        raise ValueError("workspace depth exceeded")
    # Enumerate incrementally: listdir materializes every name before the
    # entry limit is checked, so a huge directory could exhaust the Worker.
    # A concurrent or skipped entry still makes the final rmdir fail closed.
    with os.scandir(fd) as entries:
        for entry in entries:
            name = entry.name
            remaining[0] -= 1
            if remaining[0] < 0:
                raise ValueError("workspace entries exceeded")
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
                )
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (
                        info.st_dev,
                        info.st_ino,
                    ) or cgroup_namespace._mount_id(child) != mount_id:
                        raise ValueError("workspace directory changed")
                    _remove_contents(child, mount_id, remaining, depth + 1)
                    workspace_fs_v4._check_child(fd, name, child)
                    os.rmdir(name, dir_fd=fd)
                finally:
                    os.close(child)
            elif stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1):
                os.unlink(name, dir_fd=fd)
            else:
                raise ValueError("unsupported workspace entry")
            os.fsync(fd)


def _remove_sandbox_mountpoint(live: LiveWorkspace) -> None:
    """Remove only the expected, empty mountpoint after the supervisor is reaped."""
    if not live._sandbox_mount_expected:
        return
    if not live._cgroup._execution_finished:
        raise ValueError("sandbox stop unconfirmed")
    name = ".dlr-sandbox-mount"
    try:
        child = workspace_fs_v4._open_private_child(live._parent, name)
    except FileNotFoundError:
        return
    try:
        if cgroup_namespace._mount_id(child) != live._mount_id:
            raise ValueError("sandbox mount remains")
        workspace_fs_v4._check_child(live._parent, name, child)
        os.rmdir(name, dir_fd=live._parent)
        os.fsync(live._parent)
    finally:
        os.close(child)


def cleanup_live_workspace(
    live: LiveWorkspace, *, max_entries: int = 10000
) -> WorkspaceObservation:
    """Remove the still-bound controlled tree only after the helper has stopped."""
    if type(live) is not LiveWorkspace or live._pid != os.getpid():
        return WorkspaceObservation("blocked", "workspace_handle_invalid")
    with live._lock, live._cgroup._mutex:
        cg = live._cgroup
        if (
            not live._active
            or live._borrows
            or live._scratch_inflight
            or live._external_io_inflight
            or live._broker_owner is not None
            or not cg._removed
            or cg._helper_inflight
        ):
            return WorkspaceObservation("blocked", "workspace_not_quiescent")
        try:
            from dlr.worker import recovery_checkpoint_v4, recovery_local_v4

            if type(max_entries) is not int or not 0 <= max_entries <= 100000:
                raise ValueError("invalid cleanup limit")
            with cg._root_lock.borrow(cg._runtime_root, [cg._attempt_journal_root]):
                _check_journal(live, for_cleanup=True)
                _verify_workspace(live)
                directory = recovery_checkpoint_v4._root_fd(cg._root_lock, cg._attempt_journal_root)
                first, last = recovery_checkpoint_v4.names(cg._prepared.payload.attempt_id)
                prepared, stopped = (
                    journal_v4._read(directory, first),
                    journal_v4._read(directory, last),
                )
                if (
                    prepared is None
                    or stopped is None
                    or len(stopped.get("workspace_entries", {})) > max_entries
                ):
                    raise ValueError("recovery checkpoint unavailable")
                recovery_local_v4.clean_workspace(
                    directory,
                    live._root,
                    cg._runtime_root,
                    prepared,
                    stopped,
                )
            live._close_locked()
            live._cleanup_confirmed = True
            return WorkspaceObservation("removed_observation", "workspace_removed")
        except Exception:
            live._close_locked()
            return WorkspaceObservation("uncertain", "workspace_cleanup_unverified")


def observe_removed_workspace(
    live: LiveWorkspace, *, owner: object, snapshot: object
) -> WorkspaceObservation:
    """Recheck removal for the same stopped supervisor and confirmed result."""
    from dlr.worker.execution_supervision_v4 import ResultSnapshot, SupervisedExecution

    if (
        type(live) is not LiveWorkspace
        or live._pid != os.getpid()
        or type(owner) is not SupervisedExecution
        or type(snapshot) is not ResultSnapshot
        or owner._pid != os.getpid()
        or owner._staged.workspace is not live
        or owner._staged.cgroup is not live._cgroup
        or owner._snapshot is not snapshot
        or snapshot.owner is not owner
        or type(snapshot.receipt) is not str
        or not snapshot.receipt
        or not owner._physical_done.is_set()
        or owner._observation is None
        or owner._observation.code != "cgroup_empty_supervisor_reaped"
        or owner._watchdog.is_alive()
        or owner._collector._thread.is_alive()
        or owner._collector._failed
        or owner._stop.is_set()
        or not live._cgroup._execution_finished
    ):
        return WorkspaceObservation("blocked", "workspace_handle_invalid")
    with live._lock, live._cgroup._mutex:
        return _observe_removed_workspace_locked(live)


def _observe_removed_workspace_locked(live: LiveWorkspace) -> WorkspaceObservation:
    """Caller holds the workspace and cgroup locks, in that order."""
    cg = live._cgroup
    if (
        live._active
        or not live._cleanup_confirmed
        or live._borrows
        or live._scratch_inflight
        or live._external_io_inflight
        or live._broker_owner is not None
        or not cg._removed
        or cg._helper_inflight
        or not cg._budget._v4_start_allowed(cg._prepared.reservation)
    ):
        return WorkspaceObservation("blocked", "workspace_not_quiescent")
    root_fd = workspaces_fd = namespace_fd = -1
    result = WorkspaceObservation("uncertain", "workspace_removal_unverified")
    try:
        with cg._root_lock.borrow(cg._runtime_root, [cg._attempt_journal_root]):
            _check_journal(live, for_cleanup=True)
            runtime = Path(os.path.abspath(cg._runtime_root))
            if runtime.resolve(strict=True) != runtime or not workspace_fs_v4._mounts_safe(
                runtime, runtime / "workspaces" / live._parent_name
            ):
                raise ValueError("workspace ancestry changed")
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            namespace_fd = _current_mount_namespace()
            root_fd = os.open(runtime, flags)
            workspaces_fd = workspace_fs_v4._open_private_child(root_fd, "workspaces")
            root_info, workspaces_info = os.fstat(root_fd), os.fstat(workspaces_fd)
            if (
                _namespace_identity(namespace_fd) != live._mountns_identity
                or (root_info.st_dev, root_info.st_ino)
                != (live._parent_record["root_device"], live._parent_record["root_inode"])
                or (workspaces_info.st_dev, workspaces_info.st_ino) != live._workspaces_identity
                or cgroup_namespace._mount_id(root_fd) != live._mount_id
                or cgroup_namespace._mount_id(workspaces_fd) != live._mount_id
            ):
                raise ValueError("workspace binding changed")

            def require_absent() -> None:
                try:
                    os.stat(live._parent_name, dir_fd=workspaces_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return
                raise ValueError("workspace recreated")

            require_absent()
            os.fsync(workspaces_fd)
            os.fsync(root_fd)
            workspace_fs_v4._check_child(root_fd, "workspaces", workspaces_fd)
            _check_journal(live, for_cleanup=True)
            require_absent()
            if not cg._budget._v4_start_allowed(cg._prepared.reservation):
                raise ValueError("reservation accounting changed")
        result = WorkspaceObservation("removed_observation", "workspace_removal_rechecked")
    except Exception:
        pass
    finally:
        for fd in (namespace_fd, workspaces_fd, root_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    result = WorkspaceObservation("uncertain", "workspace_removal_unverified")
    return result
