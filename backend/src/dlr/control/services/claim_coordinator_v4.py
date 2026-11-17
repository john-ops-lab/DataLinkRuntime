"""One Control coordinator with at most two owned Claim/Result child slots."""

from __future__ import annotations

import hmac
import json
import logging
import math
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from sqlalchemy import Engine

from dlr.common.config import settings
from dlr.control.schemas.reliable_runtime import AttemptActionBody
from dlr.control.services import business_credentials_v4 as business_credentials
from dlr.control.services.attempt_lease_candidates_v4 import AttemptCandidateRef
from dlr.control.services.claim_child_v4 import _clock, _read_exact, _read_frame, _send_frame
from dlr.control.services.claim_envelope_v4 import ClaimEnvelope, ClaimEnvelopeUnavailable
from dlr.control.services.control_process_v4 import ControlProcess
from dlr.control.services.dispatch import DispatchMessage

logger = logging.getLogger(__name__)


class ClaimUnavailable(Exception):
    """No committed execution authority can be returned for this operation."""


@dataclass(eq=False)
class _Spawn:
    """Keep the Linux creator thread alive until its child is reaped."""

    child: socket.socket | None = field(repr=False)
    done: threading.Event = field(default_factory=threading.Event, repr=False)
    release: threading.Event = field(default_factory=threading.Event, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    abandoned: bool = False


@dataclass(eq=False)
class _Claim:
    process: subprocess.Popen[bytes] = field(repr=False)
    spawn: _Spawn = field(repr=False)
    pidfd: int = field(repr=False)
    channel: socket.socket = field(repr=False)
    envelope: ClaimEnvelope = field(repr=False)
    authority: ControlProcess = field(repr=False)
    token: str = field(repr=False)
    credential: str = field(repr=False)
    worker_id: int
    instance_id: UUID
    generation: int
    deadline: float
    decision: str
    size: int
    configuration: dict[str, object] = field(default_factory=dict, repr=False)
    state: Literal["streaming", "ready", "finalizing", "closed"] = "streaming"
    timer: threading.Timer | None = field(default=None, repr=False)
    owner: _ClaimSlot | None = field(default=None, repr=False)
    allocation: int = 0
    input_cutoff: float | None = None


@dataclass(eq=False)
class _Result:
    process: subprocess.Popen[bytes] = field(repr=False)
    spawn: _Spawn = field(repr=False)
    pidfd: int = field(repr=False)
    channel: socket.socket = field(repr=False)
    envelope: ClaimEnvelope = field(repr=False)
    authority: ControlProcess = field(repr=False)
    deadline: float
    size: int
    metadata: dict[str, object] = field(default_factory=dict, repr=False)
    configuration: dict[str, object] = field(default_factory=dict, repr=False)
    pending_finalize: dict[str, object] | None = field(default=None, repr=False)
    pending_stage: dict[str, object] | None = field(default=None, repr=False)
    retention_deadline: float | None = None
    sent: int = 0
    closed: bool = False
    timer: threading.Timer | None = field(default=None, repr=False)
    owner: _ClaimSlot | None = field(default=None, repr=False)
    allocation: int = 0
    input_active: bool = False
    input_cutoff: float | None = None


@dataclass(eq=False)
class _Input:
    result: _Result = field(repr=False)
    deadline: float
    size: int
    received: int = 0
    closed: bool = False
    io_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class _ClaimSlot:
    """Serialize preparation, retain unknown children and never log secrets.

    This coordinator is process-local; Wave 1 requires one Control process so
    that the confirmation reaches the process holding the prepared snapshot.
    """

    def __init__(self, coordinator: ClaimCoordinator | None = None) -> None:
        self._coordinator = coordinator
        self._allocation = 0
        self._worker_id: int | None = None
        self._calls = 0
        self._closing = False
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._current: _Claim | None = None
        self._result: _Result | None = None
        self._spawning = False
        self._poisoned = False
        self._unresolved: list[object] = []

    def _live(self) -> None:
        if self._coordinator is not None:
            self._coordinator._live()
        if self._pid != os.getpid() or self._poisoned:
            raise ClaimUnavailable("Claim unavailable")

    def _poison(self) -> None:
        self._poisoned = True
        if self._coordinator is not None:
            self._coordinator._poisoned.set()

    def _open_envelope(self, *, child_address_space_bytes: int) -> ClaimEnvelope:
        try:
            return ClaimEnvelope.open(child_address_space_bytes=child_address_space_bytes)
        except ClaimEnvelopeUnavailable:
            self._poison()
            raise

    def _check_envelope(
        self, envelope: ClaimEnvelope, *, entry: _Claim | _Result | None = None
    ) -> None:
        with self._lock:
            self._live()
            # An ordinary timer/close may win after IO. Reject that old entry
            # before inspecting its already closed handle; it is not evidence
            # that the shared resource envelope failed.
            if entry is not None and (
                (
                    isinstance(entry, _Claim)
                    and (self._current is not entry or entry.state == "closed")
                )
                or (isinstance(entry, _Result) and (self._result is not entry or entry.closed))
            ):
                raise ClaimUnavailable("Claim unavailable")
            try:
                envelope.check()
            except ClaimEnvelopeUnavailable:
                # Latch every observed live boundary failure, including checks
                # during stream/finalize/result, even if limits later recover.
                self._poison()
                raise

    def _notify_idle(self) -> None:
        if self._coordinator is None:
            return
        with self._lock:
            idle = not (
                self._current
                or self._result
                or self._spawning
                or self._closing
                or self._unresolved
                or self._poisoned
            )
            if idle:
                # Keep the idle facts stable through registry release. The
                # only nested lock order is slot -> registry; registry callers
                # release their lock before entering any slot or doing IO.
                self._coordinator._release_idle(self, self._allocation)

    def _spawn_child(
        self,
        child: socket.socket,
        deadline: float,
        address_space_bytes: int,
        *,
        module: str = "dlr.control.services.claim_child_v4",
    ) -> tuple[_Spawn, subprocess.Popen[bytes]]:
        """Bound spawn and keep its creator alive while PDEATHSIG is active."""
        if module not in {
            "dlr.control.services.claim_child_v4",
            "dlr.control.services.result_child_v4",
        }:
            raise ClaimUnavailable("Claim unavailable")
        ticket = _Spawn(child)

        def launch() -> None:
            process: subprocess.Popen[bytes] | None = None
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        module,
                        str(child.fileno()),
                        str(os.getpid()),
                        str(address_space_bytes),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    pass_fds=(child.fileno(),),
                )
            except BaseException:
                pass
            finally:
                child.close()
            with ticket.lock:
                ticket.child = None
                ticket.process = process
                abandoned = ticket.abandoned
                ticket.done.set()
            if process is not None and not abandoned:
                ticket.release.wait()
                with ticket.lock:
                    abandoned = ticket.abandoned
            if process is not None and abandoned:
                # No request or credentials were sent to an abandoned spawn.
                pidfd: int | None = None
                try:
                    pidfd = os.pidfd_open(process.pid, 0)  # type: ignore[attr-defined, unused-ignore]
                    if process.poll() is None:
                        signal.pidfd_send_signal(pidfd, signal.SIGKILL)  # type: ignore[attr-defined, unused-ignore]
                    process.wait(timeout=2)
                    os.close(pidfd)
                except Exception:
                    with self._lock:
                        self._poison()
                        self._unresolved.append((process, pidfd, ticket))

        try:
            ticket.thread = threading.Thread(target=launch, name="dlr-claim-child", daemon=True)
            ticket.thread.start()
        except BaseException as error:
            with ticket.lock:
                ticket.abandoned = True
            ticket.release.set()
            with self._lock:
                self._poison()
                self._unresolved.append(ticket)
            if not isinstance(error, Exception):
                raise
            raise ClaimUnavailable("Claim unavailable") from None
        try:
            while True:
                remaining = deadline - _clock()
                if remaining <= 0:
                    raise ClaimUnavailable("Claim unavailable")
                if ticket.done.wait(min(remaining, 0.1)):
                    break
        except BaseException as error:
            with ticket.lock:
                ticket.abandoned = True
            ticket.release.set()
            with self._lock:
                self._poison()
                self._unresolved.append(ticket)
            if not isinstance(error, Exception):
                raise
            raise ClaimUnavailable("Claim unavailable") from None
        with ticket.lock:
            process = ticket.process
        if process is None:
            ticket.release.set()
            if ticket.thread is not None:
                ticket.thread.join(timeout=2)
            raise ClaimUnavailable("Claim unavailable")
        return ticket, process

    def prepare(
        self,
        process: ControlProcess,
        *,
        worker_id: int,
        instance_id: UUID,
        generation: int,
        credential: str,
        instance_proof: str,
        message: DispatchMessage,
        wire_budget: int,
    ) -> _Claim:
        with self._lock:
            self._live()
            if (
                self._closing
                or self._spawning
                or self._current is not None
                or self._result is not None
            ):
                raise ClaimUnavailable("Claim unavailable")
            self._spawning = True
        channel: socket.socket | None = None
        child_channel: socket.socket | None = None
        child: subprocess.Popen[bytes] | None = None
        spawn: _Spawn | None = None
        pidfd: int | None = None
        envelope: ClaimEnvelope | None = None
        entry: _Claim | None = None
        published = False
        try:
            process.check_authority()
            address_space_bytes = settings.worker_v4_claim_child_address_space_bytes
            envelope = self._open_envelope(child_address_space_bytes=address_space_bytes)
            control_instance, control_token, confirmed_deadline = process.claim_child_identity()
            deadline = min(confirmed_deadline, math.nextafter(_clock() + 25.0, -math.inf))
            if deadline <= _clock() or type(wire_budget) is not int or not 0 < wire_budget < 2**63:
                raise ValueError("Claim unavailable")
            bind = process._sessions.kw.get("bind")
            if not isinstance(bind, Engine):
                raise ValueError("Claim unavailable")
            snapshot = settings.model_dump(mode="json")
            if snapshot["worker_v4_claim_child_address_space_bytes"] != address_space_bytes:
                raise ValueError("Claim unavailable")
            snapshot["database_url"] = bind.url.render_as_string(hide_password=False)
            channel, child_channel = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            channel.settimeout(min(5.0, deadline - _clock()))
            owned_child = child_channel
            child_channel = None
            spawn, child = self._spawn_child(owned_child, deadline, address_space_bytes)
            pidfd = os.pidfd_open(child.pid, 0)  # type: ignore[attr-defined, unused-ignore]
            if (
                _read_exact(channel, 1) != b"R"
                or spawn.thread is None
                or not spawn.thread.is_alive()
            ):
                raise ValueError("Claim child unavailable")
            self._check_envelope(envelope)
            process.check_authority()
            _send_frame(
                channel,
                {
                    "settings": snapshot,
                    "business_credential_policy": business_credentials.frozen(settings).snapshot(),
                    "control_instance": control_instance,
                    "control_token": control_token,
                    "control_deadline": deadline,
                    "worker_id": worker_id,
                    "instance_id": str(instance_id),
                    "generation": generation,
                    "credential": credential,
                    "instance_proof": instance_proof,
                    "dispatch": message.model_dump(mode="json"),
                    "wire_budget": wire_budget,
                },
            )
            del control_token
            metadata = _read_frame(channel)
            if (
                set(metadata) != {"decision", "size"}
                or metadata["decision"]
                not in {"EXECUTE", "ACK_NOOP", "DEFER", "REJECT_DLQ", "PAUSE_CONSUMER"}
                or type(metadata["size"]) is not int
                or not 0 < metadata["size"] <= wire_budget
                or _clock() >= deadline
            ):
                raise ValueError("Claim unavailable")
            process.check_authority()
            self._check_envelope(envelope)
            entry = _Claim(
                child,
                spawn,
                pidfd,
                channel,
                envelope,
                process,
                secrets.token_urlsafe(32),
                credential,
                worker_id,
                instance_id,
                generation,
                deadline,
                metadata["decision"],
                metadata["size"],
                configuration=snapshot,
                owner=self,
                allocation=self._allocation,
            )
            with self._lock:
                self._live()
                if self._current is not None:
                    raise ValueError("Claim unavailable")
                self._current = entry
                self._spawning = False
                timer = threading.Timer(max(0.0, deadline - _clock()), self._expire, args=(entry,))
                timer.daemon = True
                entry.timer = timer
                published = True
                try:
                    timer.start()
                except BaseException:
                    # start() can fail after creating the timer thread. Latch
                    # the coordinator until the original child is reaped.
                    self._poison()
                    self._expire(entry)
                    raise
            return entry
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise ClaimUnavailable("Claim unavailable") from None
        finally:
            if child_channel is not None:
                child_channel.close()
            if not published:
                if channel is not None:
                    channel.close()
                if envelope is not None:
                    envelope.close()
                self._discard_unpublished(child, pidfd, spawn)
                with self._lock:
                    self._spawning = False

    def stream(self, entry: _Claim) -> Iterator[bytes]:
        """Forward bounded chunks; only a fully read EXECUTE can be confirmed."""
        complete = False
        try:
            remaining = entry.size
            while remaining:
                with self._lock:
                    self._live()
                    if self._current is not entry or entry.state != "streaming":
                        raise ClaimUnavailable("Claim unavailable")
                    entry.authority.check_authority()
                    self._check_envelope(entry.envelope, entry=entry)
                    timeout = min(5.0, entry.deadline - _clock())
                    if timeout <= 0:
                        raise ClaimUnavailable("Claim unavailable")
                    entry.channel.settimeout(timeout)
                chunk = entry.channel.recv(min(65_536, remaining))
                if not chunk:
                    raise ClaimUnavailable("Claim unavailable")
                remaining -= len(chunk)
                if not remaining:
                    with self._lock:
                        self._live()
                        if self._current is not entry or entry.state != "streaming":
                            raise ClaimUnavailable("Claim unavailable")
                        entry.authority.check_authority()
                        self._check_envelope(entry.envelope, entry=entry)
                        if _clock() >= entry.deadline:
                            raise ClaimUnavailable("Claim unavailable")
                        if entry.decision == "EXECUTE":
                            entry.state = "ready"
                        complete = True
                yield chunk
        finally:
            if not complete or entry.decision != "EXECUTE":
                self._expire(entry)

    def finalize(
        self,
        *,
        token: str,
        worker_id: int,
        instance_id: UUID,
        generation: int,
        credential: str,
        instance_proof: str,
    ) -> str:
        with self._lock:
            self._live()
            entry = self._current
            if (
                entry is None
                or entry.state != "ready"
                or not hmac.compare_digest(entry.token, token)
                or entry.worker_id != worker_id
                or entry.instance_id != instance_id
                or entry.generation != generation
                or not hmac.compare_digest(entry.credential, credential)
            ):
                raise ClaimUnavailable("Claim unavailable")
            entry.authority.check_authority()
            self._check_envelope(entry.envelope, entry=entry)
            timeout = min(5.0, entry.deadline - _clock())
            if timeout <= 0:
                raise ClaimUnavailable("Claim unavailable")
            entry.state = "finalizing"
            # Freeze before the child enters finalize, including its DB/IPC
            # work. Neither a later GET nor a renewal can restart this clock.
            entry.input_cutoff = math.nextafter(_clock() + 5.0, -math.inf)
            entry.channel.settimeout(timeout)
        try:
            _send_frame(
                entry.channel,
                {
                    "op": "finalize",
                    "instance_proof": instance_proof,
                    "input_cutoff": entry.input_cutoff,
                },
            )
            response = _read_frame(entry.channel)
            if (
                set(response)
                != {
                    "state",
                    "candidate",
                    "action",
                    "execution_timeout_seconds",
                    "result_idle_until",
                }
                or response["state"] != "committed"
            ):
                raise ClaimUnavailable("Claim unavailable")
            raw = response["candidate"]
            if type(raw) is not dict or set(raw) != {
                "worker_id",
                "attempt_id",
                "instance_id",
                "generation",
                "fencing_token",
                "revision",
                "candidate_id",
            }:
                raise ClaimUnavailable("Claim unavailable")
            candidate = AttemptCandidateRef(
                raw["worker_id"],
                raw["attempt_id"],
                UUID(raw["instance_id"]),
                raw["generation"],
                raw["fencing_token"],
                raw["revision"],
                UUID(raw["candidate_id"]),
            )
            action = AttemptActionBody.model_validate(response["action"])
            if (
                candidate.worker_id != worker_id
                or candidate.instance_id != instance_id
                or candidate.generation != generation
                or candidate.attempt_id != action.attempt_id
                or candidate.fencing_token != action.fencing_token
                or candidate.revision != 1
            ):
                raise ClaimUnavailable("Claim unavailable")
            entry.authority.check_authority()
            self._check_envelope(entry.envelope, entry=entry)
            proof = entry.authority.observe_attempt_candidate(
                candidate, credential, instance_proof, action
            )
            entry.authority.check_authority()
            self._check_envelope(entry.envelope, entry=entry)
            if _clock() >= entry.deadline:
                raise ClaimUnavailable("Claim unavailable")
            self._retain_result_from_claim(
                entry,
                attempt_id=candidate.attempt_id,
                execution_timeout=response["execution_timeout_seconds"],
                idle_until=response["result_idle_until"],
            )
            entry.authority.check_authority()
            if _clock() >= entry.deadline:
                raise ClaimUnavailable("Claim unavailable")
            return proof
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise ClaimUnavailable("Claim unavailable") from None
        finally:
            self._expire(entry)

    def _retain_result_from_claim(
        self, entry: _Claim, *, attempt_id: int, execution_timeout: object, idle_until: object
    ) -> None:
        """Transfer the exact child slot; idle retention grants no execution authority."""
        with self._lock:
            self._live()
            now = _clock()
            if (
                self._current is not entry
                or entry.state != "finalizing"
                or self._result is not None
                or type(attempt_id) is not int
                or not 0 < attempt_id < 2**63
                or now >= entry.deadline
                or type(execution_timeout) is not int
                or not 0 < execution_timeout <= 86_400
                or not isinstance(idle_until, (int, float))
                or not math.isfinite(idle_until)
                or not now < idle_until <= now + execution_timeout + 30
                or entry.process.poll() is not None
            ):
                raise ClaimUnavailable("result retention unavailable")
            entry.authority.check_authority()
            self._check_envelope(entry.envelope, entry=entry)
            entry.state = "closed"
            self._current = None
            if entry.timer is not None:
                entry.timer.cancel()
            retained = _Result(
                entry.process,
                entry.spawn,
                entry.pidfd,
                entry.channel,
                entry.envelope,
                entry.authority,
                float(idle_until),
                0,
                configuration=entry.configuration,
                pending_stage={
                    "operation": "stage",
                    "worker_id": entry.worker_id,
                    "attempt_id": attempt_id,
                    "credential": entry.credential,
                    "instance_id": str(entry.instance_id),
                    "generation": entry.generation,
                },
                retention_deadline=float(idle_until),
                owner=self,
                allocation=self._allocation,
                input_cutoff=entry.input_cutoff,
            )
            self._result = retained
            self._spawning = False
            timer = threading.Timer(
                max(0.0, float(idle_until) - _clock()), self.end_result, args=(retained,)
            )
            timer.daemon = True
            retained.timer = timer
            try:
                timer.start()
            except BaseException:
                self._poison()
                self.end_result(retained)
                raise

    def _discard_unpublished(
        self, process: subprocess.Popen[bytes] | None, pidfd: int | None, spawn: _Spawn | None
    ) -> bool:
        if process is None:
            if pidfd is not None:
                os.close(pidfd)
            return True
        try:
            if process.poll() is None:
                if pidfd is None:
                    raise ValueError("child identity unavailable")
                signal.pidfd_send_signal(pidfd, signal.SIGKILL)  # type: ignore[attr-defined, unused-ignore]
            process.wait(timeout=2)
            if pidfd is not None:
                os.close(pidfd)
            if spawn is not None:
                spawn.release.set()
                if spawn.thread is None:
                    raise ValueError("child creator unavailable")
                spawn.thread.join(timeout=2)
                if spawn.thread.is_alive():
                    raise ValueError("child creator unavailable")
            return True
        except Exception:
            with self._lock:
                self._poison()
                self._unresolved.append((process, pidfd, spawn))
            return False

    def begin_input(self, process: ControlProcess, metadata: dict[str, object]) -> _Input:
        """Use the retained Claim child, never reserve another resource slot."""
        with self._lock:
            self._live()
            entry = self._result
            if (
                self._closing
                or self._spawning
                or self._current is not None
                or entry is None
                or entry.closed
                or entry.input_active
                or entry.pending_stage is None
                or entry.pending_finalize is not None
                or entry.authority is not process
                or entry.retention_deadline is None
                or type(entry.input_cutoff) is not float
                or not math.isfinite(entry.input_cutoff)
                or _clock() >= entry.input_cutoff
                or any(
                    metadata.get(k) != v for k, v in entry.pending_stage.items() if k != "operation"
                )
            ):
                raise ClaimUnavailable("input unavailable")
            self._spawning = True
            entry.input_active = True
        stream = None
        try:
            process.check_authority()
            self._check_envelope(entry.envelope, entry=entry)
            bind = process._sessions.kw.get("bind")
            if not isinstance(bind, Engine) or entry.process.poll() is not None:
                raise ClaimUnavailable("input unavailable")
            configuration = settings.model_dump(mode="json")
            configuration["database_url"] = bind.url.render_as_string(hide_password=False)
            if configuration != entry.configuration:
                raise ClaimUnavailable("input unavailable")
            control_instance, control_token, confirmed = process.claim_child_identity()
            deadline = min(entry.retention_deadline, confirmed, entry.input_cutoff, _clock() + 5.0)
            stream = _Input(entry, deadline, 0)
            if deadline <= _clock():
                # No input command or file I/O has started. Preserve the
                # original Result owner for the known preparation failure.
                self._finish_input(stream)
                raise ClaimUnavailable("input unavailable")
            entry.channel.settimeout(max(0.001, deadline - _clock()))
            _send_frame(
                entry.channel,
                {
                    **metadata,
                    "operation": "input",
                    "settings": entry.configuration,
                    "control_instance": control_instance,
                    "control_token": control_token,
                    "control_deadline": deadline,
                },
            )
            reply = _read_frame(entry.channel)
            if reply == {"state": "input_rejected"}:
                self._finish_input(stream)
                raise ClaimUnavailable("input unavailable")
            size, child_deadline = reply.get("size"), reply.get("control_deadline")
            if (
                reply.get("state") != "input_ready"
                or set(reply)
                != (
                    {"state", "size", "stream_id", "control_deadline"}
                    | ({"closed"} if size == 0 else set())
                )
                or (size == 0 and reply.get("closed") is not True)
                or type(size) is not int
                or not 0 <= size <= 1024 * 1024
                or not isinstance(child_deadline, (int, float))
                or isinstance(child_deadline, bool)
                or not math.isfinite(child_deadline)
                or not _clock() < child_deadline <= deadline
                or type(reply.get("stream_id")) is not str
                or str(UUID(reply["stream_id"])) != reply["stream_id"]
                or UUID(reply["stream_id"]).int == 0
            ):
                raise ClaimUnavailable("input unavailable")
            stream.size, stream.deadline = size, float(child_deadline)
            process.check_authority()
            self._check_envelope(entry.envelope, entry=entry)
            if _clock() >= stream.deadline:
                raise ClaimUnavailable("input unavailable")
            if size == 0:
                self._finish_input(stream)
            return stream
        except BaseException:
            if stream is None or not stream.closed:
                self.end_result(entry)
            raise

    def _finish_input(self, stream: _Input) -> None:
        with self._lock:
            entry = stream.result
            if self._result is not entry or entry.closed or not entry.input_active:
                raise ClaimUnavailable("input unavailable")
            stream.closed = True
            entry.input_active = False
            self._spawning = False

    def read_input(self, stream: _Input) -> bytes:
        with stream.io_lock:
            if stream.closed:
                if stream.received == stream.size:
                    return b""
                raise ClaimUnavailable("input unavailable")
            entry = stream.result
            try:
                with self._lock:
                    self._live()
                    if (
                        self._result is not entry
                        or entry.closed
                        or stream.closed
                        or not entry.input_active
                    ):
                        raise ClaimUnavailable("input unavailable")
                    entry.authority.check_authority()
                    self._check_envelope(entry.envelope, entry=entry)
                    timeout = min(5.0, stream.deadline - _clock())
                    if timeout <= 0:
                        raise ClaimUnavailable("input unavailable")
                    entry.channel.settimeout(timeout)
                _send_frame(entry.channel, {"op": "read_input"})
                reply = _read_frame(entry.channel)
                if reply == {"state": "input_closed"}:
                    if stream.received != stream.size:
                        raise ClaimUnavailable("input unavailable")
                    self._finish_input(stream)
                    return b""
                if reply == {"state": "input_failed"}:
                    self._finish_input(stream)
                    raise ClaimUnavailable("input unavailable")
                count = reply.get("bytes")
                if (
                    set(reply)
                    != {"state", "bytes"} | ({"closed"} if reply.get("closed") is True else set())
                    or reply["state"] != "input_chunk"
                    or type(count) is not int
                    or not 0 < count <= 65_536
                    or stream.received + count > stream.size
                    or (stream.received + count == stream.size) != (reply.get("closed") is True)
                ):
                    raise ClaimUnavailable("input unavailable")
                data = _read_exact(entry.channel, count)
                stream.received += len(data)
                entry.authority.check_authority()
                self._check_envelope(entry.envelope, entry=entry)
                if reply.get("closed") is True:
                    self._finish_input(stream)
                return data
            except BaseException:
                if not stream.closed:
                    self.end_result(entry)
                raise

    def close_input(self, stream: _Input) -> None:
        if stream.closed:
            return
        if not stream.io_lock.acquire(timeout=max(0.0, min(5.0, stream.deadline - _clock()))):
            self.end_result(stream.result)
            return
        try:
            if stream.closed:
                return
            entry = stream.result
            entry.channel.settimeout(max(0.001, min(0.5, stream.deadline - _clock())))
            _send_frame(entry.channel, {"op": "cancel_input"})
            reply = _read_frame(entry.channel)
            if reply not in ({"state": "input_closed"}, {"state": "input_failed"}):
                raise ClaimUnavailable("input unavailable")
            self._finish_input(stream)
        except Exception:
            self.end_result(stream.result)
        finally:
            stream.io_lock.release()

    def begin_result(
        self, process: ControlProcess, *, metadata: dict[str, object], body_bytes: int
    ) -> _Result:
        """Reserve the same single child slot used by Claim and stream a result."""
        with self._lock:
            self._live()
            existing = self._result
            if self._closing or self._spawning or self._current is not None:
                raise ClaimUnavailable("result unavailable")
            if existing is not None:
                incoming = dict(metadata)
                incoming.pop("receipt_nonce", None)
                expected = existing.pending_finalize
                if existing.pending_stage is not None:
                    expected = existing.pending_stage
                    incoming = {name: metadata.get(name) for name in expected}
                if (
                    existing.closed
                    or existing.authority is not process
                    or expected is None
                    or type(body_bytes) is not int
                    or not 0 < body_bytes <= settings.worker_v4_claim_child_address_space_bytes
                    or (existing.pending_stage is None and body_bytes != existing.size)
                    or not hmac.compare_digest(
                        json.dumps(incoming, sort_keys=True, separators=(",", ":")).encode(),
                        json.dumps(expected, sort_keys=True, separators=(",", ":")).encode(),
                    )
                ):
                    raise ClaimUnavailable("result unavailable")
                existing.pending_finalize = None
                existing.pending_stage = None
                existing.metadata = dict(metadata)
                existing.size = body_bytes
                existing.sent = 0
            self._spawning = True
        if existing is not None:
            return self._resume_result(existing, metadata)
        channel = child_channel = None
        child = spawn = None
        pidfd = None
        envelope = None
        published = False
        try:
            process.check_authority()
            address_space = settings.worker_v4_claim_child_address_space_bytes
            if type(body_bytes) is not int or not 0 < body_bytes <= address_space:
                raise ValueError("result unavailable")
            envelope = self._open_envelope(child_address_space_bytes=address_space)
            control_instance, control_token, confirmed_deadline = process.claim_child_identity()
            deadline = min(confirmed_deadline, math.nextafter(_clock() + 25.0, -math.inf))
            if deadline <= _clock():
                raise ValueError("result unavailable")
            bind = process._sessions.kw.get("bind")
            if not isinstance(bind, Engine):
                raise ValueError("result unavailable")
            snapshot = settings.model_dump(mode="json")
            if snapshot["worker_v4_claim_child_address_space_bytes"] != address_space:
                raise ValueError("result unavailable")
            snapshot["database_url"] = bind.url.render_as_string(hide_password=False)
            channel, child_channel = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            channel.settimeout(min(5.0, deadline - _clock()))
            owned_child, child_channel = child_channel, None
            spawn, child = self._spawn_child(
                owned_child, deadline, address_space, module="dlr.control.services.result_child_v4"
            )
            pidfd = os.pidfd_open(child.pid, 0)  # type: ignore[attr-defined, unused-ignore]
            if (
                _read_exact(channel, 1) != b"R"
                or spawn.thread is None
                or not spawn.thread.is_alive()
            ):
                raise ValueError("result child unavailable")
            self._check_envelope(envelope)
            process.check_authority()
            _send_frame(
                channel,
                {
                    **metadata,
                    "settings": snapshot,
                    "control_instance": control_instance,
                    "control_token": control_token,
                    "control_deadline": deadline,
                    "body_bytes": body_bytes,
                },
            )
            del control_token
            if _read_frame(channel) != {"state": "ready"}:
                raise ValueError("result child unavailable")
            self._check_envelope(envelope)
            process.check_authority()
            entry = _Result(
                child,
                spawn,
                pidfd,
                channel,
                envelope,
                process,
                deadline,
                body_bytes,
                metadata=dict(metadata),
                configuration=snapshot,
                owner=self,
                allocation=self._allocation,
            )
            with self._lock:
                self._live()
                if self._result is not None or self._current is not None:
                    raise ValueError("result unavailable")
                self._result = entry
                timer = threading.Timer(
                    max(0.0, deadline - _clock()), self.end_result, args=(entry,)
                )
                timer.daemon = True
                entry.timer = timer
                published = True
                try:
                    timer.start()
                except BaseException:
                    self._poison()
                    self.end_result(entry)
                    raise
            return entry
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise ClaimUnavailable("result unavailable") from None
        finally:
            if child_channel is not None:
                child_channel.close()
            if not published:
                if channel is not None:
                    channel.close()
                if envelope is not None:
                    envelope.close()
                self._discard_unpublished(child, pidfd, spawn)
                with self._lock:
                    self._spawning = False

    def _resume_result(self, entry: _Result, metadata: dict[str, object]) -> _Result:
        """End idle retention with fresh authority; never extend an intent deadline."""
        try:
            entry.authority.check_authority()
            self._check_envelope(entry.envelope, entry=entry)
            bind = entry.authority._sessions.kw.get("bind")
            if not isinstance(bind, Engine):
                raise ValueError("result unavailable")
            configuration = settings.model_dump(mode="json")
            configuration["database_url"] = bind.url.render_as_string(hide_password=False)
            if configuration != entry.configuration or entry.process.poll() is not None:
                raise ValueError("result unavailable")
            control_instance, control_token, confirmed = entry.authority.claim_child_identity()
            if entry.metadata.get("operation") == "stage" and entry.retention_deadline is not None:
                # The child was only idle while business ran. This operation
                # gets a fresh, separately confirmed Control deadline, capped
                # by its original bounded resource lifetime.
                with self._lock:
                    if self._result is not entry or entry.closed:
                        raise ValueError("result unavailable")
                    entry.deadline = min(
                        entry.retention_deadline,
                        confirmed,
                        math.nextafter(_clock() + 25, -math.inf),
                    )
                    if entry.timer is not None:
                        entry.timer.cancel()
                    timer = threading.Timer(
                        max(0.0, entry.deadline - _clock()), self.end_result, args=(entry,)
                    )
                    timer.daemon = True
                    entry.timer = timer
                    try:
                        timer.start()
                    except BaseException:
                        self._poison()
                        raise
            timeout = min(5.0, entry.deadline - _clock())
            if timeout <= 0:
                raise ValueError("result unavailable")
            entry.channel.settimeout(timeout)
            _send_frame(
                entry.channel,
                {
                    **metadata,
                    "settings": configuration,
                    "control_instance": control_instance,
                    "control_token": control_token,
                    "control_deadline": entry.deadline,
                    "body_bytes": entry.size,
                },
            )
            if _read_frame(entry.channel) != {"state": "ready"}:
                raise ValueError("result child unavailable")
            with self._lock:
                self._live()
                if self._result is not entry or entry.closed or _clock() >= entry.deadline:
                    raise ValueError("result unavailable")
                entry.authority.check_authority()
                self._check_envelope(entry.envelope, entry=entry)
            return entry
        except BaseException as error:
            self.end_result(entry)
            if not isinstance(error, Exception):
                raise
            raise ClaimUnavailable("result unavailable") from None

    def send_result(self, entry: _Result, data: bytes) -> None:
        with self._lock:
            self._live()
            if (
                self._result is not entry
                or entry.closed
                or type(data) is not bytes
                or len(data) > 65_536
                or entry.sent + len(data) > entry.size
            ):
                raise ClaimUnavailable("result unavailable")
            entry.authority.check_authority()
            self._check_envelope(entry.envelope, entry=entry)
            timeout = min(5.0, entry.deadline - _clock())
            if timeout <= 0:
                raise ClaimUnavailable("result unavailable")
            entry.channel.settimeout(timeout)
        entry.channel.sendall(data)
        with self._lock:
            self._live()
            if self._result is not entry or entry.closed:
                raise ClaimUnavailable("result unavailable")
            entry.sent += len(data)

    def finish_result(self, entry: _Result) -> dict[str, object]:
        with self._lock:
            self._live()
            if self._result is not entry or entry.closed or entry.sent != entry.size:
                raise ClaimUnavailable("result unavailable")
            entry.authority.check_authority()
            self._check_envelope(entry.envelope, entry=entry)
            timeout = min(5.0, entry.deadline - _clock())
            if timeout <= 0:
                raise ClaimUnavailable("result unavailable")
            entry.channel.settimeout(timeout)
        retained = False
        try:
            response = _read_frame(entry.channel)
            entry.authority.check_authority()
            self._check_envelope(entry.envelope, entry=entry)
            if entry.metadata.get("operation") == "stage":
                if (
                    set(response) != {"intent_id", "result_proof"}
                    or type(response["intent_id"]) is not str
                    or type(response["result_proof"]) is not str
                    or entry.process.poll() is not None
                ):
                    raise ClaimUnavailable("result unavailable")
                with self._lock:
                    self._live()
                    if self._result is not entry or entry.closed or _clock() >= entry.deadline:
                        raise ClaimUnavailable("result unavailable")
                    entry.pending_finalize = {
                        **entry.metadata,
                        "operation": "finalize",
                        "intent_id": response["intent_id"],
                        "result_proof": response["result_proof"],
                    }
                    self._spawning = False
            elif _clock() >= entry.deadline or not self.end_result(entry):
                raise ClaimUnavailable("result unavailable")
            entry.authority.check_authority()
            if _clock() >= entry.deadline:
                raise ClaimUnavailable("result unavailable")
            retained = entry.metadata.get("operation") == "stage"
            return response
        finally:
            if not retained:
                self.end_result(entry)

    def end_result(self, entry: _Result) -> bool:
        with self._lock:
            if self._result is not entry or entry.closed:
                return False
            if entry.input_active:
                # A lost read/HTTP scope is not proof that its actual I/O
                # ended. Keep this allocation unavailable, even after reap.
                self._poison()
                self._unresolved.append(entry)
            entry.closed = True
            self._result = None
            self._closing = True
            if entry.timer is not None:
                entry.timer.cancel()
        try:
            with suppress(OSError):
                entry.channel.shutdown(socket.SHUT_RDWR)
            entry.channel.close()
            entry.envelope.close()
            return self._discard_unpublished(entry.process, entry.pidfd, entry.spawn)
        except BaseException:
            self._poison()
            with self._lock:
                self._unresolved.append(entry)
            with suppress(Exception):
                self._discard_unpublished(entry.process, entry.pidfd, entry.spawn)
            raise
        finally:
            with self._lock:
                self._spawning = False
                self._closing = False
            self._notify_idle()

    def _expire(self, entry: _Claim) -> bool:
        with self._lock:
            if self._current is not entry or entry.state == "closed":
                return False
            entry.state = "closed"
            self._current = None
            self._closing = True
            if entry.timer is not None:
                entry.timer.cancel()
        try:
            with suppress(OSError):
                entry.channel.shutdown(socket.SHUT_RDWR)
            entry.channel.close()
            entry.envelope.close()
            return self._discard_unpublished(entry.process, entry.pidfd, entry.spawn)
        except BaseException:
            self._poison()
            with self._lock:
                self._unresolved.append(entry)
            with suppress(Exception):
                self._discard_unpublished(entry.process, entry.pidfd, entry.spawn)
            raise
        finally:
            with self._lock:
                self._closing = False
            self._notify_idle()

    def close(self) -> None:
        with self._lock:
            self._poison()
            entry = self._current
            result = self._result
        if entry is not None:
            self._expire(entry)
        if result is not None:
            self.end_result(result)


class ClaimCapacityBusy(ClaimUnavailable):
    """This prepare has not spawned or opened a Claim transaction."""


class ClaimCoordinator:
    """A fixed registry; no IO or child waits under its allocation lock.

    Slots include retained results and closing/unknown children. Slot objects
    are allocated once, independent of the Worker catalog or request count.
    """

    def __init__(self) -> None:
        limit = settings.worker_v4_control_slots
        if type(limit) is not int or limit not in {1, 2}:
            raise ClaimUnavailable("Control slot limit unavailable")
        self._pid = os.getpid()
        self._limit = limit
        self._address_space = settings.worker_v4_claim_child_address_space_bytes
        self._lock = threading.Lock()
        self._resource_lock = threading.Lock()
        self._envelope: ClaimEnvelope | None = None
        self._poisoned = threading.Event()
        self._shutdown_failures: list[BaseException] = []
        self._slots = tuple(_ClaimSlot(self) for _ in range(limit))

    def _live(self) -> None:
        if (
            self._pid != os.getpid()
            or self._poisoned.is_set()
            or type(settings.worker_v4_control_slots) is not int
            or settings.worker_v4_control_slots != self._limit
            or settings.worker_v4_claim_child_address_space_bytes != self._address_space
        ):
            raise ClaimUnavailable("Claim unavailable")

    def _check_resources(self, process: ControlProcess) -> None:
        self._live()
        process.check_authority()
        try:
            with self._resource_lock:
                if self._envelope is None:
                    self._envelope = ClaimEnvelope.open(
                        child_address_space_bytes=self._address_space
                    )
                self._envelope.check()
        except Exception:
            self._poisoned.set()
            raise ClaimUnavailable("Control resources unavailable") from None
        process.check_authority()
        self._live()

    def _take(self, worker_id: object, *, existing: bool) -> _ClaimSlot:
        if type(worker_id) is not int or not 0 < worker_id < 2**63:
            raise ClaimUnavailable("Claim unavailable")
        with self._lock:
            self._live()
            slot = next((item for item in self._slots if item._worker_id == worker_id), None)
            if slot is not None:
                if not existing:
                    raise ClaimCapacityBusy("Control transport busy")
            else:
                slot = next((item for item in self._slots if item._worker_id is None), None)
                if slot is None:
                    raise ClaimCapacityBusy("Control transport busy")
                slot._worker_id = worker_id
                slot._allocation += 1
            slot._calls += 1
            return slot

    def _entry_slot(self, entry: _Claim | _Result) -> _ClaimSlot:
        with self._lock:
            slot = entry.owner
            if (
                slot not in self._slots
                or slot is None
                or slot._allocation != entry.allocation
                or slot._worker_id is None
            ):
                raise ClaimUnavailable("Claim unavailable")
            slot._calls += 1
            return slot

    def _return(self, slot: _ClaimSlot) -> None:
        with self._lock:
            slot._calls -= 1
        slot._notify_idle()

    def _release_idle(self, slot: _ClaimSlot, allocation: int) -> None:
        with self._lock:
            if slot._allocation == allocation and slot._calls == 0 and not self._poisoned.is_set():
                slot._worker_id = None

    def prepare(
        self,
        process: ControlProcess,
        *,
        worker_id: int,
        instance_id: UUID,
        generation: int,
        credential: str,
        instance_proof: str,
        message: DispatchMessage,
        wire_budget: int,
    ) -> _Claim:
        self._check_resources(process)
        slot = self._take(worker_id, existing=False)
        try:
            return slot.prepare(
                process,
                worker_id=worker_id,
                instance_id=instance_id,
                generation=generation,
                credential=credential,
                instance_proof=instance_proof,
                message=message,
                wire_budget=wire_budget,
            )
        finally:
            self._return(slot)

    def stream(self, entry: _Claim) -> Iterator[bytes]:
        slot = self._entry_slot(entry)
        try:
            yield from slot.stream(entry)
        finally:
            self._return(slot)

    def finalize(
        self,
        *,
        token: str,
        worker_id: int,
        instance_id: UUID,
        generation: int,
        credential: str,
        instance_proof: str,
    ) -> str:
        with self._lock:
            self._live()
            slot = next((item for item in self._slots if item._worker_id == worker_id), None)
            if slot is None:
                raise ClaimUnavailable("Claim unavailable")
            slot._calls += 1
        try:
            return slot.finalize(
                token=token,
                worker_id=worker_id,
                instance_id=instance_id,
                generation=generation,
                credential=credential,
                instance_proof=instance_proof,
            )
        finally:
            self._return(slot)

    def begin_result(
        self, process: ControlProcess, *, metadata: dict[str, object], body_bytes: int
    ) -> _Result:
        self._check_resources(process)
        slot = self._take(metadata.get("worker_id"), existing=True)
        try:
            return slot.begin_result(process, metadata=metadata, body_bytes=body_bytes)
        finally:
            self._return(slot)

    def begin_input(self, process: ControlProcess, *, metadata: dict[str, object]) -> _Input:
        self._check_resources(process)
        slot = self._take(metadata.get("worker_id"), existing=True)
        try:
            return slot.begin_input(process, metadata)
        finally:
            self._return(slot)

    def read_input(self, entry: _Input) -> bytes:
        slot = self._entry_slot(entry.result)
        try:
            return slot.read_input(entry)
        finally:
            self._return(slot)

    def close_input(self, entry: _Input) -> None:
        if entry.closed:
            return
        slot = self._entry_slot(entry.result)
        try:
            slot.close_input(entry)
        finally:
            self._return(slot)

    def send_result(self, entry: _Result, data: bytes) -> None:
        slot = self._entry_slot(entry)
        try:
            slot.send_result(entry, data)
        finally:
            self._return(slot)

    def finish_result(self, entry: _Result) -> dict[str, object]:
        slot = self._entry_slot(entry)
        try:
            return slot.finish_result(entry)
        finally:
            self._return(slot)

    def end_result(self, entry: _Result) -> bool:
        try:
            slot = self._entry_slot(entry)
        except ClaimUnavailable:
            return False
        try:
            return slot.end_result(entry)
        finally:
            self._return(slot)

    def close(self) -> None:
        self._poisoned.set()
        failures: list[BaseException] = []
        for slot in self._slots:
            try:
                slot.close()
            except BaseException as error:
                failures.append(error)
        with self._resource_lock:
            if self._envelope is not None:
                try:
                    self._envelope.close()
                except BaseException as error:
                    failures.append(error)
        self._shutdown_failures.extend(failures)
        if failures:
            # Retain the objects/errors and report an incomplete shutdown, but
            # let the app stop its Control authority and other resources too.
            logger.error("Control transport shutdown incomplete: failures=%d", len(failures))
            for failure in failures:
                if not isinstance(failure, Exception):
                    raise failure
