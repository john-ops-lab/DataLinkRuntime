"""Unwired v4 committed-candidate prototype; it does not expose a router.

All callers retain the existing business-lock prefix, then lock Worker,
credential and candidate in this order. The caller of a mutation owns commit
or rollback. No function here starts work, proves clock/cluster health, stops
an old process, authorizes a takeover, or completes the v4 deployment gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, func, literal_column, select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.worker_protocol_v4 import INSTANCE_LEASE_SECONDS
from dlr.control.models import Worker, WorkerInstanceHistory, WorkerLeaseCandidateV4
from dlr.control.services import worker_availability, worker_credentials, worker_instances
from dlr.control.services.worker_lease_proof_v4 import (
    IssuedObservationProof,
    LeaseProofBinding,
    LeaseProofError,
    issue_observation_proof,
    public_key_bytes,
    verify_observation_proof,
)


class LeaseCandidateError(Exception):
    """A non-sensitive failure; callers must retain uncertain responsibility."""


@dataclass(frozen=True)
class ProofAuthority:
    """A private test input, not evidence of cluster key agreement."""

    cluster_id: UUID
    authority_epoch: UUID
    key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.cluster_id) is not UUID
            or self.cluster_id.int == 0
            or type(self.authority_epoch) is not UUID
            or self.authority_epoch.int == 0
            or type(self.key) is not bytes
            or len(self.key) != 32
        ):
            raise LeaseCandidateError("lease authority unavailable")

    @property
    def public_key(self) -> bytes:
        return public_key_bytes(self.key)


@dataclass(frozen=True)
class CandidateRef:
    worker_id: int
    instance_id: UUID
    generation: int
    revision: int
    candidate_id: UUID

    def __post_init__(self) -> None:
        if (
            type(self.worker_id) is not int
            or not 0 < self.worker_id < 2**63
            or type(self.instance_id) is not UUID
            or self.instance_id.int == 0
            or type(self.generation) is not int
            or not 0 < self.generation < 2**63
            or type(self.revision) is not int
            or not 0 < self.revision < 2**63
            or type(self.candidate_id) is not UUID
            or self.candidate_id.int == 0
        ):
            raise LeaseCandidateError("lease candidate unavailable")


def _instance_args(worker_id: int, instance_id: UUID, generation: int | None = None) -> None:
    if (
        type(worker_id) is not int
        or not 0 < worker_id < 2**63
        or type(instance_id) is not UUID
        or instance_id.int == 0
        or (generation is not None and (type(generation) is not int or not 0 < generation < 2**63))
    ):
        raise LeaseCandidateError("lease candidate unavailable")


def _xid(session: Session) -> int:
    xid = session.scalar(select(func.pg_catalog.txid_current()))
    if type(xid) is not int or not 0 < xid < 2**63:
        raise LeaseCandidateError("lease candidate unavailable")
    return xid


def _committed_xid_matches(current: object, writer: object, tuple_xid: object) -> bool:
    """Fail closed when a 32-bit xmin cannot identify the stored full xid.

    The observer must use a later assigned xid, and the writer must be within
    half a 32-bit xid space of it. This does not prove cluster identity,
    failover safety or a commit deadline.
    """
    return bool(
        type(current) is int
        and type(writer) is int
        and type(tuple_xid) is int
        and 0 < writer < current < 2**63
        and current - writer < 2**31
        and 3 <= tuple_xid < 2**32
        and writer % 2**32 == tuple_xid
    )


def _ref(row: WorkerLeaseCandidateV4) -> CandidateRef:
    return CandidateRef(
        row.worker_id, row.instance_id, row.instance_generation, row.revision, row.candidate_id
    )


def _binding(row: WorkerLeaseCandidateV4, authority: ProofAuthority) -> LeaseProofBinding:
    return LeaseProofBinding(
        cluster_id=authority.cluster_id,
        authority_epoch=authority.authority_epoch,
        worker_id=row.worker_id,
        identity_id=row.worker_identity_id,
        instance_id=row.instance_id,
        generation=row.instance_generation,
        revision=row.revision,
        candidate_id=row.candidate_id,
        nominal_until=row.nominal_until,
        guard_until=row.guard_until,
        proof_before=row.proof_before,
    )


def _worker_and_candidate(
    session: Session, worker_id: int
) -> tuple[Worker, WorkerLeaseCandidateV4]:
    # The caller already owns the Worker/credential locks. Do not fetch any
    # earlier business locks after this suffix.
    worker = session.get(Worker, worker_id)
    candidate = session.scalar(
        select(WorkerLeaseCandidateV4)
        .where(WorkerLeaseCandidateV4.worker_id == worker_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        worker is None
        or candidate is None
        or type(worker.protocol_version) is not int
        or worker.protocol_version != 4
        or worker.identity_id != candidate.worker_identity_id
        or worker.instance_id != candidate.instance_id
        or worker.instance_generation != candidate.instance_generation
        or worker.instance_lease_until != candidate.nominal_until
    ):
        raise LeaseCandidateError("lease candidate unavailable")
    history_generation = session.scalar(
        select(WorkerInstanceHistory.generation).where(
            WorkerInstanceHistory.worker_id == worker_id,
            WorkerInstanceHistory.instance_id == candidate.instance_id,
        )
    )
    if history_generation != candidate.instance_generation:
        raise LeaseCandidateError("lease candidate unavailable")
    return worker, candidate


def _already_committed(session: Session, row: WorkerLeaseCandidateV4) -> None:
    current_xid = _xid(session)
    tuple_xid = session.scalar(
        select(literal_column("worker_lease_candidates_v4.xmin::text::bigint", type_=BigInteger()))
        .select_from(WorkerLeaseCandidateV4)
        .where(WorkerLeaseCandidateV4.worker_id == row.worker_id)
    )
    # xmin is only 32-bit; a wrapped or unknown full-xid epoch fails closed.
    if not _committed_xid_matches(current_xid, row.writer_xid, tuple_xid):
        raise LeaseCandidateError("lease candidate unavailable")


def has_current_committed_lease(session: Session, worker: Worker, *, now: datetime) -> bool:
    """Check the fenced instance while the caller holds its Worker row lock."""
    try:
        _, row = _worker_and_candidate(session, worker.id)
        _already_committed(session, row)
    except LeaseCandidateError:
        return False
    return not row.retired and now < row.nominal_until <= row.guard_until


def create_initial_candidate(
    session: Session, *, worker_id: int, credential: str, instance_id: UUID
) -> CandidateRef:
    """Stage an instance's initial candidate or reread its committed candidate.

    A retry never advances the deadline and cannot recover a later heartbeat
    candidate without its proof. A new generation waits for every possible
    deadline of the prior committed candidate, including a renewal for which
    proof issuance or delivery is unknown. It enters reconciliation, not ready.
    The caller retains the original local start time across retries and must
    issue proof only in a separate session. This does not prove old physical
    work has stopped or authorize the new instance to execute work.
    """
    _instance_args(worker_id, instance_id)
    try:
        worker = worker_credentials.authenticate_runtime_credential(session, worker_id, credential)
        prior = session.scalar(
            select(WorkerInstanceHistory.worker_id)
            .where(WorkerInstanceHistory.worker_id == worker_id)
            .limit(1)
        )
        candidate = session.get(WorkerLeaseCandidateV4, worker_id)
        if worker.instance_id == instance_id and candidate is not None:
            _, candidate = _worker_and_candidate(session, worker_id)
            if candidate.revision != 1 or candidate.retired is not False:
                raise LeaseCandidateError("lease candidate unavailable")
            _already_committed(session, candidate)
            worker_credentials.authenticate_runtime_credential(session, worker_id, credential)
            now = worker_availability.current_time(session)
            worker_credentials._confirm_runtime_credential_at_locked(
                session, worker_id, credential, now
            )
            if now >= min(candidate.nominal_until, candidate.guard_until, candidate.proof_before):
                raise LeaseCandidateError("lease candidate unavailable")
            return _ref(candidate)
        if candidate is None:
            if (
                worker.instance_id is not None
                or worker.instance_generation != 0
                or worker.instance_lease_until is not None
                or prior is not None
            ):
                # Older v4 state without a candidate is unknown; do not turn
                # a missing proof history into an automatically safe takeover.
                raise LeaseCandidateError("lease candidate unavailable")
        else:
            _, candidate = _worker_and_candidate(session, worker_id)
            _already_committed(session, candidate)
            now = worker_availability.current_time(session)
            worker_credentials._confirm_runtime_credential_at_locked(
                session, worker_id, credential, now
            )
            if (
                candidate.instance_id == instance_id
                or candidate.instance_generation >= 2**63 - 1
                or now < max(candidate.nominal_until, candidate.guard_until)
                or session.get(WorkerInstanceHistory, (worker_id, instance_id)) is not None
            ):
                raise LeaseCandidateError("lease candidate unavailable")
        worker = worker_instances.register_instance(
            session,
            worker_id=worker_id,
            credential=credential,
            instance_id=instance_id,
            protocol_version=4,
        )
        if worker.instance_lease_until is None:
            raise LeaseCandidateError("lease candidate unavailable")
        writer_xid = _xid(session)
        if candidate is None:
            row = WorkerLeaseCandidateV4(worker_id=worker_id)
            session.add(row)
        else:
            row = candidate
        row.worker_identity_id = worker.identity_id
        row.instance_id = instance_id
        row.instance_generation = worker.instance_generation
        row.revision = 1
        row.candidate_id = uuid4()
        row.nominal_until = worker.instance_lease_until
        row.guard_until = worker.instance_lease_until
        row.proof_before = worker.instance_lease_until
        row.writer_xid = writer_xid
        row.retired = False
        session.flush()
        worker_instances._confirm_prior_lease(
            session,
            worker_id,
            credential,
            instance_id,
            worker.instance_generation,
            row.proof_before,
            ending=False,
        )
        now = worker_availability.current_time(session)
        worker_credentials._confirm_runtime_credential_at_locked(
            session, worker_id, credential, now
        )
        if now >= row.proof_before:
            raise LeaseCandidateError("lease candidate unavailable")
        return _ref(row)
    except worker_credentials.WorkerCredentialError:
        raise LeaseCandidateError("lease credential unavailable") from None


def _issue_in_session(
    session: Session, *, expected: CandidateRef, credential: str, authority: ProofAuthority
) -> IssuedObservationProof:
    """Observe a committed tuple; caller must release locks before delivery."""
    if type(expected) is not CandidateRef or type(authority) is not ProofAuthority:
        raise LeaseCandidateError("lease candidate unavailable")
    try:
        worker_credentials.authenticate_runtime_credential(session, expected.worker_id, credential)
        _, row = _worker_and_candidate(session, expected.worker_id)
        if _ref(row) != expected or row.retired is not False:
            raise LeaseCandidateError("lease candidate unavailable")
        _already_committed(session, row)
        # Recheck credential expiry after any lock wait and before the DB time
        # observation. This cannot establish cluster or physical-process health.
        worker_credentials.authenticate_runtime_credential(session, expected.worker_id, credential)
        now = worker_availability.current_time(session)
        worker_credentials._confirm_runtime_credential_at_locked(
            session, expected.worker_id, credential, now
        )
        return issue_observation_proof(
            key=authority.key, binding=_binding(row, authority), observed_at=now
        )
    except (worker_credentials.WorkerCredentialError, LeaseProofError):
        raise LeaseCandidateError("lease candidate unavailable") from None


def issue_committed_candidate_proof(
    factory: sessionmaker[Session],
    *,
    expected: CandidateRef,
    credential: str,
    authority: ProofAuthority,
) -> IssuedObservationProof:
    """Use a separate session; never deliver if its lock release is unknown."""
    with factory() as session:
        proof = _issue_in_session(
            session, expected=expected, credential=credential, authority=authority
        )
        session.rollback()
        return proof


def validate_current_proof(
    session: Session,
    *,
    worker_id: int,
    credential: str,
    instance_id: UUID,
    generation: int,
    token: str,
    authority: ProofAuthority,
) -> WorkerLeaseCandidateV4:
    """Validate under retained locks; this alone does not authorize an API."""
    _instance_args(worker_id, instance_id, generation)
    if type(authority) is not ProofAuthority:
        raise LeaseCandidateError("lease candidate unavailable")
    try:
        worker_credentials.authenticate_runtime_credential(session, worker_id, credential)
        _, row = _worker_and_candidate(session, worker_id)
        if (
            row.instance_id != instance_id
            or row.instance_generation != generation
            or row.retired is not False
        ):
            raise LeaseCandidateError("lease candidate unavailable")
        _already_committed(session, row)
        worker_credentials.authenticate_runtime_credential(session, worker_id, credential)
        now = worker_availability.current_time(session)
        worker_credentials._confirm_runtime_credential_at_locked(
            session, worker_id, credential, now
        )
        if not verify_observation_proof(
            token=token, key=authority.key, binding=_binding(row, authority), database_now=now
        ):
            raise LeaseCandidateError("lease candidate unavailable")
        return row
    except worker_credentials.WorkerCredentialError:
        raise LeaseCandidateError("lease credential unavailable") from None


def stage_renewal_candidate(
    session: Session,
    *,
    worker_id: int,
    credential: str,
    instance_id: UUID,
    generation: int,
    token: str,
    authority: ProofAuthority,
) -> CandidateRef:
    """Stage one successor candidate; a later transaction must prove commit."""
    row = validate_current_proof(
        session,
        worker_id=worker_id,
        credential=credential,
        instance_id=instance_id,
        generation=generation,
        token=token,
        authority=authority,
    )
    if row.revision >= 2**63 - 1:
        raise LeaseCandidateError("lease candidate unavailable")
    # Recheck the proof with the actual DB clock immediately before changing
    # the tuple; a delayed commit still requires a separate timely observation.
    now = worker_availability.current_time(session)
    try:
        worker_credentials._confirm_runtime_credential_at_locked(
            session, worker_id, credential, now
        )
    except worker_credentials.WorkerCredentialError:
        raise LeaseCandidateError("lease credential unavailable") from None
    old_binding = _binding(row, authority)
    if not verify_observation_proof(
        token=token, key=authority.key, binding=old_binding, database_now=now
    ):
        raise LeaseCandidateError("lease candidate unavailable")
    old_effective = min(row.nominal_until, row.guard_until)
    nominal = now + timedelta(seconds=INSTANCE_LEASE_SECONDS)
    worker = session.get(Worker, worker_id)
    if worker is None:
        raise LeaseCandidateError("lease candidate unavailable")
    row.revision += 1
    row.candidate_id = uuid4()
    row.proof_before = old_effective
    row.guard_until = row.nominal_until
    row.nominal_until = nominal
    row.writer_xid = _xid(session)
    worker.instance_lease_until = nominal
    worker.last_heartbeat = now
    worker.status = "online"
    session.flush()
    worker_instances._confirm_prior_lease(
        session, worker_id, credential, instance_id, generation, old_effective, ending=False
    )
    after = worker_availability.current_time(session)
    try:
        worker_credentials._confirm_runtime_credential_at_locked(
            session, worker_id, credential, after
        )
    except worker_credentials.WorkerCredentialError:
        raise LeaseCandidateError("lease credential unavailable") from None
    if not verify_observation_proof(
        token=token, key=authority.key, binding=old_binding, database_now=after
    ):
        raise LeaseCandidateError("lease candidate unavailable")
    return _ref(row)


def retire_instance(
    session: Session,
    *,
    worker_id: int,
    credential: str,
    instance_id: UUID,
    generation: int,
    token: str,
    authority: ProofAuthority,
) -> None:
    """Stop this generation from using its proof while retaining its deadlines.

    The caller owns commit. Retirement is not physical cleanup or permission
    for an earlier takeover. Unknown commit leaves the caller without a usable
    proof, and a successor still waits for both stored deadlines.
    """
    row = validate_current_proof(
        session,
        worker_id=worker_id,
        credential=credential,
        instance_id=instance_id,
        generation=generation,
        token=token,
        authority=authority,
    )
    binding = _binding(row, authority)
    worker = session.get(Worker, worker_id)
    if worker is None:
        raise LeaseCandidateError("lease candidate unavailable")
    row.retired = True
    writer_xid = _xid(session)
    row.writer_xid = writer_xid
    worker.status = "offline"
    session.flush()
    # A later BEFORE trigger could rewrite the intended value. Re-read the
    # database tuple while the original Worker/candidate locks are still held.
    session.refresh(worker)
    confirmed_worker, confirmed = _worker_and_candidate(session, worker_id)
    if (
        confirmed.retired is not True
        or confirmed.writer_xid != writer_xid
        or _binding(confirmed, authority) != binding
        or confirmed_worker.status != "offline"
    ):
        raise LeaseCandidateError("lease candidate unavailable")
    now = worker_availability.current_time(session)
    try:
        worker_credentials._confirm_runtime_credential_at_locked(
            session, worker_id, credential, now
        )
    except worker_credentials.WorkerCredentialError:
        raise LeaseCandidateError("lease credential unavailable") from None
    if not verify_observation_proof(
        token=token, key=authority.key, binding=binding, database_now=now
    ):
        raise LeaseCandidateError("lease candidate unavailable")


def observe_expired_candidate(session: Session, *, worker_id: int) -> bool:
    """Observe both candidate bounds, without authorizing takeover or cleanup."""
    if type(worker_id) is not int or not 0 < worker_id < 2**63:
        return False
    try:
        worker_credentials._lock_worker(session, worker_id)
        _, row = _worker_and_candidate(session, worker_id)
        _already_committed(session, row)
        now = worker_availability.current_time(session)
        return now >= max(row.nominal_until, row.guard_until)
    except (worker_credentials.WorkerCredentialError, LeaseCandidateError):
        return False
