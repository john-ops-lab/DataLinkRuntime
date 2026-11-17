"""Reserve, journal and confirm a bounded v4 fixed-Worker Claim."""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import re
import select
import signal
import socket
import stat
import struct
import sys
import threading
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from uuid import UUID, uuid4

from dlr.common.dispatch_protocol import deserialize_dispatch_message
from dlr.worker import cgroup_namespace, claim_http_v4, journal_v4, sandbox, workspace
from dlr.worker.attempt_session_v4 import AttemptLeaseSession
from dlr.worker.client import WorkerInstance
from dlr.worker.lease_clock_v4 import lease_clock
from dlr.worker.lease_session_v4 import WorkerLeaseSession

logger = logging.getLogger("dlr.worker.v4.claim")


@dataclass(frozen=True)
class ClaimIdentity:
    """Small immutable ownership metadata; no business snapshot or token."""

    worker_identity_id: UUID
    instance_id: UUID
    instance_generation: int
    execution_id: int
    attempt_id: int
    attempt_no: int
    fencing_token: int


@dataclass(eq=False)
class PreparedClaim:
    """Keep the task, proof, durable records and both reservations together.

    This handle is not a physical-execution or cleanup receipt. Its caller
    must keep it until supervision and cleanup actually settle the Attempt.
    """

    task: claim_http_v4._Spawn = field(repr=False)
    pidfd: int = field(repr=False)
    channel: socket.socket = field(repr=False)
    receive_reservation: sandbox.ResourceReservation = field(repr=False)
    attempt_reservation: sandbox.ResourceReservation = field(repr=False)
    attempt: AttemptLeaseSession = field(repr=False)
    journal: dict[str, Any] = field(repr=False)
    intent: dict[str, object] = field(repr=False)
    intent_names: tuple[str, str] = field(repr=False)
    task_fingerprint: str = field(repr=False)
    identity: ClaimIdentity
    resource_profile: Mapping[str, object] = field(repr=False)
    renew_seconds: int
    child_address_space_bytes: int
    result_wire_budget: int
    deadline: float
    business_credentials: bool = False
    managed_files: bool = False
    execution_deadline: float | None = None
    input_deadline: float | None = None
    dispatch: dict[str, Any] = field(default_factory=dict, repr=False)
    stage_state: str = "claimed"

    @property
    def payload(self) -> ClaimIdentity:
        """Compatibility for the cgroup owner; business data stays in the child."""
        return self.identity

    @property
    def reservation(self) -> sandbox.ResourceReservation:
        return self.attempt_reservation


def check_binding(
    prepared: PreparedClaim,
    instance: WorkerInstance,
    config: sandbox.SandboxConfig,
    runtime_root: Path,
    journal_root: Path,
) -> None:
    """Recheck the live identity, resource reservation and durable journal."""
    if type(prepared) is not PreparedClaim or type(instance) is not WorkerInstance:
        raise ValueError("invalid prepared Claim")
    identity = prepared.identity
    if (
        type(identity) is not ClaimIdentity
        or identity.worker_identity_id != instance.worker_identity_id
        or identity.instance_id != instance.instance_id
        or identity.instance_generation != instance.instance_generation
        or config.namespace_identity is None
        or sandbox.validate_resource_profile(prepared.resource_profile, config)
        != prepared.attempt_reservation.limits
    ):
        raise ValueError("invalid prepared Claim")
    record = journal_v4.load(
        journal_root,
        runtime_root,
        execution_id=identity.execution_id,
        attempt_id=identity.attempt_id,
        kind="attempt",
    )
    if (
        record is None
        or record != prepared.journal
        or record.get("journal_version") != 2
        or record["worker_identity_id"] != str(identity.worker_identity_id)
        or record["instance_id"] != str(identity.instance_id)
        or record["instance_generation"] != identity.instance_generation
        or record["execution_id"] != identity.execution_id
        or record["attempt_id"] != identity.attempt_id
        or record["attempt_no"] != identity.attempt_no
        or record["fencing_token"] != identity.fencing_token
        or record["namespace_identity"] != config.namespace_identity.marker()
    ):
        raise ValueError("invalid prepared Claim")
    journal_v4.write(journal_root, runtime_root, record, kind="attempt")


@dataclass(frozen=True)
class ClaimPreparation:
    state: Literal["blocked", "decision", "unknown", "claimed"]
    reason: str
    decision: str | None = None
    retry_after_seconds: int | None = None
    claimed: PreparedClaim | None = field(default=None, repr=False)


@dataclass(eq=False)
class StagedClaim:
    """Live local bindings; none of these fields is a cleanup receipt."""

    prepared: PreparedClaim = field(repr=False)
    cgroup: Any = field(repr=False)
    workspace: Any = field(repr=False)
    launch_fd: int = field(repr=False)
    launch_max_bytes: int
    redaction_fd: int = field(default=-1, repr=False)
    redaction_identity: tuple[int, int, int] | None = field(default=None, repr=False)
    preparation_failed: bool = False
    input_files: list[dict[str, Any]] = field(default_factory=list, repr=False)


class ClaimPreparer:
    """One in-flight Claim; uncertain local responsibility blocks further intake."""

    def __init__(self, *, business_credentials: bool = False, managed_files: bool = False) -> None:
        if type(business_credentials) is not bool or type(managed_files) is not bool:
            raise ValueError("Worker capability unavailable")
        self._business_credentials = business_credentials
        self._managed_files = managed_files
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._poisoned = False
        self._active: PreparedClaim | None = None
        self._staged: StagedClaim | None = None
        self._retained: list[object] = []

    @staticmethod
    def _stage_frame(
        prepared: PreparedClaim, deadline: float, stop: threading.Event
    ) -> dict[str, Any]:
        size = struct.unpack("!I", claim_http_v4._receive(prepared.channel, 4, deadline, (stop,)))[
            0
        ]
        if not 0 < size <= 4096:
            raise ValueError("task handoff unavailable")
        result = json.loads(claim_http_v4._receive(prepared.channel, size, deadline, (stop,)))
        if type(result) is not dict:
            raise ValueError("task handoff unavailable")
        return result

    @staticmethod
    def _stage_send(
        prepared: PreparedClaim, value: dict[str, Any], deadline: float, stop: threading.Event
    ) -> None:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        if len(raw) > 16_384:
            raise ValueError("input authorization unavailable")
        claim_http_v4._send_all(
            prepared.channel, struct.pack("!I", len(raw)) + raw, deadline, (stop,)
        )

    def _authorize_file(
        self, prepared: PreparedClaim, ordinal: int, deadline: float, stop: threading.Event
    ) -> None:
        attempt, parent = prepared.attempt, prepared.attempt._parent
        # Match the existing Attempt -> instance-session order. Hold only
        # until Control admission; never wait for a body under these locks.
        with attempt._lock, parent._lock:
            _, proof, current = attempt._live_locked()
            if not attempt._start_confirmed or parent._proof is None or parent._claims is None:
                raise ValueError("input authority unavailable")
            deadline = min(deadline, current)
            self._stage_send(
                prepared,
                {
                    "base_url": parent._base_url,
                    "worker_id": parent._worker_id,
                    "instance_id": str(parent._instance_id),
                    "generation": parent._claims.generation,
                    "credential": parent._credential,
                    "instance_proof": parent._proof,
                    "attempt_proof": proof,
                    "claim_token": attempt._claim_token,
                    "deadline": deadline,
                },
                deadline,
                stop,
            )
            admitted = self._stage_frame(prepared, deadline, stop)
            if admitted not in (
                {"state": "input_admitted", "ordinal": ordinal},
                {"state": "input_rejected", "ordinal": ordinal},
            ):
                raise ValueError("input admission unavailable")
            attempt._live_locked()
        self._stage_send(prepared, {"op": "read_input"}, deadline, stop)

    @staticmethod
    def _entry(root_lock: cgroup_namespace.RootLockHandle, root: Path) -> int:
        item = next((item for item in root_lock._entries if item.path == root), None)
        if item is None:
            raise ValueError("Claim journal unavailable")
        info = os.fstat(item.directory_fd)
        if (info.st_dev, info.st_ino) != item.directory_identity:
            raise ValueError("Claim journal unavailable")
        return item.directory_fd

    @staticmethod
    def _write_intent(directory: int, record: dict[str, object]) -> str:
        name = f"claim-{uuid4().hex}.pending.json"
        raw = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
        return name

    @staticmethod
    def _alive(pidfd: int) -> bool:
        poller = select.poll()
        poller.register(pidfd, select.POLLIN | select.POLLERR | select.POLLHUP)
        return not poller.poll(0)

    def _stop_uncommitted(
        self,
        ticket: claim_http_v4._Spawn | None,
        pidfd: int | None,
        channel: socket.socket | None,
    ) -> bool:
        """Reap the exact child before a pre-Claim reservation may be released."""
        if ticket is None or ticket.process is None:
            if channel is not None:
                channel.close()
            if ticket is not None:
                self._poisoned = True
                self._retained.append(ticket)
            return ticket is None
        process = ticket.process
        try:
            if pidfd is None:
                raise ValueError("Claim child unavailable")
            if self._alive(pidfd):
                signal.pidfd_send_signal(pidfd, signal.SIGKILL)  # type: ignore[attr-defined, unused-ignore]
            if not claim_http_v4._reap(process, pidfd, claim_http_v4._clock() + 2.0):
                raise ValueError("Claim child unavailable")
            if not claim_http_v4._join_creator(ticket):
                raise ValueError("Claim creator unavailable")
            os.close(pidfd)
            if channel is not None:
                channel.close()
            return True
        except BaseException as error:
            self._poisoned = True
            claim_http_v4._retain(process, pidfd, channel, ticket.release)
            self._retained.append(ticket)
            if not isinstance(error, Exception):
                raise
            return False

    @staticmethod
    def _receive_sealed_launch(channel: socket.socket, deadline: float, limit: int) -> int:
        claim_http_v4._wait_socket(channel, write=False, deadline=deadline)
        descriptors: list[int] = []
        try:
            cloexec = getattr(socket, "MSG_CMSG_CLOEXEC", None)
            if not isinstance(cloexec, int) or isinstance(cloexec, bool):
                raise ValueError("launch handoff unavailable")
            data, ancillary, flags, _ = channel.recvmsg(
                1, socket.CMSG_SPACE(struct.calcsize("i") * 4), cloexec
            )
            invalid = False
            for level, kind, raw in ancillary:
                if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                    invalid = True
                    continue
                size = struct.calcsize("i")
                invalid |= bool(len(raw) % size)
                descriptors.extend(
                    value[0] for value in struct.iter_unpack("i", raw[: len(raw) // size * size])
                )
            if (
                invalid
                or data != b"D"
                or flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC)
                or len(descriptors) != 1
            ):
                raise ValueError("launch handoff unavailable")
            fd = descriptors[0]
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or not 0 < info.st_size <= limit
                or info.st_nlink != 0
                or fcntl.fcntl(fd, fcntl.F_GET_SEALS) & sandbox._launch_seals()  # type: ignore[attr-defined, unused-ignore]
                != sandbox._launch_seals()
            ):
                raise ValueError("launch handoff unavailable")
            descriptors.clear()
            return fd
        finally:
            for fd in descriptors:
                with suppress(OSError):
                    os.close(fd)

    def stage_execution(
        self,
        prepared: PreparedClaim,
        *,
        root_lock: cgroup_namespace.RootLockHandle,
        runtime_root: Path,
        attempt_journal_root: Path,
        sandbox_config: sandbox.SandboxConfig,
        budget: sandbox.ResourceBudget,
        stop: threading.Event,
    ) -> StagedClaim | None:
        """Start the Attempt and hand its snapshot to a bound private workspace.

        A failure after entry retains the journal and both charges. The caller
        must stop intake and reconcile it; no returned value proves execution.
        """
        from dlr.worker import cgroup_intent_v4, recovery_checkpoint_v4, workspace_live_v4

        with self._lock:
            if (
                self._pid != os.getpid()
                or self._poisoned
                or self._active is not prepared
                or prepared.stage_state != "claimed"
                or stop.is_set()
            ):
                return None
            prepared.stage_state = "staging"
            live_cgroup = live_workspace = None
            launch_fd = redaction_fd = -1
            try:
                started = claim_http_v4._clock()
                prepared.attempt.start()
                if prepared.managed_files:
                    prepared.execution_deadline = math.nextafter(
                        started + prepared.reservation.limits.execution_timeout_seconds, -math.inf
                    )
                instance = WorkerInstance(
                    prepared.identity.worker_identity_id,
                    prepared.identity.instance_id,
                    prepared.identity.instance_generation,
                )
                result, live_cgroup = cgroup_intent_v4.create_live_cgroup(
                    prepared,
                    instance=instance,
                    config=sandbox_config,
                    budget=budget,
                    runtime_root=runtime_root,
                    attempt_journal_root=attempt_journal_root,
                    root_lock=root_lock,
                )
                if live_cgroup is None or result.state != "bound_observation":
                    raise ValueError("cgroup unavailable")
                result_workspace, live_workspace = workspace_live_v4.create_live_workspace(
                    live_cgroup
                )
                if live_workspace is None or result_workspace.state != "bound_observation":
                    raise ValueError("workspace unavailable")
                deadline = min(prepared.deadline, prepared.attempt.local_deadline())
                if stop.is_set() or not self._alive(prepared.pidfd):
                    raise ValueError("task authority unavailable")
                launch_max = min(prepared.reservation.limits.memory_bytes, 65_536)
                with live_workspace.borrow() as directory:
                    with live_cgroup._mutex:
                        live_workspace._external_io_inflight = True
                    info = os.fstat(directory)
                    command = {
                        "op": "stage",
                        "workspace": prepared.journal["workspace_path"],
                        "workspace_device": info.st_dev,
                        "workspace_inode": info.st_ino,
                        "staging_bytes": prepared.reservation.limits.tmp_bytes,
                        "launch_bytes": launch_max,
                        "fingerprint": prepared.task_fingerprint,
                    }
                    input_deadline = deadline
                    if prepared.managed_files:
                        if prepared.execution_deadline is None or prepared.input_deadline is None:
                            raise ValueError("input deadline unavailable")
                        input_deadline = min(
                            deadline, prepared.execution_deadline, prepared.input_deadline
                        )
                        command["input_deadline"] = input_deadline
                    raw = json.dumps(command, separators=(",", ":")).encode("utf-8")
                    claim_http_v4._send_all(
                        prepared.channel, struct.pack("!I", len(raw)) + raw, deadline, (stop,)
                    )
                    claim_http_v4._send_fd(prepared.channel, directory, deadline, (stop,))
                    expected = {
                        "state": "input_staging" if prepared.managed_files else "staged",
                        "fingerprint": prepared.task_fingerprint,
                        "launch_bytes": launch_max,
                    }
                    if self._stage_frame(prepared, input_deadline, stop) != expected:
                        raise ValueError("task handoff unavailable")
                    launch_fd = self._receive_sealed_launch(
                        prepared.channel, input_deadline, launch_max
                    )
                    input_files: list[dict[str, Any]] = []
                    failed, reaped = False, False
                    if prepared.managed_files:
                        from dlr.worker import managed_files_v4

                        ordinal = 0
                        try:
                            while True:
                                answer = self._stage_frame(prepared, input_deadline, stop)
                                if answer == {"state": "input_authorization", "ordinal": ordinal}:
                                    if ordinal >= 4:
                                        raise ValueError("input count unavailable")
                                    self._authorize_file(prepared, ordinal, input_deadline, stop)
                                    ordinal += 1
                                    continue
                                if (
                                    set(answer) != {"state", "fingerprint", "launch_bytes", "files"}
                                    or answer["fingerprint"] != prepared.task_fingerprint
                                    or answer["launch_bytes"] != launch_max
                                    or answer["state"] not in {"staged", "preparation_failed"}
                                ):
                                    raise ValueError("input staging unavailable")
                                input_files = managed_files_v4.receipt(answer["files"])
                                failed = answer["state"] == "preparation_failed"
                                if (failed and input_files) or (
                                    not failed and len(input_files) != ordinal
                                ):
                                    raise ValueError("input staging unavailable")
                                break
                        except Exception:
                            # An elapsed original preparation cutoff is
                            # distinguishable from lost authority/protocol.
                            # Reap this exact nonbusiness process first, then
                            # discard only its bounded controlled objects.
                            if stop.is_set() or claim_http_v4._clock() < input_deadline:
                                raise
                            if not self._stop_uncommitted(
                                prepared.task, prepared.pidfd, prepared.channel
                            ):
                                raise ValueError("input stop unavailable") from None
                            reaped = True
                            if not prepared.attempt.is_usable():
                                raise ValueError("input authority unavailable") from None
                            managed_files_v4.discard_after_reap(
                                directory, prepared.identity.execution_id
                            )
                            failed, input_files = True, []
                    if not reaped and not self._stop_uncommitted(
                        prepared.task, prepared.pidfd, prepared.channel
                    ):
                        raise ValueError("task child unavailable")
                    with live_cgroup._mutex:
                        live_workspace._external_io_inflight = False
                if stop.is_set() or not prepared.attempt.is_usable():
                    raise ValueError("task authority unavailable")
                prepared.stage_state = "staged"
                staged = StagedClaim(
                    prepared,
                    live_cgroup,
                    live_workspace,
                    launch_fd,
                    launch_max,
                    preparation_failed=failed,
                    input_files=input_files,
                )
                if prepared.business_credentials:
                    redaction_fd = os.dup(launch_fd)
                    info = os.fstat(redaction_fd)
                    staged.redaction_fd = redaction_fd
                    staged.redaction_identity = (info.st_dev, info.st_ino, info.st_size)
                self._staged = staged
                recovery_checkpoint_v4.persist_prepared(staged)
                return staged
            except BaseException as error:
                self._poisoned = True
                prepared.stage_state = "unknown"
                self._retained.append(
                    (prepared, live_cgroup, live_workspace, launch_fd, redaction_fd)
                )
                if not isinstance(error, Exception):
                    raise
                return None

    def prepare(
        self,
        *,
        lease: WorkerLeaseSession,
        dispatch: bytes | str | Mapping[str, Any],
        root_lock: cgroup_namespace.RootLockHandle,
        runtime_root: Path,
        attempt_journal_root: Path,
        sandbox_config: sandbox.SandboxConfig,
        budget: sandbox.ResourceBudget,
        receive_limits: sandbox.ResourceLimits,
        child_address_space_bytes: int,
        wire_budget: int,
        result_wire_budget: int,
        timeout_seconds: float,
        stop: threading.Event,
    ) -> ClaimPreparation:
        if (
            self._pid != os.getpid()
            or sys.platform != "linux"
            or type(lease) is not WorkerLeaseSession
            or lease._clock is not lease_clock
            or type(root_lock) is not cgroup_namespace.RootLockHandle
            or type(sandbox_config) is not sandbox.SandboxConfig
            or sandbox_config.namespace_identity is None
            or type(budget) is not sandbox.ResourceBudget
            or type(receive_limits) is not sandbox.ResourceLimits
            or type(receive_limits.cpu_cores) not in (int, float)
            or not math.isfinite(receive_limits.cpu_cores)
            or receive_limits.cpu_cores <= 0
            or type(receive_limits.memory_bytes) is not int
            or type(receive_limits.pids) is not int
            or receive_limits.pids < 2
            or type(receive_limits.tmp_bytes) is not int
            or receive_limits.tmp_bytes <= 0
            or type(child_address_space_bytes) is not int
            or not 128 * 1024 * 1024 <= child_address_space_bytes < 2**63
            # In addition to the child's address space, reserve room for its
            # kernel buffers and the Agent's bounded IPC bookkeeping.
            or receive_limits.memory_bytes < child_address_space_bytes + 64 * 1024 * 1024
            or type(wire_budget) is not int
            or not 4096 <= wire_budget <= child_address_space_bytes
            or type(result_wire_budget) is not int
            or not 4096 <= result_wire_budget <= child_address_space_bytes
            or type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 5
            or not isinstance(stop, threading.Event)
            or stop.is_set()
        ):
            return ClaimPreparation("blocked", "claim_configuration_invalid")
        with self._lock:
            if self._poisoned or self._active is not None:
                return ClaimPreparation("blocked", "claim_responsibility_unresolved")
            outcome = self._prepare_locked(
                lease=lease,
                dispatch=dispatch,
                root_lock=root_lock,
                runtime_root=runtime_root,
                attempt_journal_root=attempt_journal_root,
                sandbox_config=sandbox_config,
                budget=budget,
                receive_limits=receive_limits,
                child_address_space_bytes=child_address_space_bytes,
                wire_budget=wire_budget,
                result_wire_budget=result_wire_budget,
                timeout_seconds=float(timeout_seconds),
                stop=stop,
            )
            if self._poisoned and outcome.state in {"blocked", "decision"}:
                return ClaimPreparation("unknown", "claim_io_unconfirmed")
            return outcome

    def _prepare_locked(
        self,
        *,
        lease: WorkerLeaseSession,
        dispatch: bytes | str | Mapping[str, Any],
        root_lock: cgroup_namespace.RootLockHandle,
        runtime_root: Path,
        attempt_journal_root: Path,
        sandbox_config: sandbox.SandboxConfig,
        budget: sandbox.ResourceBudget,
        receive_limits: sandbox.ResourceLimits,
        child_address_space_bytes: int,
        wire_budget: int,
        result_wire_budget: int,
        timeout_seconds: float,
        stop: threading.Event,
    ) -> ClaimPreparation:
        ticket: claim_http_v4._Spawn | None = None
        channel: socket.socket | None = None
        child_channel: socket.socket | None = None
        pidfd: int | None = None
        receive: sandbox.ResourceReservation | None = None
        attempt_reservation: sandbox.ResourceReservation | None = None
        reserved_attempt_id: int | None = None
        attempt: AttemptLeaseSession | None = None
        intent_started = False
        reservation_mutating = False
        transferred = False
        phase = "resource_preflight"
        try:
            message = deserialize_dispatch_message(dispatch)
            if message.target_worker_id != lease._worker_id or not lease._same_process():
                raise ValueError("Claim dispatch unavailable")
            parent = sandbox_config.cgroup_path
            if parent is None:
                raise ValueError("Claim resources unavailable")
            sandbox.validate_private_cgroup_namespace(parent)
            envelope = sandbox.read_verified_resource_envelope(sandbox_config)
            observed = budget.snapshot()
            expected_capacity = {
                "cpu": envelope.cpu_cores,
                "memory": envelope.memory_bytes,
                "pids": envelope.pids,
                "tmp": envelope.tmp_bytes,
            }
            minimum_reserve = {
                "cpu": max(0.25, sandbox_config.cpu_cores * 0.25),
                "memory": max(64 * 1024 * 1024, sandbox_config.memory_bytes // 8),
                "pids": max(32, sandbox_config.pids // 8),
                "tmp": max(16 * 1024 * 1024, sandbox_config.tmp_bytes // 8),
            }
            if (
                observed["envelope_source"] != envelope.source
                or observed["accounting_state"] != "unflagged"
                or observed["capacity"] != expected_capacity
                or any(
                    observed["agent_reserve"][key] < value for key, value in minimum_reserve.items()
                )
                or sandbox._read(parent / "memory.swap.current") != "0"
            ):
                raise ValueError("Claim resources unavailable")

            def check_resources() -> None:
                sandbox.validate_private_cgroup_namespace(parent)
                if (
                    sandbox.read_verified_resource_envelope(sandbox_config) != envelope
                    or sandbox._read(parent / "memory.swap.current") != "0"
                    or budget.snapshot()["accounting_state"] != "unflagged"
                ):
                    raise ValueError("Claim resources unavailable")

            phase = "receive_budget"
            reservation_mutating = True
            receive = budget.try_reserve(receive_limits)
            reservation_mutating = False
            if receive is None:
                return ClaimPreparation("blocked", "claim_receive_capacity_unavailable")
            phase = "instance_authority"
            # Renewal replaces the current candidate. Keep its proof stable through
            # the bounded tentative exchange; release before re-entering lease APIs.
            with lease._lock:
                deadline = lease._active_deadline()
                started = lease._require_before(deadline)
                io_deadline = min(deadline, started + timeout_seconds)
                claims, proof = lease._claims, lease._proof
                if claims is None or proof is None or stop.is_set():
                    raise ValueError("Claim authority unavailable")
                request = {
                    "base_url": lease._base_url,
                    "worker_id": lease._worker_id,
                    "worker_identity_id": str(lease._identity_id),
                    "instance_id": str(lease._instance_id),
                    "generation": claims.generation,
                    "credential": lease._credential,
                    "instance_proof": proof,
                    "verification_key": lease._verification_key.hex(),
                    "cluster_id": str(lease._cluster_id),
                    "authority_epoch": str(lease._authority_epoch),
                    "dispatch": message.model_dump(mode="json"),
                    "wire_budget": wire_budget,
                    "timeout": min(timeout_seconds, io_deadline - started),
                    "business_credentials": self._business_credentials,
                    "managed_files": self._managed_files,
                }
                transport_start = claim_http_v4._clock()
                io_deadline = min(io_deadline, transport_start + timeout_seconds)
                numerator, denominator = deadline.as_integer_ratio()
                deadline_ns = numerator * 1_000_000_000 // denominator
                encoded = json.dumps(request, separators=(",", ":")).encode("utf-8")
                if not 0 < len(encoded) <= 65_536:
                    raise ValueError("Claim request unavailable")
                channel, child_channel = socket.socketpair()
                channel.setblocking(False)
                ticket = claim_http_v4._Spawn(child=child_channel)
                command = [
                    sys.executable,
                    "-m",
                    "dlr.worker.claim_task_child_v4",
                    str(child_channel.fileno()),
                    str(os.getpid()),
                    str(deadline_ns),
                    str(child_address_space_bytes),
                ]
                child_channel = None  # the creator owns this descriptor from now on
                environment = {
                    key: value
                    for key, value in os.environ.items()
                    if key in {"PATH", "PYTHONPATH", "SSL_CERT_FILE", "SSL_CERT_DIR"}
                    or key.lower() in {"http_proxy", "https_proxy", "no_proxy", "all_proxy"}
                }
                phase = "claim_child"
                claim_http_v4._spawn_until(
                    ticket,
                    io_deadline,
                    deadline_ns,
                    child_address_space_bytes,
                    (lease._lost, stop),
                    _process_command=command,
                    _process_env=environment,
                )
                with ticket.lock:
                    if ticket.abandoned or ticket.process is None:
                        raise ValueError("Claim child unavailable")
                pidfd = os.pidfd_open(ticket.process.pid, 0)  # type: ignore[attr-defined, unused-ignore]
                if (
                    claim_http_v4._receive(channel, 1, io_deadline, (lease._lost, stop)) != b"R"
                    or ticket.thread is None
                    or not ticket.thread.is_alive()
                ):
                    raise ValueError("Claim child unavailable")
                claim_http_v4._send_all(
                    channel,
                    struct.pack("!I", len(encoded)) + encoded,
                    io_deadline,
                    (lease._lost, stop),
                )
                del encoded, request
                phase = "claim_response"
                size = struct.unpack(
                    "!I", claim_http_v4._receive(channel, 4, io_deadline, (lease._lost, stop))
                )[0]
                if not 0 < size <= 65_536:
                    raise ValueError("Claim response unavailable")
                result = json.loads(
                    claim_http_v4._receive(channel, size, io_deadline, (lease._lost, stop))
                )
            check_resources()
            if type(result) is not dict or not lease.is_usable():
                raise ValueError("Claim response unavailable")
            if result.get("state") == "decision":
                decision, reason, retry = (
                    result.get("decision"),
                    result.get("reason"),
                    result.get("retry_after_seconds"),
                )
                if (
                    set(result) != {"state", "decision", "reason", "retry_after_seconds"}
                    or decision not in {"ACK_NOOP", "DEFER", "REJECT_DLQ", "PAUSE_CONSUMER"}
                    or type(reason) is not str
                    or not 1 <= len(reason) <= 64
                    or (
                        decision in {"DEFER", "PAUSE_CONSUMER"}
                        and (type(retry) is not int or not 0 <= retry <= 86_400)
                    )
                    or (decision not in {"DEFER", "PAUSE_CONSUMER"} and retry is not None)
                ):
                    raise ValueError("Claim response unavailable")
                return ClaimPreparation("decision", reason, decision, retry)
            if (
                set(result)
                != {
                    "state",
                    "proposal",
                    "wire_sha256",
                    "task_fingerprint",
                    "execution_id",
                    "attempt_id",
                    "attempt_no",
                    "fencing_token",
                    "renew_seconds",
                    "lease_expires_at",
                    "claim_token",
                    "cleanup_token",
                    "resource_profile",
                    "business_credentials",
                    "managed_files",
                }
                or result.get("state") != "prepared"
                or type(result.get("business_credentials")) is not bool
                or (result["business_credentials"] and not self._business_credentials)
                or type(result.get("managed_files")) is not bool
                or (result["managed_files"] and not self._managed_files)
                or result.get("execution_id") != message.execution_id
                or type(result.get("execution_id")) is not int
                or any(
                    type(result.get(name)) is not int or not 0 < result[name] < 2**63
                    for name in ("attempt_id", "attempt_no", "fencing_token")
                )
                or result["attempt_no"] >= 2**31
                or type(result.get("renew_seconds")) is not int
                or not 0 < result["renew_seconds"] <= 28_800
                or type(result.get("proposal")) is not str
                or re.fullmatch(r"[A-Za-z0-9_-]{43}", result["proposal"]) is None
                or type(result.get("claim_token")) is not str
                or re.fullmatch(r"[A-Za-z0-9_-]{1,512}", result["claim_token"]) is None
                or type(result.get("cleanup_token")) is not str
                or re.fullmatch(r"[A-Za-z0-9_-]{1,512}", result["cleanup_token"]) is None
                or not self._alive(pidfd)
                or type(result.get("task_fingerprint")) is not str
                or re.fullmatch(r"[0-9a-f]{64}", result["task_fingerprint"]) is None
                or type(result.get("wire_sha256")) is not str
                or re.fullmatch(r"[0-9a-f]{64}", result["wire_sha256"]) is None
            ):
                raise ValueError("Claim response unavailable")
            phase = "task_profile"
            limits = sandbox.validate_resource_profile(result["resource_profile"], sandbox_config)
            # The receive charge stays held through the result exchange. It
            # covers the bounded result child, sealed wire snapshot and Agent
            # output ring. Worst-case JSON escaping remains a local budget,
            # not a new global limit on existing task content.
            minimum_result_wire = (
                limits.output_max_bytes
                + 6 * limits.stream_max_bytes
                + 6 * limits.output_preview_max_bytes
                + 16_384
            )
            if (
                result_wire_budget < minimum_result_wire
                or receive_limits.memory_bytes
                < child_address_space_bytes
                + result_wire_budget
                + 8 * limits.stream_max_bytes
                + 64 * 1024 * 1024
            ):
                return ClaimPreparation("blocked", "claim_output_capacity_unavailable")
            reserved_attempt_id = result["attempt_id"]
            reservation_mutating = True
            status, attempt_reservation = budget._reserve_v4_preparation(
                limits, attempt_id=reserved_attempt_id
            )
            if status == "unknown":
                raise ValueError("Claim accounting unavailable")
            reservation_mutating = False
            if status != "reserved" or attempt_reservation is None:
                return ClaimPreparation("blocked", "claim_task_capacity_unavailable")
            intent: dict[str, object] = {
                "version": 1,
                "worker_id": lease._worker_id,
                "worker_identity_id": str(lease._identity_id),
                "instance_id": str(lease._instance_id),
                "instance_generation": claims.generation,
                "execution_id": message.execution_id,
                "dispatch_generation": message.dispatch_generation,
                "message_id": str(message.message_id),
            }
            assert sandbox_config.namespace_identity is not None
            journal = {
                "protocol_version": 4,
                "journal_version": 2,
                "namespace_identity": sandbox_config.namespace_identity.marker(),
                "worker_identity_id": str(lease._identity_id),
                "instance_id": str(lease._instance_id),
                "instance_generation": claims.generation,
                "execution_id": result["execution_id"],
                "attempt_id": result["attempt_id"],
                "attempt_no": result["attempt_no"],
                "fencing_token": result["fencing_token"],
                "lease_expires_at": result["lease_expires_at"],
                "workspace_path": str(
                    workspace.workspace_path(
                        runtime_root, result["execution_id"], result["attempt_id"]
                    )
                ),
                "cleanup_token": result["cleanup_token"],
            }
            if not journal_v4.valid_record(journal, "attempt"):
                raise ValueError("Claim journal unavailable")
            phase = "claim_journal"
            with root_lock.borrow(runtime_root, [attempt_journal_root]):
                root_lock.require_v4_journal(runtime_root, attempt_journal_root)
                runtime_fd = self._entry(root_lock, runtime_root)
                journal_fd = self._entry(root_lock, attempt_journal_root)
                intent_started = True
                journal_name = self._write_intent(journal_fd, intent)
                runtime_name = self._write_intent(runtime_fd, intent)
                journal_v4.write(attempt_journal_root, runtime_root, journal, kind="attempt")

            def check_journal() -> None:
                with root_lock.borrow(runtime_root, [attempt_journal_root]):
                    root_lock.require_v4_journal(runtime_root, attempt_journal_root)
                    if (
                        journal_v4._read(self._entry(root_lock, runtime_root), runtime_name)
                        != intent
                        or journal_v4._read(
                            self._entry(root_lock, attempt_journal_root), journal_name
                        )
                        != intent
                        or journal_v4.load(
                            attempt_journal_root,
                            runtime_root,
                            execution_id=result["execution_id"],
                            attempt_id=result["attempt_id"],
                            kind="attempt",
                        )
                        != journal
                    ):
                        raise ValueError("Claim journal unavailable")

            check_journal()
            check_resources()
            if not self._alive(pidfd) or stop.is_set() or not lease.is_usable():
                raise ValueError("Claim authority unavailable")
            phase = "claim_finalize"
            finalize_started = claim_http_v4._clock()
            attempt = AttemptLeaseSession.finalize_prepared(
                lease,
                proposal=result["proposal"],
                attempt_id=result["attempt_id"],
                fencing_token=result["fencing_token"],
                claim_token=result["claim_token"],
            )
            check_journal()
            check_resources()
            if not self._alive(pidfd) or stop.is_set() or not attempt.is_usable():
                attempt.close()
                raise ValueError("Claim authority unavailable")
            prepared = PreparedClaim(
                task=ticket,
                pidfd=pidfd,
                channel=channel,
                receive_reservation=receive,
                attempt_reservation=attempt_reservation,
                attempt=attempt,
                journal=journal,
                intent=intent,
                intent_names=(journal_name, runtime_name),
                task_fingerprint=result["task_fingerprint"],
                identity=ClaimIdentity(
                    lease._identity_id,
                    lease._instance_id,
                    claims.generation,
                    result["execution_id"],
                    result["attempt_id"],
                    result["attempt_no"],
                    result["fencing_token"],
                ),
                resource_profile=MappingProxyType(dict(result["resource_profile"])),
                renew_seconds=result["renew_seconds"],
                child_address_space_bytes=child_address_space_bytes,
                result_wire_budget=result_wire_budget,
                deadline=deadline,
                dispatch=message.model_dump(mode="json"),
                business_credentials=result["business_credentials"],
                managed_files=result["managed_files"],
                input_deadline=math.nextafter(finalize_started + 5.0, -math.inf)
                if result["managed_files"]
                else None,
            )
            self._active = prepared
            transferred = True
            return ClaimPreparation("claimed", "claim_proved", claimed=prepared)
        except BaseException as error:
            logger.warning(
                "fixed Worker Claim preparation unavailable: phase=%s error=%s",
                phase,
                type(error).__name__,
            )
            if intent_started or reservation_mutating:
                self._poisoned = True
                self._retained.append((ticket, pidfd, channel, receive, attempt_reservation))
                transferred = True
                if attempt is not None:
                    with suppress(BaseException):
                        attempt.close()
                if not isinstance(error, Exception):
                    raise
                return ClaimPreparation("unknown", "claim_responsibility_unresolved")
            if not isinstance(error, Exception):
                raise
            return ClaimPreparation("blocked", "claim_preparation_unavailable")
        finally:
            if child_channel is not None:
                child_channel.close()
            if not transferred:
                reaped = self._stop_uncommitted(ticket, pidfd, channel)
                if reaped:
                    # A partial budget mutation cannot be reused for admission.
                    self._poisoned = True
                    if attempt_reservation is not None:
                        assert reserved_attempt_id is not None
                        budget._release_uncommitted_v4(
                            attempt_reservation, attempt_id=reserved_attempt_id
                        )
                    if receive is not None:
                        budget.release(receive)
                    self._poisoned = False
