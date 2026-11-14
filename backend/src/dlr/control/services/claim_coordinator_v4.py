"""Bounded Control-side ownership for one two-phase v4 Claim at a time."""

from __future__ import annotations

import hmac
import json
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
from dlr.control.services.attempt_lease_candidates_v4 import AttemptCandidateRef
from dlr.control.services.claim_child_v4 import _clock, _read_exact, _read_frame, _send_frame
from dlr.control.services.claim_envelope_v4 import ClaimEnvelope
from dlr.control.services.control_process_v4 import ControlProcess
from dlr.control.services.dispatch import DispatchMessage


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


class ClaimCoordinator:
    """Serialize preparation, retain unknown children and never log secrets.

    This coordinator is process-local; Wave 1 requires one Control process so
    that the confirmation reaches the process holding the prepared snapshot.
    """

    def __init__(self) -> None:
        self._pid = os.getpid()
        self._lock = threading.RLock()
        self._current: _Claim | None = None
        self._result: _Result | None = None
        self._spawning = False
        self._poisoned = False
        self._unresolved: list[object] = []

    def _live(self) -> None:
        if self._pid != os.getpid() or self._poisoned:
            raise ClaimUnavailable("Claim unavailable")

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
                    pidfd = os.pidfd_open(process.pid, 0)  # type: ignore[attr-defined]
                    if process.poll() is None:
                        signal.pidfd_send_signal(pidfd, signal.SIGKILL)  # type: ignore[attr-defined]
                    process.wait(timeout=2)
                    os.close(pidfd)
                except Exception:
                    with self._lock:
                        self._poisoned = True
                        self._unresolved.append((process, pidfd, ticket))

        try:
            ticket.thread = threading.Thread(target=launch, name="dlr-claim-child", daemon=True)
            ticket.thread.start()
        except BaseException as error:
            with ticket.lock:
                ticket.abandoned = True
            ticket.release.set()
            with self._lock:
                self._poisoned = True
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
                self._poisoned = True
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
            if self._spawning or self._current is not None or self._result is not None:
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
            envelope = ClaimEnvelope.open(child_address_space_bytes=address_space_bytes)
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
            pidfd = os.pidfd_open(child.pid, 0)  # type: ignore[attr-defined]
            if (
                _read_exact(channel, 1) != b"R"
                or spawn.thread is None
                or not spawn.thread.is_alive()
            ):
                raise ValueError("Claim child unavailable")
            envelope.check()
            process.check_authority()
            _send_frame(
                channel,
                {
                    "settings": snapshot,
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
            envelope.check()
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
                    self._poisoned = True
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
                    entry.envelope.check()
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
                        entry.envelope.check()
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
            entry.envelope.check()
            timeout = min(5.0, entry.deadline - _clock())
            if timeout <= 0:
                raise ClaimUnavailable("Claim unavailable")
            entry.state = "finalizing"
            entry.channel.settimeout(timeout)
        try:
            _send_frame(entry.channel, {"op": "finalize", "instance_proof": instance_proof})
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
            entry.envelope.check()
            proof = entry.authority.observe_attempt_candidate(
                candidate, credential, instance_proof, action
            )
            entry.authority.check_authority()
            entry.envelope.check()
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
            entry.envelope.check()
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
                self._poisoned = True
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
                signal.pidfd_send_signal(pidfd, signal.SIGKILL)  # type: ignore[attr-defined]
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
                self._poisoned = True
                self._unresolved.append((process, pidfd, spawn))
            return False

    def begin_result(
        self, process: ControlProcess, *, metadata: dict[str, object], body_bytes: int
    ) -> _Result:
        """Reserve the same single child slot used by Claim and stream a result."""
        with self._lock:
            self._live()
            existing = self._result
            if self._spawning or self._current is not None:
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
            envelope = ClaimEnvelope.open(child_address_space_bytes=address_space)
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
            pidfd = os.pidfd_open(child.pid, 0)  # type: ignore[attr-defined]
            if (
                _read_exact(channel, 1) != b"R"
                or spawn.thread is None
                or not spawn.thread.is_alive()
            ):
                raise ValueError("result child unavailable")
            envelope.check()
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
            envelope.check()
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
                    self._poisoned = True
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
            entry.envelope.check()
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
                        self._poisoned = True
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
                entry.envelope.check()
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
            entry.envelope.check()
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
            entry.envelope.check()
            timeout = min(5.0, entry.deadline - _clock())
            if timeout <= 0:
                raise ClaimUnavailable("result unavailable")
            entry.channel.settimeout(timeout)
        retained = False
        try:
            response = _read_frame(entry.channel)
            entry.authority.check_authority()
            entry.envelope.check()
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
            entry.closed = True
            self._result = None
            if entry.timer is not None:
                entry.timer.cancel()
            with suppress(OSError):
                entry.channel.shutdown(socket.SHUT_RDWR)
            entry.channel.close()
            entry.envelope.close()
            reaped = self._discard_unpublished(entry.process, entry.pidfd, entry.spawn)
            self._spawning = False
            return reaped

    def _expire(self, entry: _Claim) -> bool:
        with self._lock:
            if self._current is not entry or entry.state == "closed":
                return False
            entry.state = "closed"
            self._current = None
            if entry.timer is not None:
                entry.timer.cancel()
            with suppress(OSError):
                entry.channel.shutdown(socket.SHUT_RDWR)
            entry.channel.close()
            entry.envelope.close()
            return self._discard_unpublished(entry.process, entry.pidfd, entry.spawn)

    def close(self) -> None:
        with self._lock:
            self._poisoned = True
            entry = self._current
            result = self._result
        if entry is not None:
            self._expire(entry)
        if result is not None:
            self.end_result(result)
