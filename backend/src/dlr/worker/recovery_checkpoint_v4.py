"""Local W1-B provenance, written by the real staged and stopped owners.

These records grant no execution or cleanup authority. Recovery additionally
requires current Control authorization and the supervised host stop procedure.
Old W1-A journals cannot be upgraded into these facts after a failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from dlr.common.cpu_accounting import CPU_ACCOUNTING_VERSION, attempt_cpu_units
from dlr.common.dispatch_protocol import deserialize_dispatch_message
from dlr.worker import cgroup_namespace, journal_v4, sandbox, workspace_fs_v4

VERSION = 1
FILE_VERSION = 3
MAX_RECORDS = 128
_HEX = re.compile(r"[0-9a-f]{64}")
_PREPARED_FIELDS = frozenset(
    {
        "version",
        "phase",
        "worker_id",
        "worker_identity_id",
        "instance_id",
        "instance_generation",
        "execution_id",
        "attempt_id",
        "attempt_no",
        "fencing_token",
        "dispatch",
        "boot_id",
        "storage_id",
        "roots",
        "receive_charge",
        "task_charge",
        "journal_records",
        "runtime_record",
        "objects",
        "process",
    }
)
_STOPPED_FIELDS = frozenset(
    {
        "version",
        "phase",
        "attempt_id",
        "prepared_sha256",
        "intent_id",
        "body_mac",
        "result_record",
        "stop_observation",
        "workspace_entries",
        "sandbox_mount_expected",
        "sandbox_mount_binding",
    }
)


def digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")
    ).hexdigest()


def names(attempt_id: int) -> tuple[str, str]:
    if type(attempt_id) is not int or not 0 < attempt_id < 2**63:
        raise ValueError("recovery checkpoint invalid")
    return f"recovery-{attempt_id}.prepared.json", f"recovery-{attempt_id}.stopped.json"


def _uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and UUID(value).int != 0 and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _positive(value: object) -> bool:
    return type(value) is int and 0 < value < 2**63


def _binding(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"device", "inode"}
        and all(type(item) is int and 0 <= item < 2**63 for item in value.values())
    )


def valid_process(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == {"start_ticks", "pid_namespace", "mount_namespace"}
        and _positive(value["start_ticks"])
        and _binding(value["pid_namespace"])
        and _binding(value["mount_namespace"])
    )


def fresh_process(original: object, current: object) -> bool:
    """Same-boot PID 1 must be newer; namespace inode numbers may be reused.

    This is only a local observation. The original container's complete exit,
    exclusive storage and no exported namespace/FD remain operator premises.
    """
    return bool(
        valid_process(original)
        and valid_process(current)
        and isinstance(original, dict)
        and isinstance(current, dict)
        and current["start_ticks"] > original["start_ticks"]
    )


def _refs(value: object) -> bool:
    return (
        isinstance(value, dict)
        and 1 <= len(value) <= MAX_RECORDS
        and all(
            isinstance(name, str)
            and re.fullmatch(r"[a-z0-9.-]{1,160}\.json", name)
            and isinstance(sha, str)
            and _HEX.fullmatch(sha)
            for name, sha in value.items()
        )
    )


def _valid_charge(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"accounting_version", "cpu_units", "limits"}:
        return False
    if (
        type(value["accounting_version"]) is not int
        or value["accounting_version"] != CPU_ACCOUNTING_VERSION
    ):
        return False
    try:
        limits = sandbox.ResourceLimits.from_mapping(value["limits"])
        return (
            limits.as_dict() == value["limits"]
            and type(value["cpu_units"]) is int
            and value["cpu_units"] == attempt_cpu_units(limits.cpu_cores)
        )
    except (TypeError, ValueError, AttributeError, sandbox.SandboxError):
        return False


def valid_prepared(value: object, *, _active_files: bool = False) -> bool:
    files = isinstance(value, dict) and value.get("version") == FILE_VERSION and _active_files
    expected = (
        _PREPARED_FIELDS | {"input_files", "input_directory", "preparation_failed"}
        if files
        else _PREPARED_FIELDS
    )
    if not isinstance(value, dict) or set(value) != expected:
        return False
    if files:
        from dlr.worker.managed_files_v4 import receipt

        try:
            receipt(value["input_files"])
        except (ValueError, TypeError):
            return False
        if (
            type(value["preparation_failed"]) is not bool
            or not _binding(value["input_directory"])
            or (not value["input_files"]) != value["preparation_failed"]
        ):
            return False
    if (
        type(value["version"]) is not int
        or value["version"] != (FILE_VERSION if files else VERSION)
        or value["phase"] != "prepared"
        or not all(
            _positive(value[key])
            for key in (
                "worker_id",
                "instance_generation",
                "execution_id",
                "attempt_id",
                "attempt_no",
                "fencing_token",
            )
        )
        or not all(_uuid(value[key]) for key in ("worker_identity_id", "instance_id", "boot_id"))
        or not _valid_charge(value["receive_charge"])
        or not _valid_charge(value["task_charge"])
        or not _refs(value["journal_records"])
        or not _refs(value["runtime_record"])
    ):
        return False
    roots = value["roots"]
    if (
        not isinstance(roots, dict)
        or set(roots) != {"runtime", "anchor", "attempts"}
        or not all(_binding(root) for root in roots.values())
        or value["storage_id"] != digest({"boot_id": value["boot_id"], "roots": roots})
    ):
        return False
    objects = value["objects"]
    if (
        not isinstance(objects, dict)
        or set(objects) != {"workspaces", "parent", "workspace", "cgroup"}
        or not all(_binding(item) for item in objects.values())
    ):
        return False
    process = value["process"]
    if not valid_process(process):
        return False
    try:
        dispatch = deserialize_dispatch_message(value["dispatch"])
        return bool(
            dispatch.execution_id == value["execution_id"]
            and dispatch.target_worker_id == value["worker_id"]
        )
    except (ValueError, TypeError):
        return False


def valid_stopped(
    value: object, prepared: Mapping[str, Any], *, _active_files: bool = False
) -> bool:
    files = prepared.get("version") == FILE_VERSION and _active_files
    expected = _STOPPED_FIELDS | {"stop_reason"} if files else _STOPPED_FIELDS
    if not isinstance(value, dict) or set(value) != expected:
        return False
    if files:
        if prepared.get("preparation_failed"):
            if value["stop_reason"] != "preparation_failed":
                return False
        elif value["stop_reason"] not in (None, "execution_cutoff"):
            return False
    return bool(
        valid_prepared(dict(prepared), _active_files=_active_files)
        and type(value["version"]) is int
        and value["version"] == (FILE_VERSION if files else VERSION)
        and value["phase"] == "stopped"
        and type(value["attempt_id"]) is int
        and value["attempt_id"] == prepared["attempt_id"]
        and value["prepared_sha256"] == digest(prepared)
        and _uuid(value["intent_id"])
        and isinstance(value["body_mac"], str)
        and _HEX.fullmatch(value["body_mac"])
        and _refs(value["result_record"])
        and value["stop_observation"] == "cgroup_empty_supervisor_reaped"
        and type(value["sandbox_mount_expected"]) is bool
        and (value["sandbox_mount_binding"] is None or _binding(value["sandbox_mount_binding"]))
        and isinstance(value["workspace_entries"], dict)
        and len(value["workspace_entries"]) <= 32
        and prepared["dispatch"]["language"] in {"python", "javascript"}
        and all(
            name
            in {
                "input.json",
                "runtime_config.json",
                "input_manifest.json",
                "output.json",
                ".dlr-output-meta",
                "input",
                "temp",
                "output",
            }
            | (
                {".dlr-harness.mjs", "adapter.mjs"}
                if prepared["dispatch"]["language"] == "javascript"
                else {".dlr-harness.py", "adapter.py"}
            )
            and _binding(binding)
            for name, binding in value["workspace_entries"].items()
        )
    )


def _root_fd(lock: cgroup_namespace.RootLockHandle, path: Path) -> int:
    item = next((item for item in lock._entries if item.path == path), None)
    if item is None:
        raise ValueError("recovery roots unavailable")
    return item.directory_fd


def binding(fd: int) -> dict[str, int]:
    info = os.fstat(fd)
    return {"device": info.st_dev, "inode": info.st_ino}


def process_identity() -> dict[str, Any]:
    """Kernel observations for binding the operator's pre-fault container map."""
    if os.getpid() != 1:
        raise ValueError("container owner unavailable")
    fields = Path("/proc/1/stat").read_text(encoding="ascii").rpartition(")")[2].split()
    ticks = int(fields[19])
    namespaces: dict[str, Any] = {"start_ticks": ticks}
    for key, kind in (("pid_namespace", "pid"), ("mount_namespace", "mnt")):
        descriptor = os.open(f"/proc/1/ns/{kind}", os.O_RDONLY | os.O_CLOEXEC)
        try:
            namespaces[key] = binding(descriptor)
        finally:
            os.close(descriptor)
    return namespaces


def persist(directory: int, name: str, record: dict[str, Any]) -> None:
    """Idempotently sync an exact record; never replace an unknown publication."""
    raw = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    if len(raw) + 1 > journal_v4.MAX_BYTES or not _refs({name: digest(record)}):
        raise ValueError("recovery record invalid")
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        workspace_fs_v4._write_new(directory, name, record)
    if journal_v4._read(directory, name) != record:
        raise ValueError("recovery record changed")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(directory)
    if journal_v4._read(directory, name) != record:
        raise ValueError("recovery record changed")


def _charge(reservation: sandbox.ResourceReservation) -> dict[str, Any]:
    return {
        "accounting_version": CPU_ACCOUNTING_VERSION,
        "cpu_units": attempt_cpu_units(reservation.limits.cpu_cores),
        "limits": reservation.limits.as_dict(),
    }


def prepared_record(staged: Any) -> dict[str, Any]:
    """Construct provenance only from still-live production owners and handles."""
    from dlr.worker import claim_preparation_v4, workspace_live_v4

    if type(staged) is not claim_preparation_v4.StagedClaim:
        raise ValueError("recovery owner invalid")
    prepared, cg, ws = staged.prepared, staged.cgroup, staged.workspace
    lock, runtime, attempts = cg._root_lock, cg._runtime_root, cg._attempt_journal_root
    anchor = lock._v4_anchor
    if anchor is None or cg._pid != os.getpid() or ws._pid != os.getpid() or ws._cgroup is not cg:
        raise ValueError("recovery owner invalid")
    with ws._lock, cg._mutex, lock.borrow(runtime, [attempts]):
        anchor.verify()
        workspace_live_v4._check_journal(ws)
        workspace_live_v4._verify_workspace(ws)
        budget = cg._budget
        with budget._lock:
            receive, task = prepared.receive_reservation, prepared.attempt_reservation
            if (
                budget._owner_pid != os.getpid()
                or budget._accounting_lost
                or receive is task
                or budget._reservation_handles.get(receive.token) is not receive
                or budget._reservation_handles.get(task.token) is not task
                or budget._reservations.get(receive.token) != receive.limits
                or budget._reservations.get(task.token) != task.limits
                or budget._v4_prepared_attempts.get(prepared.identity.attempt_id) != task.token
                or task.token not in budget._v4_in_use
                or receive.token in budget._v4_in_use
                or receive.token in budget._v4_prepared_attempts.values()
            ):
                raise ValueError("recovery budget ownership unavailable")
        identity = prepared.identity
        assert cg._config.namespace_identity is not None
        boot = cg._config.namespace_identity.boot_id
        roots = {
            "runtime": binding(_root_fd(lock, runtime)),
            "anchor": binding(anchor.fd),
            "attempts": binding(_root_fd(lock, attempts)),
        }
        records = {
            journal_v4._name(prepared.journal, "attempt"): prepared.journal,
            prepared.intent_names[0]: prepared.intent,
            f"cgroup-{identity.attempt_id}.intent.json": cg._intent,
            f"cgroup-{identity.attempt_id}.bound.json": cg._bound,
            f"physical-parent-{identity.attempt_id}.intent.json": ws._parent_record,
            f"physical-parent-{identity.attempt_id}.bound.json": ws._parent_bound,
            f"physical-{identity.attempt_id}.intent.json": ws._record,
            f"physical-{identity.attempt_id}.bound.json": ws._bound,
        }
        record = {
            "version": FILE_VERSION if prepared.managed_files else VERSION,
            "phase": "prepared",
            "worker_id": prepared.intent["worker_id"],
            "worker_identity_id": str(identity.worker_identity_id),
            "instance_id": str(identity.instance_id),
            "instance_generation": identity.instance_generation,
            "execution_id": identity.execution_id,
            "attempt_id": identity.attempt_id,
            "attempt_no": identity.attempt_no,
            "fencing_token": identity.fencing_token,
            "dispatch": dict(prepared.dispatch),
            "boot_id": boot,
            "storage_id": digest({"boot_id": boot, "roots": roots}),
            "roots": roots,
            "receive_charge": _charge(prepared.receive_reservation),
            "task_charge": _charge(prepared.attempt_reservation),
            "journal_records": {name: digest(record) for name, record in records.items()},
            "runtime_record": {prepared.intent_names[1]: digest(prepared.intent)},
            "objects": {
                "workspaces": binding(ws._workspaces),
                "parent": binding(ws._parent),
                "workspace": binding(ws._leaf),
                "cgroup": binding(cg._child_fd),
            },
            "process": process_identity(),
        }
        if prepared.managed_files:
            fd = open_file_input(staged)
            try:
                record.update(
                    input_files=list(staged.input_files),
                    input_directory=binding(fd),
                    preparation_failed=staged.preparation_failed,
                )
            finally:
                os.close(fd)
        return record


def open_file_input(staged: Any) -> int:
    """Verify only the frozen metadata; never hash file contents in the Agent."""
    from dlr.worker.managed_files_v4 import receipt

    records = receipt(staged.input_files)
    if not staged.prepared.managed_files or (not records) != staged.preparation_failed:
        raise ValueError("file checkpoint unavailable")
    fd = os.open(
        "input",
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=staged.workspace._leaf,
    )
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != (
            0o555 if records else 0o700
        ):
            raise ValueError("file checkpoint unavailable")
        with os.scandir(fd) as entries:
            names_seen = set()
            for entry in entries:
                names_seen.add(entry.name)
                if len(names_seen) > 4:
                    raise ValueError("file checkpoint unavailable")
        if names_seen != {item["mount_name"] for item in records}:
            raise ValueError("file checkpoint unavailable")
        for item in records:
            value = os.stat(item["mount_name"], dir_fd=fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(value.st_mode)
                or value.st_uid != os.geteuid()
                or value.st_nlink != 1
                or stat.S_IMODE(value.st_mode) != 0o444
                or value.st_size != item["size_bytes"]
                or (value.st_dev, value.st_ino) != (item["device"], item["inode"])
            ):
                raise ValueError("file checkpoint unavailable")
        return fd
    except BaseException:
        os.close(fd)
        raise


def persist_prepared(staged: Any) -> None:
    cg = staged.cgroup
    record = prepared_record(staged)
    if not valid_prepared(record, _active_files=staged.prepared.managed_files):
        raise ValueError("recovery checkpoint invalid")
    with cg._root_lock.borrow(cg._runtime_root, [cg._attempt_journal_root]):
        persist(
            _root_fd(cg._root_lock, cg._attempt_journal_root),
            names(record["attempt_id"])[0],
            record,
        )


def require_startable(staged: Any) -> None:
    cg = staged.cgroup
    record = prepared_record(staged)
    with cg._root_lock.borrow(cg._runtime_root, [cg._attempt_journal_root]):
        directory = _root_fd(cg._root_lock, cg._attempt_journal_root)
        prepared_name, stopped_name = names(record["attempt_id"])
        if journal_v4._read(directory, prepared_name) != record:
            raise ValueError("recovery checkpoint unavailable")
        # Existence, including a corrupt or incomplete stopped publication,
        # irreversibly bars business from starting again.
        try:
            os.stat(stopped_name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise ValueError("Attempt already stopped")


def persist_stopped(
    owner: Any, snapshot: Any, result_record: dict[str, Any], result_name: str
) -> None:
    from dlr.worker import cgroup_intent_v4, execution_supervision_v4, workspace_live_v4

    if (
        type(owner) is not execution_supervision_v4.SupervisedExecution
        or type(snapshot) is not execution_supervision_v4.ResultSnapshot
        or owner._pid != os.getpid()
        or owner._snapshot is not snapshot
        or snapshot.owner is not owner
        or owner._unresolved
        or not owner._physical_done.is_set()
        or owner._observation is None
        or owner._observation.code != "cgroup_empty_supervisor_reaped"
        or owner._watchdog.is_alive()
        or owner._collector._thread.is_alive()
        or owner._collector._failed
        or owner._stop.is_set()
    ):
        raise ValueError("stopped owner unavailable")
    staged, cg, ws = owner._staged, owner._staged.cgroup, owner._staged.workspace
    current = prepared_record(staged)
    with ws._lock, cg._mutex:
        if (
            not cg._execution_finished
            or cg._helper_inflight
            or cg._workspace_borrows
            or ws._borrows
            or ws._scratch_inflight
            or ws._external_io_inflight
            or ws._broker_owner is not None
            or cgroup_intent_v4._populated(cg._child_fd)
        ):
            raise ValueError("physical stop unavailable")
        workspace_live_v4._check_journal(ws)
        workspace_live_v4._verify_workspace(ws)
        entries: dict[str, Any] = {}
        with os.scandir(ws._leaf) as items:
            for item in items:
                if len(entries) >= 32:
                    raise ValueError("workspace recovery bound exceeded")
                info = item.stat(follow_symlinks=False)
                is_directory = item.name in {"input", "temp", "output"}
                if (
                    info.st_uid != os.geteuid()
                    or (
                        is_directory
                        and (
                            not stat.S_ISDIR(info.st_mode)
                            or stat.S_IMODE(info.st_mode)
                            != (0o555 if item.name == "input" and staged.input_files else 0o700)
                        )
                    )
                    or (
                        not is_directory
                        and (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_nlink != 1
                            or stat.S_IMODE(info.st_mode) != 0o600
                        )
                    )
                ):
                    raise ValueError("workspace recovery object unavailable")
                entries[item.name] = {"device": info.st_dev, "inode": info.st_ino}
        mount_binding = None
        try:
            descriptor = workspace_fs_v4._open_private_child(ws._parent, ".dlr-sandbox-mount")
        except FileNotFoundError:
            pass
        else:
            try:
                if (
                    not workspace_fs_v4._directory_is_empty(descriptor)
                    or cgroup_namespace._mount_id(descriptor) != ws._mount_id
                ):
                    raise ValueError("sandbox mount remains")
                mount_binding = binding(descriptor)
            finally:
                os.close(descriptor)
        with cg._root_lock.borrow(cg._runtime_root, [cg._attempt_journal_root]):
            directory = _root_fd(cg._root_lock, cg._attempt_journal_root)
            first, last = names(staged.prepared.identity.attempt_id)
            prepared = journal_v4._read(directory, first)
            if (
                not valid_prepared(prepared, _active_files=staged.prepared.managed_files)
                or prepared is None
                or prepared != current
                or journal_v4._read(directory, result_name) != result_record
            ):
                raise ValueError("recovery provenance unavailable")
            stopped = {
                "version": current["version"],
                "phase": "stopped",
                "attempt_id": prepared["attempt_id"],
                "prepared_sha256": digest(prepared),
                "intent_id": result_record["intent_id"],
                "body_mac": result_record["body_mac"],
                "result_record": {result_name: digest(result_record)},
                "stop_observation": owner._observation.code,
                "workspace_entries": entries,
                "sandbox_mount_expected": ws._sandbox_mount_expected,
                "sandbox_mount_binding": mount_binding,
            }
            if staged.prepared.managed_files:
                stopped["stop_reason"] = owner._observation.stop_reason
            if not valid_stopped(stopped, prepared, _active_files=staged.prepared.managed_files):
                raise ValueError("recovery stop checkpoint invalid")
            persist(directory, last, stopped)
