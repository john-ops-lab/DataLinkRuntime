"""Digest-only result intents and timely post-commit observation.

The caller must provide a bounded transport and a fenced transaction; these
functions alone do not prove physical Worker stop or authorize a Broker ACK.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import struct
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from sqlalchemy import BigInteger, literal_column, select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.result_intent_mac_v4 import RESULT_INTENT_MAC_CONTEXT, result_body_mac
from dlr.common.result_intent_proof_wire_v4 import (
    RESULT_INTENT_PROOF_CONTEXT,
    RESULT_INTENT_PROOF_STRUCT,
    RESULT_INTENT_PROOF_VERSION,
    parse_untrusted_result_intent_proof,
    verify_result_intent_proof_signature,
)
from dlr.common.worker_protocol_v4 import WORKER_FORCE_STOP_CODE
from dlr.control.models import (
    AttemptLeaseCandidateV4,
    AttemptResultIntentV4,
    ExecutionAttempt,
    Worker,
    WorkerLeaseCandidateV4,
)
from dlr.control.schemas.reliable_runtime import AttemptResultBody
from dlr.control.services import worker_availability, worker_credentials
from dlr.control.services.attempt_lease_candidates_v4 import (
    AttemptLeaseCandidateError,
    _binding,
    _check_instance_at,
    _credential_expiry,
    validate_current_attempt_proof,
)
from dlr.control.services.attempt_lease_proof_v4 import verify_attempt_lease_proof
from dlr.control.services.worker_lease_candidates_v4 import (
    LeaseCandidateError,
    ProofAuthority,
    _committed_xid_matches,
    _xid,
)
from dlr.control.services.worker_lease_proof_v4 import LeaseProofError, _timestamp

_MAC_CONTEXT = RESULT_INTENT_MAC_CONTEXT
_SIGN_CONTEXT = RESULT_INTENT_PROOF_CONTEXT
_STRUCT = RESULT_INTENT_PROOF_STRUCT


class ResultIntentError(Exception):
    """A non-sensitive unavailable or conflicting intent."""


@dataclass(frozen=True)
class ResultIntentContext:
    worker_id: int
    attempt_id: int
    credential: str = field(repr=False)
    instance_id: UUID
    generation: int
    instance_proof: str = field(repr=False)
    attempt_proof: str = field(repr=False)
    action: AttemptResultBody = field(repr=False)
    authority: ProofAuthority = field(repr=False)

    def with_action(self, action: AttemptResultBody) -> ResultIntentContext:
        return replace(self, action=action)


@dataclass(frozen=True)
class ResultIntentRef:
    attempt_id: int
    intent_id: UUID


@dataclass(frozen=True)
class ResultIntentAttestation:
    observed_at: datetime
    signature: bytes = field(repr=False)
    token: str | None = field(default=None, repr=False, compare=False)
    observation_nonce: UUID | None = None


def attestation_from_token(
    token: object,
    authority: ProofAuthority,
    *,
    intent_id: UUID,
    observation_nonce: UUID,
) -> ResultIntentAttestation:
    """Decode one signed observation; ownership and DB state remain unverified."""
    try:
        if (
            type(authority) is not ProofAuthority
            or type(intent_id) is not UUID
            or intent_id.int == 0
            or type(observation_nonce) is not UUID
            or observation_nonce.int == 0
        ):
            raise ValueError
        claims = verify_result_intent_proof_signature(token, authority.public_key)
        if (
            claims is None
            or claims.cluster_id != authority.cluster_id
            or claims.authority_epoch != authority.authority_epoch
            or claims.intent_id != intent_id
            or claims.observation_nonce != observation_nonce
            or type(token) is not str
        ):
            raise ValueError
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        observed_at = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=claims.observed_us)
        return ResultIntentAttestation(observed_at, raw[-64:], token, observation_nonce)
    except (
        AttributeError,
        binascii.Error,
        LeaseProofError,
        OverflowError,
        TypeError,
        UnsupportedAlgorithm,
        ValueError,
    ):
        raise ResultIntentError("result intent unavailable") from None


def _body_mac(context: ResultIntentContext, max_body_bytes: int) -> bytes:
    if type(context) is not ResultIntentContext or type(context.action) is not AttemptResultBody:
        raise ResultIntentError("result intent unavailable")
    try:
        body = {
            name: getattr(context.action, name)
            for name in AttemptResultBody.model_fields
            if name != "claim_token"
        }
        return result_body_mac(body, context.action.claim_token, max_body_bytes)
    except Exception:
        raise ResultIntentError("result intent unavailable") from None


def _snapshot(
    session: Session, context: ResultIntentContext
) -> tuple[AttemptLeaseCandidateV4, WorkerLeaseCandidateV4, datetime | None, datetime]:
    try:
        stop_result = (
            context.action.status == "cancelled"
            and context.action.error_code == WORKER_FORCE_STOP_CODE
        )
        lease = validate_current_attempt_proof(
            session,
            worker_id=context.worker_id,
            attempt_id=context.attempt_id,
            credential=context.credential,
            instance_id=context.instance_id,
            generation=context.generation,
            instance_proof=context.instance_proof,
            attempt_proof=context.attempt_proof,
            action=context.action,
            authority=context.authority,
            _allow_stopped_for_signal=stop_result,
        )
        if stop_result:
            worker = session.get(Worker, context.worker_id)
            attempt = session.get(ExecutionAttempt, context.attempt_id)
            if (
                worker is None
                or attempt is None
                or attempt.status != "running"
                or worker_availability.force_stop_operation_locked(session, worker, attempt=attempt)
                is None
            ):
                raise ResultIntentError("result intent unavailable")
        instance = session.get(WorkerLeaseCandidateV4, context.worker_id)
        if instance is None:
            raise ResultIntentError("result intent unavailable")
        expires = _credential_expiry(session, context.worker_id, context.credential)
        now = worker_availability.current_time(session)
        worker_credentials._confirm_runtime_credential_at_locked(
            session, context.worker_id, context.credential, now
        )
        _check_instance_at(instance, context.authority, context.instance_proof, now)
        if not verify_attempt_lease_proof(
            token=context.attempt_proof,
            key=context.authority.key,
            binding=_binding(lease, context.authority),
            database_now=now,
        ):
            raise ResultIntentError("result intent unavailable")
        return lease, instance, expires, now
    except (
        AttemptLeaseCandidateError,
        LeaseCandidateError,
        worker_credentials.WorkerCredentialError,
        HTTPException,
    ):
        raise ResultIntentError("result intent unavailable") from None


def _matches(
    row: AttemptResultIntentV4,
    context: ResultIntentContext,
    lease: AttemptLeaseCandidateV4,
    instance: WorkerLeaseCandidateV4,
    body_mac: bytes,
) -> bool:
    return bool(
        _same_responsibility(row, context, lease, instance, body_mac)
        and row.attempt_revision == lease.revision
        and row.attempt_candidate_id == lease.candidate_id
        and row.instance_revision == instance.revision
        and row.instance_candidate_id == instance.candidate_id
        and row.proof_before
        <= min(lease.nominal_until, lease.guard_until, instance.nominal_until, instance.guard_until)
    )


def _same_responsibility(
    row: AttemptResultIntentV4,
    context: ResultIntentContext,
    lease: AttemptLeaseCandidateV4,
    instance: WorkerLeaseCandidateV4,
    body_mac: bytes,
) -> bool:
    return bool(
        row.attempt_id == context.attempt_id == lease.attempt_id
        and row.worker_id == context.worker_id == lease.worker_id == instance.worker_id
        and row.worker_identity_id == lease.worker_identity_id == instance.worker_identity_id
        and row.instance_id == context.instance_id == lease.instance_id == instance.instance_id
        and row.instance_generation == context.generation == lease.instance_generation
        and row.instance_generation == instance.instance_generation
        and row.fencing_token == lease.fencing_token == context.action.fencing_token
        and hmac.compare_digest(row.body_mac, body_mac)
    )


def _already_committed(session: Session, row: AttemptResultIntentV4) -> None:
    current = _xid(session)
    tuple_xid = session.scalar(
        select(literal_column("attempt_result_intents_v4.xmin::text::bigint", type_=BigInteger()))
        .select_from(AttemptResultIntentV4)
        .where(AttemptResultIntentV4.attempt_id == row.attempt_id)
    )
    if not _committed_xid_matches(current, row.writer_xid, tuple_xid):
        raise ResultIntentError("result intent unavailable")


def stage_result_intent(
    session: Session, context: ResultIntentContext, *, max_body_bytes: int
) -> ResultIntentRef:
    """Stage or rebind one keyed digest under the existing ownership locks."""
    if type(context) is not ResultIntentContext or type(context.authority) is not ProofAuthority:
        raise ResultIntentError("result intent unavailable")
    lease, instance, expires, _ = _snapshot(session, context)
    body_mac = _body_mac(context, max_body_bytes)
    lease, instance, expires, now = _snapshot(session, context)
    row = session.scalar(
        select(AttemptResultIntentV4)
        .where(AttemptResultIntentV4.attempt_id == context.attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is not None:
        if row.finalization_id is not None:
            raise ResultIntentError("result intent unavailable")
        _already_committed(session, row)
        lease, instance, _, now = _snapshot(session, context)
        if _matches(row, context, lease, instance, body_mac) and now < row.proof_before:
            return ResultIntentRef(row.attempt_id, row.intent_id)
        # A proven renewal may invalidate the original candidate binding.
        # Rebind only the exact same result and responsibility, monotonically;
        # a fresh intent ID makes every old attestation unusable. The update
        # must itself commit and be independently observed before its deadline.
        if (
            not _same_responsibility(row, context, lease, instance, body_mac)
            or lease.revision < row.attempt_revision
            or instance.revision < row.instance_revision
            or (
                lease.revision == row.attempt_revision
                and lease.candidate_id != row.attempt_candidate_id
            )
            or (
                instance.revision == row.instance_revision
                and instance.candidate_id != row.instance_candidate_id
            )
            or (
                lease.revision == row.attempt_revision
                and instance.revision == row.instance_revision
                and now < row.proof_before
            )
        ):
            raise ResultIntentError("result intent unavailable")
        lease, instance, expires, now = _snapshot(session, context)
    else:
        lease, instance, expires, now = _snapshot(session, context)
    proof_before = min(
        lease.nominal_until, lease.guard_until, instance.nominal_until, instance.guard_until
    )
    if expires is not None:
        proof_before = min(proof_before, expires)
    if now >= proof_before:
        raise ResultIntentError("result intent unavailable")
    writer_xid = _xid(session)
    if row is None:
        row = AttemptResultIntentV4(
            attempt_id=context.attempt_id,
            worker_id=context.worker_id,
            worker_identity_id=lease.worker_identity_id,
            instance_id=context.instance_id,
            instance_generation=context.generation,
            fencing_token=context.action.fencing_token,
            body_mac=body_mac,
        )
        session.add(row)
    row.attempt_revision = lease.revision
    row.attempt_candidate_id = lease.candidate_id
    row.instance_revision = instance.revision
    row.instance_candidate_id = instance.candidate_id
    row.intent_id = uuid4()
    row.proof_before = proof_before
    row.writer_xid = writer_xid
    session.flush()
    lease, instance, _, now = _snapshot(session, context)
    if now >= proof_before or not _matches(row, context, lease, instance, body_mac):
        raise ResultIntentError("result intent unavailable")
    return ResultIntentRef(row.attempt_id, row.intent_id)


def _packed(
    row: AttemptResultIntentV4, authority: ProofAuthority, observed_at: datetime, nonce: UUID
) -> bytes:
    observed, previous = _timestamp(observed_at), _timestamp(row.proof_before)
    if (
        observed >= previous
        or type(row.body_mac) is not bytes
        or len(row.body_mac) != 32
        or type(nonce) is not UUID
        or nonce.int == 0
    ):
        raise ResultIntentError("result intent unavailable")
    try:
        return _STRUCT.pack(
            RESULT_INTENT_PROOF_VERSION,
            authority.cluster_id.bytes,
            authority.authority_epoch.bytes,
            row.worker_id,
            row.worker_identity_id.bytes,
            row.instance_id.bytes,
            row.instance_generation,
            row.attempt_id,
            row.fencing_token,
            row.attempt_revision,
            row.attempt_candidate_id.bytes,
            row.instance_revision,
            row.instance_candidate_id.bytes,
            row.intent_id.bytes,
            nonce.bytes,
            row.body_mac,
            previous,
            observed,
        )
    except (AttributeError, OverflowError, struct.error):
        raise ResultIntentError("result intent unavailable") from None


def observe_result_in_session(
    session: Session,
    context: ResultIntentContext,
    ref: ResultIntentRef,
    *,
    max_body_bytes: int,
    observation_nonce: UUID,
) -> ResultIntentAttestation:
    """Sign only an already committed matching intent observed before its guard."""
    if (
        type(ref) is not ResultIntentRef
        or ref.attempt_id != context.attempt_id
        or type(observation_nonce) is not UUID
        or observation_nonce.int == 0
    ):
        raise ResultIntentError("result intent unavailable")
    lease, instance, _, _ = _snapshot(session, context)
    row = session.scalar(
        select(AttemptResultIntentV4)
        .where(AttemptResultIntentV4.attempt_id == ref.attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    body_mac = _body_mac(context, max_body_bytes)
    if row is None or row.intent_id != ref.intent_id or row.finalization_id is not None:
        raise ResultIntentError("result intent unavailable")
    _already_committed(session, row)
    lease, instance, _, now = _snapshot(session, context)
    if now >= row.proof_before or not _matches(row, context, lease, instance, body_mac):
        raise ResultIntentError("result intent unavailable")
    try:
        payload = _packed(row, context.authority, now, observation_nonce)
        signature = Ed25519PrivateKey.from_private_bytes(context.authority.key).sign(
            _SIGN_CONTEXT + payload
        )
        token = base64.urlsafe_b64encode(payload + signature).decode("ascii")
        if parse_untrusted_result_intent_proof(token) is None:
            raise ResultIntentError("result intent unavailable")
        return ResultIntentAttestation(now, signature, token, observation_nonce)
    except (LeaseProofError, UnsupportedAlgorithm, ValueError):
        raise ResultIntentError("result intent unavailable") from None


def observe_result_intent(
    factory: sessionmaker[Session],
    context: ResultIntentContext,
    ref: ResultIntentRef,
    *,
    max_body_bytes: int,
    observation_nonce: UUID,
) -> ResultIntentAttestation:
    with factory() as session:
        attestation = observe_result_in_session(
            session,
            context,
            ref,
            max_body_bytes=max_body_bytes,
            observation_nonce=observation_nonce,
        )
        session.rollback()
        return attestation


def verify_result_attestation(
    row: AttemptResultIntentV4,
    authority: ProofAuthority,
    attestation: ResultIntentAttestation,
    *,
    now: datetime,
) -> bool:
    """Verify only the certificate, never current ownership or the result body.

    A prospective finalizer must also lock and revalidate the credentials,
    both lease candidates, and the matching result body before acting.
    """
    if (
        type(row) is not AttemptResultIntentV4
        or type(authority) is not ProofAuthority
        or type(attestation) is not ResultIntentAttestation
        or type(attestation.signature) is not bytes
        or len(attestation.signature) != 64
        or type(attestation.observation_nonce) is not UUID
        or attestation.observation_nonce.int == 0
    ):
        return False
    try:
        if (
            not _timestamp(attestation.observed_at)
            <= _timestamp(now)
            < _timestamp(row.proof_before)
        ):
            return False
        payload = _packed(row, authority, attestation.observed_at, attestation.observation_nonce)
        if attestation.token is not None:
            if parse_untrusted_result_intent_proof(attestation.token) is None:
                return False
            raw = base64.urlsafe_b64decode(attestation.token.encode("ascii"))
            if not hmac.compare_digest(raw, payload + attestation.signature):
                return False
        Ed25519PrivateKey.from_private_bytes(authority.key).public_key().verify(
            attestation.signature, _SIGN_CONTEXT + payload
        )
        return True
    except (
        binascii.Error,
        InvalidSignature,
        LeaseProofError,
        ResultIntentError,
        UnsupportedAlgorithm,
        ValueError,
    ):
        return False
