"""Signed, locally fenced v4 Worker instance session.

This session alone does not stop running business processes or authorize work.
"""

from __future__ import annotations

import json
import logging
import math
import os
import secrets
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from dlr.common.lease_proof_wire_v4 import UntrustedLeaseProof, verify_worker_proof_signature
from dlr.common.worker_capacity_report import SignedCapacityObservation
from dlr.common.worker_protocol_v4 import (
    ADAPTER_CLEANUP_CAPABILITY,
    INSTANCE_LEASE_SECONDS,
    V4_LEASE_CLOCK_MARGIN_NS,
    is_worker_credential,
    validate_control_url,
)
from dlr.worker import proof_trust_v4, runtime_credential_v4
from dlr.worker.client import _open_v4
from dlr.worker.lease_clock_v4 import lease_clock, local_lease_deadline

_RESPONSE_LIMIT = 8192
logger = logging.getLogger("dlr.worker.v4.lease")


class WorkerLeaseError(Exception):
    """A redacted, locally unavailable instance lease."""


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError("invalid constant")


def _proof_deadline(request_start: float, deadline_us: int, observed_us: int) -> float:
    remaining_ns = (deadline_us - observed_us) * 1000 - V4_LEASE_CLOCK_MARGIN_NS
    if remaining_ns <= 0:
        raise WorkerLeaseError("proof session unavailable")
    deadline = request_start + remaining_ns / 1_000_000_000
    # Bias conversion and addition earlier; never round a lease boundary later.
    return math.nextafter(math.nextafter(deadline, -math.inf), -math.inf)


class WorkerLeaseSession:
    """Memory-only instance proof with irrevocable expiry after registration."""

    @classmethod
    def from_trusted_file(
        cls,
        *,
        trust_file: Path,
        base_url: str,
        worker_id: int,
        worker_identity_id: UUID,
        credential: str,
        credential_record_id: int | None = None,
        instance_id: UUID,
        timeout_seconds: float = 5.0,
        clock: Callable[[], float | None] = lease_clock,
        _test_only_plaintext_loopback: bool = False,
    ) -> WorkerLeaseSession:
        """Load configured trust; never learn a verification key from the peer."""
        trust = proof_trust_v4.load(trust_file)
        client = cls(
            base_url=base_url,
            worker_id=worker_id,
            worker_identity_id=worker_identity_id,
            credential=credential,
            credential_record_id=credential_record_id,
            instance_id=instance_id,
            verification_key=trust.public_key,
            cluster_id=trust.cluster_id,
            authority_epoch=trust.authority_epoch,
            timeout_seconds=timeout_seconds,
            clock=clock,
            _test_only_plaintext_loopback=_test_only_plaintext_loopback,
        )
        client._trust_source = (trust_file, trust)
        return client

    def __init__(
        self,
        *,
        base_url: str,
        worker_id: int,
        worker_identity_id: UUID,
        credential: str,
        credential_record_id: int | None = None,
        instance_id: UUID,
        verification_key: bytes,
        cluster_id: UUID,
        authority_epoch: UUID,
        timeout_seconds: float = 5.0,
        clock: Callable[[], float | None] = lease_clock,
        _test_only_plaintext_loopback: bool = False,
    ) -> None:
        try:
            validate_control_url(
                base_url, _test_only_plaintext_loopback=_test_only_plaintext_loopback
            )
            parts = urlsplit(base_url)
            if (
                type(_test_only_plaintext_loopback) is not bool
                or (
                    parts.scheme != "https"
                    and not (
                        _test_only_plaintext_loopback
                        and parts.scheme == "http"
                        and parts.hostname in {"127.0.0.1", "::1"}
                    )
                )
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.query
                or parts.fragment
                or any(char.isspace() or ord(char) < 32 for char in base_url)
                or credential in base_url
                or type(worker_id) is not int
                or not 0 < worker_id < 2**63
                or type(worker_identity_id) is not UUID
                or worker_identity_id.int == 0
                or type(instance_id) is not UUID
                or instance_id.int == 0
                or type(verification_key) is not bytes
                or len(verification_key) != 32
                or type(cluster_id) is not UUID
                or cluster_id.int == 0
                or type(authority_epoch) is not UUID
                or authority_epoch.int == 0
                or type(credential) is not str
                or not is_worker_credential(credential)
                or (
                    credential_record_id is not None
                    and (
                        type(credential_record_id) is not int
                        or not 0 < credential_record_id < 2**63
                    )
                )
                or isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 5
                or not callable(clock)
            ):
                raise ValueError
            _ = parts.port
        except (TypeError, ValueError):
            raise ValueError("invalid proof session configuration") from None
        self._base_url = base_url.rstrip("/")
        self._test_only_plaintext_loopback = _test_only_plaintext_loopback
        self._trust_source: tuple[Path, proof_trust_v4.ProofTrust] | None = None
        self._pid = os.getpid()
        self._worker_id = worker_id
        self._identity_id = worker_identity_id
        self._instance_id = instance_id
        self._verification_key = verification_key
        self._cluster_id = cluster_id
        self._authority_epoch = authority_epoch
        self._credential = credential
        self._credential_record_id = credential_record_id
        self._timeout = timeout_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._lost = threading.Event()
        self._state = "new"
        self._last_clock: float | None = None
        self._initial_start: float | None = None
        self._nominal_deadline: float | None = None
        self._guard_deadline: float | None = None
        self._proof: str | None = None
        self._claims: UntrustedLeaseProof | None = None
        self._observation: tuple[int, float, float] | None = None
        # One conservative offset interval per elapsed-clock second. This
        # retains every signed sample in a lease-sized rolling window without
        # growing with Claim or heartbeat frequency.
        self._clock_samples: dict[int, tuple[int, int]] = {}
        self._clock_sample_latest: int | None = None

    def _die(self) -> None:
        # A forked copy can inherit a lock held by a vanished thread. It must
        # discard its secrets without entering even an Event's internal lock.
        if os.getpid() == self._pid:
            self._lost.set()
        self._state = "dead"
        self._proof = None
        self._claims = None
        self._observation = None
        self._clock_samples.clear()
        self._clock_sample_latest = None
        self._credential = ""
        self._credential_record_id = None

    def _same_process(self) -> bool:
        # After fork the inherited lock may have belonged to a vanished
        # thread. Clear the child's copied secrets before touching that lock.
        if os.getpid() != self._pid:
            self._die()
            return False
        return True

    def _now(self) -> float:
        try:
            now = self._clock()
            if (
                isinstance(now, bool)
                or not isinstance(now, (int, float))
                or not math.isfinite(now)
                or now < 0
                or (self._last_clock is not None and now < self._last_clock)
            ):
                raise ValueError
            self._last_clock = float(now)
            return float(now)
        except BaseException as error:
            self._die()
            if not isinstance(error, Exception):
                raise
            raise WorkerLeaseError("proof session unavailable") from None

    def _require_before(self, deadline: float) -> float:
        if self._lost.is_set():
            raise WorkerLeaseError("proof session unavailable")
        if self._trust_source is not None:
            path, expected = self._trust_source
            try:
                if proof_trust_v4.load(path) != expected:
                    raise WorkerLeaseError("proof session unavailable")
            except BaseException as error:
                self._die()
                if not isinstance(error, Exception):
                    raise
                raise WorkerLeaseError("proof session unavailable") from None
        now = self._now()
        if self._lost.is_set() or now >= deadline:
            self._die()
            raise WorkerLeaseError("proof session unavailable")
        return now

    def _active_deadline(self) -> float:
        if (
            self._state != "active"
            or self._lost.is_set()
            or self._proof is None
            or self._claims is None
            or self._nominal_deadline is None
            or self._guard_deadline is None
        ):
            raise WorkerLeaseError("proof session unavailable")
        return min(self._nominal_deadline, self._guard_deadline)

    def _check_observation_clock(
        self,
        observed_us: int,
        started: float,
        finished: float,
        *,
        previous: tuple[int, float, float] | None = None,
        allow_overlap: bool = False,
    ) -> None:
        """Reject clock drift or network uncertainty outside the lease margin.

        A signed DB observation occurred somewhere during each request. Compare
        all possible local elapsed times, biasing float conversion outward. This
        checks elapsed-time differences between samples; it cannot exclude a
        clock jump between observations or replace the continuous monitor.
        """
        previous = self._observation if previous is None else previous
        if previous is None:
            raise WorkerLeaseError("proof session unavailable")
        previous_us, previous_started, previous_finished = previous
        if (
            finished < started
            or previous_finished < previous_started
            or (not allow_overlap and started < previous_finished)
            or (started >= previous_finished and observed_us < previous_us)
            or (finished <= previous_started and observed_us > previous_us)
        ):
            logger.warning("fixed Worker clock observation unavailable: reason=request_order")
            raise WorkerLeaseError("proof session unavailable")
        uncertainty_us = (
            math.ceil(
                1_000_000
                * sum(
                    math.ulp(value)
                    for value in (previous_started, previous_finished, started, finished)
                )
            )
            + 2
        )
        least_us = math.floor((started - previous_finished) * 1_000_000) - uncertainty_us
        most_us = math.ceil((finished - previous_started) * 1_000_000) + uncertainty_us
        database_us = observed_us - previous_us
        margin_us = V4_LEASE_CLOCK_MARGIN_NS // 1000
        if database_us < most_us - margin_us or database_us > least_us + margin_us:
            logger.warning(
                "fixed Worker clock observation unavailable: reason=elapsed_bounds "
                "request_ms=%d previous_ms=%d deviation_min_us=%d deviation_max_us=%d",
                round((finished - started) * 1000),
                round((previous_finished - previous_started) * 1000),
                database_us - most_us,
                database_us - least_us,
            )
            raise WorkerLeaseError("proof session unavailable")
        self._remember_observation_clock(observed_us, started, finished)

    def _remember_observation_clock(
        self, observed_us: int, started: float, finished: float
    ) -> None:
        """Bound cumulative sampled drift, including overlapping requests.

        A bucket stores the most extreme possible DB-minus-local offsets of
        every sample in that second. Retaining a little over one maximum lease
        is conservative, but cannot detect an excursion between samples.
        The caller holds the proof lock and has already verified the signature.
        """
        try:
            if (
                type(observed_us) is not int
                or isinstance(started, bool)
                or not isinstance(started, (int, float))
                or isinstance(finished, bool)
                or not isinstance(finished, (int, float))
                or not math.isfinite(started)
                or not math.isfinite(finished)
                or started < 0
                or finished < started
            ):
                raise ValueError
            started_us = started * 1_000_000
            finished_us = finished * 1_000_000
            start_error = math.ceil(math.ulp(started) * 1_000_000 + math.ulp(started_us)) + 1
            finish_error = math.ceil(math.ulp(finished) * 1_000_000 + math.ulp(finished_us)) + 1
            lower = observed_us - math.ceil(finished_us) - finish_error
            upper = observed_us - math.floor(started_us) + start_error
            second = math.floor(finished)
            latest = (
                second
                if self._clock_sample_latest is None
                else max(second, self._clock_sample_latest)
            )
            oldest = latest - INSTANCE_LEASE_SECONDS - 1
            if second < oldest:
                raise ValueError
            for bucket in tuple(self._clock_samples):
                if bucket < oldest:
                    del self._clock_samples[bucket]
            margin = V4_LEASE_CLOCK_MARGIN_NS // 1000
            if any(
                upper - old_lower > margin or old_upper - lower > margin
                for old_lower, old_upper in self._clock_samples.values()
            ):
                logger.warning(
                    "fixed Worker clock observation unavailable: reason=rolling_bounds "
                    "request_ms=%d max_deviation_us=%d",
                    round((finished - started) * 1000),
                    max(
                        max(upper - old_lower, old_upper - lower)
                        for old_lower, old_upper in self._clock_samples.values()
                    ),
                )
                raise ValueError
            prior = self._clock_samples.get(second)
            self._clock_samples[second] = (
                min(lower, prior[0]) if prior is not None else lower,
                max(upper, prior[1]) if prior is not None else upper,
            )
            self._clock_sample_latest = latest
        except BaseException as error:
            self._die()
            if not isinstance(error, Exception):
                raise
            raise WorkerLeaseError("proof session unavailable") from None

    def _open(self, request: urllib.request.Request, *, timeout: float) -> Any:
        if self._test_only_plaintext_loopback:
            return _open_v4(request, timeout=timeout, _test_only_plaintext_loopback=True)
        return _open_v4(request, timeout=timeout)

    def _post_in_process(
        self,
        request: urllib.request.Request,
        *,
        timeout: float,
        deadline: float,
        expected: int,
        _cancellation_events: tuple[threading.Event, ...] = (),
    ) -> bytes:
        """Portable isolated-test transport; Linux Workers use the supervised child."""
        if any(event.is_set() for event in _cancellation_events):
            raise WorkerLeaseError("proof session unavailable")
        now = self._require_before(deadline)
        try:
            with self._open(request, timeout=min(timeout, deadline - now)) as response:
                raw: bytes = response.read(_RESPONSE_LIMIT + 1)
                if (
                    response.status != expected
                    or len(raw) > _RESPONSE_LIMIT
                    or any(event.is_set() for event in _cancellation_events)
                ):
                    raise ValueError
                return raw
        except urllib.error.HTTPError as error:
            with suppress(Exception):
                error.close()
            raise WorkerLeaseError("proof session unavailable") from None
        except Exception:
            raise WorkerLeaseError("proof session unavailable") from None

    def _post(
        self,
        request: urllib.request.Request,
        *,
        timeout: float,
        deadline: float,
        expected: int,
        _cancellation_events: tuple[threading.Event, ...] = (),
    ) -> bytes:
        """Bound the whole Linux proof exchange and retain unresolved child IO."""
        if not sys.platform.startswith("linux"):
            return self._post_in_process(
                request,
                timeout=timeout,
                deadline=deadline,
                expected=expected,
                _cancellation_events=_cancellation_events,
            )
        from dlr.worker.claim_http_v4 import _clock as transport_clock
        from dlr.worker.claim_http_v4 import request_proof

        # Sample BOOTTIME first so conversion from an injected local clock can
        # only make the child's absolute deadline earlier, never later.
        transport_start = transport_clock()
        now = self._require_before(deadline)
        timeout = min(timeout, deadline - now)
        headers = {key.lower(): value for key, value in request.header_items()}
        authorization = headers.get("authorization", "")
        instance = headers.get("x-dlr-worker-instance", "")
        generation_text = headers.get("x-dlr-worker-generation")
        instance_proof = headers.get("x-dlr-worker-lease-proof")
        attempt_proof = headers.get("x-dlr-worker-attempt-proof")
        claim_proposal = headers.get("x-dlr-claim-proposal")
        try:
            if (
                request.get_method() != "POST"
                or not secrets.compare_digest(authorization, f"Bearer {self._credential}")
                or instance != str(self._instance_id)
                or (request.data is not None and type(request.data) is not bytes)
                or (
                    claim_proposal is not None
                    and request.full_url
                    != f"{self._base_url}/api/workers/{self._worker_id}/v4/claim/finalize"
                )
            ):
                raise ValueError
            generation = int(generation_text) if generation_text is not None else None
            body = request.data.decode("utf-8") if request.data is not None else ""
        except (TypeError, ValueError, UnicodeError):
            raise WorkerLeaseError("proof session unavailable") from None
        result = request_proof(
            url=request.full_url,
            credential=self._credential,
            instance=instance,
            generation=generation,
            body=body,
            instance_proof=instance_proof,
            attempt_proof=attempt_proof,
            timeout_seconds=timeout,
            response_limit_bytes=_RESPONSE_LIMIT,
            _test_only_plaintext_loopback=self._test_only_plaintext_loopback,
            _absolute_deadline=math.nextafter(transport_start + timeout, -math.inf),
            _cancellation_events=(self._lost, *_cancellation_events),
            _claim_proposal=claim_proposal,
        )
        if result.state == "io_unknown":
            self._die()
        if result.state != "response" or result.status != expected:
            raise WorkerLeaseError("proof session unavailable")
        return result.body

    def _request(
        self,
        operation: str,
        *,
        proof: str | None,
        expected: int,
        deadline: float,
        payload: bytes | None = None,
        _cancellation_events: tuple[threading.Event, ...] = (),
    ) -> bytes:
        request = urllib.request.Request(
            f"{self._base_url}/api/workers/{self._worker_id}/v4/proof-{operation}",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._credential}",
                "X-DLR-Worker-Instance": str(self._instance_id),
            },
        )
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        if proof is not None and self._claims is not None:
            request.add_header("X-DLR-Worker-Generation", str(self._claims.generation))
            request.add_header("X-DLR-Worker-Lease-Proof", proof)
        return self._post(
            request,
            timeout=self._timeout,
            deadline=deadline,
            expected=expected,
            _cancellation_events=_cancellation_events,
        )

    @staticmethod
    def _body(raw: bytes, expected: set[str]) -> dict[str, object]:
        try:
            value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
            if not isinstance(value, dict) or set(value) != expected:
                raise ValueError
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise WorkerLeaseError("proof session unavailable") from None

    def _validate_identity(self, claims: UntrustedLeaseProof | None) -> UntrustedLeaseProof:
        if (
            claims is None
            or claims.cluster_id != self._cluster_id
            or claims.authority_epoch != self._authority_epoch
            or claims.worker_id != self._worker_id
            or claims.worker_identity_id != self._identity_id
            or claims.instance_id != self._instance_id
        ):
            raise WorkerLeaseError("proof session unavailable")
        return claims

    def register(self, *, stop: threading.Event | None = None) -> None:
        """Retry only the same initial candidate and retain the first local start."""
        if stop is not None and not isinstance(stop, threading.Event):
            raise ValueError("invalid stop signal")
        if not self._same_process():
            raise WorkerLeaseError("proof session unavailable")
        with self._lock:
            if stop is not None and stop.is_set():
                self._die()
                raise WorkerLeaseError("proof session unavailable")
            if self._state not in {"new", "pending"}:
                raise WorkerLeaseError("proof session unavailable")
            if self._initial_start is None:
                self._initial_start = self._now()
                self._state = "pending"
            deadline = local_lease_deadline(self._initial_start)
            request_started = self._require_before(deadline)
            try:
                raw = self._request(
                    "register",
                    proof=None,
                    expected=200,
                    deadline=deadline,
                    _cancellation_events=(stop,) if stop is not None else (),
                )
            except WorkerLeaseError:
                if stop is not None and stop.is_set():
                    self._die()
                    raise
                self._require_before(deadline)
                raise
            except BaseException:
                self._die()
                raise
            try:
                request_finished = self._require_before(deadline)
                body = self._body(
                    raw,
                    {"worker_identity_id", "instance_id", "instance_generation", "instance_proof"},
                )
                token = body["instance_proof"]
                claims = self._validate_identity(
                    verify_worker_proof_signature(token, self._verification_key)
                )
                if (
                    body["worker_identity_id"] != str(self._identity_id)
                    or body["instance_id"] != str(self._instance_id)
                    or type(body["instance_generation"]) is not int
                    or body["instance_generation"] != claims.generation
                    or claims.revision != 1
                    or claims.nominal_us != claims.guard_us
                    or claims.guard_us != claims.proof_before_us
                    or not isinstance(token, str)
                ):
                    raise WorkerLeaseError("proof session unavailable")
                deadline = min(
                    deadline,
                    _proof_deadline(self._initial_start, claims.nominal_us, claims.observed_us),
                )
                self._require_before(deadline)
                self._remember_observation_clock(
                    claims.observed_us, request_started, request_finished
                )
                self._proof, self._claims = token, claims
                self._nominal_deadline = self._guard_deadline = deadline
                self._observation = (claims.observed_us, request_started, request_finished)
                self._state = "active"
                if stop is not None and stop.is_set():
                    raise WorkerLeaseError("proof session unavailable")
            except BaseException as error:
                self._die()
                if not isinstance(error, Exception):
                    raise
                raise WorkerLeaseError("proof session unavailable") from None

    def heartbeat(
        self,
        observation: Mapping[str, Any] | None = None,
        *,
        stop: threading.Event | None = None,
    ) -> None:
        """A late or uncertain renewal permanently discards the proof."""
        if stop is not None and not isinstance(stop, threading.Event):
            raise ValueError("invalid stop signal")
        if not self._same_process():
            raise WorkerLeaseError("proof session unavailable")
        with self._lock:
            try:
                if stop is not None and stop.is_set():
                    raise WorkerLeaseError("proof session unavailable")
                deadline = self._active_deadline()
                start = self._require_before(deadline)
                old_proof, old = self._proof, self._claims
                assert old_proof is not None and old is not None
                payload = None
                if observation is not None:
                    raise WorkerLeaseError("capability observation unavailable")
                raw = self._request(
                    "heartbeat",
                    proof=old_proof,
                    expected=200,
                    deadline=deadline,
                    payload=payload,
                    _cancellation_events=(stop,) if stop is not None else (),
                )
                request_finished = self._require_before(deadline)
                body = self._body(raw, {"instance_proof"})
                token = body["instance_proof"]
                claims = self._validate_identity(
                    verify_worker_proof_signature(token, self._verification_key)
                )
                if (
                    not isinstance(token, str)
                    or token == old_proof
                    or claims.cluster_id != old.cluster_id
                    or claims.authority_epoch != old.authority_epoch
                    or claims.generation != old.generation
                    or claims.revision != old.revision + 1
                    or claims.candidate_id == old.candidate_id
                    or claims.guard_us != old.nominal_us
                    or claims.proof_before_us != min(old.nominal_us, old.guard_us)
                    or claims.observed_us < old.observed_us
                ):
                    raise WorkerLeaseError("proof session unavailable")
                self._check_observation_clock(claims.observed_us, start, request_finished)
                assert self._nominal_deadline is not None
                nominal_deadline = min(
                    local_lease_deadline(start),
                    _proof_deadline(start, claims.nominal_us, claims.observed_us),
                )
                guard_deadline = min(
                    self._nominal_deadline,
                    _proof_deadline(start, claims.guard_us, claims.observed_us),
                )
                self._require_before(min(deadline, nominal_deadline, guard_deadline))
                self._proof, self._claims = token, claims
                self._observation = (claims.observed_us, start, request_finished)
                self._guard_deadline = guard_deadline
                self._nominal_deadline = nominal_deadline
                if stop is not None and stop.is_set():
                    raise WorkerLeaseError("proof session unavailable")
            except BaseException as error:
                self._die()
                if not isinstance(error, Exception):
                    raise
                raise WorkerLeaseError("proof session unavailable") from None

    def report_capacity(
        self, make_sample: Callable[[UUID, UUID, int], SignedCapacityObservation]
    ) -> None:
        from dlr.worker.claim_http_v4 import request_proof

        if not self._same_process():
            raise WorkerLeaseError("proof session unavailable")
        with self._lock:
            deadline = self._active_deadline()
            started = self._require_before(deadline)
            claims, proof = self._claims, self._proof
            if claims is None or proof is None:
                raise WorkerLeaseError("proof session unavailable")
            sample = make_sample(self._identity_id, self._instance_id, claims.generation)
            cutoff = min(deadline, started + self._timeout)
            remaining = cutoff - self._require_before(cutoff)
            result = request_proof(
                url=f"{self._base_url}/api/workers/{self._worker_id}/v4/capacity",
                credential=self._credential,
                instance=str(self._instance_id),
                generation=claims.generation,
                body=sample.model_dump_json(),
                instance_proof=proof,
                attempt_proof=None,
                timeout_seconds=remaining,
                response_limit_bytes=_RESPONSE_LIMIT,
                _test_only_plaintext_loopback=self._test_only_plaintext_loopback,
                _absolute_deadline=cutoff,
                _cancellation_events=(self._lost,),
            )
            self._require_before(deadline)
            if result.state != "response" or result.status != 200:
                raise WorkerLeaseError("capacity observation unavailable")
            body = self._body(result.body, {"capacity_version"})
            if type(body["capacity_version"]) is not int or body["capacity_version"] <= 0:
                raise WorkerLeaseError("capacity observation unavailable")

    def report_ready(
        self,
        flags: dict[str, bool],
        *,
        capabilities: list[str] | None = None,
        business_credentials: bool = False,
        managed_files: bool = False,
        builtin_wheel: bool = False,
        adapter_cleanup: bool = False,
        cache_governance: bool = False,
    ) -> None:
        """Report a completed local fixed-Worker preflight under this proof."""
        if not self._same_process():
            raise WorkerLeaseError("proof session unavailable")
        with self._lock:
            try:
                from dlr.common.worker_languages_v4 import valid_capabilities
                from dlr.control.schemas.worker import REQUIRED_ISOLATION_CAPABILITIES
                from dlr.worker.claim_http_v4 import request_proof

                languages = ["python"] if capabilities is None else capabilities
                if (
                    type(flags) is not dict
                    or set(flags) != REQUIRED_ISOLATION_CAPABILITIES
                    or any(value is not True for value in flags.values())
                    or not valid_capabilities(languages)
                    or type(business_credentials) is not bool
                    or type(managed_files) is not bool
                    or type(builtin_wheel) is not bool
                    or type(adapter_cleanup) is not bool
                    or type(cache_governance) is not bool
                    or (cache_governance and adapter_cleanup)
                    or (builtin_wheel and not managed_files)
                ):
                    raise ValueError("preflight unavailable")
                deadline = self._active_deadline()
                started = self._require_before(deadline)
                claims, proof = self._claims, self._proof
                if claims is None or proof is None:
                    raise ValueError("proof unavailable")
                cutoff = min(deadline, started + self._timeout)
                result = request_proof(
                    url=f"{self._base_url}/api/workers/{self._worker_id}/v4/ready",
                    credential=self._credential,
                    instance=str(self._instance_id),
                    generation=claims.generation,
                    body=json.dumps(
                        {
                            "isolation_capabilities": flags,
                            "capabilities": languages,
                            **({"business_credentials_v1": True} if business_credentials else {}),
                            **({"managed_files_v1": True} if managed_files else {}),
                            **({"builtin_wheel_v1": True} if builtin_wheel else {}),
                            **({ADAPTER_CLEANUP_CAPABILITY: True} if adapter_cleanup else {}),
                            **({"cache_governance_v1": True} if cache_governance else {}),
                        },
                        separators=(",", ":"),
                    ),
                    instance_proof=proof,
                    attempt_proof=None,
                    timeout_seconds=min(self._timeout, cutoff - started),
                    response_limit_bytes=_RESPONSE_LIMIT,
                    _test_only_plaintext_loopback=self._test_only_plaintext_loopback,
                    _absolute_deadline=cutoff,
                    _cancellation_events=(self._lost,),
                )
                if result.state == "io_unknown":
                    self._die()
                self._require_before(deadline)
                if result.state != "response" or result.status != 200:
                    raise ValueError("preflight observation unavailable")
                response = self._body(result.body, {"capacity_state", "instance_generation"})
                if (
                    response["capacity_state"] not in ("ready", "quarantined")
                    or response["instance_generation"] != claims.generation
                ):
                    raise ValueError("preflight observation unavailable")
                self._require_before(deadline)
            except BaseException as error:
                self._die()
                if not isinstance(error, Exception):
                    raise
                raise WorkerLeaseError("proof session unavailable") from None

    def _data_exchange(
        self, route: str, payload: Mapping[str, object], stop: threading.Event
    ) -> bytes:
        if not self._same_process() or stop.is_set():
            raise WorkerLeaseError("proof session unavailable")
        with self._lock:
            active_before = self._active_deadline()
            started = self._require_before(active_before)
            if self._claims is None or self._proof is None:
                raise WorkerLeaseError("proof session unavailable")
            deadline = min(active_before, started + 1.5)
            request = urllib.request.Request(
                f"{self._base_url}/api/workers/{self._worker_id}/v4/{route}",
                data=json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._credential}",
                    "X-DLR-Worker-Instance": str(self._instance_id),
                    "X-DLR-Worker-Generation": str(self._claims.generation),
                    "X-DLR-Worker-Lease-Proof": self._proof,
                },
            )
            body = self._post(
                request,
                timeout=min(self._timeout, 1.5),
                deadline=deadline,
                expected=200,
                _cancellation_events=(stop,),
            )
            self._require_before(deadline)
            return body

    def replace_runtime_credential(
        self, credential: runtime_credential_v4.RuntimeCredential
    ) -> None:
        """Switch only to a confirmed identity-bound credential without regression."""
        if not self._same_process():
            raise WorkerLeaseError("proof session unavailable")
        with self._lock:
            try:
                deadline = self._active_deadline()
                self._require_before(deadline)
                if (
                    type(credential) is not runtime_credential_v4.RuntimeCredential
                    or type(credential.worker_id) is not int
                    or credential.worker_id != self._worker_id
                    or type(credential.worker_identity_id) is not UUID
                    or credential.worker_identity_id != self._identity_id
                    or type(credential.credential_record_id) is not int
                    or not 0 < credential.credential_record_id < 2**63
                    or not is_worker_credential(credential.secret)
                    or self._credential_record_id is None
                ):
                    raise WorkerLeaseError("proof session unavailable")
                same = secrets.compare_digest(credential.secret, self._credential)
                if (
                    credential.credential_record_id < self._credential_record_id
                    or (credential.credential_record_id == self._credential_record_id and not same)
                    or (credential.credential_record_id > self._credential_record_id and same)
                ):
                    raise WorkerLeaseError("proof session unavailable")
                self._require_before(deadline)
                self._credential = credential.secret
                self._credential_record_id = credential.credential_record_id
            except BaseException as error:
                self._die()
                if not isinstance(error, Exception):
                    raise
                raise WorkerLeaseError("proof session unavailable") from None

    def retire(self) -> None:
        """Retire this proof-bound generation and always discard local authority."""
        if not self._same_process():
            raise WorkerLeaseError("proof session unavailable")
        with self._lock:
            try:
                deadline = self._active_deadline()
                self._require_before(deadline)
                raw = self._request("offline", proof=self._proof, expected=204, deadline=deadline)
                self._require_before(deadline)
                if raw:
                    raise WorkerLeaseError("proof session unavailable")
            except BaseException as error:
                self._die()
                if not isinstance(error, Exception):
                    raise
                raise WorkerLeaseError("proof session unavailable") from None
            self._die()

    def is_usable(self) -> bool:
        """Observe the local timing fence only; no physical stop is implied."""
        if not self._same_process():
            return False
        with self._lock:
            if self._state != "active":
                return False
            try:
                self._require_before(self._active_deadline())
                return True
            except WorkerLeaseError:
                self._die()
                return False

    def local_deadline(self) -> float:
        """Return the currently verified local deadline for scheduling only."""
        if not self._same_process():
            raise WorkerLeaseError("proof session unavailable")
        with self._lock:
            try:
                deadline = self._active_deadline()
                self._require_before(deadline)
                return deadline
            except BaseException as error:
                self._die()
                if not isinstance(error, Exception):
                    raise
                raise WorkerLeaseError("proof session unavailable") from None

    def close(self) -> None:
        """Forget this session without reporting physical cleanup."""
        if not self._same_process():
            return
        # The proof lock may be held by network IO. Signal watchdogs before
        # waiting for it; any in-flight reply must then fail its local check.
        self._lost.set()
        with self._lock:
            self._die()
