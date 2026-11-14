"""Private Linux cgroup supervisor entrypoint and trusted launch owner."""

from __future__ import annotations

import ctypes
import json
import math
import os
import select
import signal
import socket
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

_LIMIT = 512
_STOP_MARGIN = 0.25


@dataclass(frozen=True)
class _SandboxLaunch:
    launch_fd: int
    max_bytes: int
    stdout_fd: int
    diagnostic_fd: int
    usage_fd: int
    workspace: str
    mount_root: str
    nofile: int
    tmp_bytes: int
    output_max_bytes: int
    uid: int
    gid: int
    cgroup_root: str
    cgroup_path: str

    @classmethod
    def parse(cls, args: list[str], *, sock_fd: int, cgroup_fd: int) -> _SandboxLaunch:
        if len(args) != 15 or args[0] != "sandbox":
            raise ValueError("invalid sandbox launch")
        value = cls(
            launch_fd=int(args[1]),
            max_bytes=int(args[2]),
            stdout_fd=int(args[3]),
            diagnostic_fd=int(args[4]),
            usage_fd=int(args[5]),
            workspace=args[6],
            mount_root=args[7],
            nofile=int(args[8]),
            tmp_bytes=int(args[9]),
            output_max_bytes=int(args[10]),
            uid=int(args[11]),
            gid=int(args[12]),
            cgroup_root=args[13],
            cgroup_path=args[14],
        )
        fds = (
            sock_fd,
            cgroup_fd,
            value.launch_fd,
            value.stdout_fd,
            value.diagnostic_fd,
            value.usage_fd,
        )
        if (
            len(set(fds)) != len(fds)
            or any(fd < 3 for fd in fds)
            or min(
                value.max_bytes,
                value.nofile,
                value.tmp_bytes,
                value.output_max_bytes,
                value.uid,
                value.gid,
            )
            <= 0
            or any(
                not Path(path).is_absolute()
                for path in (
                    value.workspace,
                    value.mount_root,
                    value.cgroup_root,
                    value.cgroup_path,
                )
            )
        ):
            raise ValueError("invalid sandbox launch")
        for fd in fds:
            os.fstat(fd)
        return value


class _Sigevent(ctypes.Structure):
    # Linux 64-bit glibc sigevent: sigval, signal, notification, 48-byte union.
    _fields_ = [
        ("value", ctypes.c_void_p),
        ("signo", ctypes.c_int),
        ("notify", ctypes.c_int),
        ("reserved", ctypes.c_int * 12),
    ]


class _Timespec(ctypes.Structure):
    _fields_ = [("seconds", ctypes.c_long), ("nanoseconds", ctypes.c_long)]


class _Itimerspec(ctypes.Structure):
    _fields_ = [("interval", _Timespec), ("value", _Timespec)]


class _KernelKillTimer:
    """A kernel deadline that kills this non-init supervisor even if stopped."""

    def __init__(self, deadline: float) -> None:
        if (
            os.getpid() == 1
            or ctypes.sizeof(ctypes.c_void_p) != 8
            or ctypes.sizeof(ctypes.c_long) != 8
            or ctypes.sizeof(_Sigevent) != 64
        ):
            raise ValueError("supervisor timer unavailable")
        numerator, denominator = deadline.as_integer_ratio()
        nanoseconds = numerator * 1_000_000_000 // denominator
        if nanoseconds <= 0:
            raise ValueError("invalid supervisor deadline")
        libc = ctypes.CDLL(None, use_errno=True)
        libc.timer_create.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(_Sigevent),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        libc.timer_settime.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(_Itimerspec),
            ctypes.c_void_p,
        ]
        libc.timer_delete.argtypes = [ctypes.c_void_p]
        self._libc = libc
        self._timer = ctypes.c_void_p()
        event = _Sigevent(None, signal.SIGKILL, 0, (ctypes.c_int * 12)())  # SIGEV_SIGNAL
        clock = time.CLOCK_BOOTTIME  # type: ignore[attr-defined]
        if libc.timer_create(clock, ctypes.byref(event), ctypes.byref(self._timer)):
            raise OSError(ctypes.get_errno(), "supervisor timer unavailable")
        self._active = True
        value = _Itimerspec(
            _Timespec(0, 0),
            _Timespec(nanoseconds // 1_000_000_000, nanoseconds % 1_000_000_000),
        )
        if libc.timer_settime(self._timer, 1, ctypes.byref(value), None):  # TIMER_ABSTIME
            with suppress(Exception):
                self.delete()
            raise OSError(ctypes.get_errno(), "supervisor timer unavailable")

    def delete(self) -> None:
        if not self._active or self._libc.timer_delete(self._timer):
            raise OSError(ctypes.get_errno(), "supervisor timer unavailable")
        self._active = False


def _clock() -> float:
    value = time.clock_gettime(time.CLOCK_BOOTTIME)  # type: ignore[attr-defined]
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid clock")
    return value


def _send(sock: socket.socket, value: dict[str, object]) -> None:
    data = json.dumps(value, separators=(",", ":")).encode("ascii")
    if len(data) > _LIMIT or sock.send(data) != len(data):
        raise OSError("invalid IPC write")


def _receive(sock: socket.socket) -> dict[str, object]:
    data, _, flags, _ = sock.recvmsg(_LIMIT)
    if not data or flags & socket.MSG_TRUNC:
        raise ValueError("IPC unavailable")
    value = json.loads(data)
    if type(value) is not dict:
        raise ValueError("invalid IPC")
    return value


def _deadline(value: dict[str, object], now: float) -> float:
    if (
        set(value) not in ({"op", "deadline"}, {"op", "deadline", "execution_deadline"})
        or value["op"] != "renew"
    ):
        raise ValueError("invalid renewal")
    deadline = value["deadline"]
    if (
        type(deadline) is not float
        or not math.isfinite(deadline)
        or not now < deadline <= now + 86400
    ):
        raise ValueError("invalid deadline")
    return deadline


def _report_terminal(
    sock: socket.socket,
    drained: bool,
    exit_code: int | None = None,
    *,
    execution_cutoff: bool = False,
) -> bool:
    """Keep IPC open until the parent acknowledges the terminal observation.

    A simultaneous stop can remain unread when the timer wins. On Linux,
    closing a Unix seqpacket socket with unread data can reset its peer and
    discard the terminal message. Drain earlier requests until the explicit
    acknowledgement arrives; lack of acknowledgement remains unknown.
    """
    response: dict[str, object] = {"event": "stopped", "cgroup_drained": drained}
    if exit_code is not None:
        response["exit_code"] = exit_code
    elif drained and execution_cutoff:
        response["stop_reason"] = "execution_cutoff"
    _send(sock, response)
    end = _clock() + 5.0
    poller = select.poll()
    poller.register(sock, select.POLLIN | select.POLLHUP | select.POLLERR)
    for _ in range(16):
        remaining = end - _clock()
        if remaining <= 0:
            return False
        events = poller.poll(max(1, math.ceil(remaining * 1000)))
        if not events:
            return False
        if any(fd == sock.fileno() and flags & select.POLLIN for fd, flags in events):
            if _receive(sock) == {"op": "ack_stopped"}:
                return True
            continue
        return False
    return False


def _members(directory: int) -> set[bytes]:
    fd = os.open("cgroup.procs", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        raw = os.read(fd, 65537)
    finally:
        os.close(fd)
    lines = raw.splitlines()
    if (
        len(raw) > 65536
        or any(not value.isdigit() or value.startswith(b"0") for value in lines)
        or len(set(lines)) != len(lines)
    ):
        raise ValueError("invalid cgroup membership")
    return set(lines)


def _enter_cgroup(directory: int) -> None:
    """Move only this trusted bootstrap into the already-bound cgroup."""
    fd = os.open("cgroup.procs", os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        # Kernel cgroup.procs defines zero as the calling process. Unlike a
        # parent writing a numeric PID, this cannot move a reused process.
        if os.write(fd, b"0\n") != 2:
            raise OSError("short cgroup placement")
    finally:
        os.close(fd)
    if _members(directory) != {str(os.getpid()).encode("ascii")}:
        raise ValueError("supervisor placement unverified")


def _observe_drained(directory: int) -> bool:
    """Observe only this supervisor remains; the parent checks empty on exit."""
    previous = _clock()
    end = previous + 2.0
    while True:
        members = _members(directory)
        now = _clock()
        if now < previous:
            return False
        previous = now
        if members == {str(os.getpid()).encode("ascii")}:
            return True
        if str(os.getpid()).encode("ascii") not in members or now >= end:
            return False
        time.sleep(0.02)


def _arm(timer_fd: int, deadline: float) -> None:
    # Convert the exact float value to integer nanoseconds without a second
    # floating-point rounding, and floor so the kernel deadline is no later.
    numerator, denominator = deadline.as_integer_ratio()
    nanoseconds = numerator * 1_000_000_000 // denominator
    if nanoseconds <= 0:
        raise ValueError("invalid timer deadline")
    os.timerfd_settime_ns(  # type: ignore[attr-defined]
        timer_fd,
        flags=os.TFD_TIMER_ABSTIME,  # type: ignore[attr-defined]
        initial=nanoseconds,
    )


def _guardian_wait(
    control: socket.socket, supervisor_fd: int, helper: socket.socket | None, helper_fd: int
) -> tuple[int, int]:
    """Give supervisor death priority over all other guardian activity."""
    poller = select.poll()
    for fd in (control.fileno(), supervisor_fd, helper_fd):
        if fd >= 0:
            poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    if helper is not None:
        poller.register(helper, select.POLLIN | select.POLLHUP | select.POLLERR)
    events = poller.poll()
    if any(fd == supervisor_fd for fd, _ in events):
        raise ValueError("supervisor exited")
    if any(fd == helper_fd for fd, _ in events):
        raise ValueError("helper exited")
    if len(events) != 1:
        raise ValueError("guardian event ambiguous")
    return events[0]


def _close_except(keep: tuple[int, ...]) -> None:
    retained = sorted(set(keep))
    start = 3
    for fd in retained:
        if fd < 3:
            raise ValueError("invalid retained descriptor")
        os.closerange(start, fd)
        start = fd + 1
    os.closerange(start, max(1 << 20, start))


def _sandbox_guardian(
    control: socket.socket, supervisor_fd: int, directory: int, launch: _SandboxLaunch
) -> None:
    """Own a gated sandbox helper while remaining trusted namespace init."""
    gate_read, gate_write = os.pipe()
    try:
        pid = os.fork()
        if pid == 0:
            try:
                if os.getppid() != 1:
                    os._exit(127)
                os.close(gate_write)
                os.dup2(launch.stdout_fd, 1, inheritable=True)
                os.dup2(launch.stdout_fd, 2, inheritable=True)
                retained = (
                    directory,
                    launch.launch_fd,
                    launch.diagnostic_fd,
                    launch.usage_fd,
                    gate_read,
                )
                _close_except(retained)
                for fd in retained:
                    os.set_inheritable(fd, True)
                os.execve(
                    sys.executable,
                    [
                        sys.executable,
                        "-I",
                        "-S",
                        str(Path(__file__).with_name("sandbox.py")),
                        "--sandbox-child",
                        "--launch-fd",
                        str(launch.launch_fd),
                        "--launch-max-bytes",
                        str(launch.max_bytes),
                        "--host-workspace",
                        launch.workspace,
                        "--mount-root",
                        launch.mount_root,
                        "--nofile",
                        str(launch.nofile),
                        "--tmp-bytes",
                        str(launch.tmp_bytes),
                        "--output-max-bytes",
                        str(launch.output_max_bytes),
                        "--payload-uid",
                        str(launch.uid),
                        "--payload-gid",
                        str(launch.gid),
                        "--hidden-cgroup-path",
                        launch.cgroup_root,
                        "--attempt-cgroup",
                        launch.cgroup_path,
                        "--inherited-cgroup-fd",
                        str(directory),
                        "--ready-fd",
                        str(gate_read),
                        "--diagnostic-fd",
                        str(launch.diagnostic_fd),
                        "--usage-fd",
                        str(launch.usage_fd),
                    ],
                    {},
                )
            except BaseException:
                os._exit(127)
        os.close(gate_read)
        gate_read = -1
        helper_fd = os.pidfd_open(pid, 0)  # type: ignore[attr-defined]
        try:
            if control.send(b"GATED") != 5:
                raise OSError("sandbox gate unavailable")
            fd, flags = _guardian_wait(control, supervisor_fd, None, helper_fd)
            if fd != control.fileno() or flags != select.POLLIN or control.recv(16) != b"GO":
                raise ValueError("sandbox gate unavailable")
            if os.write(gate_write, b"1") != 1:
                raise OSError("sandbox gate unavailable")
            os.close(gate_write)
            gate_write = -1
            if control.send(b"RUNNING") != 7:
                raise OSError("sandbox release unavailable")
            poller = select.poll()
            for fd in (control.fileno(), supervisor_fd, helper_fd):
                poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
            events = poller.poll()
            if any(fd in {control.fileno(), supervisor_fd} for fd, _ in events):
                raise ValueError("sandbox supervision ended")
            if not any(fd == helper_fd for fd, _ in events):
                raise ValueError("sandbox exit unavailable")
            reaped, status = os.waitpid(pid, 0)
            if reaped != pid:
                raise ValueError("sandbox exit unavailable")
            result = os.waitstatus_to_exitcode(status)
            code = result if result >= 0 else 128 - result
            data = f"EXIT:{code}".encode("ascii")
            if not 0 <= code <= 255 or control.send(data) != len(data):
                raise ValueError("sandbox exit unavailable")
        finally:
            os.close(helper_fd)
    finally:
        if gate_read >= 0:
            os.close(gate_read)
        if gate_write >= 0:
            os.close(gate_write)


def _guardian_main(
    control: socket.socket,
    supervisor_fd: int,
    slow: bool,
    directory: int,
    launch: _SandboxLaunch | None,
) -> None:
    """Keep PID namespace init trusted while its gated child runs."""
    if os.getpid() != 1 or os.getuid() != 0 or os.geteuid() != 0:
        raise ValueError("guardian identity unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    parent_signal = ctypes.c_int()
    if (
        libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0  # PR_SET_PDEATHSIG
        or libc.prctl(2, ctypes.byref(parent_signal), 0, 0, 0) != 0  # PR_GET_PDEATHSIG
        or parent_signal.value != signal.SIGKILL
        or libc.prctl(4, 0, 0, 0, 0) != 0  # PR_SET_DUMPABLE
        or libc.prctl(3, 0, 0, 0, 0) != 0  # PR_GET_DUMPABLE
        or libc.prctl(38, 1, 0, 0, 0) != 0  # PR_SET_NO_NEW_PRIVS
        or libc.prctl(39, 0, 0, 0, 0) != 1  # PR_GET_NO_NEW_PRIVS
    ):
        raise ValueError("guardian protection unavailable")
    poller = select.poll()
    poller.register(supervisor_fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    if poller.poll(0):
        raise ValueError("supervisor exited")
    if control.send(b"ARMED") != 5:
        raise OSError("guardian handshake failed")
    fd, flags = _guardian_wait(control, supervisor_fd, None, -1)
    if fd != control.fileno() or flags != select.POLLIN or control.recv(16) != b"SPAWN":
        raise ValueError("guardian placement unconfirmed")

    if launch is None:
        raise ValueError("sandbox launch unavailable")
    _sandbox_guardian(control, supervisor_fd, directory, launch)


def _probe_spawn(
    control: socket.socket,
    directory: int,
    parent_fd: int,
    timer_fd: int,
    slow: bool,
    children: list[int],
    launch: _SandboxLaunch | None = None,
) -> socket.socket:
    """Place a protected namespace init before it starts its gated helper."""
    supervisor, guardian = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    supervisor_fd = -1
    try:
        supervisor_fd = os.pidfd_open(os.getpid(), 0)  # type: ignore[attr-defined]
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.unshare(0x20000000) != 0:  # CLONE_NEWPID; only subsequent children enter it.
            raise OSError(ctypes.get_errno(), "PID namespace unavailable")
        pid = os.fork()
    except BaseException:
        supervisor.close()
        guardian.close()
        if supervisor_fd >= 0:
            os.close(supervisor_fd)
        raise
    if pid == 0:
        try:
            keep: tuple[int, ...] = (guardian.fileno(), supervisor_fd)
            if launch is not None:
                keep += (
                    directory,
                    launch.launch_fd,
                    launch.stdout_fd,
                    launch.diagnostic_fd,
                    launch.usage_fd,
                )
            _close_except(keep)
            _guardian_main(guardian, supervisor_fd, slow, directory, launch)
        except BaseException:
            pass
        os._exit(127)
    children.append(pid)
    guardian.close()
    os.close(supervisor_fd)
    try:
        if not _probe_wait_signal(control, supervisor, parent_fd, timer_fd, b"ARMED"):
            raise ValueError("guardian protection unconfirmed")
        fd = os.open("cgroup.procs", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        try:
            value = f"{pid}\n".encode("ascii")
            if os.write(fd, value) != len(value):
                raise OSError("short cgroup placement")
            os.lseek(fd, 0, os.SEEK_SET)
            if str(pid).encode("ascii") not in os.read(fd, 65536).splitlines():
                raise ValueError("placement unverified")
        finally:
            os.close(fd)
        if supervisor.send(b"SPAWN", socket.MSG_DONTWAIT) != 5:
            raise OSError("guardian spawn gate unavailable")
        return supervisor
    except BaseException:
        supervisor.close()
        raise


def _probe_wait_signal(
    control: socket.socket, helper: socket.socket, parent_fd: int, timer_fd: int, expected: bytes
) -> bool:
    poller = select.poll()
    for fd in (control.fileno(), helper.fileno(), parent_fd, timer_fd):
        poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    while True:
        events = poller.poll()
        if any(fd in {parent_fd, timer_fd, control.fileno()} for fd, _ in events):
            # Consume a queued packet before closing the socket. Leaving the
            # stop unread may reset the connection and discard the stop reply.
            if any(fd == control.fileno() and flags & select.POLLIN for fd, flags in events):
                with suppress(Exception):
                    _receive(control)
            # Any control activity while launch is pending is uncertain;
            # especially stop must win without waiting for the gated child.
            return False
        if any(
            fd == helper.fileno() and flags & (select.POLLHUP | select.POLLERR)
            for fd, flags in events
        ):
            return False
        if any(fd == helper.fileno() and flags & select.POLLIN for fd, flags in events):
            return helper.recv(16) == expected


def _run(
    sock: socket.socket,
    parent_pid: int,
    directory: int,
    children: list[int],
    timers: list[_KernelKillTimer] | None = None,
    launch: _SandboxLaunch | None = None,
    exit_codes: list[int] | None = None,
    stop_reasons: list[str] | None = None,
) -> None:
    parent_fd = timer_fd = -1
    probe: socket.socket | None = None
    timers = [] if timers is None else timers
    try:
        if os.getppid() != parent_pid:
            raise ValueError("parent changed")
        parent_fd = os.pidfd_open(parent_pid, 0)  # type: ignore[attr-defined]
        if os.getppid() != parent_pid:
            raise ValueError("parent changed")
        timer_fd = os.timerfd_create(  # type: ignore[attr-defined]
            time.CLOCK_BOOTTIME,  # type: ignore[attr-defined]
            flags=os.TFD_CLOEXEC,  # type: ignore[attr-defined]
        )
        poller = select.poll()
        poller.register(sock, select.POLLIN | select.POLLHUP | select.POLLERR)
        poller.register(parent_fd, select.POLLIN | select.POLLHUP | select.POLLERR)
        poller.register(timer_fd, select.POLLIN | select.POLLHUP | select.POLLERR)
        # A BOOTTIME timer includes suspend; it does not wake a suspended host.
        previous = _clock()
        deadline = previous + 2.0
        kill_timer = _KernelKillTimer(deadline)
        timers.append(kill_timer)
        _arm(timer_fd, max(0.000000001, deadline - _STOP_MARGIN))
        active = False
        probe_gated = False
        execution_deadline: float | None = None
        pending_renewal: tuple[_KernelKillTimer, float] | None = None
        while True:
            now = _clock()
            if now < previous or now >= deadline:
                return
            previous = now
            events = poller.poll()
            if any(fd == parent_fd for fd, _ in events):
                return
            timer_flags = next((flags for fd, flags in events if fd == timer_fd), 0)
            if timer_flags:
                if (
                    timer_flags & select.POLLIN
                    and execution_deadline is not None
                    and deadline == execution_deadline
                    and probe is not None
                    and not probe_gated
                    and stop_reasons is not None
                    and not any(fd in {sock.fileno(), probe.fileno()} for fd, _ in events)
                ):
                    count = os.read(timer_fd, 8)
                    if len(count) == 8 and int.from_bytes(count, sys.byteorder) > 0:
                        stop_reasons.append("execution_cutoff")
                return
            if any(
                fd == sock.fileno() and flags & (select.POLLHUP | select.POLLERR)
                for fd, flags in events
            ):
                return
            if probe is not None and any(fd == probe.fileno() for fd, _ in events):
                if launch is None or probe_gated:
                    return
                flags = next(flags for fd, flags in events if fd == probe.fileno())
                if not flags & select.POLLIN:
                    return
                data = probe.recv(32)
                if not data.startswith(b"EXIT:") or not data[5:].isdigit():
                    return
                code = int(data[5:])
                if 0 <= code <= 255 and exit_codes is not None:
                    exit_codes.append(code)
                return
            message = _receive(sock)
            if message == {"op": "stop"}:
                return
            if pending_renewal is not None:
                successor, new_deadline = pending_renewal
                now = _clock()
                if (
                    message != {"op": "renew_commit", "deadline": new_deadline}
                    or type(message.get("deadline")) is not float
                    or now < previous
                    or now >= deadline
                ):
                    return
                # The Agent has already accepted the signed renewal locally.
                # Until this exact commit arrived, both old timers remained.
                _arm(timer_fd, max(0.000000001, new_deadline - _STOP_MARGIN))
                after_arm = _clock()
                if after_arm < now or after_arm >= deadline:
                    return
                kill_timer.delete()
                timers.remove(kill_timer)
                kill_timer = successor
                after_delete = _clock()
                if after_delete < after_arm or after_delete >= deadline:
                    return
                previous, deadline = after_delete, new_deadline
                pending_renewal = None
                _send(sock, {"event": "renewed"})
                continue
            if launch is not None and message == {"op": "sandbox_prepare"}:
                if not active or probe is not None:
                    raise ValueError("sandbox unavailable")
                probe = _probe_spawn(sock, directory, parent_fd, timer_fd, False, children, launch)
                if not _probe_wait_signal(sock, probe, parent_fd, timer_fd, b"GATED"):
                    return
                now = _clock()
                if now < previous or now >= deadline:
                    return
                previous = now
                probe_gated = True
                poller.register(probe, select.POLLIN | select.POLLHUP | select.POLLERR)
                _send(sock, {"event": "sandbox_gated"})
                continue
            if launch is not None and message == {"op": "sandbox_release"}:
                if probe is None or not probe_gated:
                    raise ValueError("probe gate unavailable")
                now = _clock()
                if now < previous or now >= deadline:
                    return
                previous = now
                # Timer, parent death or an already-queued stop always wins.
                pending = poller.poll(0)
                if pending:
                    if any(fd == sock.fileno() and flags & select.POLLIN for fd, flags in pending):
                        with suppress(Exception):
                            _receive(sock)
                    return
                if probe.send(b"GO", socket.MSG_DONTWAIT) != 2:
                    raise OSError("probe release failed")
                probe_gated = False
                if not _probe_wait_signal(sock, probe, parent_fd, timer_fd, b"RUNNING"):
                    return
                _send(sock, {"event": "sandbox_released"})
                continue
            now = _clock()
            if now < previous or now >= deadline:
                return
            if "execution_deadline" in message:
                candidate = message["execution_deadline"]
                if (
                    active
                    or type(candidate) is not float
                    or not math.isfinite(candidate)
                    or not now < candidate <= now + 86400
                ):
                    raise ValueError("invalid execution deadline")
                execution_deadline = candidate
            new_deadline = _deadline(message, now)
            if execution_deadline is not None and new_deadline > execution_deadline:
                raise ValueError("execution deadline exceeded")
            # Keep the old kernel timer armed while installing the successor.
            # A paused supervisor cannot extend its authority merely because
            # its ordinary timerfd event loop is not being scheduled.
            successor = _KernelKillTimer(new_deadline)
            timers.append(successor)
            if active:
                # Do not extend the active physical deadline until the Agent
                # has itself committed this renewal within the old deadline.
                pending_renewal = successor, new_deadline
                _send(sock, {"event": "renew_prepared"})
                continue
            _arm(timer_fd, max(0.000000001, new_deadline - _STOP_MARGIN))
            # Rearming resets pending timer events. Check that the old deadline
            # did not expire between the preceding check and the rearm.
            after_arm = _clock()
            if after_arm < now or after_arm >= deadline:
                return
            kill_timer.delete()
            timers.remove(kill_timer)
            kill_timer = successor
            after_delete = _clock()
            if after_delete < after_arm or after_delete >= deadline:
                return
            previous, deadline = after_delete, new_deadline
            _send(sock, {"event": "ready"})
            active = True
    finally:
        if probe is not None:
            probe.close()
        if timer_fd >= 0:
            os.close(timer_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def main() -> int:
    if len(sys.argv) not in (4, 5, 19, 20) or sys.platform != "linux":
        return 2
    try:
        sock_fd, directory, parent_pid = map(int, sys.argv[1:4])
        if min(sock_fd, directory) < 3 or parent_pid <= 0:
            return 2
        sock = socket.socket(fileno=sock_fd)
        os.fstat(directory)
        if os.getppid() != parent_pid:
            return 2
        entered = sys.argv[-1] == "entered"
        options = sys.argv[4:-1] if entered else sys.argv[4:]
        launch = (
            _SandboxLaunch.parse(options, sock_fd=sock_fd, cgroup_fd=directory) if options else None
        )
        if launch is None:
            return 2
        if not entered:
            _enter_cgroup(directory)
            # Reload the interpreter in its target cgroup before accepting
            # authority or forking a namespace init. Bootstrap memory remains
            # part of the Agent's separate resource-accounting obligation.
            os.set_inheritable(sock_fd, True)
            os.set_inheritable(directory, True)
            if launch is not None:
                for fd in (
                    launch.launch_fd,
                    launch.stdout_fd,
                    launch.diagnostic_fd,
                    launch.usage_fd,
                ):
                    os.set_inheritable(fd, True)
            os.execve(
                sys.executable,
                [sys.executable, "-I", "-S", __file__, *sys.argv[1:], "entered"],
                {},
            )
        if _members(directory) != {str(os.getpid()).encode("ascii")}:
            return 2
    except Exception:
        return 2
    # Any protocol, clock or parent failure must lead to stop.
    children: list[int] = []
    timers: list[_KernelKillTimer] = []
    exit_codes: list[int] = []
    stop_reasons: list[str] = []
    with suppress(BaseException):
        _run(sock, parent_pid, directory, children, timers, launch, exit_codes, stop_reasons)
    settled = True
    for pid in children:
        # Direct children are never reaped before this point, so PID reuse is
        # impossible. This also covers a helper not yet placed in the cgroup.
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        try:
            deadline = _clock() + 2.0
            while True:
                reaped, _ = os.waitpid(pid, os.WNOHANG)
                if reaped == pid:
                    break
                if _clock() >= deadline:
                    settled = False
                    break
                time.sleep(0.02)
        except (ChildProcessError, OSError, ValueError):
            settled = False
    drained = False
    if settled:
        with suppress(BaseException):
            drained = _observe_drained(directory)
    if drained:
        # Once only the supervisor remains and direct children are reaped,
        # the deadline need no longer interrupt the terminal acknowledgement.
        # This observation does not cover any external IO or mount cleanup.
        try:
            for timer in timers:
                timer.delete()
        except Exception:
            drained = False
    with suppress(BaseException):
        _report_terminal(
            sock,
            drained,
            exit_codes[0] if len(exit_codes) == 1 else None,
            execution_cutoff=stop_reasons == ["execution_cutoff"],
        )
    sock.close()
    return 0 if drained else 1


if __name__ == "__main__":
    raise SystemExit(main())
