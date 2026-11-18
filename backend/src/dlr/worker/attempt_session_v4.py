"""Locally fenced proof session for one v4 Worker Attempt.

This manages the small start/renew/progress messages. A caller must separately
supervise physical work, retain its journal and settle the result and cleanup.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import urllib.request
from collections.abc import Callable
from uuid import UUID

from dlr.common.attempt_lease_proof_wire_v4 import (
    UntrustedAttemptProof,
    verify_attempt_proof_signature,
)
from dlr.worker.lease_session_v4 import WorkerLeaseError, WorkerLeaseSession, _proof_deadline


class AttemptLeaseError(Exception):
    """The Attempt's authority is unavailable or an operation is unknown."""


class AttemptCancelled(AttemptLeaseError):
    """Control observed cancellation before the Attempt could be started."""


class AttemptForceStopRequested(AttemptLeaseError):
    """Control confirmed a stop intent without issuing a successor proof."""

    def __init__(self, operation_id: UUID) -> None:
        super().__init__("Attempt stop requested")
        self.operation_id = operation_id


# The body cap allows this many bytes even if all need JSON escapes.
PROGRESS_STDOUT_BYTES = 384


class AttemptLeaseSession:
    """A single Attempt bound to its current Worker instance and Claim Token."""

    def __init__(
        self,
        parent: WorkerLeaseSession,
        *,
        attempt_id: int,
        fencing_token: int,
        claim_token: str,
        initial_proof: str,
        request_started: float,
        request_finished: float,
    ) -> None:
        """Bind the initial proof returned by this Attempt's Claim exchange.

        The trusted caller records the two timestamps around that exact
        exchange. Replayed proofs or timestamps from a different exchange are
        not a valid Claim receipt.
        """
        self._initialize(parent)
        try:
            with parent._lock:
                self._bind_locked(
                    attempt_id,
                    fencing_token,
                    claim_token,
                    initial_proof,
                    request_started,
                    request_finished,
                )
        except BaseException as error:
            self._die()
            if not isinstance(error, Exception):
                raise
            raise AttemptLeaseError("Attempt authority unavailable") from None

    def _initialize(self, parent: WorkerLeaseSession) -> None:
        self._parent = parent
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._lost = threading.Event()
        self._proof: str | None = None
        self._claims: UntrustedAttemptProof | None = None
        self._claim_token = ""
        self._deadline = 0.0
        self._start_confirmed = False
        self._force_stopping = False

    def _bind_locked(
        self,
        attempt_id: int,
        fencing_token: int,
        claim_token: str,
        initial_proof: str,
        request_started: float,
        request_finished: float,
    ) -> None:
        parent = self._parent
        if (
            type(parent) is not WorkerLeaseSession
            or type(attempt_id) is not int
            or not 0 < attempt_id < 2**63
            or type(fencing_token) is not int
            or not 0 < fencing_token < 2**63
            or type(claim_token) is not str
            or not 1 <= len(claim_token) <= 512
            or type(initial_proof) is not str
            or any(
                type(value) not in (int, float) or not math.isfinite(value) or value < 0
                for value in (request_started, request_finished)
            )
            or request_finished < request_started
            or not parent._same_process()
        ):
            raise AttemptLeaseError("Attempt authority unavailable")
        now = parent._require_before(parent._active_deadline())
        instance = parent._claims
        claims = verify_attempt_proof_signature(initial_proof, parent._verification_key)
        if (
            instance is None
            or claims is None
            or request_finished > now
            or claims.cluster_id != instance.cluster_id
            or claims.authority_epoch != instance.authority_epoch
            or claims.worker_id != parent._worker_id
            or claims.worker_identity_id != parent._identity_id
            or claims.instance_id != parent._instance_id
            or claims.generation != instance.generation
            or claims.attempt_id != attempt_id
            or claims.fencing_token != fencing_token
            or claims.revision != 1
            or claims.nominal_us != claims.guard_us
            or claims.guard_us != claims.proof_before_us
            or claims.nominal_us > min(instance.nominal_us, instance.guard_us)
            or claims.observed_us < instance.observed_us
        ):
            raise AttemptLeaseError("Attempt authority unavailable")
        parent._check_observation_clock(claims.observed_us, request_started, request_finished)
        deadline = _proof_deadline(request_started, claims.nominal_us, claims.observed_us)
        parent._require_before(min(deadline, parent._active_deadline()))
        parent._observation = (claims.observed_us, request_started, request_finished)
        self._proof, self._claims, self._deadline = initial_proof, claims, deadline
        self._claim_token = claim_token

    @classmethod
    def finalize_prepared(
        cls,
        parent: WorkerLeaseSession,
        *,
        proposal: str,
        attempt_id: int,
        fencing_token: int,
        claim_token: str,
    ) -> AttemptLeaseSession:
        """Finalize one prepared Claim; unknown responses retain its local intent."""
        if (
            type(parent) is not WorkerLeaseSession
            or not parent._same_process()
            or type(proposal) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", proposal) is None
            or type(attempt_id) is not int
            or not 0 < attempt_id < 2**63
            or type(fencing_token) is not int
            or not 0 < fencing_token < 2**63
            or type(claim_token) is not str
            or not 1 <= len(claim_token) <= 512
        ):
            raise AttemptLeaseError("Attempt authority unavailable")
        result = cls.__new__(cls)
        result._initialize(parent)
        try:
            with parent._lock:
                deadline = parent._active_deadline()
                started = parent._require_before(deadline)
                instance, proof = parent._claims, parent._proof
                if instance is None or proof is None:
                    raise AttemptLeaseError("Attempt authority unavailable")
                request = urllib.request.Request(
                    f"{parent._base_url}/api/workers/{parent._worker_id}/v4/claim/finalize",
                    data=b"",
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {parent._credential}",
                        "X-DLR-Worker-Instance": str(parent._instance_id),
                        "X-DLR-Worker-Generation": str(instance.generation),
                        "X-DLR-Worker-Lease-Proof": proof,
                        "X-DLR-Claim-Proposal": proposal,
                    },
                )
                raw = parent._post(
                    request, timeout=parent._timeout, deadline=deadline, expected=200
                )
                finished = parent._require_before(deadline)
                token = parent._body(raw, {"attempt_proof"})["attempt_proof"]
                if type(token) is not str:
                    raise AttemptLeaseError("Attempt authority unavailable")
                result._bind_locked(
                    attempt_id, fencing_token, claim_token, token, started, finished
                )
                return result
        except BaseException as error:
            result._die()
            if not isinstance(error, Exception):
                raise
            raise AttemptLeaseError("Attempt authority unavailable") from None

    def _die(self) -> None:
        if os.getpid() == self._pid:
            self._lost.set()
        self._proof = None
        self._claims = None
        self._claim_token = ""
        self._start_confirmed = False

    def _live_locked(self) -> tuple[UntrustedAttemptProof, str, float]:
        parent = self._parent
        if (
            self._lost.is_set()
            or self._claims is None
            or self._proof is None
            or not parent._same_process()
        ):
            raise AttemptLeaseError("Attempt authority unavailable")
        deadline = min(self._deadline, parent._active_deadline())
        parent._require_before(deadline)
        return self._claims, self._proof, deadline

    def _request_locked(
        self, operation: str, *, stdout_chunk: str | None = None
    ) -> tuple[bytes, float, float]:
        parent = self._parent
        old, proof, deadline = self._live_locked()
        instance = parent._claims
        instance_proof = parent._proof
        if instance is None or instance_proof is None:
            raise AttemptLeaseError("Attempt authority unavailable")
        started = parent._require_before(deadline)
        body: dict[str, int | str] = {
            "attempt_id": old.attempt_id,
            "fencing_token": old.fencing_token,
            "claim_token": self._claim_token,
        }
        timeout = parent._timeout
        if stdout_chunk is not None:
            body["stdout_chunk"] = stdout_chunk
            # Optional output cannot consume the slack required for renewal.
            deadline = min(deadline - 2.0, started + 0.5)
            parent._require_before(deadline)
            timeout = min(timeout, 0.5)
        request = urllib.request.Request(
            f"{parent._base_url}/api/workers/{parent._worker_id}/v4/attempts/"
            f"{old.attempt_id}/{operation}",
            data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {parent._credential}",
                "X-DLR-Worker-Instance": str(parent._instance_id),
                "X-DLR-Worker-Generation": str(instance.generation),
                "X-DLR-Worker-Lease-Proof": instance_proof,
                "X-DLR-Worker-Attempt-Proof": proof,
            },
        )
        raw = parent._post(
            request,
            timeout=timeout,
            deadline=deadline,
            expected=200,
            _cancellation_events=(self._lost,),
        )
        finished = parent._require_before(deadline)
        if self._lost.is_set():
            raise AttemptLeaseError("Attempt authority unavailable")
        return raw, started, finished

    def progress(self, stdout_chunk: str) -> bool:
        """Publish one bounded, non-retryable excerpt. Return an observed cancellation."""
        if (
            os.getpid() != self._pid
            or not self._parent._same_process()
            or not stdout_chunk
            or len(stdout_chunk.encode("utf-8")) > PROGRESS_STDOUT_BYTES
        ):
            return False
        with self._lock, self._parent._lock:
            try:
                claims, _, _ = self._live_locked()
                if not self._start_confirmed:
                    return False
                raw, _, _ = self._request_locked("progress", stdout_chunk=stdout_chunk)
                value = self._parent._body(
                    raw, {"decision", "reason", "attempt_id", "cancel_requested"}
                )
                return (
                    value["decision"] == "ACK_NOOP"
                    and type(value["attempt_id"]) is int
                    and value["attempt_id"] == claims.attempt_id
                    and value["reason"] == "cancel_requested"
                    and value["cancel_requested"] is True
                )
            except (AttemptLeaseError, WorkerLeaseError, ValueError):
                # An unknown append must not be replayed or renew the proof.
                return False

    def start(self) -> None:
        """Confirm a timely committed start before the caller releases work."""
        if os.getpid() != self._pid or not self._parent._same_process():
            self._die()
            raise AttemptLeaseError("Attempt authority unavailable")
        with self._lock:
            try:
                with self._parent._lock:
                    claims, _, _ = self._live_locked()
                    raw, _, _ = self._request_locked("start")
                    value = self._parent._body(
                        raw, {"decision", "reason", "attempt_id", "cancel_requested"}
                    )
                    if (
                        value["decision"] != "ACK_NOOP"
                        or type(value["attempt_id"]) is not int
                        or value["attempt_id"] != claims.attempt_id
                        or type(value["cancel_requested"]) is not bool
                    ):
                        raise AttemptLeaseError("Attempt authority unavailable")
                    if value["reason"] == "cancel_requested" and value["cancel_requested"]:
                        raise AttemptCancelled("Attempt cancelled")
                    if value["reason"] != "started" or value["cancel_requested"]:
                        raise AttemptLeaseError("Attempt authority unavailable")
                    self._live_locked()
                    self._start_confirmed = True
            except BaseException as error:
                self._die()
                if not isinstance(error, Exception):
                    raise
                if isinstance(error, AttemptCancelled):
                    raise
                raise AttemptLeaseError("Attempt authority unavailable") from None

    def renew(self) -> None:
        """Accept only a timely, signed successor for the same Attempt."""
        if os.getpid() != self._pid or not self._parent._same_process():
            self._die()
            raise AttemptLeaseError("Attempt authority unavailable")
        with self._lock:
            try:
                with self._parent._lock:
                    parent = self._parent
                    old, old_proof, deadline = self._live_locked()
                    instance = parent._claims
                    assert instance is not None
                    raw, started, finished = self._request_locked("renew")
                    if b'"stop_requested"' in raw:
                        action = parent._body(raw, {"stop_requested", "operation_id"})
                        raw_id = action["operation_id"]
                        if action["stop_requested"] is not True or not isinstance(raw_id, str):
                            raise ValueError("stop intent invalid")
                        operation_id = UUID(raw_id)
                        if not operation_id.int or str(operation_id) != raw_id:
                            raise ValueError("stop intent invalid")
                        self._force_stopping = True
                        raise AttemptForceStopRequested(operation_id)
                    token = parent._body(raw, {"attempt_proof"})["attempt_proof"]
                    claims = verify_attempt_proof_signature(token, parent._verification_key)
                    if (
                        type(token) is not str
                        or claims is None
                        or token == old_proof
                        or claims.cluster_id != old.cluster_id
                        or claims.authority_epoch != old.authority_epoch
                        or claims.worker_id != old.worker_id
                        or claims.worker_identity_id != old.worker_identity_id
                        or claims.instance_id != old.instance_id
                        or claims.generation != old.generation
                        or claims.attempt_id != old.attempt_id
                        or claims.fencing_token != old.fencing_token
                        or claims.revision != old.revision + 1
                        or claims.candidate_id == old.candidate_id
                        or claims.guard_us != old.nominal_us
                        or claims.proof_before_us
                        > min(old.nominal_us, old.guard_us, instance.nominal_us, instance.guard_us)
                        or claims.nominal_us > min(instance.nominal_us, instance.guard_us)
                        or claims.observed_us < max(old.observed_us, instance.observed_us)
                    ):
                        raise AttemptLeaseError("Attempt authority unavailable")
                    parent._check_observation_clock(claims.observed_us, started, finished)
                    new_deadline = _proof_deadline(
                        started, min(claims.nominal_us, claims.guard_us), claims.observed_us
                    )
                    parent._require_before(min(deadline, new_deadline, parent._active_deadline()))
                    parent._observation = (claims.observed_us, started, finished)
                    self._claims, self._proof, self._deadline = claims, token, new_deadline
            except BaseException as error:
                if isinstance(error, AttemptForceStopRequested):
                    # Existing authority may only settle the stopped Attempt.
                    # Control rejects further work and issues no successor.
                    raise
                self._die()
                if not isinstance(error, Exception):
                    raise
                raise AttemptLeaseError("Attempt authority unavailable") from None

    def is_usable(self) -> bool:
        """Check both local deadlines; this does not itself stop physical work."""
        if os.getpid() != self._pid or not self._parent._same_process():
            self._die()
            return False
        try:
            with self._parent._lock:
                self._live_locked()
                return True
        except (AttemptLeaseError, WorkerLeaseError):
            self._die()
            return False

    def local_deadline(self) -> float:
        """Return the earlier instance/Attempt deadline for a physical watchdog."""
        with self._lock, self._parent._lock:
            if not self._start_confirmed:
                raise AttemptLeaseError("Attempt authority unavailable")
            _, _, deadline = self._live_locked()
            return deadline

    def send_with_authority(
        self, send: Callable[[], None], *, deadline: float | None = None
    ) -> bool:
        """Fence a single nonblocking supervisor gate write with both proofs."""
        if not callable(send) or os.getpid() != self._pid:
            return False
        try:
            with self._lock, self._parent._lock:
                if not self._start_confirmed or self._force_stopping:
                    return False
                _, _, current_deadline = self._live_locked()
                if deadline is not None and (
                    type(deadline) is not float
                    or not math.isfinite(deadline)
                    or deadline > current_deadline
                ):
                    return False
                send()
                self._live_locked()
                return True
        except (AttemptLeaseError, WorkerLeaseError, OSError, ValueError):
            self._die()
            return False

    def close(self) -> None:
        if os.getpid() != self._pid:
            self._die()
            return
        self._lost.set()
        with self._lock:
            self._die()
