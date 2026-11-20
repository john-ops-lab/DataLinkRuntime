"""Opt-in Control process registration and lease, not business authorization.

Only recent confirmed commits are displayed. Unknown commit, expired lease,
configuration change, suspend-unsafe clock or local stop never reports a live
instance. The database lease is not physical process/host or HA proof.
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import Settings
from dlr.common.schema_gate import try_control_gate
from dlr.control.schemas.reliable_runtime import AttemptActionBody
from dlr.control.services import attempt_lease_candidates_v4 as attempt_candidates
from dlr.control.services import control_cluster_identity as identity
from dlr.control.services import worker_candidate_storage_preflight_v4 as candidate_storage
from dlr.control.services import worker_lease_candidates_v4 as lease_candidates
from dlr.control.services import worker_proof_authority_v4 as proof_authority
from dlr.control.services.control_declaration_v4 import DeploymentDeclaration, read_declaration
from dlr.control.services.worker_token_fence_v4 import Fence, TokenFenceUnavailable
from dlr.control.services.worker_token_fence_v4 import acquire as acquire_worker_fence

Status = Literal["unknown", "registered", "incompatible", "expired", "stopped"]
_RENEW_SECONDS = 10.0
_PROBE_WAIT_SECONDS = 3.0


def _cache_clock() -> float | None:
    """Use elapsed time including suspend on Linux."""
    try:
        if sys.platform.startswith("linux"):
            return time.clock_gettime(time.CLOCK_BOOTTIME)
        if sys.platform == "darwin":
            return time.clock_gettime(time.CLOCK_MONOTONIC)
    except (AttributeError, OSError):
        pass
    return None


@dataclass(frozen=True)
class ProcessStatus:
    state: Status


class ControlProcess:
    def __init__(
        self, config: Settings, sessions: sessionmaker[Session], deployment: DeploymentDeclaration
    ) -> None:
        from dlr.control.services.business_credentials_v4 import freeze

        freeze(config)
        self._config, self._sessions, self._deployment = config, sessions, deployment
        self.ha_slot = config.control_ha_slot
        self._pid = os.getpid()
        self._instance, self._token = uuid4(), identity.new_instance_token()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: Status = "unknown"
        self._confirmed_until: float | None = None
        self._registered = False
        self._deadline_lost = False
        self._last_clock: float | None = None
        self._probe_active: threading.Event | None = None
        self._probe_result: Status = "unknown"
        self._compatibility: identity.DeclaredCompatibility | None = None

    def _lose_deadline_locked(self) -> None:
        self._deadline_lost = True
        self._state = "unknown"
        self._confirmed_until = None

    def _clock_locked(self) -> float | None:
        try:
            now = _cache_clock()
        except Exception:
            self._lose_deadline_locked()
            return None
        if (
            type(now) is not float
            or not math.isfinite(now)
            or (self._last_clock is not None and now < self._last_clock)
        ):
            self._lose_deadline_locked()
            return None
        self._last_clock = now
        return now

    def _mark_unknown(self) -> None:
        """Keep the prior deadline so a retry cannot extend unknown authority."""
        if self._pid != os.getpid():
            return
        with self._lock:
            if not self._stop.is_set():
                self._state = "unknown"

    @property
    def instance_id(self) -> UUID:
        return self._instance

    def check_authority(self) -> None:
        """Reject operations once this process cannot prove its local lease."""
        if self._pid != os.getpid():
            raise TokenFenceUnavailable("control authority unavailable")
        with self._lock:
            now = self._clock_locked()
            if (
                self._stop.is_set()
                or self._state != "registered"
                or not self._registered
                or now is None
                or self._confirmed_until is None
                or now >= self._confirmed_until
            ):
                raise TokenFenceUnavailable("control authority unavailable")
        try:
            if (
                self._config.control_ha_slot != self.ha_slot
                or read_declaration(self._config) != self._deployment
            ):
                raise ValueError
        except Exception:
            raise TokenFenceUnavailable("control authority unavailable") from None

    def capacity_compatible(self) -> bool:
        self.check_authority()
        with self._lock:
            return self._compatibility is not None and self._compatibility.capacity_match

    def compatibility(self) -> identity.DeclaredCompatibility | None:
        self.check_authority()
        with self._lock:
            return self._compatibility

    def assert_live(self, session: Session) -> None:
        self.check_authority()
        epoch = identity.assert_live_control(
            session,
            self._deployment.authority,
            instance_id=self._instance,
            instance_token=self._token,
        )
        if epoch != self._deployment.authority_epoch:
            raise TokenFenceUnavailable("control authority unavailable")
        self.check_authority()

    def worker_fence(self, session: Session, *, issue_credentials: bool = False) -> Fence:
        """Acquire the database fence before taking any Worker business lock."""
        self.check_authority()
        fence = acquire_worker_fence(
            session,
            self._deployment.authority,
            instance_id=self._instance,
            instance_token=self._token,
            mode="issuance" if issue_credentials else "operation",
        )
        self.check_authority()
        return fence

    def claim_child_identity(self) -> tuple[str, str, float]:
        """Bound a supervised Claim child by this process's confirmed deadline.

        The token is private IPC material. The parent must recheck authority
        before accepting a child result or issuing a post-commit proof.
        """
        self.check_authority()
        with self._lock:
            now = self._clock_locked()
            if (
                self._stop.is_set()
                or self._state != "registered"
                or not self._registered
                or now is None
                or self._confirmed_until is None
                or now >= self._confirmed_until
            ):
                raise TokenFenceUnavailable("control authority unavailable")
            return str(self._instance), self._token, self._confirmed_until

    def worker_proof_authority(self, session: Session) -> lease_candidates.ProofAuthority:
        """Bind signatures to the current authority and protected candidate storage."""
        self.check_authority()
        if (
            candidate_storage.inspect_candidate_storage(session, expected_schema="public").state
            != "observed"
        ):
            raise TokenFenceUnavailable("worker proof unavailable")
        result = proof_authority.for_transaction(session, self._deployment)
        self.check_authority()
        return result

    def observe_worker_candidate(
        self, candidate: lease_candidates.CandidateRef, credential: str
    ) -> str:
        """Sign only a candidate observed committed in a separate transaction."""
        with self._sessions() as session:
            fence = self.worker_fence(session)
            authority = self.worker_proof_authority(session)
            proof = lease_candidates._issue_in_session(
                session, expected=candidate, credential=credential, authority=authority
            )
            fence.check()
            self.check_authority()
            session.rollback()
        self.check_authority()
        return proof.token

    def observe_attempt_candidate(
        self,
        candidate: attempt_candidates.AttemptCandidateRef,
        credential: str,
        instance_proof: str,
        action: AttemptActionBody,
    ) -> str:
        """Observe a committed Attempt before returning its execution proof."""
        with self._sessions() as session:
            fence = self.worker_fence(session)
            authority = self.worker_proof_authority(session)
            proof = attempt_candidates._issue_in_session(
                session,
                expected=candidate,
                credential=credential,
                instance_proof=instance_proof,
                action=action,
                authority=authority,
            )
            fence.check()
            self.check_authority()
            session.rollback()
        self.check_authority()
        return proof.token

    def start(self) -> None:
        # A forked child cannot reuse the parent's lifecycle or locks.
        if self._pid != os.getpid():
            raise ValueError("Control lifecycle unavailable")
        if (
            self._config is not None
            and self._config.worker_remote_v4_enabled
            and (not self._cycle() or self._state != "registered")
        ):
            raise ValueError("Control lifecycle unavailable")
        with self._lock:
            if self._thread is not None or self._pid != os.getpid() or self._stop.is_set():
                raise ValueError("Control lifecycle unavailable")
            thread = threading.Thread(target=self._run, name="dlr-control-identity", daemon=True)
            self._thread = thread
            try:
                thread.start()
            except BaseException as error:
                # start() can raise after a thread was launched. Retain its
                # handle, request stop and never allow a duplicate lifecycle.
                self._stop.set()
                if isinstance(error, Exception):
                    raise ValueError("Control lifecycle unavailable") from None
                raise

    def status(self) -> ProcessStatus:
        """Return a bounded committed authority observation, never readiness."""
        if self._pid != os.getpid():
            return ProcessStatus("stopped")
        with self._lock:
            if self._pid != os.getpid() or self._stop.is_set():
                return ProcessStatus("stopped")
            if self._state not in {"registered", "incompatible"}:
                return ProcessStatus(self._state)
            now = self._clock_locked()
            if now is None or self._confirmed_until is None or now >= self._confirmed_until:
                self._lose_deadline_locked()
                return ProcessStatus("unknown")
            event = self._probe_active
            if event is None:
                event = threading.Event()
                self._probe_active = event
                try:
                    threading.Thread(
                        target=self._probe,
                        args=(event,),
                        name="dlr-control-identity-probe",
                        daemon=True,
                    ).start()
                except Exception:
                    # A start failure does not prove no probe thread exists.
                    # Retain the event until that thread itself confirms exit;
                    # if it never started, observations remain unknown.
                    return ProcessStatus("unknown")
        # If a network/commit hangs, retain the one in-flight thread and never
        # start a duplicate; callers receive unknown after a bounded wait.
        if not event.wait(_PROBE_WAIT_SECONDS):
            return ProcessStatus("unknown")
        with self._lock:
            if self._stop.is_set():
                return ProcessStatus("stopped")
            now = self._clock_locked()
            if now is None or self._confirmed_until is None or now >= self._confirmed_until:
                self._lose_deadline_locked()
                return ProcessStatus("unknown")
            return ProcessStatus(self._probe_result)

    def _probe(self, event: threading.Event) -> None:
        result: Status = "unknown"
        try:
            if read_declaration(self._config) != self._deployment:
                raise ValueError
            with self._sessions() as session:
                try_control_gate(session)
                session.execute(text("SET LOCAL statement_timeout = '2000ms'"))
                session.execute(text("SET LOCAL lock_timeout = '500ms'"))
                epoch = identity.assert_live_control(
                    session,
                    self._deployment.authority,
                    instance_id=self._instance,
                    instance_token=self._token,
                )
                if (
                    epoch != self._deployment.authority_epoch
                    or read_declaration(self._config) != self._deployment
                ):
                    raise ValueError
                session.commit()
            with self._lock:
                now = self._clock_locked()
                if (
                    not self._stop.is_set()
                    and self._state in {"registered", "incompatible"}
                    and now is not None
                    and self._confirmed_until is not None
                    and now < self._confirmed_until
                ):
                    result = self._state
        except Exception:
            pass
        finally:
            with self._lock:
                self._probe_result = result
                self._probe_active = None
                event.set()

    def stop(self, timeout: float = 2.0) -> None:
        if self._pid != os.getpid():
            return
        self._stop.set()
        with self._lock:
            self._state = "stopped"
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # The retained Thread may never have started. This does not prove
            # that other resources or external work have stopped.
            with suppress(RuntimeError):
                thread.join(max(0.0, min(timeout, 2.0)))

    def _set(self, state: Status, until: float | None = None) -> None:
        if self._pid != os.getpid():
            return
        with self._lock:
            if not self._stop.is_set() and (
                not self._deadline_lost or state in {"unknown", "expired", "stopped"}
            ):
                self._state, self._confirmed_until = state, until

    def _cycle(self) -> bool:
        """One commit-confirmed register/renew; False means terminal expiry."""
        try:
            try:
                return self._cycle_once()
            except identity.RenewalRowLockTimeout:
                # The failed session has rolled back and closed. Recheck the
                # previous deadline and all authority in a fresh transaction.
                return self._cycle_once()
        except BaseException:
            self._mark_unknown()
            raise

    @contextmanager
    def _cycle_session(self, *, was_authorized: bool) -> Iterator[Session]:
        with self._sessions() as session:
            try:
                yield session
            except identity.RenewalRowLockTimeout:
                self._mark_unknown()
                session.rollback()
                if not was_authorized:
                    raise identity.ControlIdentityUnavailable(
                        "control authority unavailable"
                    ) from None
                raise
            except BaseException:
                # Revoke local use before implicit rollback or connection cleanup can block.
                self._mark_unknown()
                raise

    def _cycle_once(self) -> bool:
        if self._pid != os.getpid():
            return False
        with self._lock:
            if self._deadline_lost or self._stop.is_set():
                return False
            previous_until = self._confirmed_until
            was_registered = self._registered
            was_authorized = self._state == "registered"
            started = self._clock_locked()
            if started is None:
                return False
            if was_registered and (previous_until is None or started >= previous_until):
                self._lose_deadline_locked()
                return False
            # An in-flight renewal does not invalidate the previously
            # confirmed lease. It grants no extension until COMMIT is known;
            # any failed/unknown outcome below immediately marks us unknown.
            if not was_registered:
                self._state = "unknown"
        if (
            self._config.control_ha_slot != self.ha_slot
            or read_declaration(self._config) != self._deployment
        ):
            self._mark_unknown()
            return True
        with self._cycle_session(was_authorized=was_authorized) as session:
            try_control_gate(session)
            session.execute(text("SET LOCAL statement_timeout = '2000ms'"))
            session.execute(text("SET LOCAL lock_timeout = '500ms'"))
            if was_registered:
                result = identity.renew_control(
                    session,
                    self._deployment.authority,
                    instance_id=self._instance,
                    instance_token=self._token,
                    ha_slot=self.ha_slot,
                )
            else:
                result = identity.register_control(
                    session,
                    self._deployment.authority,
                    instance_id=self._instance,
                    instance_token=self._token,
                    single_process=self._config.worker_remote_v4_enabled and self.ha_slot is None,
                    ha_slot=self.ha_slot,
                )
            if isinstance(result, identity.ExpiredControl):
                session.commit()  # revocation must itself be confirmed
                self._set("expired")
                return False
            now = session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
            if now is None:
                self._mark_unknown()
                return True
            remaining = (result.lease_until - now).total_seconds()
            # Validate the exact authorized epoch, including after row waits.
            epoch = identity.assert_live_control(
                session,
                self._deployment.authority,
                instance_id=self._instance,
                instance_token=self._token,
            )
            if (
                epoch != self._deployment.authority_epoch
                or not 0 < remaining <= identity.LEASE_SECONDS
            ):
                self._mark_unknown()
                return True
            if (
                self._config.control_ha_slot != self.ha_slot
                or read_declaration(self._config) != self._deployment
            ):
                self._mark_unknown()
                return True
            session.commit()
        with self._lock:
            self._registered = True
            self._compatibility = result
            finished = self._clock_locked()
            until = started + remaining
            if (
                finished is None
                or finished >= until
                or (was_registered and (previous_until is None or finished >= previous_until))
            ):
                self._lose_deadline_locked()
                return False
        matched = all(
            (
                result.managed_match,
                result.builtin_match,
                result.attachment_match,
                result.capacity_match,
            )
        )
        self._set("registered" if matched or self.ha_slot is not None else "incompatible", until)
        return True

    def _finish(self) -> None:
        # One best-effort fence, on the lifecycle thread after all prior IO ends.
        # Failure or process death leaves the lease to expire; never report it
        # as proof of host, native connector or Worker physical shutdown.
        try:
            with self._sessions() as session:
                try_control_gate(session)
                session.execute(text("SET LOCAL statement_timeout = '2000ms'"))
                session.execute(text("SET LOCAL lock_timeout = '500ms'"))
                identity.fence_control(
                    session,
                    self._deployment.authority,
                    instance_id=self._instance,
                    instance_token=self._token,
                )
                session.commit()
        except Exception:
            pass

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    if not self._cycle():
                        break
                except Exception:
                    self._mark_unknown()
                if self._stop.wait(_RENEW_SECONDS):
                    break
        finally:
            self._finish()
