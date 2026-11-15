"""Isolated v4 cgroup supervisor; no Agent execution path is connected.

A separate process joins the Attempt cgroup, holds its original FD, and watches the parent's
pidfd, a private IPC socket and a suspend-aware authority deadline. Its empty
cgroup observation does not prove external IO, mounts or workspace cleanup.
The Attempt journal and resource reservation always remain with the caller.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import select
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from dlr.worker import cgroup_intent_v4, claim_preparation_v4, journal_v4
from dlr.worker.client import V4Renewal, _v4_lease_clock

_LIMIT = 512


@dataclass(frozen=True)
class SupervisorObservation:
    state: str
    code: str
    exit_code: int | None = None
    stop_reason: str | None = None


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

    def arguments(self) -> list[str]:
        return [
            "sandbox",
            str(self.launch_fd),
            str(self.max_bytes),
            str(self.stdout_fd),
            str(self.diagnostic_fd),
            str(self.usage_fd),
            self.workspace,
            self.mount_root,
            str(self.nofile),
            str(self.tmp_bytes),
            str(self.output_max_bytes),
            str(self.uid),
            str(self.gid),
            self.cgroup_root,
            self.cgroup_path,
        ]

    def descriptors(self) -> tuple[int, ...]:
        return self.launch_fd, self.stdout_fd, self.diagnostic_fd, self.usage_fd


class SandboxStreams:
    """Caller-owned pipe readers; stdout is untrusted and may contain secrets.

    The caller must bound and redact output before publishing it. Closing these
    readers does not prove external IO or filesystem cleanup has stopped.
    """

    def __init__(
        self, stdout: int, diagnostic: int, usage: int, *, live: cgroup_intent_v4.LiveCgroup
    ) -> None:
        self._pid = os.getpid()
        self._fds = (stdout, diagnostic, usage)
        self._budget = live._budget
        self._lock = threading.Lock()
        self._closed = False
        self._collector_claimed = False

    @property
    def stdout(self) -> int:
        return self._fds[0]

    @property
    def diagnostic(self) -> int:
        return self._fds[1]

    @property
    def usage(self) -> int:
        return self._fds[2]

    def close(self) -> None:
        if self._pid != os.getpid():
            return
        with self._lock:
            if self._closed:
                return
            self._closed = True
            interruption: BaseException | None = None
            for fd in self._fds:
                try:
                    os.close(fd)
                except BaseException as error:
                    self._budget._mark_claim_receive_accounting_unknown()
                    if not isinstance(error, Exception) and interruption is None:
                        interruption = error
            if interruption is not None:
                raise interruption


def _send(sock: socket.socket, message: dict[str, object]) -> None:
    data = json.dumps(message, separators=(",", ":")).encode("ascii")
    if len(data) > _LIMIT or sock.send(data) != len(data):
        raise ValueError("invalid IPC write")


def _receive(sock: socket.socket) -> dict[str, object]:
    data, _, flags, _ = sock.recvmsg(_LIMIT)
    if not data or flags & socket.MSG_TRUNC:
        raise ValueError("invalid IPC response")
    value = json.loads(data)
    if type(value) is not dict:
        raise ValueError("invalid IPC response")
    return value


def _receive_if_ready(sock: socket.socket) -> dict[str, object] | None:
    """Read one packet only when available, without delaying lease renewal."""
    poller = select.poll()
    poller.register(sock, select.POLLIN | select.POLLHUP | select.POLLERR)
    if not poller.poll(0):
        return None
    data, _, flags, _ = sock.recvmsg(_LIMIT, 0, socket.MSG_DONTWAIT)
    if not data or flags & socket.MSG_TRUNC:
        raise ValueError("invalid IPC response")
    value = json.loads(data)
    if type(value) is not dict:
        raise ValueError("invalid IPC response")
    return value


def _is_terminal(value: dict[str, object]) -> bool:
    if value.get("event") != "stopped" or type(value.get("cgroup_drained")) is not bool:
        return False
    if set(value) == {"event", "cgroup_drained"}:
        return True
    if set(value) == {"event", "cgroup_drained", "stop_reason"}:
        return value.get("stop_reason") == "execution_cutoff"
    code = value.get("exit_code")
    return (
        set(value) == {"event", "cgroup_drained", "exit_code"}
        and type(code) is int
        and 0 <= code <= 255
    )


def _check_live(live: cgroup_intent_v4.LiveCgroup, *, for_stop: bool = False) -> None:
    claim_preparation_v4.check_binding(
        live._prepared, live._instance, live._config, live._runtime_root, live._attempt_journal_root
    )
    reservation = live._prepared.reservation
    permitted = (
        live._budget._v4_stop_allowed(reservation)
        if for_stop
        else live._budget._v4_handle_state(reservation) == "in_use"
    )
    if not permitted:
        raise ValueError("reservation unavailable")
    with journal_v4._locked_root(live._attempt_journal_root) as directory:
        attempt = live._prepared.payload.attempt_id
        if (
            journal_v4._read(directory, f"cgroup-{attempt}.intent.json") != live._intent
            or journal_v4._read(directory, f"cgroup-{attempt}.bound.json") != live._bound
        ):
            raise ValueError("cgroup journal changed")
    parent = (
        cgroup_intent_v4._verify_stop_kernel(live._config)
        if for_stop
        else cgroup_intent_v4._verify_live_kernel(live._config, live._budget)
    )
    cgroup_intent_v4._verify_visible(live, parent, live._config)


def _deadline(renewal: V4Renewal) -> float:
    if type(renewal) is not V4Renewal or renewal.reason != "renewed":
        raise ValueError("renewal unavailable")
    value = renewal.deadline
    now = _v4_lease_clock()
    if type(value) is not float or not math.isfinite(value) or not now < value <= now + 86400:
        raise ValueError("renewal deadline invalid")
    return value


class AttemptSupervisor:
    """One same-process handle; closing IPC asks the child to stop the cgroup."""

    def __init__(
        self,
        live: cgroup_intent_v4.LiveCgroup,
        sock: socket.socket,
        process: subprocess.Popen[bytes],
        *,
        sandbox_launch: bool = False,
        execution_deadline: float | None = None,
    ) -> None:
        self._pid = os.getpid()
        self._live, self._sock, self._process = live, sock, process
        self._lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._closed = False
        self._terminal: dict[str, object] | None = None
        self._collected: SupervisorObservation | None = None
        self._probe_state = "unused"
        self._sandbox_launch = sandbox_launch
        self._execution_deadline = execution_deadline

    def _watch_exit(self) -> None:
        """If the supervisor dies, attempt local stop but keep cleanup unknown.

        This is cooperative parent-process fallback; it cannot run if the
        Agent itself is dead or stalled, and never authorizes a receipt.
        """
        try:
            if self._process.wait() == 0:
                return
            live = self._live
            deadline = time.monotonic() + 2.0
            while True:
                result = cgroup_intent_v4.stop_live_cgroup(
                    live,
                    config=live._config,
                    budget=live._budget,
                    runtime_root=live._runtime_root,
                    attempt_journal_root=live._attempt_journal_root,
                    root_lock=live._root_lock,
                )
                if result.code != "cgroup_still_populated" or time.monotonic() >= deadline:
                    return
                time.sleep(0.02)
        except Exception:
            # A dead supervisor is never proof of a stopped Attempt.
            return

    def alive(self) -> bool:
        return self._pid == os.getpid() and self._process.poll() is None

    def renew(
        self,
        renewal: V4Renewal,
        *,
        send_with_authority: Callable[[Callable[[], None]], bool],
    ) -> bool:
        """Prepare a renewal, then commit only after local authority advances."""
        if self._pid != os.getpid():
            return False
        with self._lock:
            if (
                self._pid != os.getpid()
                or self._closed
                or self._stop_requested.is_set()
                or self._process.poll() is not None
            ):
                return False
            try:
                if not self._live._budget._v4_start_allowed(self._live._prepared.reservation):
                    self.request_stop()
                    return False
                value = _deadline(renewal)
                _send(self._sock, {"op": "renew", "deadline": value})
                response = _receive(self._sock)
                if _is_terminal(response):
                    # The terminal observation belongs to collect, even when
                    # the old timer beats this in-flight renewal.
                    with self._stop_lock:
                        self._terminal = response
                        self._stop_requested.set()
                    return False
                if response != {"event": "renew_prepared"}:
                    raise ValueError("unexpected response")
                if self._stop_requested.is_set():
                    return False
                if not self._live._budget._v4_start_allowed(self._live._prepared.reservation):
                    self.request_stop()
                    return False
                commit = json.dumps(
                    {"op": "renew_commit", "deadline": value}, separators=(",", ":")
                ).encode("ascii")
                sent = False

                def commit_once() -> None:
                    nonlocal sent
                    with self._live._budget._lock, self._stop_lock:
                        if (
                            sent
                            or self._stop_requested.is_set()
                            or not self._live._budget._v4_start_allowed(
                                self._live._prepared.reservation
                            )
                            or len(commit) > _LIMIT
                            or self._sock.send(commit, socket.MSG_DONTWAIT) != len(commit)
                        ):
                            raise ValueError("renewal commit unavailable")
                        sent = True

                if not send_with_authority(commit_once) or not sent:
                    self.request_stop()
                    return False
                response = _receive(self._sock)
                if _is_terminal(response):
                    with self._stop_lock:
                        self._terminal = response
                        self._stop_requested.set()
                    return False
                if response != {"event": "renewed"}:
                    raise ValueError("unexpected response")
                return not self._stop_requested.is_set()
            except Exception:
                self._abandon()
                return False

    def prepare_sandbox(self) -> bool:
        """Place the bound sandbox helper behind its still-closed gate."""
        if not self._sandbox_launch:
            return False
        return self._prepare_helper({"op": "sandbox_prepare"}, "sandbox_gated")

    def _prepare_helper(self, message: dict[str, object], expected: str) -> bool:
        if self._pid != os.getpid():
            return False
        with self._lock:
            if (
                self._pid != os.getpid()
                or self._closed
                or self._stop_requested.is_set()
                or self._process.poll() is not None
                or self._probe_state != "unused"
            ):
                return False
            try:
                if not self._live._budget._v4_start_allowed(self._live._prepared.reservation):
                    self.request_stop()
                    return False
                _send(self._sock, message)
                response = _receive(self._sock)
                if _is_terminal(response):
                    self._terminal = response
                    self._stop_requested.set()
                    return False
                if response != {"event": expected}:
                    raise ValueError("probe gate unavailable")
                self._probe_state = "gated"
                return not self._stop_requested.is_set()
            except Exception:
                self._abandon()
                return False

    def release_sandbox(self, *, send_with_authority: Callable[[Callable[[], None]], bool]) -> bool:
        """Release a sandbox only through the caller's one-shot authority gate."""
        if not self._sandbox_launch:
            return False
        if not callable(send_with_authority):
            self.request_stop()
            return False
        return self._release_helper("sandbox_release", "sandbox_released", send_with_authority)

    def _release_helper(
        self,
        operation: str,
        expected: str,
        send_with_authority: Callable[[Callable[[], None]], bool] | None,
    ) -> bool:
        if self._pid != os.getpid():
            return False
        with self._lock:
            if (
                self._pid != os.getpid()
                or self._closed
                or self._stop_requested.is_set()
                or self._process.poll() is not None
                or self._probe_state != "gated"
            ):
                return False
            try:
                if not self._live._budget._v4_start_allowed(self._live._prepared.reservation):
                    self.request_stop()
                    return False
                sent, active, owner = False, True, threading.get_ident()

                def send() -> None:
                    nonlocal sent
                    if not active or sent or threading.get_ident() != owner:
                        raise ValueError("probe release unavailable")
                    data = json.dumps({"op": operation}, separators=(",", ":")).encode("ascii")
                    with self._stop_lock:
                        if self._stop_requested.is_set():
                            raise ValueError("probe release unavailable")
                        if self._sock.send(data, socket.MSG_DONTWAIT) != len(data):
                            raise ValueError("probe release unavailable")
                    sent = True

                try:
                    if send_with_authority is None:
                        send()
                    elif not send_with_authority(send) or not sent:
                        self.request_stop()
                        return False
                finally:
                    active = False
                response = _receive(self._sock)
                if _is_terminal(response):
                    self._terminal = response
                    self._stop_requested.set()
                    return False
                if response != {"event": expected}:
                    raise ValueError("probe release unavailable")
                self._probe_state = "released"
                return not self._stop_requested.is_set()
            except Exception:
                self._abandon()
                return False

    def _abandon(self) -> None:
        """Close the channel and reap eventually without inferring cleanup."""
        self._closed = True
        self._sock.close()
        with suppress(RuntimeError):
            threading.Thread(
                target=self._process.wait, name="dlr-v4-supervisor-reap", daemon=True
            ).start()

    def request_stop(self) -> None:
        """Request stop without waiting for a blocked renewal or IO thread."""
        if self._pid != os.getpid():
            return
        with self._stop_lock:
            if self._pid != os.getpid() or self._stop_requested.is_set():
                return
            self._stop_requested.set()
            try:
                data = b'{"op":"stop"}'
                if self._sock.send(data, socket.MSG_DONTWAIT) != len(data):
                    raise OSError("short IPC write")
            except Exception:
                # EOF/HUP independently tells the supervisor to stop.
                with suppress(OSError):
                    self._sock.shutdown(socket.SHUT_RDWR)

    def collect(self, timeout: float = 3.0) -> SupervisorObservation:
        """Observe exit without blocking renewal or inferring complete cleanup."""
        if (
            self._pid != os.getpid()
            or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 10
            or not math.isfinite(timeout)
        ):
            return SupervisorObservation("uncertain", "supervisor_handle_invalid")
        end = time.monotonic() + timeout
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                if self._collected is not None:
                    return self._collected
                if self._closed:
                    return SupervisorObservation("uncertain", "supervisor_channel_closed")
                if self._sandbox_launch and not self._stop_requested.is_set():
                    return SupervisorObservation("pending", "supervisor_exit_pending")
                self.request_stop()
                if self._lock.acquire(blocking=False):
                    try:
                        if not self._closed:
                            self._abandon()
                    finally:
                        self._lock.release()
                return SupervisorObservation("uncertain", "supervisor_stop_unverified")
            if not self._lock.acquire(timeout=min(remaining, 0.05)):
                continue
            try:
                if self._collected is not None:
                    return self._collected
                if self._closed:
                    return SupervisorObservation("uncertain", "supervisor_channel_closed")
                response = (
                    self._terminal if self._terminal is not None else _receive_if_ready(self._sock)
                )
                if response is not None:
                    return self._collect_terminal(response, end)
            except Exception:
                self._abandon()
                return SupervisorObservation("uncertain", "supervisor_stop_unverified")
            finally:
                self._lock.release()
            time.sleep(min(0.02, max(0.0, end - time.monotonic())))

    def _collect_terminal(self, response: dict[str, object], end: float) -> SupervisorObservation:
        """Finish one observed terminal message while holding the IPC lock."""
        if not _is_terminal(response) or response["cgroup_drained"] is not True:
            raise ValueError("cgroup stop unverified")
        if response.get("stop_reason") is not None and self._execution_deadline is None:
            raise ValueError("execution cutoff unverified")
        # No stop can be queued after the acknowledgement.
        with self._stop_lock:
            self._stop_requested.set()
            _send(self._sock, {"op": "ack_stopped"})
        if self._process.wait(timeout=max(0.001, end - time.monotonic())) != 0:
            raise ValueError("supervisor exit unverified")
        live = self._live
        with live._mutex:
            if not live._active or not live._helper_inflight:
                raise ValueError("live cgroup unavailable")
            with live._root_lock.borrow(live._runtime_root, [live._attempt_journal_root]):
                _check_live(live, for_stop=True)
                if cgroup_intent_v4._populated(live._child_fd):
                    raise ValueError("cgroup repopulated")
            # Set the irreversible no-more-work gate before dropping inflight.
            live._execution_finished = True
            live._helper_inflight = False
        self._closed = True
        self._sock.close()
        exit_code = response.get("exit_code")
        stop_reason = response.get("stop_reason")
        self._collected = SupervisorObservation(
            "observed",
            "cgroup_empty_supervisor_reaped",
            exit_code if type(exit_code) is int else None,
            stop_reason if isinstance(stop_reason, str) else None,
        )
        return self._collected

    def close(self) -> None:
        if self._pid != os.getpid():
            return
        with self._lock:
            if self._pid == os.getpid() and not self._closed:
                self._abandon()


def start_supervisor(
    live: cgroup_intent_v4.LiveCgroup,
    initial: V4Renewal,
    *,
    _sandbox_launch: _SandboxLaunch | None = None,
    execution_deadline: float | None = None,
) -> tuple[SupervisorObservation, AttemptSupervisor | None]:
    """Bind an empty live cgroup to a separate process; leave all budget retained."""
    if (
        sys.platform != "linux"
        or type(live) is not cgroup_intent_v4.LiveCgroup
        or live._pid != os.getpid()
        or type(_sandbox_launch) is not _SandboxLaunch
    ):
        return SupervisorObservation("blocked", "supervisor_unavailable"), None
    parent: socket.socket | None = None
    child: socket.socket | None = None
    process: subprocess.Popen[bytes] | None = None
    started = False
    try:
        deadline = _deadline(initial)
        now = _v4_lease_clock()
        if execution_deadline is not None and (
            type(execution_deadline) is not float
            or not math.isfinite(execution_deadline)
            or not now < execution_deadline <= now + 86400
            or deadline > execution_deadline
        ):
            raise ValueError("execution deadline unavailable")
        # Configure once before any helper can be started. Partial writes
        # remain unknown and the existing reservation must stay retained.
        if not live._budget._v4_start_allowed(live._prepared.reservation):
            raise ValueError("reservation accounting unknown")
        with live._mutex:
            if live._execution_finished:
                return SupervisorObservation("blocked", "supervisor_unavailable"), None
            needs_limits = live._limits_state == "unconfigured"
        if needs_limits:
            configured = cgroup_intent_v4.configure_live_cgroup(live)
            if configured.code != "cgroup_limits_verified":
                return SupervisorObservation(configured.state, configured.code), None
        with live._mutex:
            if (
                live._pid != os.getpid()
                or not live._active
                or live._execution_finished
                or live._helper_inflight
                or live._workspace_borrows
            ):
                raise ValueError("invalid live cgroup")
            with live._root_lock.borrow(live._runtime_root, [live._attempt_journal_root]):
                _check_live(live)
                if not live._budget._v4_start_allowed(live._prepared.reservation):
                    raise ValueError("reservation accounting unknown")
                try:
                    cgroup_intent_v4._verify_configured_limits(live)
                except Exception:
                    # A changed or unreadable cgroup cannot be re-authorized
                    # merely because a later read happens to match again.
                    live._limits_state = "uncertain"
                    raise
                _deadline(initial)
                parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                parent.settimeout(2.0)
                live._helper_inflight = True
                started = True
                args = [
                    sys.executable,
                    "-I",
                    "-S",
                    str(Path(__file__).with_name("attempt_supervisor_child_v4.py")),
                    str(child.fileno()),
                    str(live._child_fd),
                    str(os.getpid()),
                ]
                descriptors: tuple[int, ...] = (child.fileno(), live._child_fd)
                if _sandbox_launch is not None:
                    args.extend(_sandbox_launch.arguments())
                    descriptors += _sandbox_launch.descriptors()
                process = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={},
                    close_fds=True,
                    pass_fds=descriptors,
                )
                child.close()
                child = None
                first: dict[str, object] = {"op": "renew", "deadline": deadline}
                if execution_deadline is not None:
                    first["execution_deadline"] = execution_deadline
                _send(parent, first)
                if _receive(parent) != {"event": "ready"} or process.poll() is not None:
                    raise ValueError("supervisor handshake failed")
                # The bootstrap moves itself before it can accept the first
                # renewal. Recheck the same cgroup and its frozen limits.
                _check_live(live)
                if (
                    not live._budget._v4_start_allowed(live._prepared.reservation)
                    or cgroup_intent_v4._read_limit(live._child_fd, "cgroup.procs")
                    != str(process.pid).encode("ascii")
                    or any(
                        cgroup_intent_v4._read_limit(live._child_fd, name) != value.encode("ascii")
                        for name, value in cgroup_intent_v4._limit_values(
                            live._prepared.reservation.limits
                        ).items()
                    )
                ):
                    raise ValueError("supervisor placement unverified")
                _deadline(initial)  # Handshake delay may have consumed the authority.
        handle = AttemptSupervisor(
            live,
            parent,
            process,
            sandbox_launch=_sandbox_launch is not None,
            execution_deadline=execution_deadline,
        )
        threading.Thread(
            target=handle._watch_exit, name="dlr-v4-supervisor-watch", daemon=True
        ).start()
        parent = None
        return SupervisorObservation("observed", "supervisor_started"), handle
    except BaseException as error:
        if started:
            # A bootstrap may still be in the Agent cgroup when startup or
            # its final root-lock verification fails. Do not admit another
            # Attempt against an unconfirmed management-process charge.
            live._budget._mark_claim_receive_accounting_unknown()
        if process is not None:
            with suppress(RuntimeError):
                threading.Thread(
                    target=process.wait, name="dlr-v4-supervisor-reap", daemon=True
                ).start()
        if not isinstance(error, Exception):
            raise
        return SupervisorObservation(
            "uncertain" if started else "blocked", "supervisor_start_unverified"
        ), None
    finally:
        if child is not None:
            child.close()
        if parent is not None:
            parent.close()


def start_sandbox_supervisor(
    staged: claim_preparation_v4.StagedClaim,
    initial: V4Renewal,
    *,
    execution_deadline: float | None = None,
) -> tuple[SupervisorObservation, AttemptSupervisor | None, SandboxStreams | None]:
    """Start the guardian for the exact staged Claim, retaining both charges."""
    from dlr.worker import recovery_checkpoint_v4, sandbox, workspace_live_v4

    blocked = SupervisorObservation("blocked", "sandbox_binding_unavailable")
    if type(staged) is not claim_preparation_v4.StagedClaim:
        return blocked, None, None
    live, workspace = staged.cgroup, staged.workspace
    launch_max_bytes = staged.launch_max_bytes
    if (
        sys.platform != "linux"
        or type(live) is not cgroup_intent_v4.LiveCgroup
        or type(workspace) is not workspace_live_v4.LiveWorkspace
        or live._pid != os.getpid()
        or workspace._pid != os.getpid()
        or workspace._cgroup is not live
        or live._prepared is not staged.prepared
        or staged.prepared.stage_state != "staged"
        or type(staged.launch_fd) is not int
        or staged.launch_fd < 3
        or type(launch_max_bytes) is not int
        or not 0 < launch_max_bytes <= live._prepared.reservation.limits.memory_bytes
    ):
        return blocked, None, None
    launch_fd = -1
    readers: list[int] = []
    writers: list[int] = []
    supervisor: AttemptSupervisor | None = None
    streams: SandboxStreams | None = None
    started = False
    side_effects_entered = False
    published = False
    try:
        recovery_checkpoint_v4.require_startable(staged)
        with workspace._lock:
            with live._mutex:
                if (
                    not workspace._active
                    or workspace._borrows
                    or workspace._scratch_inflight
                    or workspace._external_io_inflight
                    or workspace._broker_owner is not None
                    or not live._active
                    or live._execution_finished
                    or live._helper_inflight
                    or live._workspace_borrows
                ):
                    return blocked, None, None
                with live._root_lock.borrow(live._runtime_root, [live._attempt_journal_root]):
                    _check_live(live)
                    workspace_live_v4._check_journal(workspace)
                    workspace_live_v4._verify_workspace(workspace)
                    if not live._budget._v4_start_allowed(live._prepared.reservation):
                        return blocked, None, None
                    config = live._config
                    if (
                        config.cgroup_path is None
                        or type(config.payload_uid) is not int
                        or config.payload_uid <= 0
                        or type(config.payload_gid) is not int
                        or config.payload_gid <= 0
                    ):
                        return blocked, None, None
                    path = (
                        Path(live._runtime_root)
                        / "workspaces"
                        / workspace._parent_name
                        / workspace._leaf_name
                    )
                    if live._prepared.journal.get("workspace_path") != str(path):
                        return blocked, None, None
                    if workspace._sandbox_mount_expected:
                        return blocked, None, None
                    try:
                        os.stat(
                            ".dlr-sandbox-mount",
                            dir_fd=workspace._parent,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        return blocked, None, None
            current_deadline = staged.prepared.attempt.local_deadline()
            if (
                type(initial) is not V4Renewal
                or initial.reason != "renewed"
                or type(initial.deadline) is not float
                or initial.deadline > current_deadline
            ):
                return blocked, None, None
            info = os.fstat(staged.launch_fd)
            seals = sandbox._launch_seals()
            if (
                not 0 < info.st_size <= launch_max_bytes
                or info.st_nlink != 0
                or fcntl.fcntl(staged.launch_fd, fcntl.F_GET_SEALS) & seals != seals
            ):
                return blocked, None, None
            staged.prepared.stage_state = "starting"
            launch_fd, staged.launch_fd = staged.launch_fd, -1
            side_effects_entered = True
            for _ in range(3):
                read_fd, write_fd = os.pipe()
                readers.append(read_fd)
                writers.append(write_fd)
            limits = live._prepared.reservation.limits
            assert live._config.cgroup_path is not None
            launch = _SandboxLaunch(
                launch_fd,
                launch_max_bytes,
                writers[0],
                writers[1],
                writers[2],
                str(path),
                str(path.parent / ".dlr-sandbox-mount"),
                limits.nofile,
                limits.tmp_bytes,
                limits.output_max_bytes,
                live._config.payload_uid,
                live._config.payload_gid,
                str(live._config.cgroup_path),
                str(live._config.cgroup_path / live._name),
            )
            workspace._sandbox_mount_expected = True
            started = True
            observation, supervisor = start_supervisor(
                live, initial, _sandbox_launch=launch, execution_deadline=execution_deadline
            )
            if supervisor is None:
                return observation, None, None
            with (
                live._mutex,
                live._root_lock.borrow(live._runtime_root, [live._attempt_journal_root]),
            ):
                _check_live(live)
                workspace_live_v4._check_journal(workspace)
                workspace_live_v4._verify_workspace(workspace)
                if not live._helper_inflight or not live._budget._v4_start_allowed(
                    live._prepared.reservation
                ):
                    raise ValueError("sandbox binding changed")
            streams = SandboxStreams(*readers, live=live)
            readers.clear()
            staged.prepared.stage_state = "supervising"
            published = True
            return observation, supervisor, streams
    except BaseException as error:
        published = False
        if side_effects_entered:
            live._budget._mark_claim_receive_accounting_unknown()
        if not isinstance(error, Exception):
            raise
        return (
            SupervisorObservation(
                "uncertain" if started else "blocked", "sandbox_start_unverified"
            ),
            None,
            None,
        )
    finally:
        if side_effects_entered and not published:
            staged.prepared.stage_state = "unknown"
        try:
            if not published and supervisor is not None:
                with suppress(Exception):
                    supervisor.request_stop()
                with suppress(Exception):
                    supervisor.close()
            if not published and streams is not None:
                streams.close()
        finally:
            interruption: BaseException | None = None
            for fd in ([launch_fd] if launch_fd >= 0 else []) + readers + writers:
                try:
                    os.close(fd)
                except BaseException as error:
                    live._budget._mark_claim_receive_accounting_unknown()
                    if not isinstance(error, Exception) and interruption is None:
                        interruption = error
            if interruption is not None:
                raise interruption
