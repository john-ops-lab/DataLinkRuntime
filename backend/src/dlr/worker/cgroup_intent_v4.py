"""Durable v4 cgroup creation intent; not an execution or cleanup authority.

This isolated primitive writes intent before creating any cgroup and retains
all records and budget. A process-local handle can stop and remove its exact
cgroup while the owning root lock and journal binding remain valid. It never
starts a helper, releases resources or grants cleanup receipts.
Device/inode snapshots are observations, not capabilities across a restart;
a durable helper and complete cleanup protocol remain necessary. A live handle
can configure its frozen kernel limits, but this alone does not authorize work.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dlr.worker import cgroup_namespace, claim_preparation_v4, journal_v4, sandbox
from dlr.worker.client import WorkerInstance


@dataclass(frozen=True)
class CgroupIntentObservation:
    """A local observation, never permission to launch or delete a cgroup."""

    state: Literal["blocked", "uncertain", "bound_observation"]
    code: str


def _persist_new(directory: int, name: str, record: dict[str, object]) -> None:
    """Keep even an incomplete file after uncertainty; never replace a record."""
    data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory,
    )
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(directory)


def _verify_stop_kernel(config: sandbox.SandboxConfig) -> Path:
    """Verify the original kernel namespace without requiring unchanged capacity."""
    identity = config.namespace_identity
    if identity is None or config.cgroup_path is None:
        raise ValueError("namespace unavailable")
    if sandbox._read(Path("/proc/sys/kernel/random/boot_id")) != identity.boot_id:
        raise ValueError("boot identity changed")
    parent = config.cgroup_path
    if not parent.is_absolute() or parent.resolve(strict=True) != parent:
        raise ValueError("cgroup root changed")
    sandbox.validate_private_cgroup_namespace(parent)
    return parent


def _verify_live_kernel(config: sandbox.SandboxConfig, budget: sandbox.ResourceBudget) -> Path:
    _verify_stop_kernel(config)
    parent = sandbox.validate_delegated_parent(config)
    envelope = sandbox.read_verified_resource_envelope(config)
    # A zero limit does not immediately evict pages that were swapped before
    # it was applied. This is a necessary current-state check, not proof that
    # credentials were never swapped or that outside charges are accounted.
    if (
        sandbox._read(parent / "memory.swap.current") != "0"
        or sandbox._read(parent / "memory.swap.max") != "0"
    ):
        raise ValueError("existing swap usage unverified")
    capacity = budget.snapshot()["capacity"]
    if capacity != {
        "cpu": envelope.cpu_cores,
        "memory": envelope.memory_bytes,
        "pids": envelope.pids,
        "tmp": envelope.tmp_bytes,
    }:
        raise ValueError("budget does not match kernel envelope")
    return parent


_LIVE_KEY = object()


class LiveCgroup:
    """Same-process descriptor binding; it is never recoverable from a journal.

    The caller must hold exclusive control of the delegated parent. A root lock
    only coordinates this Worker; it cannot constrain a hostile cgroup manager.
    Closing the handle deliberately forfeits cleanup authority.
    """

    def __init__(
        self,
        key: object,
        parent_fd: int,
        child_fd: int,
        name: str,
        config: sandbox.SandboxConfig,
        budget: sandbox.ResourceBudget,
        runtime_root: Path,
        attempt_journal_root: Path,
        root_lock: cgroup_namespace.RootLockHandle,
        prepared: claim_preparation_v4.PreparedClaim,
        instance: WorkerInstance,
        intent: dict[str, object],
        bound: dict[str, object],
    ) -> None:
        if key is not _LIVE_KEY:
            raise TypeError("live cgroups are created by create_live_cgroup")
        self._pid = os.getpid()
        self._parent_fd = parent_fd
        self._child_fd = child_fd
        self._name = name
        self._config = config
        self._budget = budget
        self._runtime_root = runtime_root
        self._attempt_journal_root = attempt_journal_root
        self._root_lock = root_lock
        self._prepared = prepared
        self._instance = instance
        self._intent = intent
        self._bound = bound
        child = os.fstat(child_fd)
        self._child_identity = (child.st_dev, child.st_ino)
        self._mutex = threading.Lock()
        self._active = True
        self._removed = False
        self._helper_inflight = False
        # Once the execution supervisor is verified reaped, this handle may
        # retain output/cleanup responsibility but cannot launch more work.
        self._execution_finished = False
        self._workspace_borrows = 0
        self._limits_state = "unconfigured"

    def close(self) -> None:
        if self._pid != os.getpid():
            return
        with self._mutex:
            if self._active:
                self._active = False
                os.close(self._child_fd)
                os.close(self._parent_fd)


def create_live_cgroup(
    prepared: claim_preparation_v4.PreparedClaim,
    *,
    instance: WorkerInstance,
    config: sandbox.SandboxConfig,
    budget: sandbox.ResourceBudget,
    runtime_root: Path,
    attempt_journal_root: Path,
    root_lock: cgroup_namespace.RootLockHandle,
) -> tuple[CgroupIntentObservation, LiveCgroup | None]:
    """Persist intent, create one empty cgroup, and retain responsibility.

    The root lock, Attempt record, reservation and kernel cgroup are checked
    in the current process. Once the reservation is consumed it stays in use,
    including on failure. No returned state proves launch or cleanup is safe.
    """
    if (
        type(root_lock) is not cgroup_namespace.RootLockHandle
        or type(budget) is not sandbox.ResourceBudget
        or type(prepared) is not claim_preparation_v4.PreparedClaim
        or type(instance) is not WorkerInstance
        or not prepared.attempt.is_usable()
    ):
        return CgroupIntentObservation("blocked", "intent_invalid"), None
    claimed = False
    parent_fd = child_fd = -1
    try:
        with root_lock.borrow(runtime_root, [attempt_journal_root]):
            claim_preparation_v4.check_binding(
                prepared, instance, config, runtime_root, attempt_journal_root
            )
            if not prepared.attempt.is_usable():
                raise ValueError("Attempt authority unavailable")
            parent = _verify_live_kernel(config, budget)
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            identity = config.namespace_identity
            assert identity is not None
            parent_info = os.fstat(parent_fd)
            if (parent_info.st_dev, parent_info.st_ino) != (
                identity.root_device,
                identity.root_inode,
            ):
                raise ValueError("cgroup root changed")
            if not budget._begin_v4_use(prepared.reservation):
                return CgroupIntentObservation("blocked", "reservation_unavailable"), None
            claimed = True
            task = prepared.payload
            name = sandbox._child_name(task.execution_id, task.attempt_id)
            record: dict[str, object] = {
                "version": 1,
                "worker_identity_id": str(task.worker_identity_id),
                "instance_id": str(task.instance_id),
                "instance_generation": task.instance_generation,
                "execution_id": task.execution_id,
                "attempt_id": task.attempt_id,
                "attempt_no": task.attempt_no,
                "fencing_token": task.fencing_token,
                "namespace_identity": identity.marker(),
                "cgroup_name": name,
            }
            with journal_v4._locked_root(Path(attempt_journal_root)) as directory:
                _persist_new(directory, f"cgroup-{task.attempt_id}.intent.json", record)
                # Revalidate after the durable write and immediately before mkdir.
                _verify_live_kernel(config, budget)
                if not budget._v4_start_allowed(prepared.reservation):
                    raise ValueError("reservation accounting unknown")
                if (os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino) != (
                    identity.root_device,
                    identity.root_inode,
                ):
                    raise ValueError("cgroup root changed")
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
                info = os.fstat(child_fd)
                visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != (visible.st_dev, visible.st_ino):
                    raise ValueError("cgroup identity changed")
                bound = dict(record, observed_device=info.st_dev, observed_inode=info.st_ino)
                _persist_new(directory, f"cgroup-{task.attempt_id}.bound.json", bound)
                live = LiveCgroup(
                    _LIVE_KEY,
                    parent_fd,
                    child_fd,
                    name,
                    config,
                    budget,
                    runtime_root,
                    attempt_journal_root,
                    root_lock,
                    prepared,
                    instance,
                    record,
                    bound,
                )
        # Transfer ownership only after the root-lock context has revalidated.
        parent_fd = child_fd = -1
        return CgroupIntentObservation("bound_observation", "helper_not_authorized"), live
    except Exception:
        return CgroupIntentObservation(
            "uncertain" if claimed else "blocked", "cgroup_intent_unavailable"
        ), None
    finally:
        if child_fd >= 0:
            os.close(child_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def observe_created_cgroup(
    prepared: claim_preparation_v4.PreparedClaim,
    *,
    instance: WorkerInstance,
    config: sandbox.SandboxConfig,
    budget: sandbox.ResourceBudget,
    runtime_root: Path,
    attempt_journal_root: Path,
    root_lock: cgroup_namespace.RootLockHandle,
) -> CgroupIntentObservation:
    """Compatibility observation that intentionally discards live authority."""
    observation, live = create_live_cgroup(
        prepared,
        instance=instance,
        config=config,
        budget=budget,
        runtime_root=runtime_root,
        attempt_journal_root=attempt_journal_root,
        root_lock=root_lock,
    )
    if live is not None:
        live.close()
    return observation


def _verify_visible(live: LiveCgroup, parent: Path, config: sandbox.SandboxConfig) -> None:
    identity = config.namespace_identity
    if identity is None:
        raise ValueError("namespace unavailable")
    parent_fd_info = os.fstat(live._parent_fd)
    parent_path_info = os.stat(parent, follow_symlinks=False)
    parent_identity = (identity.root_device, identity.root_inode)
    if (parent_fd_info.st_dev, parent_fd_info.st_ino) != parent_identity or (
        parent_path_info.st_dev,
        parent_path_info.st_ino,
    ) != parent_identity:
        raise ValueError("parent changed")
    child = os.fstat(live._child_fd)
    visible = os.stat(live._name, dir_fd=live._parent_fd, follow_symlinks=False)
    if (child.st_dev, child.st_ino) != live._child_identity or (
        visible.st_dev,
        visible.st_ino,
    ) != live._child_identity:
        raise ValueError("cgroup changed")


def _populated(child_fd: int) -> bool:
    fd = os.open("cgroup.events", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=child_fd)
    try:
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    if len(raw) > 4096:
        raise ValueError("invalid cgroup events")
    lines = [line.split() for line in raw.splitlines()]
    values = [line[1] for line in lines if len(line) == 2 and line[0] == b"populated"]
    if values not in ([b"0"], [b"1"]):
        raise ValueError("invalid cgroup events")
    return values == [b"1"]


def _configuration_binding(live: LiveCgroup) -> None:
    """Recheck the live reservation, durable binding and kernel identity."""
    claim_preparation_v4.check_binding(
        live._prepared,
        live._instance,
        live._config,
        live._runtime_root,
        live._attempt_journal_root,
    )
    if not live._budget._v4_start_allowed(live._prepared.reservation):
        raise ValueError("reservation unavailable")
    with journal_v4._locked_root(live._attempt_journal_root) as directory:
        attempt = live._intent["attempt_id"]
        if (
            journal_v4._read(directory, f"cgroup-{attempt}.intent.json") != live._intent
            or journal_v4._read(directory, f"cgroup-{attempt}.bound.json") != live._bound
        ):
            raise ValueError("cgroup journal changed")
    parent = _verify_live_kernel(live._config, live._budget)
    _verify_visible(live, parent, live._config)


def _read_limit(directory: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        value = os.read(fd, 256)
    finally:
        os.close(fd)
    if len(value) == 256:
        raise ValueError("invalid cgroup limit")
    return value.strip()


def _limit_values(limits: sandbox.ResourceLimits) -> dict[str, str]:
    return {
        "cpu.max": sandbox._cpu_max(limits.cpu_cores),
        "memory.max": str(limits.memory_bytes),
        "memory.swap.max": "0",
        "pids.max": str(limits.pids),
    }


def _verify_configured_limits(live: LiveCgroup) -> None:
    """Called while holding the live mutex and borrowing the root lock."""
    if live._limits_state != "configured" or _populated(live._child_fd):
        raise ValueError("cgroup limits unavailable")
    if any(
        _read_limit(live._child_fd, name) != value.encode("ascii")
        for name, value in _limit_values(live._prepared.reservation.limits).items()
    ):
        raise ValueError("cgroup limit changed")


def configure_live_cgroup(live: LiveCgroup) -> CgroupIntentObservation:
    """Write and read back the frozen limits on the exact, empty live cgroup.

    A partial write or changed binding remains uncertain and cannot be retried
    through this handle. The caller retains its Attempt journal and resource
    reservation; neither this observation nor an empty cgroup permits launch or
    cleanup. Trusted deployment must exclusively manage the cgroup subtree.
    """
    if type(live) is not LiveCgroup or live._pid != os.getpid():
        return CgroupIntentObservation("blocked", "live_cgroup_invalid")
    with live._mutex:
        if not live._active or live._helper_inflight or live._limits_state != "unconfigured":
            return CgroupIntentObservation("blocked", "cgroup_limits_unavailable")
        # An exception at any point must not make partial limits look complete.
        live._limits_state = "uncertain"
        try:
            with live._root_lock.borrow(live._runtime_root, [live._attempt_journal_root]):
                _configuration_binding(live)
                if _populated(live._child_fd):
                    raise ValueError("cgroup populated")
                values = _limit_values(live._prepared.reservation.limits)
                for name, value in values.items():
                    data = value.encode("ascii") + b"\n"
                    fd = os.open(
                        name,
                        os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=live._child_fd,
                    )
                    try:
                        if os.write(fd, data) != len(data):
                            raise OSError("short cgroup limit write")
                    finally:
                        os.close(fd)
                if any(
                    _read_limit(live._child_fd, name) != value.encode("ascii")
                    for name, value in values.items()
                ):
                    raise ValueError("cgroup limit readback failed")
                _configuration_binding(live)
                if _populated(live._child_fd):
                    raise ValueError("cgroup populated")
            live._limits_state = "configured"
            return CgroupIntentObservation("bound_observation", "cgroup_limits_verified")
        except Exception:
            return CgroupIntentObservation("uncertain", "cgroup_limits_unverified")


def stop_live_cgroup(
    live: LiveCgroup,
    *,
    config: sandbox.SandboxConfig,
    budget: sandbox.ResourceBudget,
    runtime_root: Path,
    attempt_journal_root: Path,
    root_lock: cgroup_namespace.RootLockHandle,
) -> CgroupIntentObservation:
    """Kill, observe empty, and remove only the still-bound cgroup.

    No process is launched. A busy cgroup returns uncertain and may be retried
    with the same live handle; any other error is also uncertain. This records
    no durable cleanup receipt and never releases Attempt or budget ownership.
    Other resources and durable journal synchronization require an orchestrator.
    """
    if type(live) is not LiveCgroup or live._pid != os.getpid():
        return CgroupIntentObservation("blocked", "live_cgroup_invalid")
    with live._mutex:
        if not live._active or (
            config is not live._config
            or budget is not live._budget
            or root_lock is not live._root_lock
            or runtime_root != live._runtime_root
            or attempt_journal_root != live._attempt_journal_root
        ):
            return CgroupIntentObservation("blocked", "live_cgroup_invalid")
        try:
            with root_lock.borrow(runtime_root, [attempt_journal_root]):
                claim_preparation_v4.check_binding(
                    live._prepared, live._instance, config, runtime_root, attempt_journal_root
                )
                if not budget._v4_stop_allowed(live._prepared.reservation):
                    raise ValueError("reservation changed")
                with journal_v4._locked_root(attempt_journal_root) as directory:
                    attempt = live._intent["attempt_id"]
                    if (
                        journal_v4._read(directory, f"cgroup-{attempt}.intent.json") != live._intent
                        or journal_v4._read(directory, f"cgroup-{attempt}.bound.json")
                        != live._bound
                    ):
                        raise ValueError("cgroup journal changed")
                # Capacity drift must prevent new work but cannot prevent a
                # stop against the same verified kernel and cgroup binding.
                parent = _verify_stop_kernel(config)
                _verify_visible(live, parent, config)
                kill_fd = os.open(
                    "cgroup.kill",
                    os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=live._child_fd,
                )
                try:
                    if os.write(kill_fd, b"1\n") != 2:
                        raise OSError("short cgroup kill")
                finally:
                    os.close(kill_fd)
                if _populated(live._child_fd):
                    return CgroupIntentObservation("uncertain", "cgroup_still_populated")
                _verify_visible(live, parent, config)
                os.rmdir(live._name, dir_fd=live._parent_fd)
                # Kernel cgroupfs is virtual; fsync on it is not a durable
                # journal operation. No cross-restart authority is inferred.
            live._active = False
            live._removed = True
            os.close(live._child_fd)
            os.close(live._parent_fd)
            return CgroupIntentObservation("bound_observation", "cgroup_removed")
        except Exception:
            live._active = False
            os.close(live._child_fd)
            os.close(live._parent_fd)
            return CgroupIntentObservation("uncertain", "cgroup_stop_unverified")
