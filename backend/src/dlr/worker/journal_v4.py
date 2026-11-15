"""Private v4 journals; no recovery or credential migration is performed here.

The caller must hold the Worker process's existing lock_roots ownership and
must not activate this format until old instances have actually stopped.
"""

import fcntl
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from dlr.worker import workspace

IDENTITY_FIELDS = frozenset({"worker_identity_id", "instance_id", "instance_generation"})
ATTEMPT_FIELDS = (workspace.ATTEMPT_JOURNAL_FIELDS - {"claim_token"}) | IDENTITY_FIELDS
CLEANUP_FIELDS = workspace.JOURNAL_FIELDS | IDENTITY_FIELDS | {"fencing_token"}
ATTEMPT_V2_FIELDS = ATTEMPT_FIELDS | {"journal_version", "namespace_identity"}
MAX_BYTES = 64 * 1024


def valid_namespace_identity(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "boot_id",
        "parent_device",
        "parent_inode",
        "root_device",
        "root_inode",
    }:
        return False
    boot_id = value.get("boot_id")
    if not isinstance(boot_id, str) or not re.fullmatch(
        r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot_id
    ):
        return False
    return all(
        type(value[key]) is int and 0 <= value[key] <= 2**63 - 1
        for key in ("parent_device", "parent_inode", "root_device", "root_inode")
    )


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate journal field")
        result[key] = value
    return result


def valid_record(value: object, kind: str) -> bool:
    if not isinstance(value, dict) or kind not in {"attempt", "cleanup"}:
        return False
    fields = ATTEMPT_FIELDS if kind == "attempt" else CLEANUP_FIELDS
    keys = frozenset(value)
    is_v2_attempt = kind == "attempt" and keys == ATTEMPT_V2_FIELDS
    if keys != fields and not is_v2_attempt:
        return False
    if is_v2_attempt and (
        type(value.get("journal_version")) is not int
        or value["journal_version"] != 2
        or not valid_namespace_identity(value.get("namespace_identity"))
    ):
        return False
    if type(value.get("protocol_version")) is not int:
        return False
    if value["protocol_version"] != 4:
        return False
    ints = ["execution_id", "attempt_id", "fencing_token", "instance_generation"]
    if kind == "attempt":
        ints.append("attempt_no")
    if any(type(value[key]) is not int or not 0 < value[key] <= 2**63 - 1 for key in ints):
        return False
    for key in ("worker_identity_id", "instance_id"):
        raw = value[key]
        try:
            if not isinstance(raw, str):
                return False
            parsed = uuid.UUID(raw)
            if parsed.int == 0 or str(parsed) != raw:
                return False
        except (ValueError, AttributeError):
            return False
    token, path = value["cleanup_token"], value["workspace_path"]
    if not isinstance(token, str) or not 1 <= len(token) <= 512:
        return False
    if is_v2_attempt and re.fullmatch(r"[A-Za-z0-9_-]{1,512}", token) is None:
        return False
    if not isinstance(path, str) or not Path(path).is_absolute():
        return False
    planned = Path(path)
    if (
        planned.name != f"dlr-exec-{value['execution_id']}"
        or planned.parent.name != f"attempt-{value['attempt_id']}"
        or planned.parent.parent.name != workspace.WORKSPACES_DIRNAME
    ):
        return False
    if kind == "attempt":
        expires = value["lease_expires_at"]
        if not isinstance(expires, str) or len(expires) > 64:
            return False
        try:
            if datetime.fromisoformat(expires.replace("Z", "+00:00")).utcoffset() is None:
                return False
        except ValueError:
            return False
    return True


@contextmanager
def _locked_root(root: Path) -> Iterator[int]:
    if not root.is_absolute():
        raise workspace.WorkspaceError("workspace_cleanup_unknown")
    workspace.ensure_private_directory(root)
    fd = -1
    try:
        fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise workspace.WorkspaceError("workspace_cleanup_unknown")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fd
    except OSError as error:
        raise workspace.WorkspaceError("workspace_cleanup_failed") from error
    finally:
        if fd >= 0:
            os.close(fd)


def _read(fd: int, name: str) -> dict[str, Any] | None:
    descriptor = -1
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > MAX_BYTES
        ):
            return None
        data = bytearray()
        while len(data) <= MAX_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        visible = os.stat(name, dir_fd=fd, follow_symlinks=False)

        def fingerprint(value: os.stat_result) -> tuple[int, ...]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_uid,
                value.st_nlink,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if (
            len(data) > MAX_BYTES
            or len(data) != info.st_size
            or fingerprint(info) != fingerprint(after)
            or fingerprint(info) != fingerprint(visible)
        ):
            return None
        result = json.loads(bytes(data), object_pairs_hook=_unique_pairs)
        return result if isinstance(result, dict) else None
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _name(value: Mapping[str, Any], kind: str) -> str:
    if kind == "attempt":
        return f"attempt-{value['attempt_id']}.attempt.json"
    return f"execution-{value['execution_id']}-attempt-{value['attempt_id']}.cleanup.json"


def load(
    root: Path, runtime_root: Path, *, execution_id: int, attempt_id: int, kind: str
) -> dict[str, Any] | None:
    if any(type(x) is not int or x <= 0 for x in (execution_id, attempt_id)) or kind not in {
        "attempt",
        "cleanup",
    }:
        return None
    try:
        target = _name({"execution_id": execution_id, "attempt_id": attempt_id}, kind)
        with _locked_root(Path(root)) as fd:
            value = _read(fd, target)
        if (
            not valid_record(value, kind)
            or value is None
            or value["execution_id"] != execution_id
            or value["attempt_id"] != attempt_id
            or Path(value["workspace_path"])
            != workspace.workspace_path(runtime_root, execution_id, attempt_id)
        ):
            return None
        return value
    except workspace.WorkspaceError:
        return None


def write(root: Path, runtime_root: Path, value: Mapping[str, Any], *, kind: str) -> Path:
    """Write one closed v4 record; a conflicting or v3 record is never replaced."""
    data = dict(value)
    if not valid_record(data, kind):
        raise workspace.WorkspaceError("attempt_journal_invalid")
    if Path(data["workspace_path"]) != workspace.workspace_path(
        runtime_root, data["execution_id"], data["attempt_id"]
    ):
        raise workspace.WorkspaceError("attempt_journal_invalid")
    root = Path(root)
    planned = Path(data["workspace_path"])
    if root == planned or planned in root.parents:
        raise workspace.WorkspaceError("attempt_journal_invalid")
    name = _name(data, kind)
    with _locked_root(root) as directory:
        try:
            existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            previous = _read(directory, name)
            if not valid_record(previous, kind) or previous is None:
                raise workspace.WorkspaceError("attempt_journal_invalid")
            ignored = {"lease_expires_at"} if kind == "attempt" else set()
            if {k: v for k, v in previous.items() if k not in ignored} != {
                k: v for k, v in data.items() if k not in ignored
            }:
                raise workspace.WorkspaceError("attempt_journal_conflict")
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                if (info.st_dev, info.st_ino) != (existing.st_dev, existing.st_ino):
                    raise workspace.WorkspaceError("attempt_journal_invalid")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(directory)
            return root / name
        temporary = f".{name}.{uuid.uuid4().hex}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            encoded = (
                json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("ascii")
            if len(encoded) > MAX_BYTES:
                raise workspace.WorkspaceError("attempt_journal_invalid")
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            # A competing or unexpected writer must not be able to replace an
            # existing responsibility between the lookup above and publish.
            # If interrupted after link and before unlink, nlink != 1 makes
            # the incomplete publication fail closed on the next read.
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raise workspace.WorkspaceError("attempt_journal_conflict") from None
            os.unlink(temporary, dir_fd=directory)
            os.fsync(directory)
            if _read(directory, name) != data:
                raise workspace.WorkspaceError("attempt_journal_invalid")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with suppress(OSError):
                os.unlink(temporary, dir_fd=directory)
    return root / name


def remove(root: Path, runtime_root: Path, expected: Mapping[str, Any], *, kind: str) -> None:
    """Durably remove only the exact v4 record that the caller has reconciled.

    The caller establishes the remote receipt and physical-cleanup facts first;
    this function cannot infer them. A retry after an uncertain directory fsync
    syncs the directory even when the name is already absent.
    """
    data = dict(expected)
    if not valid_record(data, kind) or Path(data["workspace_path"]) != workspace.workspace_path(
        runtime_root, data["execution_id"], data["attempt_id"]
    ):
        raise workspace.WorkspaceError("attempt_journal_invalid")
    with _locked_root(Path(root)) as directory:
        name = _name(data, kind)
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            os.fsync(directory)
            return
        current = _read(directory, name)
        if not valid_record(current, kind) or current != data:
            raise workspace.WorkspaceError("attempt_journal_conflict")
        # Reject replacement between our initial lookup and validation. All
        # cooperating Worker writers also hold this directory lock.
        checked = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (checked.st_dev, checked.st_ino):
            raise workspace.WorkspaceError("attempt_journal_conflict")
        os.unlink(name, dir_fd=directory)
        os.fsync(directory)
