"""Linux resource sandbox primitives for the v3 Worker.

The Worker owns one exact delegated cgroup subtree supplied by deployment.
Every v3 Attempt gets a fresh sibling child below that subtree; the Worker
process itself is never moved into the Attempt child.  This module deliberately
contains no Docker or systemd control-plane integration: provisioning owns the
delegated parent and the Worker only reads the parent contract and manages its
own children.

The small helper process in this file is executed with the Worker's already
approved ``CAP_SYS_ADMIN``, ``CAP_SETUID``, and ``CAP_SETGID``.  It creates a
private mount/PID namespace, mounts the bounded tmpfs, copies only the
controlled staging workspace, and drops all capabilities plus
``NoNewPrivileges`` before execing the Adapter harness.  The parent cgroup is
never written; only the task-owned Attempt child is changed.
"""

from __future__ import annotations

import base64
import ctypes
import errno
import json
import logging
import math
import os
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("dlr.worker.sandbox")

CGROUP_CONTROLLERS = ("cpu", "memory", "pids")
LIMIT_FILES = ("cpu.max", "memory.max", "memory.swap.max", "pids.max")
ATTEMPT_NAME_PATTERN = re.compile(
    r"(?:attempt-[1-9][0-9]*-[1-9][0-9]*|dlr-preflight-[0-9a-f]{16,64})\Z"
)
RECOVERY_NAME_PATTERN = re.compile(
    r"sandbox-(attempt-[1-9][0-9]*-[1-9][0-9]*|dlr-preflight-[0-9a-f]{16,64})\.json\Z"
)
RECOVERY_FIELDS = frozenset({"cgroup_name", "execution_id", "mount_name", "mount_path"})
RESOURCE_PROFILE_FIELDS = frozenset(
    {
        "schema_version",
        "resource_class",
        "backend",
        "cpu_cores",
        "memory_bytes",
        "pids",
        "tmp_bytes",
        "nofile",
        "execution_timeout_seconds",
        "claim_timeout_seconds",
        "recovery_grace_seconds",
        "workspace_cleanup_attempt_timeout_seconds",
        "workspace_cleanup_total_timeout_seconds",
        "stream_max_bytes",
        "output_max_bytes",
        "output_preview_max_bytes",
    }
)

# Linux values are kept local so importing the Worker on macOS remains safe.
CLONE_NEWNS = 0x0002_0000
CLONE_NEWPID = 0x2000_0000
MS_RDONLY = 1
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_REC = 16_384
MS_PRIVATE = 1 << 18
MS_BIND = 4096
MNT_DETACH = 2
PR_SET_NO_NEW_PRIVS = 38
PR_GET_NO_NEW_PRIVS = 39
PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_CLEAR_ALL = 4
CAPSET_VERSION = 0x2008_0522
CGROUP2_SUPER_MAGIC = 0x6367_7270
CAP_SETGID = 6
CAP_SETUID = 7
CAP_SYS_ADMIN = 21
SUPERVISOR_CAPABILITY_MASK = (1 << CAP_SETGID) | (1 << CAP_SETUID) | (1 << CAP_SYS_ADMIN)

HELPER_PHASE_ERROR_CODES = {
    "helper_parse": "sandbox_helper_parse_failed",
    "ready_gate": "sandbox_helper_ready_failed",
    "validate_cgroup_hide_target": "sandbox_cgroup_hide_target_invalid",
    "mount_namespace_unshare": "sandbox_mount_namespace_failed",
    "mount_namespace_private": "sandbox_mount_namespace_failed",
    "workspace_tmpfs_mount": "sandbox_tmpfs_mount_failed",
    "workspace_copy": "sandbox_workspace_copy_failed",
    "workspace_ownership": "sandbox_workspace_ownership_failed",
    "cgroup_root_hide": "sandbox_cgroup_hide_failed",
    "exact_cgroup_hide": "sandbox_cgroup_hide_failed",
    "secrets_hide": "sandbox_secrets_mount_failed",
    "pid_namespace": "sandbox_pid_namespace_failed",
    "payload_setup": "sandbox_payload_setup_failed",
    "payload_setrlimit": "sandbox_payload_setup_failed",
    "payload_identity": "sandbox_payload_setup_failed",
    "payload_no_new_privileges": "sandbox_payload_setup_failed",
    "payload_capabilities": "sandbox_payload_setup_failed",
    "payload_workspace_chdir": "sandbox_payload_setup_failed",
    "payload_fd_setup": "sandbox_payload_setup_failed",
    "payload_environment": "sandbox_payload_setup_failed",
    "payload_exec": "sandbox_payload_setup_failed",
    "attempt_membership": "sandbox_process_membership_failed",
    "payload_wait": "sandbox_payload_wait_failed",
    "output_copy": "sandbox_output_copy_failed",
}
HELPER_DIAGNOSTIC_PATTERN = re.compile(
    r"\ADLR_SANDBOX_HELPER_DIAGNOSTIC "
    r"phase=(?P<phase>[a-z_]+) "
    r"kind=(?P<kind>os_error|exception) "
    r"errno=(?P<errno>[0-9]+)\Z"
)


class SandboxError(Exception):
    """Stable Worker-side sandbox failure without host-path details."""

    def __init__(self, code: str, message: str = "Sandbox operation failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HelperDiagnostic:
    """Path-free syscall evidence emitted on a private helper FD."""

    phase: str
    kind: str
    errno: int
    error_code: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "phase": self.phase,
            "kind": self.kind,
            "errno": self.errno,
            "error_code": self.error_code,
        }


def _parse_helper_diagnostic(value: bytes | str) -> HelperDiagnostic | None:
    """Parse one strict, path-free helper diagnostic line."""

    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return None
    match = HELPER_DIAGNOSTIC_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    phase = match.group("phase")
    error_code = HELPER_PHASE_ERROR_CODES.get(phase)
    if error_code is None:
        return None
    try:
        error_number = int(match.group("errno"), 10)
    except ValueError:
        return None
    if not 0 <= error_number <= 4095:
        return None
    kind = match.group("kind")
    if kind == "os_error" and error_number == 0:
        return None
    if kind == "exception" and error_number != 0:
        return None
    return HelperDiagnostic(phase, kind, error_number, error_code)


def _write_helper_diagnostic(
    diagnostic_fd: int | None,
    phase: str,
    *,
    kind: str,
    error_number: int,
) -> None:
    """Write only fixed phase/kind/errno data; never include a host path."""

    if diagnostic_fd is None or phase not in HELPER_PHASE_ERROR_CODES:
        return
    if kind not in {"os_error", "exception"}:
        return
    error_number = error_number or errno.EIO if kind == "os_error" else 0
    line = f"DLR_SANDBOX_HELPER_DIAGNOSTIC phase={phase} kind={kind} errno={error_number}\n".encode(
        "ascii"
    )
    with suppress(OSError):
        os.write(diagnostic_fd, line)


@dataclass(frozen=True)
class SandboxConfig:
    """Worker capability ceiling and delegated subtree location."""

    cgroup_path: Path | None
    cpu_cores: float = 1.0
    memory_bytes: int = 512 * 1024 * 1024
    pids: int = 128
    tmp_bytes: int = 1024 * 1024 * 1024
    nofile: int = 1024
    execution_timeout_seconds: int = 300
    claim_timeout_seconds: int = 300
    recovery_grace_seconds: int = 60
    cleanup_attempt_seconds: int = 5
    cleanup_total_seconds: int = 20
    stream_max_bytes: int = 1024 * 1024
    output_max_bytes: int = 512 * 1024
    output_preview_max_bytes: int = 16 * 1024
    payload_uid: int = 501
    payload_gid: int = 1000

    @classmethod
    def from_environment(cls) -> SandboxConfig:
        """Read the Worker-side sandbox ceiling without exposing secrets."""

        def integer(name: str, default: int, minimum: int, maximum: int) -> int:
            raw = os.environ.get(name)
            try:
                value = default if raw is None else int(raw)
            except ValueError as error:
                raise ValueError(f"{name} must be an integer") from error
            if isinstance(value, bool) or not minimum <= value <= maximum:
                raise ValueError(f"{name} is outside its supported range")
            return value

        def number(name: str, default: float, minimum: float, maximum: float) -> float:
            raw = os.environ.get(name)
            try:
                value = default if raw is None else float(raw)
            except ValueError as error:
                raise ValueError(f"{name} must be a number") from error
            if not math.isfinite(value) or not minimum < value <= maximum:
                raise ValueError(f"{name} is outside its supported range")
            return value

        raw_path = os.environ.get("DLR_SANDBOX_CGROUP_PATH")
        return cls(
            cgroup_path=Path(raw_path) if raw_path else None,
            cpu_cores=number("DLR_SANDBOX_CPU_CORES", 1.0, 0.0, 128.0),
            memory_bytes=integer(
                "DLR_SANDBOX_MEMORY_BYTES", 512 * 1024 * 1024, 16 * 1024 * 1024, 1 << 40
            ),
            pids=integer("DLR_SANDBOX_PIDS", 128, 16, 1_000_000),
            tmp_bytes=integer("DLR_SANDBOX_TMP_BYTES", 1 << 30, 1 << 20, 1 << 40),
            nofile=integer("DLR_SANDBOX_NOFILE", 1024, 64, 1_048_576),
            execution_timeout_seconds=integer("DLR_EXECUTION_TIMEOUT_SECONDS", 300, 1, 86_400),
            claim_timeout_seconds=integer("DLR_EXECUTION_CLAIM_TIMEOUT_SECONDS", 300, 30, 86_400),
            recovery_grace_seconds=integer("DLR_EXECUTION_RECOVERY_GRACE_SECONDS", 60, 10, 3_600),
            cleanup_attempt_seconds=integer(
                "DLR_WORKSPACE_CLEANUP_ATTEMPT_TIMEOUT_SECONDS", 5, 1, 60
            ),
            cleanup_total_seconds=integer(
                "DLR_WORKSPACE_CLEANUP_TOTAL_TIMEOUT_SECONDS", 20, 5, 300
            ),
            stream_max_bytes=integer("DLR_EXECUTION_STREAM_MAX_BYTES", 1 << 20, 1, 64 << 20),
            output_max_bytes=integer("DLR_EXECUTION_OUTPUT_MAX_BYTES", 512 << 10, 1, 64 << 20),
            output_preview_max_bytes=integer(
                "DLR_EXECUTION_OUTPUT_PREVIEW_MAX_BYTES", 16 << 10, 1, 64 << 20
            ),
            payload_uid=integer("DLR_SANDBOX_PAYLOAD_UID", 501, 1, 2_147_483_647),
            payload_gid=integer("DLR_SANDBOX_PAYLOAD_GID", 1000, 1, 2_147_483_647),
        )


@dataclass(frozen=True)
class ResourceLimits:
    """Strict, serializable limits copied from one immutable v3 profile."""

    cpu_cores: float
    memory_bytes: int
    pids: int
    tmp_bytes: int
    nofile: int
    execution_timeout_seconds: int
    claim_timeout_seconds: int = 300
    recovery_grace_seconds: int = 60
    cleanup_attempt_seconds: int = 5
    cleanup_total_seconds: int = 20
    stream_max_bytes: int = 1024 * 1024
    output_max_bytes: int = 512 * 1024
    output_preview_max_bytes: int = 16 * 1024

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ResourceLimits:
        def positive_integer(name: str) -> int:
            raw = value.get(name)
            if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
                raise SandboxError("resource_profile_invalid")
            return raw

        raw_cpu = value.get("cpu_cores")
        if isinstance(raw_cpu, bool) or not isinstance(raw_cpu, (int, float)):
            raise SandboxError("resource_profile_invalid")
        cpu = float(raw_cpu)
        if not math.isfinite(cpu) or cpu <= 0:
            raise SandboxError("resource_profile_invalid")
        return cls(
            cpu_cores=cpu,
            memory_bytes=positive_integer("memory_bytes"),
            pids=positive_integer("pids"),
            tmp_bytes=positive_integer("tmp_bytes"),
            nofile=positive_integer("nofile"),
            execution_timeout_seconds=positive_integer("execution_timeout_seconds"),
            claim_timeout_seconds=positive_integer("claim_timeout_seconds"),
            recovery_grace_seconds=positive_integer("recovery_grace_seconds"),
            cleanup_attempt_seconds=positive_integer("workspace_cleanup_attempt_timeout_seconds"),
            cleanup_total_seconds=positive_integer("workspace_cleanup_total_timeout_seconds"),
            stream_max_bytes=positive_integer("stream_max_bytes"),
            output_max_bytes=positive_integer("output_max_bytes"),
            output_preview_max_bytes=positive_integer("output_preview_max_bytes"),
        )

    def as_dict(self) -> dict[str, int | float]:
        return {
            "cpu_cores": self.cpu_cores,
            "memory_bytes": self.memory_bytes,
            "pids": self.pids,
            "tmp_bytes": self.tmp_bytes,
            "nofile": self.nofile,
            "execution_timeout_seconds": self.execution_timeout_seconds,
            "claim_timeout_seconds": self.claim_timeout_seconds,
            "recovery_grace_seconds": self.recovery_grace_seconds,
            "workspace_cleanup_attempt_timeout_seconds": self.cleanup_attempt_seconds,
            "workspace_cleanup_total_timeout_seconds": self.cleanup_total_seconds,
            "stream_max_bytes": self.stream_max_bytes,
            "output_max_bytes": self.output_max_bytes,
            "output_preview_max_bytes": self.output_preview_max_bytes,
        }


@dataclass(frozen=True)
class CleanupResult:
    status: str
    error_code: str | None = None
    cgroup_name: str | None = None
    mount_name: str | None = None
    killed: bool = False
    unmounted: bool = False
    residue: bool = False


def validate_resource_profile(
    value: Mapping[str, Any] | object,
    config: SandboxConfig,
) -> ResourceLimits:
    """Validate a v3 profile before journal/workspace/Adapter side effects."""

    if not isinstance(value, Mapping):
        # Pydantic ResourceProfile instances expose a strict dump method.
        dump = getattr(value, "model_dump", None)
        value = dump(mode="python") if callable(dump) else {}
    if not isinstance(value, Mapping):
        raise SandboxError("resource_profile_invalid")
    if set(value) != RESOURCE_PROFILE_FIELDS:
        raise SandboxError("resource_profile_invalid")
    required = {"schema_version", "resource_class", "backend"}
    schema_version = value.get("schema_version")
    resource_class = value.get("resource_class")
    if (
        not required.issubset(value)
        or not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
        or not isinstance(resource_class, str)
        or not resource_class
        or value.get("backend") != "cgroup_v2"
    ):
        raise SandboxError("resource_profile_invalid")
    profile = ResourceLimits.from_mapping(value)

    # Classify malformed profiles by their own invariant failure before
    # comparing them with the Worker ceiling.  A profile that is both
    # intrinsically invalid and too large must not leak a misleading
    # capability-classification error to Control.
    if (
        profile.memory_bytes < 16 * 1024 * 1024
        or profile.pids < 16
        or profile.tmp_bytes < 1 * 1024 * 1024
        or profile.nofile < 64
        or profile.claim_timeout_seconds < 1
        or profile.recovery_grace_seconds < 1
        or profile.cleanup_attempt_seconds > profile.cleanup_total_seconds
        or profile.cleanup_total_seconds >= profile.recovery_grace_seconds
        or profile.output_preview_max_bytes > profile.output_max_bytes
    ):
        raise SandboxError("resource_profile_invalid")
    if (
        profile.cpu_cores > config.cpu_cores
        or profile.memory_bytes > config.memory_bytes
        or profile.pids > config.pids
        or profile.tmp_bytes > config.tmp_bytes
        or profile.nofile > config.nofile
        or profile.execution_timeout_seconds > config.execution_timeout_seconds
        or profile.claim_timeout_seconds > config.claim_timeout_seconds
        or profile.recovery_grace_seconds > config.recovery_grace_seconds
        or profile.cleanup_attempt_seconds > config.cleanup_attempt_seconds
        or profile.cleanup_total_seconds > config.cleanup_total_seconds
        or profile.stream_max_bytes > config.stream_max_bytes
        or profile.output_max_bytes > config.output_max_bytes
        or profile.output_preview_max_bytes > config.output_preview_max_bytes
    ):
        raise SandboxError("resource_profile_exceeds_worker_capability")
    return profile


def validate_v3_payload_snapshots(payload: Mapping[str, Any], profile: ResourceLimits) -> None:
    """Require top-level v3 budget snapshots to match the queued profile."""

    expected = {
        "execution_timeout_seconds": profile.execution_timeout_seconds,
        "recovery_grace_seconds_snapshot": profile.recovery_grace_seconds,
        "workspace_cleanup_attempt_timeout_seconds_snapshot": profile.cleanup_attempt_seconds,
        "workspace_cleanup_total_timeout_seconds_snapshot": profile.cleanup_total_seconds,
    }
    for name, value in expected.items():
        actual = payload.get(name)
        if not isinstance(actual, int) or isinstance(actual, bool) or actual != value:
            raise SandboxError("resource_profile_invalid")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise SandboxError("sandbox_cgroup_unavailable") from error


def _write(path: Path, value: str) -> None:
    try:
        with path.open("w", encoding="ascii") as stream:
            stream.write(value)
            stream.flush()
    except (OSError, UnicodeError) as error:
        raise SandboxError("sandbox_cgroup_write_failed") from error


def _is_empty(path: Path) -> bool:
    return _read(path) == ""


def _controllers(value: str) -> set[str]:
    return set(value.split())


def validate_delegated_parent(config: SandboxConfig) -> Path:
    """Validate, but never modify, the exact delegated parent."""

    if sys.platform != "linux" or config.cgroup_path is None:
        raise SandboxError("sandbox_linux_target_required")
    parent = config.cgroup_path
    if not parent.is_absolute() or parent == Path("/sys/fs/cgroup"):
        raise SandboxError("sandbox_cgroup_parent_invalid")
    if "app.slice" in parent.parts:
        raise SandboxError("sandbox_cgroup_parent_invalid")
    try:
        resolved = parent.resolve(strict=True)
    except OSError as error:
        raise SandboxError("sandbox_cgroup_unavailable") from error
    if (
        resolved != parent
        or not resolved.is_dir()
        or resolved == Path("/sys/fs/cgroup")
        or "app.slice" in resolved.parts
    ):
        raise SandboxError("sandbox_cgroup_parent_invalid")
    try:
        if _filesystem_magic(resolved) != CGROUP2_SUPER_MAGIC:
            raise SandboxError("sandbox_cgroup_parent_invalid")
    except OSError as error:
        raise SandboxError("sandbox_cgroup_unavailable") from error
    required_files = (
        "cgroup.controllers",
        "cgroup.subtree_control",
        "cgroup.procs",
        "cgroup.kill",
    )
    if any(not (parent / name).is_file() for name in required_files):
        raise SandboxError("sandbox_cgroup_unavailable")
    try:
        available = _controllers(_read(parent / "cgroup.controllers"))
        enabled = _controllers(_read(parent / "cgroup.subtree_control"))
    except SandboxError:
        raise
    if not set(CGROUP_CONTROLLERS).issubset(available) or not set(CGROUP_CONTROLLERS).issubset(
        enabled
    ):
        raise SandboxError("sandbox_cgroup_controllers_unavailable")
    if not _is_empty(parent / "cgroup.procs"):
        raise SandboxError("sandbox_cgroup_parent_has_internal_process")
    return parent


def _child_name(execution_id: int, attempt_id: int) -> str:
    if (
        not isinstance(execution_id, int)
        or isinstance(execution_id, bool)
        or execution_id <= 0
        or not isinstance(attempt_id, int)
        or isinstance(attempt_id, bool)
        or attempt_id <= 0
    ):
        raise SandboxError("sandbox_attempt_invalid")
    name = f"attempt-{execution_id}-{attempt_id}"
    if not ATTEMPT_NAME_PATTERN.fullmatch(name):
        raise SandboxError("sandbox_attempt_invalid")
    return name


def _mkdir_child(parent: Path, name: str) -> Path:
    if not ATTEMPT_NAME_PATTERN.fullmatch(name):
        raise SandboxError("sandbox_attempt_invalid")
    child = parent / name
    if child.parent != parent:
        raise SandboxError("sandbox_attempt_invalid")
    try:
        child.mkdir(mode=0o700)
    except FileExistsError as error:
        raise SandboxError("sandbox_attempt_already_exists") from error
    except OSError as error:
        raise SandboxError("sandbox_cgroup_create_failed") from error
    return child


def _cpu_max(cpu_cores: float) -> str:
    quota = max(1_000, int(round(cpu_cores * 100_000)))
    return f"{quota} 100000"


def _configure_child(child: Path, limits: ResourceLimits) -> dict[str, str]:
    values = {
        "cpu.max": _cpu_max(limits.cpu_cores),
        "memory.max": str(limits.memory_bytes),
        "memory.swap.max": "0",
        "pids.max": str(limits.pids),
    }
    if not _is_empty(child / "cgroup.procs"):
        raise SandboxError("sandbox_cgroup_child_not_empty")
    for filename, value in values.items():
        target = child / filename
        if not target.is_file() or not os.access(target, os.R_OK | os.W_OK):
            raise SandboxError("sandbox_cgroup_limit_unavailable")
        _write(target, value + "\n")
        if _read(target) != value:
            raise SandboxError("sandbox_cgroup_limit_readback_failed")
    return values


def _pid_is_in_child(pid: int, child: Path) -> bool:
    try:
        raw = (child / "cgroup.procs").read_text(encoding="ascii")
    except OSError:
        return False
    return str(pid) in raw.split()


def _pid_cgroup(pid: int) -> str:
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("0::"):
            return line[3:]
    return ""


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _filesystem_magic(path: Path) -> int:
    """Read the Linux filesystem magic without shelling out to ``stat``."""

    if sys.platform != "linux":
        raise OSError(errno.ENOSYS, "Linux statfs is required")
    # Linux struct statfs is smaller than this buffer on the supported
    # architectures; the first long is f_type on both x86_64 and aarch64.
    result = (ctypes.c_long * 16)()
    statfs = getattr(_libc(), "statfs", None)
    if statfs is None:
        raise OSError(errno.ENOSYS, "statfs is unavailable")
    statfs.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
    statfs.restype = ctypes.c_int
    if statfs(os.fsencode(str(path)), ctypes.byref(result)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return int(result[0])


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _validated_hidden_cgroup_path(
    hidden_cgroup_path: str | None,
    mount_root: Path,
    host_workspace: Path,
) -> Path:
    """Validate the exact cgroup bind before any private overmount."""

    if not hidden_cgroup_path:
        raise OSError(errno.EINVAL, "configured cgroup path is required")
    target = Path(hidden_cgroup_path)
    if not target.is_absolute():
        raise OSError(errno.EINVAL, "configured cgroup path must be absolute")
    try:
        resolved_target = target.resolve(strict=True)
        resolved_mount_root = mount_root.resolve(strict=False)
        resolved_workspace = host_workspace.resolve(strict=True)
    except OSError as error:
        raise OSError(error.errno or errno.ENOENT, "configured cgroup path unavailable") from error
    if resolved_target != target or not target.is_dir():
        raise OSError(errno.EINVAL, "configured cgroup path must not be a symlink")
    if _filesystem_magic(target) != CGROUP2_SUPER_MAGIC:
        raise OSError(errno.EINVAL, "configured cgroup path is not cgroup2fs")
    if _paths_overlap(target, resolved_mount_root) or _paths_overlap(target, resolved_workspace):
        raise OSError(errno.EINVAL, "configured cgroup path overlaps workspace")
    return target


def _unshare(flags: int) -> None:
    result = _libc().unshare(ctypes.c_int(flags))
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _mount(
    source: str | None,
    target: str,
    fs_type: str | None,
    flags: int,
    data: str | None,
) -> None:
    libc = _libc()
    result = libc.mount(
        ctypes.c_char_p(source.encode() if source is not None else None),
        ctypes.c_char_p(target.encode()),
        ctypes.c_char_p(fs_type.encode() if fs_type is not None else None),
        ctypes.c_ulong(flags),
        ctypes.c_char_p(data.encode() if data is not None else None),
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _unmount(target: str) -> None:
    result = _libc().umount2(ctypes.c_char_p(target.encode()), ctypes.c_int(MNT_DETACH))
    if result != 0:
        error = ctypes.get_errno()
        if error != errno.EINVAL:
            raise OSError(error, os.strerror(error))


def _set_no_new_privileges() -> None:
    libc = _libc()
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if libc.prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1:
        raise OSError(errno.EPERM, "NoNewPrivileges readback failed")
    # Clear any ambient capability that may have been inherited from the
    # Worker container before the explicit capset below.
    libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


def _drop_capabilities() -> None:
    header = _CapHeader(CAPSET_VERSION, 0)
    data = (_CapData * 2)()
    if _libc().capset(ctypes.byref(header), data) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _drop_identity(uid: int, gid: int) -> None:
    """Make the Adapter payload non-root before capabilities are removed."""
    current_uid = os.getuid()
    if current_uid == 0:
        try:
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)
        except OSError as error:
            raise OSError(error.errno or errno.EPERM, "payload identity setup failed") from error
    elif current_uid != uid or os.getgid() != gid:
        raise OSError(errno.EPERM, "payload identity does not match Worker contract")
    if os.getuid() == 0 or os.getgid() == 0:
        raise OSError(errno.EPERM, "root payload is forbidden")


def _parse_nnp() -> bool:
    try:
        fields = {}
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            key, separator, value = line.partition(":")
            if separator:
                fields[key.strip()] = value.strip().split()[0] if value.strip() else ""
        return fields.get("NoNewPrivs") == "1"
    except (OSError, IndexError):
        return False


def _copy_tree(source: Path, target: Path) -> None:
    """Copy only the controlled staging tree into the private tmpfs."""

    target.mkdir(mode=0o700)
    for entry in os.scandir(source):
        source_path = Path(entry.path)
        if source_path.name == ".dlr-sandbox-mount":
            continue
        target_path = target / source_path.name
        if entry.is_symlink():
            target_path.symlink_to(os.readlink(source_path))
        elif entry.is_dir(follow_symlinks=False):
            shutil.copytree(source_path, target_path, symlinks=True)
        elif entry.is_file(follow_symlinks=False):
            shutil.copy2(source_path, target_path, follow_symlinks=False)
        else:
            raise OSError(errno.ELOOP, "unsupported workspace entry")


@contextmanager
def _filesystem_identity(uid: int, gid: int) -> Iterator[None]:
    """Temporarily set filesystem ownership without adding ``CAP_CHOWN``.

    The helper remains an effective-root supervisor with exactly the approved
    three capabilities, but the tmpfs workspace is mounted for the payload
    uid/gid.  Linux uses fsuid/fsgid for path permission checks and ownership
    of newly-created nodes, allowing the supervisor to copy already-opened
    source files into that mount without a post-copy ``chown`` capability.
    """

    if sys.platform != "linux":
        raise OSError(errno.ENOSYS, "Linux filesystem identity is required")
    libc = _libc()
    setfsuid = getattr(libc, "setfsuid", None)
    setfsgid = getattr(libc, "setfsgid", None)
    if setfsuid is None or setfsgid is None:
        raise OSError(errno.ENOSYS, "filesystem identity syscalls are unavailable")
    for function in (setfsuid, setfsgid):
        function.argtypes = [ctypes.c_uint]
        function.restype = ctypes.c_int
    query = ctypes.c_uint(-1).value
    previous_uid = int(setfsuid(query))
    previous_gid = int(setfsgid(query))
    setfsgid(gid)
    setfsuid(uid)
    if int(setfsuid(query)) != uid or int(setfsgid(query)) != gid:
        setfsgid(previous_gid)
        setfsuid(previous_uid)
        raise OSError(errno.EPERM, "filesystem identity setup failed")
    try:
        yield
    finally:
        setfsgid(previous_gid)
        setfsuid(previous_uid)
        if int(setfsuid(query)) != previous_uid or int(setfsgid(query)) != previous_gid:
            raise OSError(errno.EPERM, "filesystem identity restore failed")


def _copy_tree_as_owner(source: Path, target: Path, uid: int, gid: int) -> None:
    """Copy the staging tree as payload-owned nodes without following links.

    Source descriptors are opened while the supervisor still has filesystem
    identity ``0``.  The destination is an already-mounted tmpfs owned by the
    payload; each destination node is created under the payload fsuid/fsgid,
    preserving its managed mode bits and never following a staged symlink.
    """

    with _filesystem_identity(uid, gid):
        target_info = os.stat(target, follow_symlinks=False)
    if not stat.S_ISDIR(target_info.st_mode) or (target_info.st_uid, target_info.st_gid) != (
        uid,
        gid,
    ):
        raise OSError(errno.EPERM, "payload workspace ownership readback failed")

    def copy_directory(source_dir: Path, target_dir: Path) -> None:
        with os.scandir(source_dir) as entries:
            for entry in entries:
                if entry.name == ".dlr-sandbox-mount":
                    continue
                source_path = Path(entry.path)
                target_path = target_dir / entry.name
                source_info = entry.stat(follow_symlinks=False)
                mode = stat.S_IMODE(source_info.st_mode)
                if stat.S_ISLNK(source_info.st_mode):
                    link = os.readlink(source_path)
                    with _filesystem_identity(uid, gid):
                        os.symlink(link, target_path)
                elif stat.S_ISDIR(source_info.st_mode):
                    creation_mode = mode | stat.S_IWUSR | stat.S_IXUSR
                    with _filesystem_identity(uid, gid):
                        target_path.mkdir(mode=creation_mode)
                        os.chmod(target_path, creation_mode, follow_symlinks=False)
                    copy_directory(source_path, target_path)
                    with _filesystem_identity(uid, gid):
                        os.chmod(target_path, mode, follow_symlinks=False)
                elif stat.S_ISREG(source_info.st_mode):
                    source_fd = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    destination_fd = -1
                    try:
                        with _filesystem_identity(uid, gid):
                            destination_fd = os.open(
                                target_path,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                                mode,
                            )
                            os.fchmod(destination_fd, mode)
                            if (
                                os.fstat(destination_fd).st_uid != uid
                                or os.fstat(destination_fd).st_gid != gid
                            ):
                                raise OSError(
                                    errno.EPERM, "payload workspace file ownership readback failed"
                                )
                            with (
                                os.fdopen(source_fd, "rb", closefd=False) as source_stream,
                                os.fdopen(
                                    destination_fd, "wb", closefd=False
                                ) as destination_stream,
                            ):
                                shutil.copyfileobj(source_stream, destination_stream)
                    finally:
                        os.close(source_fd)
                        if destination_fd >= 0:
                            os.close(destination_fd)
                else:
                    raise OSError(errno.ELOOP, "unsupported workspace entry")

    copy_directory(source, target)


def _replace_workspace(command: list[str], source: Path, target: Path) -> list[str]:
    """Rewrite the workspace root and every command argument below it."""

    source_text = str(source)
    source_path = source.resolve(strict=False) if source.is_absolute() else source
    replaced: list[str] = []
    for item in command:
        if item == source_text:
            replaced.append(target.as_posix())
            continue
        item_path = Path(item)
        if item_path.is_absolute():
            try:
                relative = item_path.relative_to(source_path)
            except ValueError:
                pass
            else:
                replaced.append((target / relative).as_posix())
                continue
        replaced.append(item)
    return replaced


def _helper_child(
    command: list[str],
    host_workspace: Path,
    mount_root: Path,
    nofile: int,
    tmp_bytes: int,
    payload_uid: int,
    payload_gid: int,
    hidden_cgroup_path: str | None,
    attempt_cgroup: Path,
    ready_fd: int,
    diagnostic_fd: int,
) -> int:
    """Run in a standalone helper and report fixed syscall evidence."""

    stage = "ready_gate"
    try:
        try:
            if os.read(ready_fd, 1) != b"1":
                _write_helper_diagnostic(
                    diagnostic_fd,
                    stage,
                    kind="exception",
                    error_number=0,
                )
                return 125
        except OSError as error:
            _write_helper_diagnostic(
                diagnostic_fd,
                stage,
                kind="os_error",
                error_number=error.errno or errno.EIO,
            )
            return 125
    finally:
        os.close(ready_fd)

    mounted_tmpfs = False
    mounted_workspace_tmpfs = False
    outer_fd: int | None = None
    inner_fd: int | None = None
    mount_parent_fd: int | None = None
    payload_parent_fd: int | None = None
    original_cwd_fd: int | None = None
    payload_outer_fd: int | None = None
    workspace_mount_relative: Path | None = None
    workspace_relative: Path | None = None
    payload_workspace: Path | None = None
    payload_root: Path | None = None
    created_payload_root = False
    mounted_payload_bind = False
    hidden_mounts: list[str] = []
    stage = "ready_gate"
    try:
        original_cwd_fd = os.open(".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        mount_parent_fd = os.open(
            mount_root.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        stage = "validate_cgroup_hide_target"
        exact_cgroup_mount = _validated_hidden_cgroup_path(
            hidden_cgroup_path,
            mount_root,
            host_workspace,
        )
        stage = "mount_namespace_unshare"
        _unshare(CLONE_NEWNS)
        stage = "mount_namespace_private"
        _mount(None, "/", None, MS_REC | MS_PRIVATE, None)
        mount_root.mkdir(mode=0o700, exist_ok=True)
        stage = "workspace_tmpfs_mount"
        _mount(
            "tmpfs",
            str(mount_root),
            "tmpfs",
            MS_NOSUID | MS_NODEV | MS_NOEXEC,
            # The supervisor retains ownership of the mount root.  The
            # non-root payload reaches its payload-owned workspace leaf through
            # the inherited directory fd.
            f"size={tmp_bytes},mode=0711",
        )
        mounted_tmpfs = True
        outer_fd = os.open(mount_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        payload_root = Path("/tmp") / f".dlr-sandbox-{mount_root.parent.name}"
        payload_parent_fd = os.open(
            payload_root.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        payload_root.mkdir(mode=0o711)
        created_payload_root = True
        _mount(str(mount_root), str(payload_root), None, MS_BIND | MS_REC, None)
        mounted_payload_bind = True
        payload_outer_fd = os.open(
            payload_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        os.fchdir(payload_outer_fd)
        workspace_mount_relative = Path(host_workspace.name)
        workspace_mount_relative.mkdir(mode=0o711)
        stage = "workspace_tmpfs_workspace_mount"
        _mount(
            "tmpfs",
            str(workspace_mount_relative),
            "tmpfs",
            MS_NOSUID | MS_NODEV | MS_NOEXEC,
            f"size={tmp_bytes},uid={payload_uid},gid={payload_gid},mode=0700",
        )
        mounted_workspace_tmpfs = True
        workspace_relative = Path(host_workspace.name)
        with _filesystem_identity(payload_uid, payload_gid):
            inner_fd = os.open(
                workspace_mount_relative,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            os.set_inheritable(inner_fd, True)
            os.fchdir(inner_fd)
            workspace_relative.mkdir(mode=0o700)
            os.chmod(workspace_relative, 0o700, follow_symlinks=False)
        payload_workspace = Path(f"/proc/self/fd/{inner_fd}/{host_workspace.name}")
        stage = "workspace_ownership"
        _copy_tree_as_owner(host_workspace, workspace_relative, payload_uid, payload_gid)

        # PID namespaces only affect children, so fork after unshare.  The
        # helper stays outside the Attempt child; only the payload PID is
        # moved into the task-owned cgroup before identity is dropped.  This
        # lets cgroup.kill terminate the payload while the helper remains
        # available to copy output and unmount the private filesystems.
        stage = "pid_namespace"
        _unshare(CLONE_NEWPID)
        payload_gate_read, payload_gate_write = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(payload_gate_write)
            try:
                if os.read(payload_gate_read, 1) != b"1":
                    raise OSError(errno.ECANCELED, "payload cgroup gate was not released")
                os.close(payload_gate_read)
                stage = "payload_setrlimit"
                resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
                stage = "payload_identity"
                _drop_identity(payload_uid, payload_gid)
                stage = "payload_no_new_privileges"
                _set_no_new_privileges()
                stage = "payload_capabilities"
                _drop_capabilities()
                assert payload_workspace is not None
                stage = "payload_workspace_chdir"
                os.chdir(payload_workspace)
                # The diagnostic channel belongs to the helper, not the
                # Adapter.  CLOEXEC keeps the payload from discovering or
                # writing the supervisor's private control channel.
                stage = "payload_fd_setup"
                os.set_inheritable(diagnostic_fd, False)
                stage = "payload_environment"
                adapter_environment = {
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("DLR_SANDBOX_")
                }
                # These are probe-only namespace assertions.  Production
                # attempts do not provide DLR_PREFLIGHT_* values.
                adapter_environment.update(
                    {
                        key: value
                        for key, value in os.environ.items()
                        if key.startswith("DLR_PREFLIGHT_")
                    }
                )
                stage = "payload_exec"
                os.execvpe(
                    command[0],
                    _replace_workspace(command, host_workspace, payload_workspace),
                    adapter_environment,
                )
            except OSError as error:
                _write_helper_diagnostic(
                    diagnostic_fd,
                    stage,
                    kind="os_error",
                    error_number=error.errno or errno.EIO,
                )
                os._exit(126)
            except BaseException:
                _write_helper_diagnostic(
                    diagnostic_fd,
                    stage,
                    kind="exception",
                    error_number=0,
                )
                os._exit(126)
        os.close(payload_gate_read)
        try:
            stage = "attempt_membership"
            if not (attempt_cgroup / "cgroup.procs").is_file():
                raise OSError(errno.ENOENT, "Attempt cgroup is unavailable")
            _write(attempt_cgroup / "cgroup.procs", f"{child_pid}\n")
            if not _pid_is_in_child(child_pid, attempt_cgroup):
                raise OSError(errno.EPERM, "payload cgroup membership readback failed")

            # The Adapter inherits no cgroup control plane, even though the
            # private namespace starts from the Worker mount namespace. The
            # configured bind target is validated above and must be
            # overmounted only after the payload has entered Attempt; hiding
            # it earlier would also hide the cgroup.procs needed for that
            # migration. The payload is still blocked on the gate, so it
            # cannot execute before both overmounts are complete.
            cgroup_mount = Path("/sys/fs/cgroup")
            if not cgroup_mount.is_dir():
                raise OSError(errno.ENOENT, "canonical cgroup mount is unavailable")
            stage = "cgroup_root_hide"
            _mount(
                "tmpfs",
                str(cgroup_mount),
                "tmpfs",
                MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC,
                "size=4m,mode=0555",
            )
            hidden_mounts.append(str(cgroup_mount))
            # A target below the canonical mount is already hidden by the
            # root overmount. The normal Compose target (/run/dlr-cgroup) is
            # separate and must receive its own exact overmount.
            if exact_cgroup_mount != cgroup_mount and not exact_cgroup_mount.is_relative_to(
                cgroup_mount
            ):
                stage = "exact_cgroup_hide"
                _mount(
                    "tmpfs",
                    str(exact_cgroup_mount),
                    "tmpfs",
                    MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC,
                    "size=4m,mode=0555",
                )
                hidden_mounts.append(str(exact_cgroup_mount))
            secrets_mount = Path("/run/secrets")
            if secrets_mount.is_dir():
                stage = "secrets_hide"
                _mount(
                    "tmpfs",
                    str(secrets_mount),
                    "tmpfs",
                    MS_NOSUID | MS_NODEV | MS_NOEXEC,
                    "size=1m,mode=0555",
                )
                hidden_mounts.append(str(secrets_mount))
            os.write(payload_gate_write, b"1")
        except BaseException:
            with suppress(OSError):
                os.kill(child_pid, signal.SIGKILL)
            with suppress(OSError):
                os.waitpid(child_pid, 0)
            raise
        finally:
            with suppress(OSError):
                os.close(payload_gate_write)
        stage = "payload_wait"
        _, status = os.waitpid(child_pid, 0)
        if os.WIFEXITED(status):
            exit_code = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            exit_code = 128 + os.WTERMSIG(status)
        else:
            exit_code = 125
        stage = "output_copy"
        assert workspace_relative is not None
        output = workspace_relative / "output.json"
        output_fd = -1
        try:
            with _filesystem_identity(payload_uid, payload_gid):
                output_info = os.lstat(output)
                if stat.S_ISREG(output_info.st_mode):
                    output_fd = os.open(output, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            output_fd = -1
        if output_fd >= 0:
            staged_output = host_workspace / "output.json"
            temporary = host_workspace / ".output.json.sandbox.tmp"
            try:
                with (
                    os.fdopen(output_fd, "rb") as source_stream,
                    temporary.open("wb") as destination_stream,
                ):
                    output_fd = -1
                    shutil.copyfileobj(source_stream, destination_stream)
                os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
                os.replace(temporary, staged_output)
            finally:
                if output_fd >= 0:
                    os.close(output_fd)
        return exit_code
    except OSError as error:
        _write_helper_diagnostic(
            diagnostic_fd,
            stage,
            kind="os_error",
            error_number=error.errno or errno.EIO,
        )
        print(
            f"DLR_SANDBOX_HELPER_ERROR:{stage}:errno={error.errno or errno.EIO}",
            flush=True,
        )
        return 125
    except BaseException:
        _write_helper_diagnostic(
            diagnostic_fd,
            stage,
            kind="exception",
            error_number=0,
        )
        print(f"DLR_SANDBOX_HELPER_ERROR:{stage}:exception", flush=True)
        return 125
    finally:
        for target in reversed(hidden_mounts):
            with suppress(OSError):
                _unmount(target)
        if mounted_workspace_tmpfs and workspace_mount_relative is not None:
            with suppress(OSError):
                if payload_outer_fd is not None:
                    os.fchdir(payload_outer_fd)
                _unmount(workspace_mount_relative.name)
        if inner_fd is not None:
            with suppress(OSError):
                os.close(inner_fd)
        if payload_outer_fd is not None:
            with suppress(OSError):
                os.close(payload_outer_fd)
        if mounted_payload_bind and payload_root is not None:
            with suppress(OSError):
                if payload_parent_fd is not None:
                    os.fchdir(payload_parent_fd)
                _unmount(payload_root.name)
        if created_payload_root and payload_root is not None:
            with suppress(OSError):
                payload_root.rmdir()
        if mounted_tmpfs:
            with suppress(OSError):
                if mount_parent_fd is not None:
                    os.fchdir(mount_parent_fd)
                _unmount(mount_root.name)
        if outer_fd is not None:
            with suppress(OSError):
                os.close(outer_fd)
        if mount_parent_fd is not None:
            with suppress(OSError):
                os.close(mount_parent_fd)
        if payload_parent_fd is not None:
            with suppress(OSError):
                os.close(payload_parent_fd)
        if original_cwd_fd is not None:
            with suppress(OSError):
                os.fchdir(original_cwd_fd)
            with suppress(OSError):
                os.close(original_cwd_fd)
        with suppress(OSError):
            os.close(diagnostic_fd)


def _helper_main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--sandbox-child", action="store_true")
    parser.add_argument("--command", required=True)
    parser.add_argument("--host-workspace", required=True)
    parser.add_argument("--mount-root", required=True)
    parser.add_argument("--nofile", required=True, type=int)
    parser.add_argument("--tmp-bytes", required=True, type=int)
    parser.add_argument("--payload-uid", required=True, type=int)
    parser.add_argument("--payload-gid", required=True, type=int)
    parser.add_argument("--hidden-cgroup-path", default="")
    parser.add_argument("--attempt-cgroup", required=True)
    parser.add_argument("--ready-fd", required=True, type=int)
    parser.add_argument("--diagnostic-fd", required=True, type=int)
    args = parser.parse_args(argv)
    if not args.sandbox_child:
        return 125
    try:
        command = json.loads(base64.urlsafe_b64decode(args.command.encode()).decode())
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) for item in command)
        ):
            _write_helper_diagnostic(
                args.diagnostic_fd,
                "helper_parse",
                kind="exception",
                error_number=0,
            )
            return 125
        return _helper_child(
            command,
            Path(args.host_workspace),
            Path(args.mount_root),
            args.nofile,
            args.tmp_bytes,
            args.payload_uid,
            args.payload_gid,
            args.hidden_cgroup_path or None,
            Path(args.attempt_cgroup),
            args.ready_fd,
            args.diagnostic_fd,
        )
    except OSError as error:
        _write_helper_diagnostic(
            args.diagnostic_fd,
            "helper_parse",
            kind="os_error",
            error_number=error.errno or errno.EIO,
        )
        return 125
    except BaseException:
        _write_helper_diagnostic(
            args.diagnostic_fd,
            "helper_parse",
            kind="exception",
            error_number=0,
        )
        return 125


class AttemptSandbox:
    """One task-owned cgroup/tmpfs/namespace lifecycle."""

    def __init__(
        self,
        config: SandboxConfig,
        limits: ResourceLimits,
        *,
        execution_id: int,
        attempt_id: int,
        workspace: Path,
        recovery_root: Path,
        cgroup_name: str | None = None,
    ) -> None:
        self.config = config
        self.limits = limits
        self.parent = validate_delegated_parent(config)
        self.cgroup_name = cgroup_name or _child_name(execution_id, attempt_id)
        self.execution_id = execution_id
        self.attempt_id = attempt_id
        self.cgroup = _mkdir_child(self.parent, self.cgroup_name)
        self.workspace = workspace
        self.mount_root = (workspace.parent / ".dlr-sandbox-mount").resolve()
        self.recovery_root = recovery_root
        try:
            self._limits_readback = _configure_child(self.cgroup, limits)
        except SandboxError:
            try:
                _write(self.cgroup / "cgroup.kill", "1\n")
                if _is_empty(self.cgroup / "cgroup.procs"):
                    self.cgroup.rmdir()
                else:
                    self._write_recovery_marker()
            except (OSError, SandboxError):
                self._write_recovery_marker()
            raise
        self._process: subprocess.Popen[bytes] | None = None
        self._payload_pid: int | None = None
        self._diagnostic_read_fd: int | None = None
        self._killed = False
        self._unmounted = False

    @classmethod
    def for_preflight(
        cls,
        config: SandboxConfig,
        *,
        workspace: Path,
        recovery_root: Path,
        tmp_bytes: int = 1 << 20,
        cgroup_name: str | None = None,
    ) -> AttemptSandbox:
        limits = ResourceLimits(
            cpu_cores=min(config.cpu_cores, 1.0),
            memory_bytes=min(config.memory_bytes, 64 * 1024 * 1024),
            pids=min(config.pids, 64),
            tmp_bytes=tmp_bytes,
            nofile=min(config.nofile, 64),
            execution_timeout_seconds=min(config.execution_timeout_seconds, 30),
        )
        return cls(
            config,
            limits,
            execution_id=1,
            attempt_id=1,
            workspace=workspace,
            recovery_root=recovery_root,
            cgroup_name=cgroup_name or f"dlr-preflight-{uuid.uuid4().hex}",
        )

    @property
    def limits_readback(self) -> dict[str, str]:
        return dict(self._limits_readback)

    @property
    def payload_pid(self) -> int | None:
        """Host PID of the payload child while it remains in Attempt."""

        return self._payload_pid

    def read_helper_diagnostic(self) -> HelperDiagnostic | None:
        """Read the helper's private diagnostic after its process exits."""

        if self._diagnostic_read_fd is None:
            return None
        if self._process is not None and self._process.poll() is None:
            return None
        read_fd = self._diagnostic_read_fd
        self._diagnostic_read_fd = None
        chunks: list[bytes] = []
        try:
            os.set_blocking(read_fd, False)
            while True:
                try:
                    chunk = os.read(read_fd, 4096)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError:
            return None
        finally:
            with suppress(OSError):
                os.close(read_fd)
        if not chunks:
            return None
        return _parse_helper_diagnostic(b"".join(chunks))

    def start(
        self,
        command: list[str],
        *,
        stdout: Any,
        environment: Mapping[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        try:
            self.mount_root.mkdir(mode=0o700)
        except FileExistsError as error:
            raise SandboxError("sandbox_mount_already_exists") from error
        except OSError as error:
            raise SandboxError("sandbox_mount_prepare_failed") from error
        read_fd, write_fd = os.pipe()
        diagnostic_read_fd, diagnostic_write_fd = os.pipe()
        os.set_inheritable(read_fd, True)
        os.set_inheritable(diagnostic_write_fd, True)
        encoded_command = base64.urlsafe_b64encode(
            json.dumps(command, separators=(",", ":")).encode()
        ).decode()
        helper_environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        }
        if environment:
            helper_environment.update({str(key): str(value) for key, value in environment.items()})
        for key, value in os.environ.items():
            if key.startswith("DLR_SECRET_"):
                helper_environment[key] = value
        helper_environment["DLR_SANDBOX_ATTEMPT_NAME"] = self.cgroup_name
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__)),
                    "--sandbox-child",
                    "--command",
                    encoded_command,
                    "--host-workspace",
                    str(self.workspace),
                    "--mount-root",
                    str(self.mount_root),
                    "--nofile",
                    str(self.limits.nofile),
                    "--tmp-bytes",
                    str(self.limits.tmp_bytes),
                    "--payload-uid",
                    str(self.config.payload_uid),
                    "--payload-gid",
                    str(self.config.payload_gid),
                    "--hidden-cgroup-path",
                    str(self.parent),
                    "--attempt-cgroup",
                    str(self.cgroup),
                    "--ready-fd",
                    str(read_fd),
                    "--diagnostic-fd",
                    str(diagnostic_write_fd),
                ],
                stdout=stdout,
                stderr=subprocess.STDOUT,
                cwd=str(self.workspace),
                env=helper_environment,
                pass_fds=(read_fd, diagnostic_write_fd),
                start_new_session=True,
            )
        except OSError as error:
            for descriptor in (write_fd, diagnostic_read_fd, diagnostic_write_fd):
                with suppress(OSError):
                    os.close(descriptor)
            raise SandboxError("sandbox_process_start_failed") from error
        finally:
            os.close(read_fd)
            with suppress(OSError):
                os.close(diagnostic_write_fd)
        self._diagnostic_read_fd = diagnostic_read_fd
        self._process = process
        try:
            os.write(write_fd, b"1")
        except (OSError, SandboxError) as error:
            with suppress(OSError):
                os.close(write_fd)
            self.kill(process)
            if isinstance(error, SandboxError):
                raise
            raise SandboxError("sandbox_process_membership_failed") from error
        os.close(write_fd)
        # The helper remains in the Worker cgroup.  The child moves itself
        # into the Attempt; preflight uses this best-effort readback while
        # short-lived production payloads may already have exited.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and process.poll() is None:
            try:
                pids = [int(value) for value in _read(self.cgroup / "cgroup.procs").split()]
            except (SandboxError, ValueError):
                pids = []
            if pids:
                self._payload_pid = pids[0]
                break
            time.sleep(0.01)
        return process

    def kill(self, process: subprocess.Popen[bytes] | None = None) -> None:
        target = process or self._process
        try:
            _write(self.cgroup / "cgroup.kill", "1\n")
            self._killed = True
        except SandboxError:
            if target is not None and target.poll() is None:
                with suppress(OSError):
                    os.killpg(os.getpgid(target.pid), signal.SIGKILL)
            raise

    def resource_error_code(self) -> str | None:
        """Translate kernel cgroup event counters to stable result codes."""

        def counters(filename: str) -> dict[str, int]:
            try:
                values: dict[str, int] = {}
                for line in (self.cgroup / filename).read_text(encoding="ascii").splitlines():
                    key, separator, raw = line.partition(" ")
                    if separator:
                        values[key] = int(raw.strip())
                return values
            except (OSError, ValueError):
                return {}

        memory = counters("memory.events")
        if memory.get("oom_kill", 0) or memory.get("oom", 0):
            return "resource_exceeded_memory"
        pids = counters("pids.events")
        if pids.get("max", 0):
            return "resource_exceeded_pids"
        return None

    def _write_recovery_marker(self) -> None:
        try:
            self.recovery_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            marker = self.recovery_root / f"sandbox-{self.cgroup_name}.json"
            descriptor = os.open(
                marker,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(
                    json.dumps(
                        {
                            "cgroup_name": self.cgroup_name,
                            "execution_id": self.execution_id,
                            "mount_name": self.mount_root.name,
                            "mount_path": str(self.mount_root),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass
        except OSError:
            logger.warning("sandbox recovery marker could not be persisted")

    def cleanup(self, *, wait_seconds: float = 2.0) -> CleanupResult:
        killed = self._killed
        if self._process is not None and self._process.poll() is None:
            try:
                self.kill(self._process)
                killed = True
            except SandboxError:
                self._write_recovery_marker()
                return CleanupResult(
                    "deferred",
                    "sandbox_cleanup_failed",
                    self.cgroup_name,
                    self.mount_root.name,
                    killed=killed,
                    residue=True,
                )
        if self._process is not None:
            try:
                self._process.wait(timeout=wait_seconds)
            except subprocess.TimeoutExpired:
                self._write_recovery_marker()
                return CleanupResult(
                    "deferred",
                    "sandbox_cleanup_failed",
                    self.cgroup_name,
                    self.mount_root.name,
                    killed=killed,
                    residue=True,
                )
        try:
            for _ in range(20):
                if _is_empty(self.cgroup / "cgroup.procs"):
                    break
                time.sleep(0.05)
            else:
                raise SandboxError("sandbox_cleanup_failed")
            self.cgroup.rmdir()
            if self.mount_root.exists():
                shutil.rmtree(self.mount_root)
            self._unmounted = True
        except (OSError, SandboxError):
            self._write_recovery_marker()
            return CleanupResult(
                "deferred",
                "sandbox_cleanup_failed",
                self.cgroup_name,
                self.mount_root.name,
                killed=killed,
                unmounted=self._unmounted,
                residue=True,
            )
        return CleanupResult(
            "completed",
            None,
            self.cgroup_name,
            self.mount_root.name,
            killed=killed,
            unmounted=True,
            residue=False,
        )


def _profile_from_preflight(config: SandboxConfig) -> ResourceLimits:
    return ResourceLimits(
        cpu_cores=min(config.cpu_cores, 1.0),
        memory_bytes=min(config.memory_bytes, 64 * 1024 * 1024),
        pids=min(config.pids, 64),
        tmp_bytes=1 << 20,
        nofile=min(config.nofile, 64),
        execution_timeout_seconds=min(config.execution_timeout_seconds, 30),
    )


def run_preflight(
    config: SandboxConfig, *, recovery_root: Path, runtime_root: Path | None = None
) -> dict[str, Any]:
    """Run one disposable real probe and return a sanitized capability receipt."""

    capabilities: dict[str, bool] = {
        "cgroup_v2": False,
        "mount_namespace": False,
        "pid_namespace": False,
        "memory_hard_limit": False,
        "pids_hard_limit": False,
        "tmpfs_hard_limit": False,
        "bounded_output": True,
        "preflight_passed": False,
        "cpu_hard_limit": False,
        "swap_hard_limit": False,
        "nofile_hard_limit": False,
        "no_new_privileges": False,
        "cgroup_kill": False,
        "adapter_control_plane_hidden": False,
        "adapter_mount_blocked": False,
        "sandbox_cleanup": False,
    }
    try:
        parent = validate_delegated_parent(config)
    except SandboxError as error:
        failure_details = {
            "status": "failed",
            "error_code": error.code,
            "capabilities": dict(capabilities),
        }
        return {"capabilities": capabilities, "details": failure_details}
    capabilities["cgroup_v2"] = True
    # Keep the disposable mount/workspace below the Worker-owned runtime root
    # so a recovery marker can never authorize deletion of an arbitrary host
    # path.  Tests without an explicit runtime root use the recovery journal's
    # parent, which is their task-owned temporary root.
    preflight_root = (runtime_root or recovery_root.parent).resolve()
    preflight_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    preflight_name = f"dlr-preflight-{uuid.uuid4().hex}"
    temp_root = preflight_root / preflight_name
    temp_root.mkdir(mode=0o700)
    workspace = temp_root / "dlr-preflight-workspace"
    workspace.mkdir(mode=0o700)
    output_log = workspace / ".probe.log"
    attempt: AttemptSandbox | None = None
    process: subprocess.Popen[bytes] | None = None
    helper_diagnostic: HelperDiagnostic | None = None
    details: dict[str, Any] = {
        "configured_cgroup_path": str(parent),
        "parent_basename": parent.name,
        "agent_pid": os.getpid(),
        "agent_cgroup": _pid_cgroup(os.getpid()),
        "limits": _profile_from_preflight(config).as_dict(),
        "worker_cgroup_management": {
            "parent_controllers_read": True,
            "child_limit_write_read": False,
        },
        "helper_diagnostic": None,
        "workspace_residue": False,
    }
    probe = (
        "import ctypes,errno,json,os,pathlib,resource,time\n"
        "def field(name):\n"
        "    values={}\n"
        "    for line in pathlib.Path('/proc/self/status').read_text().splitlines():\n"
        "        key,sep,value=line.partition(':')\n"
        "        if sep: values[key.strip()]=value.strip().split()[0] if value.strip() else ''\n"
        "    return values.get(name,'')\n"
        "expected=os.environ['DLR_PREFLIGHT_ATTEMPT_NAME']\n"
        "hidden_target=pathlib.Path(os.environ['DLR_PREFLIGHT_CGROUP_PATH'])\n"
        "def inaccessible(root):\n"
        "    result={'read_blocked':False,'write_blocked':False}\n"
        "    try: (root/'cgroup.controllers').read_text()\n"
        "    except OSError: result['read_blocked']=True\n"
        "    else: raise AssertionError('cgroup control read visible')\n"
        "    try: (root/'dlr-write-probe').write_text('x')\n"
        "    except OSError: result['write_blocked']=True\n"
        "    else: raise AssertionError('cgroup control write visible')\n"
        "    return result\n"
        "def mount_blocked():\n"
        "    target=pathlib.Path('adapter-mount-probe')\n"
        "    target.mkdir()\n"
        "    libc=ctypes.CDLL(None,use_errno=True)\n"
        "    mount=libc.mount\n"
        "    mount.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_char_p,"
        "ctypes.c_ulong,ctypes.c_char_p]\n"
        "    mount.restype=ctypes.c_int\n"
        "    unmount=libc.umount2\n"
        "    unmount.argtypes=[ctypes.c_char_p,ctypes.c_int]\n"
        "    unmount.restype=ctypes.c_int\n"
        "    target_bytes=os.fsencode(str(target))\n"
        "    result=mount(b'tmpfs',target_bytes,b'tmpfs',0,b'size=1m')\n"
        "    mount_errno=ctypes.get_errno()\n"
        "    if result==0:\n"
        "        unmount(target_bytes,2)\n"
        "        raise AssertionError('Adapter mount unexpectedly succeeded')\n"
        "    assert mount_errno in (errno.EPERM,errno.EACCES)\n"
        "    return {'blocked':True,'errno':mount_errno}\n"
        "cg=pathlib.Path('/proc/self/cgroup').read_text()\n"
        "assert expected in cg\n"
        "assert os.getpid()==1\n"
        "assert os.getuid()==int(os.environ['DLR_PREFLIGHT_PAYLOAD_UID'])\n"
        "assert os.getgid()==int(os.environ['DLR_PREFLIGHT_PAYLOAD_GID'])\n"
        "assert os.readlink('/proc/self/ns/mnt') != os.environ['DLR_PREFLIGHT_PARENT_MNT']\n"
        "assert os.readlink('/proc/self/ns/pid') != os.environ['DLR_PREFLIGHT_PARENT_PID']\n"
        "assert resource.getrlimit(resource.RLIMIT_NOFILE)==(64,64)\n"
        "assert field('NoNewPrivs')=='1'\n"
        "cap_prm=int(field('CapPrm') or '0',16)\n"
        "cap_eff=int(field('CapEff') or '0',16)\n"
        "cap_inh=int(field('CapInh') or '0',16)\n"
        "cap_bnd=int(field('CapBnd') or '0',16)\n"
        "cap_amb=int(field('CapAmb') or '0',16)\n"
        f"allowed_caps={SUPERVISOR_CAPABILITY_MASK}\n"
        "assert cap_prm==0\n"
        "assert cap_eff==0\n"
        "assert cap_inh==0\n"
        "assert cap_amb==0\n"
        "assert cap_bnd & ~allowed_caps == 0\n"
        "adapter_mount=mount_blocked()\n"
        "hidden_paths={'/sys/fs/cgroup':inaccessible(pathlib.Path('/sys/fs/cgroup')),str(hidden_target):inaccessible(hidden_target)}\n"
        "pathlib.Path('output.json').write_text(json.dumps({'ok':True,'identity':{'uid':os.getuid(),'gid':os.getgid(),'CapPrm':field('CapPrm'),'CapEff':field('CapEff'),'CapInh':field('CapInh'),'CapBnd':field('CapBnd'),'CapAmb':field('CapAmb'),'NoNewPrivs':field('NoNewPrivs')},'adapter_mount':adapter_mount,'hidden_cgroup_paths':hidden_paths}))\n"
        "saw=False\n"
        "for index in range(1,5):\n"
        "    try:\n"
        "        with open('fill-'+str(index),'wb') as stream:\n"
        "            stream.write(b'x'*(768*1024)); stream.flush()\n"
        "    except OSError as error:\n"
        "        if error.errno != errno.ENOSPC: raise\n"
        "        saw=True; break\n"
        "assert saw\n"
        "print('DLR_SANDBOX_PREFLIGHT_READY',flush=True)\n"
        "time.sleep(60)\n"
    )
    try:
        limits = _profile_from_preflight(config)
        attempt = AttemptSandbox.for_preflight(
            config,
            workspace=workspace,
            recovery_root=recovery_root,
            tmp_bytes=limits.tmp_bytes,
            cgroup_name=preflight_name,
        )
        details["limits_readback"] = attempt.limits_readback
        details["worker_cgroup_management"]["child_limit_write_read"] = attempt.limits_readback == {
            "cpu.max": "100000 100000",
            "memory.max": "67108864",
            "memory.swap.max": "0",
            "pids.max": "64",
        }
        capabilities["cpu_hard_limit"] = attempt.limits_readback["cpu.max"] == "100000 100000"
        capabilities["memory_hard_limit"] = attempt.limits_readback["memory.max"] == str(
            limits.memory_bytes
        )
        capabilities["swap_hard_limit"] = attempt.limits_readback["memory.swap.max"] == "0"
        capabilities["pids_hard_limit"] = attempt.limits_readback["pids.max"] == str(limits.pids)
        parent_mnt = os.readlink("/proc/self/ns/mnt")
        parent_pid = os.readlink("/proc/self/ns/pid")
        env = {
            "DLR_PREFLIGHT_ATTEMPT_NAME": attempt.cgroup_name,
            "DLR_PREFLIGHT_PARENT_MNT": parent_mnt,
            "DLR_PREFLIGHT_PARENT_PID": parent_pid,
            "DLR_PREFLIGHT_CGROUP_PATH": str(config.cgroup_path or ""),
            "DLR_PREFLIGHT_PAYLOAD_UID": str(config.payload_uid),
            "DLR_PREFLIGHT_PAYLOAD_GID": str(config.payload_gid),
        }
        with output_log.open("wb") as stream:
            process = attempt.start([sys.executable, "-c", probe], stdout=stream, environment=env)
            details["cgroup_name"] = attempt.cgroup_name
            details["helper_pid"] = process.pid
            details["helper_outside_attempt"] = not _pid_is_in_child(process.pid, attempt.cgroup)
            details["process_pid"] = attempt.payload_pid
            deadline = time.monotonic() + 10
            ready = False
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                probe_log = output_log.read_text(encoding="utf-8", errors="replace")
                if "DLR_SANDBOX_PREFLIGHT_READY" in probe_log:
                    ready = True
                    break
                time.sleep(0.05)
            if not ready:
                helper_diagnostic = attempt.read_helper_diagnostic()
                if helper_diagnostic is not None:
                    details["helper_diagnostic"] = helper_diagnostic.as_dict()
                    raise SandboxError(helper_diagnostic.error_code)
                raise SandboxError("sandbox_preflight_probe_failed")
        capabilities["mount_namespace"] = True
        capabilities["pid_namespace"] = True
        capabilities["tmpfs_hard_limit"] = True
        capabilities["nofile_hard_limit"] = True
        capabilities["no_new_privileges"] = True
        details["agent_outside_attempt"] = not _pid_is_in_child(os.getpid(), attempt.cgroup)
        details["process_pid"] = attempt.payload_pid
        details["probe_in_attempt"] = attempt.payload_pid is not None and _pid_is_in_child(
            attempt.payload_pid, attempt.cgroup
        )
        if (
            not details["agent_outside_attempt"]
            or not details.get("helper_outside_attempt", False)
            or not details["probe_in_attempt"]
        ):
            raise SandboxError("sandbox_process_membership_failed")
        attempt.kill(process)
        for _ in range(20):
            if process.poll() is not None and _is_empty(attempt.cgroup / "cgroup.procs"):
                break
            time.sleep(0.05)
        details["process_exited_after_kill"] = process.poll() is not None
        details["child_empty_after_kill"] = _is_empty(attempt.cgroup / "cgroup.procs")
        capabilities["cgroup_kill"] = process.poll() is not None and _is_empty(
            attempt.cgroup / "cgroup.procs"
        )
    except (SandboxError, OSError, AssertionError) as error:
        details["error_code"] = (
            error.code if isinstance(error, SandboxError) else "sandbox_preflight_probe_failed"
        )
    finally:
        if attempt is not None:
            cleanup = attempt.cleanup()
            helper_diagnostic = attempt.read_helper_diagnostic()
            if helper_diagnostic is not None:
                details["helper_diagnostic"] = helper_diagnostic.as_dict()
            details["cleanup"] = {
                "status": cleanup.status,
                "error_code": cleanup.error_code,
                "cgroup_name": cleanup.cgroup_name,
                "mount_name": cleanup.mount_name,
                "residue": cleanup.residue,
            }
            capabilities["sandbox_cleanup"] = cleanup.status == "completed" and not cleanup.residue
            try:
                receipt = json.loads((workspace / "output.json").read_text(encoding="ascii"))
            except (OSError, UnicodeError, TypeError, ValueError):
                receipt = None
            if isinstance(receipt, dict):
                details["adapter_identity"] = receipt.get("identity")
                hidden_paths = receipt.get("hidden_cgroup_paths")
                details["adapter_hidden_cgroup_paths"] = hidden_paths
                capabilities["adapter_control_plane_hidden"] = (
                    isinstance(hidden_paths, dict)
                    and set(hidden_paths) == {"/sys/fs/cgroup", str(config.cgroup_path)}
                    and all(
                        isinstance(value, dict)
                        and value.get("read_blocked") is True
                        and value.get("write_blocked") is True
                        for value in hidden_paths.values()
                    )
                )
                adapter_mount = receipt.get("adapter_mount")
                details["adapter_mount"] = adapter_mount
                capabilities["adapter_mount_blocked"] = (
                    isinstance(adapter_mount, dict)
                    and adapter_mount.get("blocked") is True
                    and adapter_mount.get("errno") in {errno.EPERM, errno.EACCES}
                )
            if helper_diagnostic is not None and details.get("status") != "passed":
                details["error_code"] = helper_diagnostic.error_code
        try:
            shutil.rmtree(temp_root)
        except OSError:
            details["workspace_residue"] = True
    required: tuple[str, ...] = (
        "cgroup_v2",
        "mount_namespace",
        "pid_namespace",
        "memory_hard_limit",
        "pids_hard_limit",
        "tmpfs_hard_limit",
        "bounded_output",
        "preflight_passed",
    )
    capabilities["sandbox_cleanup"] = capabilities["sandbox_cleanup"] and not bool(
        details.get("workspace_residue")
    )
    required = required + (
        "cpu_hard_limit",
        "swap_hard_limit",
        "nofile_hard_limit",
        "no_new_privileges",
        "cgroup_kill",
        "adapter_control_plane_hidden",
        "adapter_mount_blocked",
        "sandbox_cleanup",
    )
    capabilities["preflight_passed"] = (
        all(capabilities[key] for key in required if key != "preflight_passed")
        and details.get("agent_outside_attempt") is True
        and details.get("helper_outside_attempt") is True
        and details.get("probe_in_attempt") is True
    )
    details["capabilities"] = dict(capabilities)
    details["status"] = "passed" if capabilities["preflight_passed"] else "failed"
    return {"capabilities": capabilities, "details": details}


def _derived_recovery_mount(runtime_root: Path, name: str, execution_id: int) -> Path:
    """Derive the only mount path a task-owned recovery marker may name."""

    try:
        root = runtime_root.resolve(strict=True)
    except OSError as error:
        raise ValueError from error
    if not root.is_dir():
        raise ValueError
    if name.startswith("attempt-"):
        parts = name.split("-", 2)
        if len(parts) != 3 or int(parts[1]) != execution_id:
            raise ValueError
        return root / "workspaces" / f"attempt-{int(parts[2])}" / ".dlr-sandbox-mount"
    if name.startswith("dlr-preflight-") and execution_id == 1:
        return root / name / ".dlr-sandbox-mount"
    raise ValueError


def _validated_recovery_mount(mount_path: str, runtime_root: Path, expected_mount: Path) -> Path:
    """Return a marker mount path only when it equals the derived task path."""

    if not os.path.isabs(mount_path):
        raise ValueError
    try:
        root = runtime_root.resolve(strict=True)
        mount = Path(mount_path)
        resolved_mount = mount.resolve(strict=False)
    except OSError as error:
        raise ValueError from error
    if not root.is_dir() or not resolved_mount.is_relative_to(root):
        raise ValueError
    if mount != expected_mount:
        raise ValueError
    if mount.name != ".dlr-sandbox-mount":
        raise ValueError
    if mount.exists():
        info = mount.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError
    return mount


def recover(config: SandboxConfig, recovery_root: Path, *, runtime_root: Path) -> dict[str, int]:
    """Recover only valid task-owned sandbox markers."""

    counts = {"inspected": 0, "completed": 0, "retained": 0}
    if not recovery_root.is_dir() or config.cgroup_path is None or sys.platform != "linux":
        return counts
    try:
        recovery_info = recovery_root.lstat()
        runtime_info = runtime_root.lstat()
        if (
            not stat.S_ISDIR(recovery_info.st_mode)
            or stat.S_IMODE(recovery_info.st_mode) != 0o700
            or recovery_info.st_uid != os.geteuid()
            or not stat.S_ISDIR(runtime_info.st_mode)
            or stat.S_IMODE(runtime_info.st_mode) & 0o077
            or runtime_info.st_uid != os.geteuid()
        ):
            return counts
    except OSError:
        return counts
    for marker in sorted(recovery_root.iterdir()):
        if not RECOVERY_NAME_PATTERN.fullmatch(marker.name):
            continue
        counts["inspected"] += 1
        try:
            info = marker.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
            ):
                raise ValueError
            value = json.loads(marker.read_text(encoding="ascii"))
            if not isinstance(value, dict) or set(value) != RECOVERY_FIELDS:
                raise ValueError
            name = value["cgroup_name"]
            if not isinstance(name, str) or not ATTEMPT_NAME_PATTERN.fullmatch(name):
                raise ValueError
            execution_id = value["execution_id"]
            if (
                not isinstance(execution_id, int)
                or isinstance(execution_id, bool)
                or execution_id <= 0
            ):
                raise ValueError
            if name.startswith("attempt-"):
                name_execution_id = int(name.split("-", 2)[1])
                if name_execution_id != execution_id:
                    raise ValueError
            elif execution_id != 1:
                raise ValueError
            if marker.name != f"sandbox-{name}.json":
                raise ValueError
            mount_name = value["mount_name"]
            mount_path = value["mount_path"]
            if (
                not isinstance(mount_name, str)
                or mount_name != ".dlr-sandbox-mount"
                or not isinstance(mount_path, str)
            ):
                raise ValueError
            expected_mount = _derived_recovery_mount(runtime_root, name, execution_id)
            mount = _validated_recovery_mount(mount_path, runtime_root, expected_mount)
            parent = validate_delegated_parent(config)
            child = parent / name
            if child.parent != parent:
                raise ValueError
            if child.exists():
                child_info = child.lstat()
                if not stat.S_ISDIR(child_info.st_mode):
                    raise ValueError
                if not _is_empty(child / "cgroup.procs"):
                    _write(child / "cgroup.kill", "1\n")
                    for _ in range(20):
                        if _is_empty(child / "cgroup.procs"):
                            break
                        time.sleep(0.05)
                child.rmdir()
            if mount.exists() and mount.is_dir():
                shutil.rmtree(mount)
            marker.unlink()
            counts["completed"] += 1
        except (OSError, ValueError, SandboxError):
            counts["retained"] += 1
    return counts


if __name__ == "__main__":
    raise SystemExit(_helper_main(sys.argv[1:]))
