"""Pin the Control cgroup used by bounded v4 Claim preparation."""

from __future__ import annotations

import os
import re
import sys
from contextlib import suppress
from pathlib import Path

from dlr.common.config import settings
from dlr.worker import cgroup_namespace, sandbox


class ClaimEnvelopeUnavailable(Exception):
    """The Control process cannot prove its configured local resource boundary."""


class ClaimEnvelope:
    """Keep the process tree under one finite, no-swap Linux cgroup.

    Deployment must create the process in that cgroup and keep its limits and
    mount stable. This handle does not prove anything about PostgreSQL memory.
    """

    _root = Path("/sys/fs/cgroup")

    def __init__(self, fd: int, *, child_address_space_bytes: int) -> None:
        self._fd = fd
        self._pid = os.getpid()
        self._child_address_space_bytes = child_address_space_bytes
        slots = settings.worker_v4_control_slots
        if type(slots) is not int or slots not in {1, 2}:
            raise ValueError("invalid Control slot limit")
        self._slots = slots
        current = os.fstat(fd)
        namespace = os.stat("/proc/self/ns/cgroup")
        self._identity = (current.st_dev, current.st_ino)
        self._namespace = (namespace.st_dev, namespace.st_ino)
        self._mount = cgroup_namespace._mount_id(fd)
        self._limits: tuple[int, int] | None = None

    @classmethod
    def open(cls, *, child_address_space_bytes: int) -> ClaimEnvelope:
        fd = -1
        try:
            if (
                sys.platform != "linux"
                or type(child_address_space_bytes) is not int
                or not 128 * 1024 * 1024 <= child_address_space_bytes <= 1 << 40
            ):
                raise ValueError
            fd = os.open(cls._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            envelope = cls(fd, child_address_space_bytes=child_address_space_bytes)
            envelope.check()
            return envelope
        except Exception:
            if fd >= 0:
                with suppress(OSError):
                    os.close(fd)
            raise ClaimEnvelopeUnavailable("Claim resources unavailable") from None

    def close(self) -> None:
        if self._pid == os.getpid() and self._fd >= 0:
            fd, self._fd = self._fd, -1
            os.close(fd)

    def _read(self, name: str, limit: int = 128) -> str:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self._fd)
        try:
            raw = os.read(fd, limit + 1)
        finally:
            os.close(fd)
        if not 0 < len(raw) <= limit:
            raise ValueError
        return raw.decode("ascii").strip()

    def _location(self) -> None:
        if self._pid != os.getpid() or self._fd < 0:
            raise ValueError
        if Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines() != ["0::/"]:
            raise ValueError
        namespace = os.stat("/proc/self/ns/cgroup")
        if (namespace.st_dev, namespace.st_ino) != self._namespace:
            raise ValueError
        sandbox.validate_cgroup_mount(self._root, root="/")
        if sandbox._filesystem_magic_fd(self._fd) != sandbox.CGROUP2_SUPER_MAGIC:
            raise ValueError
        if not os.fstatvfs(self._fd).f_flag & os.ST_RDONLY:
            raise ValueError
        if str(self._pid) not in self._read("cgroup.procs", 65_536).splitlines():
            raise ValueError
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            before, separator, after = line.partition(" - ")
            mount_fields, filesystem = before.split(), after.split()
            if not separator or len(mount_fields) < 6 or not filesystem:
                raise ValueError
            if filesystem[0] == "cgroup2" and "ro" not in mount_fields[5].split(","):
                raise ValueError
        fields: dict[str, str] = {}
        keys = {"NoNewPrivs", "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"}
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            name, separator, value = line.partition(":")
            if separator and name in keys:
                if name in fields:
                    raise ValueError
                fields[name] = value.strip()
        if fields.keys() != keys or fields["NoNewPrivs"] != "1":
            raise ValueError
        for name in keys - {"NoNewPrivs"}:
            if re.fullmatch(r"[0-9a-fA-F]{1,32}", fields[name]) is None or (
                int(fields[name], 16) & (1 << 21)
            ):
                raise ValueError
        visible = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            for fd in (self._fd, visible):
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != self._identity or (
                    cgroup_namespace._mount_id(fd) != self._mount
                ):
                    raise ValueError
        finally:
            os.close(visible)

    def check(self) -> None:
        try:
            self._location()
            memory, pids = (self._read(name) for name in ("memory.max", "pids.max"))
            if (
                not memory.isascii()
                or not memory.isdecimal()
                or not pids.isascii()
                or not pids.isdecimal()
                or settings.worker_v4_control_slots != self._slots
                or int(pids) < 1 + self._slots
                or int(memory) < self._slots * self._child_address_space_bytes + 128 * 1024 * 1024
                or self._read("memory.swap.max") != "0"
                or self._read("memory.swap.current") != "0"
                or self._read("memory.swap.max") != "0"
            ):
                raise ValueError
            self._location()
            limits = (int(memory), int(pids))
            if self._limits is not None and limits != self._limits:
                raise ValueError
            self._limits = limits
        except Exception:
            raise ClaimEnvelopeUnavailable("Claim resources unavailable") from None
