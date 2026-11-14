"""Unwired Attempt lease candidate prototype; not a production API.

Claim must own its existing business locks and complete all admission, budget,
Slot, and Execution mutations in the same transaction. Other callers acquire
the established business lock prefix before Worker, credential, instance
candidate and Attempt candidate. No function here commits a mutation, starts
work, proves a cluster version/clock contract, or physically stops processes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import BigInteger, inspect, literal_column, select
from sqlalchemy.orm import Session, object_session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import (
    AdapterExecutionSlot,
    AttemptLeaseCandidateV4,
    Execution,
    ExecutionAttempt,
    Worker,
    WorkerCredential,
    WorkerLeaseCandidateV4,
)
from dlr.control.schemas.reliable_runtime import AttemptActionBody
from dlr.control.services import attempt, worker_availability, worker_credentials
from dlr.control.services.attempt_lease_proof_v4 import (
    AttemptLeaseProofBinding,
    AttemptLeaseProofError,
    IssuedAttemptLeaseProof,
    issue_attempt_lease_proof,
    verify_attempt_lease_proof,
)
from dlr.control.services.worker_attempt_identity import validate_v4_attempt_locked
from dlr.control.services.worker_lease_candidates_v4 import (
    LeaseCandidateError,
    ProofAuthority,
    _committed_xid_matches,
    _worker_and_candidate,
    _xid,
    validate_current_proof,
)
from dlr.control.services.worker_lease_candidates_v4 import (
    _binding as _instance_binding,
)
from dlr.control.services.worker_lease_proof_v4 import verify_observation_proof


class AttemptLeaseCandidateError(Exception):
    """A redacted unknown or rejected candidate."""


@dataclass(frozen=True)
class AttemptCandidateRef:
    worker_id: int
    attempt_id: int
    instance_id: UUID
    generation: int
    fencing_token: int
    revision: int
    candidate_id: UUID

    def __post_init__(self) -> None:
        numbers = (
            self.worker_id,
            self.attempt_id,
            self.generation,
            self.fencing_token,
            self.revision,
        )
        if (
            any(type(value) is not int or not 0 < value < 2**63 for value in numbers)
            or type(self.instance_id) is not UUID
            or self.instance_id.int == 0
            or type(self.candidate_id) is not UUID
            or self.candidate_id.int == 0
        ):
            raise AttemptLeaseCandidateError("Attempt lease unavailable")


def _ref(row: AttemptLeaseCandidateV4) -> AttemptCandidateRef:
    return AttemptCandidateRef(
        row.worker_id,
        row.attempt_id,
        row.instance_id,
        row.instance_generation,
        row.fencing_token,
        row.revision,
        row.candidate_id,
    )


def _binding(row: AttemptLeaseCandidateV4, authority: ProofAuthority) -> AttemptLeaseProofBinding:
    return AttemptLeaseProofBinding(
        authority.cluster_id,
        authority.authority_epoch,
        row.worker_id,
        row.worker_identity_id,
        row.instance_id,
        row.instance_generation,
        row.attempt_id,
        row.fencing_token,
        row.revision,
        row.candidate_id,
        row.nominal_until,
        row.guard_until,
        row.proof_before,
    )


def _already_committed(session: Session, row: AttemptLeaseCandidateV4) -> None:
    current = _xid(session)
    tuple_xid = session.scalar(
        select(literal_column("attempt_lease_candidates_v4.xmin::text::bigint", type_=BigInteger()))
        .select_from(AttemptLeaseCandidateV4)
        .where(AttemptLeaseCandidateV4.attempt_id == row.attempt_id)
    )
    if not _committed_xid_matches(current, row.writer_xid, tuple_xid):
        raise AttemptLeaseCandidateError("Attempt lease unavailable")


def _credential_expiry(session: Session, worker_id: int, credential: str) -> datetime | None:
    worker_credentials.authenticate_runtime_credential(session, worker_id, credential)
    row = session.scalar(
        select(WorkerCredential).where(
            WorkerCredential.worker_id == worker_id,
            WorkerCredential.kind == "runtime",
            WorkerCredential.secret_digest == worker_credentials._digest(credential),
        )
    )
    if row is None:
        raise AttemptLeaseCandidateError("Attempt lease unavailable")
    return row.expires_at


def _check_instance_at(
    row: WorkerLeaseCandidateV4, authority: ProofAuthority, token: str, now: datetime
) -> None:
    if row.retired is not False or not verify_observation_proof(
        token=token, key=authority.key, binding=_instance_binding(row, authority), database_now=now
    ):
        raise AttemptLeaseCandidateError("Attempt lease unavailable")


def stage_initial_attempt_candidate(
    session: Session,
    *,
    execution: Execution,
    slot: AdapterExecutionSlot,
    attempt: ExecutionAttempt,
    credential: str,
    instance_proof: str,
    authority: ProofAuthority,
    expected_attempt_id: int | None = None,
) -> AttemptCandidateRef:
    """Add a transient new Attempt; caller must complete Claim before commit.

    Existing persisted Attempts cannot be backfilled by this entry. Session
    membership is checked, but only the Claim caller can prove its lock prefix.
    """
    try:
        if (
            type(authority) is not ProofAuthority
            or object_session(execution) is not session
            or object_session(slot) is not session
            or not inspect(attempt).transient
            or (
                expected_attempt_id is None
                and attempt.id is not None
                or expected_attempt_id is not None
                and (
                    type(expected_attempt_id) is not int
                    or not 0 < expected_attempt_id < 2**63
                    or attempt.id != expected_attempt_id
                )
            )
            or attempt.status != "claimed"
            or attempt.execution_id != execution.id
            or attempt.adapter_id != execution.adapter_id
            or execution.cancel_requested
            or slot.adapter_id != execution.adapter_id
            or slot.slot_no != 0
            or slot.active_attempt_id is not None
            or type(attempt.worker_id) is not int
            or not 0 < attempt.worker_id < 2**63
            or type(attempt.worker_instance_id) is not UUID
            or attempt.worker_instance_id.int == 0
            or type(attempt.worker_instance_generation) is not int
            or not 0 < attempt.worker_instance_generation < 2**63
            or type(attempt.fencing_token) is not int
            or not 0 < attempt.fencing_token < 2**63
            or attempt.fencing_token != max(int(slot.fencing_token) + 1, 1)
            or attempt.attempt_no != execution.attempt_count + 1
        ):
            raise AttemptLeaseCandidateError("Attempt lease unavailable")
        instance = validate_current_proof(
            session,
            worker_id=attempt.worker_id,
            credential=credential,
            instance_id=attempt.worker_instance_id,
            generation=attempt.worker_instance_generation,
            token=instance_proof,
            authority=authority,
        )
        worker = session.get(Worker, attempt.worker_id)
        if (
            worker is None
            or worker.identity_id != attempt.worker_identity_id
            or worker.scheduling_state not in {"active", "draining"}
        ):
            raise AttemptLeaseCandidateError("Attempt lease unavailable")
        expires = _credential_expiry(session, attempt.worker_id, credential)
        now = worker_availability.current_time(session)
        worker_credentials._confirm_runtime_credential_at_locked(
            session, attempt.worker_id, credential, now
        )
        _check_instance_at(instance, authority, instance_proof, now)
        nominal = min(attempt.lease_expires_at, instance.nominal_until, instance.guard_until)
        if expires is not None:
            nominal = min(nominal, expires)
        if nominal <= now or (
            attempt.recovery_not_before is not None and attempt.recovery_not_before < nominal
        ):
            raise AttemptLeaseCandidateError("Attempt lease unavailable")
        attempt.lease_expires_at = nominal
        session.add(attempt)
        session.flush()
        row = AttemptLeaseCandidateV4(
            attempt_id=attempt.id,
            worker_id=attempt.worker_id,
            worker_identity_id=worker.identity_id,
            instance_id=attempt.worker_instance_id,
            instance_generation=attempt.worker_instance_generation,
            fencing_token=attempt.fencing_token,
            revision=1,
            candidate_id=uuid4(),
            nominal_until=nominal,
            guard_until=nominal,
            proof_before=nominal,
            writer_xid=_xid(session),
        )
        session.add(row)
        session.flush()
        _credential_expiry(session, attempt.worker_id, credential)
        after = worker_availability.current_time(session)
        worker_credentials._confirm_runtime_credential_at_locked(
            session, attempt.worker_id, credential, after
        )
        _check_instance_at(instance, authority, instance_proof, after)
        if after >= nominal:
            raise AttemptLeaseCandidateError("Attempt lease unavailable")
        return _ref(row)
    except (HTTPException, LeaseCandidateError, worker_credentials.WorkerCredentialError):
        raise AttemptLeaseCandidateError("Attempt lease unavailable") from None


def _locked_candidate(
    session: Session,
    *,
    worker_id: int,
    attempt_id: int,
    credential: str,
    instance_id: UUID,
    generation: int,
    instance_proof: str,
    action: AttemptActionBody,
    authority: ProofAuthority,
) -> tuple[ExecutionAttempt, AdapterExecutionSlot, AttemptLeaseCandidateV4, WorkerLeaseCandidateV4]:
    if (
        type(authority) is not ProofAuthority
        or type(worker_id) is not int
        or not 0 < worker_id < 2**63
        or type(attempt_id) is not int
        or not 0 < attempt_id < 2**63
        or type(instance_id) is not UUID
        or instance_id.int == 0
        or type(generation) is not int
        or not 0 < generation < 2**63
        or not isinstance(action, AttemptActionBody)
    ):
        raise AttemptLeaseCandidateError("Attempt lease unavailable")
    try:
        execution, _adapter, current, slot = attempt._lock_attempt_context(
            session, worker_id, attempt_id, existing_only=True
        )
        state = validate_v4_attempt_locked(
            session,
            execution=execution,
            attempt=current,
            slot=slot,
            worker_id=worker_id,
            credential=credential,
            instance_id=instance_id,
            instance_generation=generation,
            action=action,
        )
        if state != "active":
            raise AttemptLeaseCandidateError("Attempt lease unavailable")
        instance = validate_current_proof(
            session,
            worker_id=worker_id,
            credential=credential,
            instance_id=instance_id,
            generation=generation,
            token=instance_proof,
            authority=authority,
        )
        row = session.scalar(
            select(AttemptLeaseCandidateV4)
            .where(AttemptLeaseCandidateV4.attempt_id == attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            row is None
            or row.worker_id != worker_id
            or row.worker_identity_id != current.worker_identity_id
            or row.worker_identity_id != instance.worker_identity_id
            or row.instance_id != instance_id
            or row.instance_id != current.worker_instance_id
            or row.instance_generation != generation
            or row.instance_generation != current.worker_instance_generation
            or row.fencing_token != current.fencing_token
            or row.fencing_token != slot.fencing_token
            or row.nominal_until != current.lease_expires_at
            or row.nominal_until != slot.lease_expires_at
        ):
            raise AttemptLeaseCandidateError("Attempt lease unavailable")
        _already_committed(session, row)
        return current, slot, row, instance
    except (HTTPException, LeaseCandidateError, worker_credentials.WorkerCredentialError):
        raise AttemptLeaseCandidateError("Attempt lease unavailable") from None


def worker_stop_requested_locked(session: Session, worker_id: int) -> bool:
    """Read the Worker barrier only after the caller has locked that Worker."""
    worker = session.get(Worker, worker_id)
    return worker is None or worker.scheduling_state not in {"active", "draining"}


def validate_current_attempt_proof(
    session: Session,
    *,
    worker_id: int,
    attempt_id: int,
    credential: str,
    instance_id: UUID,
    generation: int,
    instance_proof: str,
    attempt_proof: str,
    action: AttemptActionBody,
    authority: ProofAuthority,
    _allow_stopped_for_signal: bool = False,
) -> AttemptLeaseCandidateV4:
    """Check both proofs under locks; the caller retains them until commit."""
    current, slot, row, instance = _locked_candidate(
        session,
        worker_id=worker_id,
        attempt_id=attempt_id,
        credential=credential,
        instance_id=instance_id,
        generation=generation,
        instance_proof=instance_proof,
        action=action,
        authority=authority,
    )
    del current, slot
    try:
        _credential_expiry(session, worker_id, credential)
        now = worker_availability.current_time(session)
        worker_credentials._confirm_runtime_credential_at_locked(
            session, worker_id, credential, now
        )
        _check_instance_at(instance, authority, instance_proof, now)
        if not verify_attempt_lease_proof(
            token=attempt_proof,
            key=authority.key,
            binding=_binding(row, authority),
            database_now=now,
        ):
            raise AttemptLeaseCandidateError("Attempt lease unavailable")
        if worker_stop_requested_locked(session, worker_id) and not _allow_stopped_for_signal:
            raise AttemptLeaseCandidateError("Attempt lease unavailable")
        return row
    except worker_credentials.WorkerCredentialError:
        raise AttemptLeaseCandidateError("Attempt lease unavailable") from None


def _issue_in_session(
    session: Session,
    *,
    expected: AttemptCandidateRef,
    credential: str,
    instance_proof: str,
    action: AttemptActionBody,
    authority: ProofAuthority,
) -> IssuedAttemptLeaseProof:
    if type(expected) is not AttemptCandidateRef:
        raise AttemptLeaseCandidateError("Attempt lease unavailable")
    try:
        current, _, row, instance = _locked_candidate(
            session,
            worker_id=expected.worker_id,
            attempt_id=expected.attempt_id,
            credential=credential,
            instance_id=expected.instance_id,
            generation=expected.generation,
            instance_proof=instance_proof,
            action=action,
            authority=authority,
        )
        if _ref(row) != expected:
            raise AttemptLeaseCandidateError("Attempt lease unavailable")
        execution = session.get(Execution, current.execution_id)
        if (
            execution is None
            or execution.cancel_requested
            or worker_stop_requested_locked(session, expected.worker_id)
        ):
            raise AttemptLeaseCandidateError("Attempt lease unavailable")
        _credential_expiry(session, expected.worker_id, credential)
        now = worker_availability.current_time(session)
        worker_credentials._confirm_runtime_credential_at_locked(
            session, expected.worker_id, credential, now
        )
        _check_instance_at(instance, authority, instance_proof, now)
        return issue_attempt_lease_proof(
            key=authority.key, binding=_binding(row, authority), observed_at=now
        )
    except (AttemptLeaseProofError, worker_credentials.WorkerCredentialError):
        raise AttemptLeaseCandidateError("Attempt lease unavailable") from None


def issue_attempt_candidate_proof(
    factory: sessionmaker[Session],
    *,
    expected: AttemptCandidateRef,
    credential: str,
    instance_proof: str,
    action: AttemptActionBody,
    authority: ProofAuthority,
) -> IssuedAttemptLeaseProof:
    """Observe in a separate transaction and release locks before delivery."""
    with factory() as session:
        proof = _issue_in_session(
            session,
            expected=expected,
            credential=credential,
            instance_proof=instance_proof,
            action=action,
            authority=authority,
        )
        session.rollback()
        return proof


def stage_attempt_renewal(
    session: Session,
    *,
    worker_id: int,
    attempt_id: int,
    credential: str,
    instance_id: UUID,
    generation: int,
    instance_proof: str,
    attempt_proof: str,
    action: AttemptActionBody,
    authority: ProofAuthority,
) -> AttemptCandidateRef:
    """Write one successor; no authority exists until a timely later proof."""
    current, slot, row, instance = _locked_candidate(
        session,
        worker_id=worker_id,
        attempt_id=attempt_id,
        credential=credential,
        instance_id=instance_id,
        generation=generation,
        instance_proof=instance_proof,
        action=action,
        authority=authority,
    )
    try:
        execution = session.get(Execution, current.execution_id)
        if (
            execution is None
            or execution.cancel_requested
            or worker_stop_requested_locked(session, worker_id)
        ):
            raise AttemptLeaseCandidateError("Attempt lease unavailable")
        old = _binding(row, authority)
        expires = _credential_expiry(session, worker_id, credential)
        now = worker_availability.current_time(session)
        worker_credentials._confirm_runtime_credential_at_locked(
            session, worker_id, credential, now
        )
        _check_instance_at(instance, authority, instance_proof, now)
        if row.revision >= 2**63 - 1 or not verify_attempt_lease_proof(
            token=attempt_proof, key=authority.key, binding=old, database_now=now
        ):
            raise AttemptLeaseCandidateError("Attempt lease unavailable")
        nominal = min(
            now + timedelta(seconds=settings.attempt_lease_seconds),
            instance.nominal_until,
            instance.guard_until,
        )
        if expires is not None:
            nominal = min(nominal, expires)
        if nominal <= now or (
            current.recovery_not_before is not None and current.recovery_not_before < nominal
        ):
            raise AttemptLeaseCandidateError("Attempt lease unavailable")
        prior = min(
            row.nominal_until, row.guard_until, instance.nominal_until, instance.guard_until
        )
        if expires is not None:
            prior = min(prior, expires)
        row.guard_until = row.nominal_until
        row.nominal_until = nominal
        row.proof_before = prior
        row.revision += 1
        row.candidate_id = uuid4()
        row.writer_xid = _xid(session)
        current.lease_expires_at = nominal
        slot.lease_expires_at = nominal
        session.flush()
        _credential_expiry(session, worker_id, credential)
        after = worker_availability.current_time(session)
        worker_credentials._confirm_runtime_credential_at_locked(
            session, worker_id, credential, after
        )
        _check_instance_at(instance, authority, instance_proof, after)
        if not verify_attempt_lease_proof(
            token=attempt_proof, key=authority.key, binding=old, database_now=after
        ):
            raise AttemptLeaseCandidateError("Attempt lease unavailable")
        return _ref(row)
    except worker_credentials.WorkerCredentialError:
        raise AttemptLeaseCandidateError("Attempt lease unavailable") from None


def observe_expired_attempt_candidate(session: Session, *, worker_id: int, attempt_id: int) -> bool:
    """Time observation only, not authorization to recover or release resources."""
    try:
        _, _, current, slot = attempt._lock_attempt_context(
            session, worker_id, attempt_id, existing_only=True
        )
        worker_credentials._lock_worker(session, worker_id)
        _, instance = _worker_and_candidate(session, worker_id)
        row = session.scalar(
            select(AttemptLeaseCandidateV4)
            .where(AttemptLeaseCandidateV4.attempt_id == attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            row is None
            or row.worker_id != worker_id
            or row.worker_identity_id != current.worker_identity_id
            or row.worker_identity_id != instance.worker_identity_id
            or row.instance_id != current.worker_instance_id
            or row.instance_id != instance.instance_id
            or row.instance_generation != current.worker_instance_generation
            or row.instance_generation != instance.instance_generation
            or row.fencing_token != current.fencing_token
            or row.fencing_token != slot.fencing_token
            or row.nominal_until != current.lease_expires_at
            or row.nominal_until != slot.lease_expires_at
        ):
            return False
        _already_committed(session, row)
        return worker_availability.current_time(session) >= min(row.nominal_until, row.guard_until)
    except (
        HTTPException,
        LeaseCandidateError,
        worker_credentials.WorkerCredentialError,
        AttemptLeaseCandidateError,
    ):
        return False
