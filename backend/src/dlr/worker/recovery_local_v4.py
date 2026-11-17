"""Descriptor-bound deletion progress and last-record retirement for W1-B.

The caller owns the original roots and has authenticated a committed result.
This module cannot establish host-wide writer exclusion or Control authority.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from dlr.worker import cgroup_namespace, journal_v4, workspace_fs_v4
from dlr.worker import recovery_checkpoint_v4 as checkpoint


def progress_name(attempt: int) -> str:
    return f"recovery-{attempt}.physical.json"


def terminal_name(attempt: int) -> str:
    return f"recovery-{attempt}.terminal.json"


def _entries(directory: int, limit: int = 128) -> set[str]:
    result: set[str] = set()
    with os.scandir(directory) as entries:
        for entry in entries:
            if len(result) >= limit:
                raise ValueError("recovery scan limit")
            result.add(entry.name)
    return result


def _same(fd: int, expected: dict[str, int], mount: int | None) -> None:
    if checkpoint.binding(fd) != expected or cgroup_namespace._mount_id(fd) != mount:
        raise ValueError("recovery object changed")


def _child(parent: int, name: str, expected: dict[str, int], mount: int | None) -> int:
    fd = workspace_fs_v4._open_private_child(parent, name)
    try:
        _same(fd, expected, mount)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _absent(directory: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return True
    return False


def physical_intent(
    directory: int,
    prepared: dict[str, Any],
    stopped: dict[str, Any],
    *,
    _active_files: bool = False,
) -> str:
    """Persist before either the normal owner or recovery deletes any object."""
    if not checkpoint.valid_stopped(stopped, prepared, _active_files=_active_files):
        raise ValueError("recovery checkpoint unavailable")
    name = progress_name(prepared["attempt_id"])
    checkpoint.persist(
        directory,
        name,
        {
            "version": 1,
            "prepared_sha256": checkpoint.digest(prepared),
            "stopped_sha256": checkpoint.digest(stopped),
        },
    )
    return name


def expected_progress(
    prepared: dict[str, Any], stopped: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Closed set of deletion records for the frozen original objects."""
    attempt = prepared["attempt_id"]
    result = {
        progress_name(attempt): {
            "version": 1,
            "prepared_sha256": checkpoint.digest(prepared),
            "stopped_sha256": checkpoint.digest(stopped),
        }
    }
    objects = [
        (str(index), name, binding, name in {"input", "temp", "output"})
        for index, (name, binding) in enumerate(sorted(stopped["workspace_entries"].items()))
    ]
    objects.extend(
        [
            (
                "workspace",
                f"dlr-exec-{prepared['execution_id']}",
                prepared["objects"]["workspace"],
                True,
            ),
            ("parent", f"attempt-{attempt}", prepared["objects"]["parent"], True),
        ]
    )
    if stopped["sandbox_mount_binding"] is not None:
        objects.append(("sandbox", ".dlr-sandbox-mount", stopped["sandbox_mount_binding"], True))
    for suffix, name, binding, is_directory in objects:
        record = {"version": 1, "name": name, "binding": binding, "directory": is_directory}
        for phase in ("intent", "done"):
            result[f"recovery-{attempt}.delete-{suffix}.{phase}.json"] = record
    return result


def inspect_tree(
    directory: int,
    runtime: int,
    runtime_path: Path,
    prepared: dict[str, Any],
    stopped: dict[str, Any],
    *,
    terminal: bool,
) -> None:
    """Read-only pre-registration check of every still-visible original leaf."""
    parent_name = f"attempt-{prepared['attempt_id']}"
    if not workspace_fs_v4._mounts_safe(runtime_path, runtime_path / "workspaces" / parent_name):
        raise ValueError("recovery mount unavailable")
    mount = cgroup_namespace._mount_id(runtime)
    progress = expected_progress(prepared, stopped)
    physical = (
        journal_v4._read(directory, progress_name(prepared["attempt_id"]))
        == progress[progress_name(prepared["attempt_id"])]
    )

    def missing(parent: int, name: str, suffix: str) -> bool:
        if not _absent(parent, name):
            return False
        marker = f"recovery-{prepared['attempt_id']}.delete-{suffix}.intent.json"
        if (
            not terminal
            and not physical
            and journal_v4._read(directory, marker) != progress[marker]
        ):
            raise ValueError("recovery object missing without intent")
        return True

    workspaces = _child(runtime, "workspaces", prepared["objects"]["workspaces"], mount)
    parent = leaf = -1
    try:
        if not _entries(workspaces) <= {parent_name}:
            raise ValueError("recovery additional workspace")
        if terminal and not _absent(workspaces, parent_name):
            raise ValueError("recovery terminal has physical residue")
        if missing(workspaces, parent_name, "parent"):
            return
        parent = _child(workspaces, parent_name, prepared["objects"]["parent"], mount)
        leaf_name = f"dlr-exec-{prepared['execution_id']}"
        allowed = {leaf_name} | (
            {".dlr-sandbox-mount"} if stopped["sandbox_mount_binding"] is not None else set()
        )
        if not _entries(parent) <= allowed:
            raise ValueError("recovery additional parent object")
        if stopped["sandbox_mount_binding"] is not None and not missing(
            parent, ".dlr-sandbox-mount", "sandbox"
        ):
            fd = _child(parent, ".dlr-sandbox-mount", stopped["sandbox_mount_binding"], mount)
            try:
                if not workspace_fs_v4._directory_is_empty(fd):
                    raise ValueError("recovery sandbox mount remains")
            finally:
                os.close(fd)
        if missing(parent, leaf_name, "workspace"):
            return
        leaf = _child(parent, leaf_name, prepared["objects"]["workspace"], mount)
        if not _entries(leaf) <= set(stopped["workspace_entries"]):
            raise ValueError("recovery additional leaf")
        for index, name in enumerate(sorted(stopped["workspace_entries"])):
            if missing(leaf, name, str(index)):
                continue
            expected = stopped["workspace_entries"][name]
            info = os.stat(name, dir_fd=leaf, follow_symlinks=False)
            if {"device": info.st_dev, "inode": info.st_ino} != expected:
                raise ValueError("recovery object changed")
            if name in {"input", "temp", "output"}:
                fd = _child(leaf, name, expected, mount)
                try:
                    if not workspace_fs_v4._directory_is_empty(fd):
                        raise ValueError("recovery unexpected directory contents")
                finally:
                    os.close(fd)
            elif (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError("recovery leaf unavailable")
    finally:
        for fd in (leaf, parent, workspaces):
            if fd >= 0:
                os.close(fd)


def _delete_leaf(
    directory: int,
    parent: int,
    name: str,
    expected: dict[str, int],
    *,
    is_directory: bool,
    mount: int | None,
    prefix: str,
) -> None:
    """A missing name is accepted only after its exact deletion intent exists."""
    record = {"version": 1, "name": name, "binding": expected, "directory": is_directory}
    marker = f"{prefix}.intent.json"
    prior = journal_v4._read(directory, marker)
    if _absent(parent, name):
        if prior != record:
            raise ValueError("recovery object missing without intent")
    else:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if {"device": info.st_dev, "inode": info.st_ino} != expected:
            raise ValueError("recovery object replaced")
        if is_directory:
            fd = _child(parent, name, expected, mount)
            try:
                if not workspace_fs_v4._directory_is_empty(fd):
                    raise ValueError("unexpected recovery directory content")
            finally:
                os.close(fd)
        elif (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("recovery file invalid")
        checkpoint.persist(directory, marker, record)
        # Reopen after the intent is durable, including mount identity.
        if is_directory:
            fd = _child(parent, name, expected, mount)
            try:
                if not workspace_fs_v4._directory_is_empty(fd):
                    raise ValueError("recovery directory changed")
                workspace_fs_v4._check_child(parent, name, fd)
                os.rmdir(name, dir_fd=parent)
            finally:
                os.close(fd)
        else:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino, current.st_mode, current.st_nlink) != (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_nlink,
            ):
                raise ValueError("recovery file changed")
            os.unlink(name, dir_fd=parent)
    os.fsync(parent)
    if not _absent(parent, name):
        raise ValueError("recovery object recreated")
    checkpoint.persist(directory, f"{prefix}.done.json", record)


def clean_workspace(
    directory: int,
    runtime: int,
    runtime_path: Path,
    prepared: dict[str, Any],
    stopped: dict[str, Any],
) -> None:
    """Reopen only stopped original objects; no synthetic live owner is built."""
    attempt, execution = prepared["attempt_id"], prepared["execution_id"]
    parent_name, leaf_name = f"attempt-{attempt}", f"dlr-exec-{execution}"
    if not workspace_fs_v4._mounts_safe(runtime_path, runtime_path / "workspaces" / parent_name):
        raise ValueError("recovery mount unavailable")
    mount = cgroup_namespace._mount_id(runtime)
    workspaces = _child(runtime, "workspaces", prepared["objects"]["workspaces"], mount)
    parent = leaf = -1
    prefix = f"recovery-{attempt}.delete"
    try:
        # A previously running normal owner can have deleted the whole tree.
        physical = journal_v4._read(directory, progress_name(attempt))
        expected = {
            "version": 1,
            "prepared_sha256": checkpoint.digest(prepared),
            "stopped_sha256": checkpoint.digest(stopped),
        }
        if _absent(workspaces, parent_name):
            if physical != expected:
                raise ValueError("recovery parent missing without intent")
            os.fsync(workspaces)
            return
        parent = _child(workspaces, parent_name, prepared["objects"]["parent"], mount)
        allowed = {leaf_name}
        if stopped["sandbox_mount_binding"] is not None:
            allowed.add(".dlr-sandbox-mount")
        if not _entries(parent) <= allowed:
            raise ValueError("unexpected recovery parent object")
        if _absent(parent, leaf_name):
            if physical != expected:
                raise ValueError("recovery workspace missing without intent")
        else:
            leaf = _child(parent, leaf_name, prepared["objects"]["workspace"], mount)
            if not _entries(leaf) <= set(stopped["workspace_entries"]):
                raise ValueError("unexpected recovery workspace object")
            for index, (name, binding) in enumerate(sorted(stopped["workspace_entries"].items())):
                # Normal cleanup may have removed a prefix before exiting.
                # Its durable whole-tree intent authorizes only original
                # descendants under the same stopped, exclusive tree.
                marker = f"{prefix}-{index}.intent.json"
                if _absent(leaf, name) and physical == expected:
                    checkpoint.persist(
                        directory,
                        marker,
                        {
                            "version": 1,
                            "name": name,
                            "binding": binding,
                            "directory": name in {"input", "temp", "output"},
                        },
                    )
                _delete_leaf(
                    directory,
                    leaf,
                    name,
                    binding,
                    is_directory=name in {"input", "temp", "output"},
                    mount=mount,
                    prefix=f"{prefix}-{index}",
                )
            os.close(leaf)
            leaf = -1
        # Persist a whole-tree intent only after all still-visible objects
        # were matched. Individual intents cover earlier deletion windows.
        physical_intent(directory, prepared, stopped)
        if _absent(parent, leaf_name):
            checkpoint.persist(
                directory,
                f"{prefix}-workspace.intent.json",
                {
                    "version": 1,
                    "name": leaf_name,
                    "binding": prepared["objects"]["workspace"],
                    "directory": True,
                },
            )
        _delete_leaf(
            directory,
            parent,
            leaf_name,
            prepared["objects"]["workspace"],
            is_directory=True,
            mount=mount,
            prefix=f"{prefix}-workspace",
        )
        sandbox_mount = stopped["sandbox_mount_binding"]
        if sandbox_mount is not None:
            if _absent(parent, ".dlr-sandbox-mount") and physical == expected:
                checkpoint.persist(
                    directory,
                    f"{prefix}-sandbox.intent.json",
                    {
                        "version": 1,
                        "name": ".dlr-sandbox-mount",
                        "binding": sandbox_mount,
                        "directory": True,
                    },
                )
            _delete_leaf(
                directory,
                parent,
                ".dlr-sandbox-mount",
                sandbox_mount,
                is_directory=True,
                mount=mount,
                prefix=f"{prefix}-sandbox",
            )
        os.close(parent)
        parent = -1
        _delete_leaf(
            directory,
            workspaces,
            parent_name,
            prepared["objects"]["parent"],
            is_directory=True,
            mount=mount,
            prefix=f"{prefix}-parent",
        )
    finally:
        for fd in (leaf, parent, workspaces):
            if fd >= 0:
                os.close(fd)


def valid_terminal(terminal: object, *, _active_files: bool = False) -> bool:
    if not isinstance(terminal, dict):
        return False
    base = {
        "version",
        "prepared",
        "stopped",
        "attempt_record",
        "completion_id",
        "records",
        "runtime_records",
    }
    if type(terminal.get("version")) is not int or not checkpoint._uuid(
        terminal.get("completion_id")
    ):
        return False
    if terminal["version"] == 1 or (
        _active_files
        and terminal["version"] == checkpoint.FILE_VERSION
        and isinstance(terminal.get("prepared"), dict)
        and terminal["prepared"].get("version") == checkpoint.FILE_VERSION
    ):
        return set(terminal) == base | {"finalization_id"} and checkpoint._uuid(
            terminal["finalization_id"]
        )
    return (
        terminal["version"] == 2
        and set(terminal) == base | {"kind", "disposition_id"}
        and terminal["kind"] == "platform_terminal"
        and checkpoint._uuid(terminal["disposition_id"])
    )


def retire(
    directory: int,
    runtime: int,
    terminal: dict[str, Any],
    *,
    allow_missing: bool,
    _active_files: bool = False,
) -> None:
    """The self-contained terminal survives every journal/runtime unlink."""
    if not valid_terminal(terminal, _active_files=_active_files):
        raise ValueError("recovery terminal invalid")
    prepared, stopped = terminal["prepared"], terminal["stopped"]
    if not checkpoint.valid_stopped(stopped, prepared, _active_files=_active_files):
        raise ValueError("recovery terminal invalid")
    name = terminal_name(prepared["attempt_id"])
    records, runtime_records = terminal["records"], terminal["runtime_records"]
    if (
        not checkpoint._refs(records)
        or not checkpoint._refs(runtime_records)
        or name in records
        or runtime_records != prepared["runtime_record"]
        or not journal_v4.valid_record(terminal["attempt_record"], "attempt")
    ):
        raise ValueError("recovery terminal invalid")
    for fd, refs in ((directory, records), (runtime, runtime_records)):
        for filename, sha in refs.items():
            record = journal_v4._read(fd, filename)
            if record is None:
                if not allow_missing or not _absent(fd, filename):
                    raise ValueError("recovery retirement record unavailable")
            elif checkpoint.digest(record) != sha:
                raise ValueError("recovery retirement conflict")
    checkpoint.persist(directory, name, terminal)
    for fd, refs in ((directory, records), (runtime, runtime_records)):
        for filename, sha in refs.items():
            record = journal_v4._read(fd, filename)
            if record is not None:
                if checkpoint.digest(record) != sha:
                    raise ValueError("recovery retirement conflict")
                os.unlink(filename, dir_fd=fd)
            elif not _absent(fd, filename):
                raise ValueError("recovery retirement conflict")
            os.fsync(fd)
    if journal_v4._read(directory, name) != terminal:
        raise ValueError("recovery terminal changed")
    os.unlink(name, dir_fd=directory)
    os.fsync(directory)
