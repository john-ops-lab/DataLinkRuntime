"""Run one staged fixed-Worker task under a proof-fenced Linux supervisor."""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import socket
import struct
import sys
import threading
from dataclasses import dataclass, field
from uuid import UUID

from dlr.common.worker_protocol_v4 import WORKER_FORCE_STOP_CODE
from dlr.worker import attempt_supervisor_v4, claim_http_v4, claim_preparation_v4
from dlr.worker.attempt_session_v4 import (
    PROGRESS_STDOUT_BYTES,
    AttemptForceStopRequested,
    AttemptLeaseSession,
)
from dlr.worker.client import V4Renewal, _v4_lease_clock


@dataclass(frozen=True)
class ExecutionObservation:
    state: str
    code: str
    exit_code: int | None = None
    stop_reason: str | None = None
    stdout: bytes = field(default=b"", repr=False)
    stdout_truncated: bool = False
    diagnostic: bytes = field(default=b"", repr=False)
    usage: bytes = field(default=b"", repr=False)


@dataclass(eq=False)
class ResultSnapshot:
    """A private sealed result bound to the same supervised execution."""

    owner: SupervisedExecution = field(repr=False)
    fd: int = field(repr=False)
    size: int
    body_mac: bytes = field(repr=False)
    identity: tuple[int, int, int] = field(repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    intent: object | None = field(default=None, repr=False)
    receipt: str | None = field(default=None, repr=False)
    finalization_id: UUID | None = field(default=None, repr=False)


class _OutputCollector:
    """Drain the three supervisor pipes with a fixed, already-reserved ceiling."""

    def __init__(
        self,
        streams: attempt_supervisor_v4.SandboxStreams,
        limit: int,
        stop: threading.Event,
        *,
        collect_progress: bool,
    ) -> None:
        self._streams, self._limit, self._stop = streams, limit, stop
        self._stdout = bytearray()
        self._diagnostic = bytearray()
        self._usage = bytearray()
        self._truncated = False
        self._collect_progress = collect_progress
        self._progress = bytearray()
        self._progress_lock = threading.Lock()
        self._failed = False
        self._finished = threading.Event()
        self._thread = threading.Thread(target=self._run, name="dlr-v4-output", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        selector: selectors.BaseSelector | None = None
        try:
            selector = selectors.DefaultSelector()
            for name, fd in (
                ("stdout", self._streams.stdout),
                ("diagnostic", self._streams.diagnostic),
                ("usage", self._streams.usage),
            ):
                os.set_blocking(fd, False)
                selector.register(fd, selectors.EVENT_READ, name)
            while selector.get_map():
                for key, _ in selector.select(0.2):
                    try:
                        chunk = os.read(key.fd, 65_536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    if key.data == "stdout":
                        self._stdout.extend(chunk)
                        if len(self._stdout) > self._limit:
                            del self._stdout[: len(self._stdout) - self._limit]
                            self._truncated = True
                        if self._collect_progress:
                            with self._progress_lock:
                                remaining = PROGRESS_STDOUT_BYTES - len(self._progress)
                                self._progress.extend(chunk[:remaining])
                    else:
                        target = self._diagnostic if key.data == "diagnostic" else self._usage
                        if len(target) + len(chunk) > 8192:
                            raise ValueError("sandbox output unavailable")
                        target.extend(chunk)
        except BaseException:
            self._failed = True
            self._stop.set()
        finally:
            if selector is not None:
                selector.close()
            self._finished.set()

    def take_progress(self) -> str:
        with self._progress_lock:
            chunk = self._progress.decode("utf-8", errors="ignore")
            self._progress.clear()
        return chunk.replace("\x00", "")

    def finish(self, timeout: float) -> tuple[bytes, bool, bytes, bytes] | None:
        if not self._finished.wait(timeout) or self._failed:
            return None
        self._thread.join(timeout=0.1)
        if self._thread.is_alive():
            return None
        return bytes(self._stdout), self._truncated, bytes(self._diagnostic), bytes(self._usage)


class SupervisedExecution:
    """Keep the proof watchdog, supervisor and output owner together."""

    def __init__(
        self,
        staged: claim_preparation_v4.StagedClaim,
        supervisor: attempt_supervisor_v4.AttemptSupervisor,
        streams: attempt_supervisor_v4.SandboxStreams,
        collector: _OutputCollector,
        stop: threading.Event,
        execution_deadline: float,
    ) -> None:
        self._pid = os.getpid()
        self._staged, self._supervisor, self._streams = staged, supervisor, streams
        self._collector, self._stop = collector, stop
        self._execution_deadline = execution_deadline
        self._done = threading.Event()
        self._physical_done = threading.Event()
        self._watchdog_failed = threading.Event()
        self._force_stopping = threading.Event()
        self._force_stop_operation: UUID | None = None
        self._renewal_lock = threading.Lock()
        self._lock = threading.Lock()
        self._observation: attempt_supervisor_v4.SupervisorObservation | None = None
        self._snapshot: ResultSnapshot | None = None
        self._snapshot_lock = threading.Lock()
        self._unresolved: list[object] = []
        self._watchdog = threading.Thread(target=self._renew, name="dlr-v4-renew", daemon=True)

    def _renew(self) -> None:
        attempt = self._staged.prepared.attempt
        try:
            while not self._done.is_set():
                now = _v4_lease_clock()
                deadline = attempt.local_deadline()
                if now >= deadline or self._stop.is_set():
                    raise ValueError("Attempt authority unavailable")
                delay = min(
                    float(self._staged.prepared.renew_seconds), max(0.1, deadline - now - 2)
                )
                end = now + delay
                while not self._done.is_set() and not self._stop.is_set():
                    remaining = end - _v4_lease_clock()
                    if remaining <= 0:
                        break
                    self._done.wait(min(remaining, 0.2))
                if self._done.is_set():
                    return
                if self._stop.is_set():
                    raise ValueError("Attempt stopped")
                with self._renewal_lock:
                    if self._force_stopping.is_set() or self._done.is_set():
                        return
                    renewed = self._renew_supervisor(attempt)
                    if (
                        renewed is not None
                        and renewed - _v4_lease_clock() > 2.5
                        and attempt.progress(self._collector.take_progress())
                    ):
                        raise ValueError("Attempt cancelled")
        except AttemptForceStopRequested as error:
            self._request_force_stop(error.operation_id)
        except BaseException:
            self._watchdog_failed.set()
            self._stop.set()
            self._supervisor.request_stop()

    def _renew_supervisor(self, attempt: AttemptLeaseSession) -> float | None:
        attempt.renew()
        if self._physical_done.is_set():
            return None
        deadline = min(attempt.local_deadline(), self._execution_deadline)
        if self._supervisor.renew(
            V4Renewal("renewed", deadline),
            send_with_authority=lambda send: attempt.send_with_authority(send, deadline=deadline),
        ):
            return deadline
        observation = self._supervisor.collect(timeout=2.0)
        if observation.code != "cgroup_empty_supervisor_reaped":
            raise ValueError("supervisor unavailable")
        with self._lock:
            self._observation = observation
            self._physical_done.set()
        return None

    def release_go_runtime(self) -> bool:
        """Obtain fresh Control authority after the build has fully stopped."""
        attempt = self._staged.prepared.attempt
        with self._renewal_lock:
            if self._force_stopping.is_set() or self._stop.is_set():
                return False
            try:
                deadline = self._renew_supervisor(attempt)
            except AttemptForceStopRequested as error:
                self._request_force_stop(error.operation_id)
                return False
            if deadline is None:
                return False
            return self._supervisor.release_sandbox(
                send_with_authority=lambda send: attempt.send_with_authority(
                    send, deadline=deadline
                )
            )

    def _request_force_stop(self, operation_id: UUID) -> None:
        self._force_stop_operation = operation_id
        self._force_stopping.set()
        self._supervisor.request_stop()

    def observe(self, timeout: float = 1.0) -> ExecutionObservation:
        """Observe a reaped task and drained output, retaining all responsibility."""
        if self._pid != os.getpid() or not 0 < timeout <= 10:
            return ExecutionObservation("unknown", "execution_observation_unavailable")
        observation = self._supervisor.collect(timeout=timeout)
        if observation.state == "pending":
            return ExecutionObservation("pending", "execution_running")
        if observation.code != "cgroup_empty_supervisor_reaped":
            self._stop.set()
            return ExecutionObservation("unknown", "execution_stop_unverified")
        with self._lock:
            self._observation = observation
            self._physical_done.set()
        output = self._collector.finish(timeout)
        if output is None or self._watchdog_failed.is_set():
            self._stop.set()
            return ExecutionObservation("unknown", "execution_output_unverified")
        stdout, truncated, diagnostic, usage = output
        if self._force_stopping.is_set():
            return ExecutionObservation(
                "observed",
                observation.code,
                stop_reason=WORKER_FORCE_STOP_CODE,
                stdout=stdout,
                stdout_truncated=truncated,
                diagnostic=diagnostic,
                usage=usage,
            )
        stop_reason = observation.stop_reason
        if stop_reason == "preparation_failed" and self._staged.capacity_failed:
            stop_reason = "resource_capacity_unavailable"
        return ExecutionObservation(
            "observed",
            observation.code,
            observation.exit_code,
            stop_reason,
            stdout,
            truncated,
            diagnostic,
            usage,
        )

    def stop_watchdog(self) -> bool:
        """Stop after result and cleanup settlement, never as a cleanup receipt."""
        if self._pid != os.getpid():
            return False
        self._done.set()
        self._watchdog.join(timeout=6)
        if self._watchdog.is_alive():
            return False
        self._streams.close()
        return not self._watchdog_failed.is_set()

    def build_result(self) -> ResultSnapshot | None:
        """Create a sealed result only after the exact supervisor was reaped."""
        if self._pid != os.getpid():
            return None
        with self._snapshot_lock:
            if self._snapshot is not None:
                return self._snapshot
            observed = self.observe(timeout=2.0)
            if observed.state != "observed" or self._stop.is_set():
                return None
            staged, attempt = self._staged, self._staged.prepared.attempt
            live, workspace = staged.cgroup, staged.workspace
            ticket = channel = child_channel = None
            pidfd = result_fd = None
            reaper = claim_preparation_v4.ClaimPreparer()
            transferred = False
            try:
                with attempt._lock, attempt._parent._lock:
                    _, _, deadline = attempt._live_locked()
                    if not attempt._start_confirmed:
                        raise ValueError("result authority unavailable")
                    token = attempt._claim_token
                now = claim_http_v4._clock()
                deadline = min(deadline, now + 10.0)
                if deadline <= now:
                    raise ValueError("result authority unavailable")
                numerator, denominator = deadline.as_integer_ratio()
                deadline_ns = numerator * 1_000_000_000 // denominator
                channel, child_channel = socket.socketpair()
                channel.setblocking(False)
                ticket = claim_http_v4._Spawn(child=child_channel)
                command = [
                    sys.executable,
                    "-m",
                    "dlr.worker.result_build_child_v4",
                    str(child_channel.fileno()),
                    str(os.getpid()),
                    str(deadline_ns),
                    str(staged.prepared.child_address_space_bytes),
                ]
                child_channel = None
                environment = {
                    key: value for key, value in os.environ.items() if key in {"PATH", "PYTHONPATH"}
                }
                claim_http_v4._spawn_until(
                    ticket,
                    deadline,
                    deadline_ns,
                    staged.prepared.child_address_space_bytes,
                    (self._stop,),
                    _process_command=command,
                    _process_env=environment,
                )
                if ticket.process is None or ticket.abandoned:
                    raise ValueError("result child unavailable")
                pidfd = os.pidfd_open(ticket.process.pid, 0)  # type: ignore[attr-defined, unused-ignore]
                if (
                    claim_http_v4._receive(channel, 1, deadline, (self._stop,)) != b"R"
                    or ticket.thread is None
                    or not ticket.thread.is_alive()
                ):
                    raise ValueError("result child unavailable")
                with workspace.borrow() as directory:
                    with live._mutex:
                        workspace._external_io_inflight = True
                    info = os.fstat(directory)
                    limits = staged.prepared.reservation.limits
                    request = {
                        "attempt_id": staged.prepared.identity.attempt_id,
                        "fencing_token": staged.prepared.identity.fencing_token,
                        "claim_token": token,
                        "exit_code": observed.exit_code,
                        "stop_reason": observed.stop_reason,
                        "stdout_bytes": len(observed.stdout),
                        "stdout_truncated": observed.stdout_truncated,
                        "output_max_bytes": limits.output_max_bytes,
                        "preview_max_bytes": limits.output_preview_max_bytes,
                        "max_body_bytes": staged.prepared.result_wire_budget,
                        "workspace_device": info.st_dev,
                        "workspace_inode": info.st_ino,
                        "redaction_required": staged.prepared.business_credentials,
                        "redaction_identity": staged.redaction_identity,
                        "launch_max_bytes": staged.launch_max_bytes,
                    }
                    raw = json.dumps(request, separators=(",", ":")).encode("utf-8")
                    del request, token
                    claim_http_v4._send_all(
                        channel, struct.pack("!I", len(raw)) + raw, deadline, (self._stop,)
                    )
                    claim_http_v4._send_all(channel, observed.stdout, deadline, (self._stop,))
                    claim_http_v4._send_fd(channel, directory, deadline, (self._stop,))
                    if staged.prepared.business_credentials:
                        if staged.redaction_fd < 3 or staged.redaction_identity is None:
                            raise ValueError("redaction snapshot unavailable")
                        launch_info = os.fstat(staged.redaction_fd)
                        if (
                            launch_info.st_dev,
                            launch_info.st_ino,
                            launch_info.st_size,
                        ) != staged.redaction_identity:
                            raise ValueError("redaction snapshot unavailable")
                        claim_http_v4._send_fd(
                            channel, staged.redaction_fd, deadline, (self._stop,)
                        )
                    size = struct.unpack(
                        "!I", claim_http_v4._receive(channel, 4, deadline, (self._stop,))
                    )[0]
                    if not 0 < size <= 4096:
                        raise ValueError("result unavailable")
                    answer = json.loads(
                        claim_http_v4._receive(channel, size, deadline, (self._stop,))
                    )
                    if (
                        type(answer) is not dict
                        or set(answer) != {"state", "size_bytes", "body_mac"}
                        or answer["state"] != "result"
                        or type(answer["size_bytes"]) is not int
                        or not 0 < answer["size_bytes"] <= staged.prepared.result_wire_budget
                        or type(answer["body_mac"]) is not str
                        or re.fullmatch(r"[0-9a-f]{64}", answer["body_mac"]) is None
                    ):
                        raise ValueError("result unavailable")
                    result_fd = reaper._receive_sealed_launch(
                        channel, deadline, answer["size_bytes"]
                    )
                    result_info = os.fstat(result_fd)
                    if result_info.st_size != answer["size_bytes"]:
                        raise ValueError("result unavailable")
                    if not reaper._stop_uncommitted(ticket, pidfd, channel):
                        raise ValueError("result child unavailable")
                    ticket = None
                    pidfd = None
                    channel = None
                    if staged.prepared.business_credentials:
                        # The result child was reaped; the task/helper was already
                        # observed stopped. Only this opaque alias remains.
                        os.close(staged.redaction_fd)
                        staged.redaction_fd = -1
                        staged.redaction_identity = None
                    with live._mutex:
                        workspace._external_io_inflight = False
                if not attempt.is_usable() or self._stop.is_set():
                    raise ValueError("result authority unavailable")
                snapshot = ResultSnapshot(
                    self,
                    result_fd,
                    answer["size_bytes"],
                    bytes.fromhex(answer["body_mac"]),
                    (result_info.st_dev, result_info.st_ino, result_info.st_size),
                )
                self._snapshot = snapshot
                transferred = True
                return snapshot
            except BaseException as error:
                live._budget._mark_claim_receive_accounting_unknown()
                self._stop.set()
                self._unresolved.append((ticket, pidfd, channel, child_channel, result_fd))
                if not isinstance(error, Exception):
                    raise
                return None
            finally:
                if child_channel is not None:
                    child_channel.close()
                if not transferred and ticket is not None:
                    reaper._stop_uncommitted(ticket, pidfd, channel)


def start_execution(
    staged: claim_preparation_v4.StagedClaim, *, stop: threading.Event
) -> SupervisedExecution | None:
    """Start the fixed task only after output drainage and the signed gate exist."""
    if type(staged) is not claim_preparation_v4.StagedClaim or stop.is_set():
        return None
    supervisor = None
    streams = None
    try:
        attempt = staged.prepared.attempt
        if staged.check_capacity and not staged.preparation_failed:
            live = staged.cgroup
            staged.capacity_failed = not live._budget.supports_live_charges(live._config)
        now = _v4_lease_clock()
        execution_deadline = math.nextafter(
            now + staged.prepared.reservation.limits.execution_timeout_seconds, -math.inf
        )
        if staged.preparation_only:
            # Nonbusiness mode has no execution gate or new business time.
            # Its short physical observation stays inside existing authority.
            execution_deadline = min(attempt.local_deadline(), now + 5.0)
        elif staged.prepared.managed_files:
            if staged.prepared.execution_deadline is None:
                raise ValueError("execution deadline unavailable")
            execution_deadline = staged.prepared.execution_deadline
        initial = V4Renewal("renewed", min(attempt.local_deadline(), execution_deadline))
        observation, supervisor, streams = attempt_supervisor_v4.start_sandbox_supervisor(
            staged, initial, execution_deadline=execution_deadline
        )
        if observation.code != "supervisor_started" or supervisor is None or streams is None:
            return None
        collector = _OutputCollector(
            streams,
            staged.prepared.reservation.limits.stream_max_bytes,
            stop,
            collect_progress=(
                not staged.prepared.business_credentials and not staged.preparation_only
            ),
        )
        owner = SupervisedExecution(
            staged, supervisor, streams, collector, stop, execution_deadline
        )
        collector.start()
        owner._watchdog.start()
        if stop.is_set():
            raise ValueError("execution gate unavailable")
        if staged.preparation_only:
            if not (staged.capacity_failed or staged.prepared.managed_files):
                raise ValueError("preparation stop unavailable")
            if not supervisor.finish_preparation_failure():
                raise ValueError("preparation stop unavailable")
            observed = owner.observe(timeout=2.0)
            expected = (
                "resource_capacity_unavailable" if staged.capacity_failed else "preparation_failed"
            )
            if observed.state != "observed" or observed.stop_reason != expected:
                raise ValueError("preparation stop unavailable")
            return owner
        if not supervisor.prepare_sandbox() or not supervisor.release_sandbox(
            send_with_authority=attempt.send_with_authority
        ):
            # The original execution timer can win during preparation. Only
            # the supervisor's exact cutoff plus reaped/empty-cgroup and output
            # observations permit the normal timed-out result path. An IPC,
            # parent or authority failure still retains unknown responsibility.
            observed = owner.observe(timeout=2.0)
            if observed.state != "observed" or observed.stop_reason != "execution_cutoff":
                raise ValueError("execution gate unavailable")
        elif staged.prepared.dispatch.get("language") == "go":
            # The Watchdog continues renewing the same Attempt while building.
            # The separate runtime helper receives a fresh one-shot gate under
            # the current proof and the original execution deadline.
            while not stop.is_set():
                phase = supervisor.prepare_go_runtime()
                if phase == "pending":
                    stop.wait(0.1)
                    continue
                if phase == "ready" and owner.release_go_runtime():
                    return owner
                observed = owner.observe(timeout=2.0)
                if observed.state == "observed" and observed.stop_reason in {
                    "toolchain_build_failed",
                    "execution_cutoff",
                    WORKER_FORCE_STOP_CODE,
                }:
                    return owner
                break
            raise ValueError("compiled runtime gate unavailable")
        return owner
    except BaseException as error:
        stop.set()
        if supervisor is not None:
            supervisor.request_stop()
        if not isinstance(error, Exception):
            raise
        return None
