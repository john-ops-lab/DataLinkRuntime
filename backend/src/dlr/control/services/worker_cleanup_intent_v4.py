"""Commit cleanup settlement and observe it under the Control and Worker fences.

The authenticated Worker must establish physical cleanup before calling this
service. This module does not itself prove remote physical cleanup.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import BigInteger, event, literal_column, select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.cleanup_intent_proof_v4 import (
    CONTEXT,
    VERSION,
    WIRE,
    verify_cleanup_intent_signature,
)
from dlr.common.cleanup_receipt_proof_v4 import CONTEXT as RECEIPT_CONTEXT
from dlr.common.cleanup_receipt_proof_v4 import VERSION as RECEIPT_VERSION
from dlr.common.cleanup_receipt_proof_v4 import WIRE as RECEIPT_WIRE
from dlr.control.models import AdapterExecutionSlot, WorkerCleanupIntentV4, WorkerLeaseCandidateV4
from dlr.control.services import (
    execution as execution_service,
)
from dlr.control.services import (
    worker_availability,
    worker_cleanup_v4,
    worker_credentials,
)
from dlr.control.services.attempt import V4AttemptAuthority
from dlr.control.services.attempt_lease_candidates_v4 import _check_instance_at, _credential_expiry
from dlr.control.services.attempt_result_finalize_v4 import (
    ResultControlFence,
    _control_authority,
)
from dlr.control.services.execution_cancellation import (
    lock_execution_attempts,
    lock_execution_in_admission_order,
)
from dlr.control.services.worker_cleanup_v4 import V4CleanupReceipt
from dlr.control.services.worker_lease_candidates_v4 import (
    ProofAuthority,
    _committed_xid_matches,
    _xid,
    validate_current_proof,
)
from dlr.control.services.worker_lease_proof_v4 import _timestamp


class CleanupIntentUnavailable(Exception):
    """A generic rejection; unknown database outcomes must remain pending."""


class CleanupReceiptUnknown(Exception):
    """The terminal cleanup transaction was not independently observed."""


@dataclass(frozen=True)
class CleanupIntentContext:
    worker_id: int
    execution_id: int
    credential: str = field(repr=False)
    instance_id: UUID
    generation: int
    instance_proof: str = field(repr=False)
    receipt: V4CleanupReceipt = field(repr=False)
    cleanup_token: str = field(repr=False)
    authority: ProofAuthority = field(repr=False)


def _frozen(context: CleanupIntentContext) -> CleanupIntentContext:
    if (
        type(context) is not CleanupIntentContext
        or type(context.worker_id) is not int
        or not 0 < context.worker_id < 2**63
        or type(context.execution_id) is not int
        or not 0 < context.execution_id < 2**63
        or type(context.instance_id) is not UUID
        or context.instance_id.int == 0
        or type(context.generation) is not int
        or not 0 < context.generation < 2**63
        or type(context.receipt) is not V4CleanupReceipt
        or type(context.cleanup_token) is not str
        or not 1 <= len(context.cleanup_token) <= 512
        or type(context.credential) is not str
        or type(context.instance_proof) is not str
        or type(context.authority) is not ProofAuthority
        or context.receipt.worker_identity_id.int == 0
        or context.receipt.original_instance_id.int == 0
    ):
        raise CleanupIntentUnavailable("cleanup intent unavailable")
    return replace(context, receipt=V4CleanupReceipt.model_validate(context.receipt.model_dump()))


def _snapshot(
    session: Session, context: CleanupIntentContext
) -> tuple[WorkerLeaseCandidateV4, datetime, datetime]:
    authority = V4AttemptAuthority(context.credential, context.instance_id, context.generation)
    worker_cleanup_v4._lock_receipt(
        session,
        context.execution_id,
        context.worker_id,
        authority,
        context.receipt,
        context.cleanup_token,
    )
    candidate = validate_current_proof(
        session,
        worker_id=context.worker_id,
        credential=context.credential,
        instance_id=context.instance_id,
        generation=context.generation,
        token=context.instance_proof,
        authority=context.authority,
    )
    credential_expiry = _credential_expiry(session, context.worker_id, context.credential)
    now = worker_availability.current_time(session)
    worker_credentials._confirm_runtime_credential_at_locked(
        session, context.worker_id, context.credential, now
    )
    _check_instance_at(candidate, context.authority, context.instance_proof, now)
    before = min(candidate.nominal_until, candidate.guard_until)
    if credential_expiry is not None:
        before = min(before, credential_expiry)
    if now >= before:
        raise CleanupIntentUnavailable("cleanup intent unavailable")
    return candidate, before, now


def _matches(
    row: WorkerCleanupIntentV4,
    context: CleanupIntentContext,
    candidate: WorkerLeaseCandidateV4,
    before: datetime,
    intent_id: UUID,
) -> bool:
    receipt = context.receipt
    return bool(
        row.attempt_id == receipt.attempt_id
        and row.worker_id == context.worker_id == candidate.worker_id
        and row.worker_identity_id == receipt.worker_identity_id == candidate.worker_identity_id
        and row.original_instance_id == receipt.original_instance_id
        and row.original_generation == receipt.original_instance_generation
        and row.fencing_token == receipt.fencing_token
        and row.instance_id == context.instance_id == candidate.instance_id
        and row.instance_generation == context.generation == candidate.instance_generation
        and row.instance_revision == candidate.revision
        and row.instance_candidate_id == candidate.candidate_id
        and row.intent_id == intent_id
        and row.proof_before <= before
    )


def _matches_after_heartbeat(
    row: WorkerCleanupIntentV4,
    context: CleanupIntentContext,
    candidate: WorkerLeaseCandidateV4,
    before: datetime,
    intent_id: UUID,
) -> bool:
    receipt = context.receipt
    return bool(
        row.attempt_id == receipt.attempt_id
        and row.worker_id == context.worker_id == candidate.worker_id
        and row.worker_identity_id == receipt.worker_identity_id == candidate.worker_identity_id
        and row.original_instance_id == receipt.original_instance_id
        and row.original_generation == receipt.original_instance_generation
        and row.fencing_token == receipt.fencing_token
        and row.instance_id == context.instance_id == candidate.instance_id
        and row.instance_generation == context.generation == candidate.instance_generation
        and candidate.revision >= row.instance_revision
        and (
            candidate.revision > row.instance_revision
            or candidate.candidate_id == row.instance_candidate_id
        )
        and row.intent_id == intent_id
        and row.proof_before <= before
    )


def _already_committed(session: Session, row: WorkerCleanupIntentV4) -> None:
    current_xid = _xid(session)
    tuple_xid = session.scalar(
        select(literal_column("worker_cleanup_intents_v4.xmin::text::bigint", type_=BigInteger()))
        .select_from(WorkerCleanupIntentV4)
        .where(WorkerCleanupIntentV4.attempt_id == row.attempt_id)
    )
    if not _committed_xid_matches(current_xid, row.writer_xid, tuple_xid):
        raise CleanupIntentUnavailable("cleanup intent unavailable")


def _require_attestation(
    row: WorkerCleanupIntentV4,
    context: CleanupIntentContext,
    candidate: WorkerLeaseCandidateV4,
    before: datetime,
    now: datetime,
    *,
    intent_id: UUID,
    observation_nonce: UUID,
    token: object,
) -> None:
    claims = verify_cleanup_intent_signature(token, context.authority.public_key)
    receipt = context.receipt
    if (
        type(intent_id) is not UUID
        or intent_id.int == 0
        or type(observation_nonce) is not UUID
        or observation_nonce.int == 0
        or claims is None
        or not _matches_after_heartbeat(row, context, candidate, before, intent_id)
        or claims.cluster_id != context.authority.cluster_id
        or claims.authority_epoch != context.authority.authority_epoch
        or claims.worker_id != context.worker_id
        or claims.worker_identity_id != receipt.worker_identity_id
        or claims.execution_id != context.execution_id
        or claims.attempt_id != receipt.attempt_id
        or claims.original_instance_id != receipt.original_instance_id
        or claims.original_generation != receipt.original_instance_generation
        or claims.instance_id != context.instance_id
        or claims.generation != context.generation
        or claims.fencing_token != receipt.fencing_token
        or claims.instance_revision != row.instance_revision
        or claims.instance_candidate_id != row.instance_candidate_id
        or claims.intent_id != intent_id
        or claims.observation_nonce != observation_nonce
        or claims.proof_before_us != _timestamp(row.proof_before)
        or claims.observed_us > _timestamp(now)
        or now >= row.proof_before
    ):
        raise CleanupIntentUnavailable("cleanup intent unavailable")


def _authorized_intent(
    session: Session,
    context: CleanupIntentContext,
    *,
    intent_id: UUID,
    observation_nonce: UUID,
    token: object,
) -> WorkerCleanupIntentV4:
    _snapshot(session, context)
    row = session.scalar(
        select(WorkerCleanupIntentV4)
        .where(WorkerCleanupIntentV4.attempt_id == context.receipt.attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise CleanupIntentUnavailable("cleanup intent unavailable")
    _already_committed(session, row)
    candidate, before, now = _snapshot(session, context)
    _require_attestation(
        row,
        context,
        candidate,
        before,
        now,
        intent_id=intent_id,
        observation_nonce=observation_nonce,
        token=token,
    )
    return row


def stage_cleanup_intent(
    session: Session, context: CleanupIntentContext, *, intent_id: UUID
) -> None:
    """Stage one intent under existing locks; the caller owns commit or rollback."""
    try:
        frozen = _frozen(context)
        if type(intent_id) is not UUID or intent_id.int == 0:
            raise ValueError
        candidate, before, _ = _snapshot(session, frozen)
        row = session.scalar(
            select(WorkerCleanupIntentV4)
            .where(WorkerCleanupIntentV4.attempt_id == frozen.receipt.attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is not None:
            if row.intent_id != intent_id or not _matches(
                row, frozen, candidate, before, intent_id
            ):
                raise ValueError
            _snapshot(session, frozen)
            return
        writer_xid = _xid(session)
        if row is None:
            row = WorkerCleanupIntentV4(attempt_id=frozen.receipt.attempt_id)
            session.add(row)
        row.worker_id = frozen.worker_id
        row.worker_identity_id = frozen.receipt.worker_identity_id
        row.original_instance_id = frozen.receipt.original_instance_id
        row.original_generation = frozen.receipt.original_instance_generation
        row.fencing_token = frozen.receipt.fencing_token
        row.instance_id = frozen.instance_id
        row.instance_generation = frozen.generation
        row.instance_revision = candidate.revision
        row.instance_candidate_id = candidate.candidate_id
        row.intent_id = intent_id
        row.proof_before = before
        row.writer_xid = writer_xid
        row.completion_id = None
        row.completion_xid = None
        session.flush()
        checked, checked_before, now = _snapshot(session, frozen)
        if not _matches(row, frozen, checked, checked_before, intent_id) or now >= row.proof_before:
            raise ValueError
    except Exception:
        raise CleanupIntentUnavailable("cleanup intent unavailable") from None


def observe_cleanup_intent(
    factory: sessionmaker[Session],
    context: CleanupIntentContext,
    *,
    intent_id: UUID,
    observation_nonce: UUID,
    control: ResultControlFence,
) -> str:
    """Sign a timely separate-transaction observation; this never completes cleanup."""
    try:
        frozen = _frozen(context)
        if (
            type(intent_id) is not UUID
            or intent_id.int == 0
            or type(observation_nonce) is not UUID
            or observation_nonce.int == 0
        ):
            raise ValueError
        with factory() as session:
            gate, authority = _control_authority(session, control)
            frozen = replace(frozen, authority=authority)
            _snapshot(session, frozen)
            row = session.scalar(
                select(WorkerCleanupIntentV4)
                .where(WorkerCleanupIntentV4.attempt_id == frozen.receipt.attempt_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None:
                raise ValueError
            _already_committed(session, row)
            candidate, before, now = _snapshot(session, frozen)
            if (
                not _matches_after_heartbeat(row, frozen, candidate, before, intent_id)
                or now >= row.proof_before
            ):
                raise ValueError
            payload = WIRE.pack(
                VERSION,
                frozen.authority.cluster_id.bytes,
                frozen.authority.authority_epoch.bytes,
                frozen.worker_id,
                frozen.receipt.worker_identity_id.bytes,
                frozen.execution_id,
                frozen.receipt.attempt_id,
                frozen.receipt.original_instance_id.bytes,
                frozen.receipt.original_instance_generation,
                frozen.instance_id.bytes,
                frozen.generation,
                frozen.receipt.fencing_token,
                row.instance_revision,
                row.instance_candidate_id.bytes,
                intent_id.bytes,
                observation_nonce.bytes,
                _timestamp(row.proof_before),
                _timestamp(now),
            )
            signature = Ed25519PrivateKey.from_private_bytes(frozen.authority.key).sign(
                CONTEXT + payload
            )
            token = base64.urlsafe_b64encode(payload + signature).decode("ascii")
            _require_attestation(
                row,
                frozen,
                candidate,
                before,
                now,
                intent_id=intent_id,
                observation_nonce=observation_nonce,
                token=token,
            )
            gate.check()
            session.rollback()
            return token
    except Exception:
        raise CleanupIntentUnavailable("cleanup intent unavailable") from None


def stage_fenced_cleanup_intent(
    factory: sessionmaker[Session],
    context: CleanupIntentContext,
    *,
    intent_id: UUID,
    control: ResultControlFence,
) -> None:
    """Stage with a Control fence; commit success alone is never a receipt."""
    try:
        with factory() as session:
            gate, authority = _control_authority(session, control)
            event.listen(session, "before_commit", lambda _session: gate.check())
            stage_cleanup_intent(
                session, replace(context, authority=authority), intent_id=intent_id
            )
            session.commit()
    except Exception:
        raise CleanupIntentUnavailable("cleanup intent unavailable") from None


def finalize_cleanup_intent(
    factory: sessionmaker[Session],
    context: CleanupIntentContext,
    *,
    intent_id: UUID,
    intent_nonce: UUID,
    intent_token: object,
    control: ResultControlFence,
) -> None:
    """Commit cleanup and resource settlement together; the outcome is unconfirmed."""
    try:
        frozen = _frozen(context)
        with factory() as session:
            gate, authority = _control_authority(session, control)
            frozen = replace(frozen, authority=authority)
            event.listen(session, "before_commit", lambda _session: gate.check())
            # Match Claim's admission -> Execution -> Attempt -> Slot -> Worker
            # lock order before the intent helper acquires the Worker lock.
            locked_execution = lock_execution_in_admission_order(session, frozen.execution_id)
            if locked_execution is None:
                raise ValueError
            lock_execution_attempts(session, frozen.execution_id)
            slot = session.get(
                AdapterExecutionSlot,
                (locked_execution.adapter_id, 0),
                with_for_update=True,
                populate_existing=True,
            )
            if slot is None:
                raise ValueError
            row = _authorized_intent(
                session,
                frozen,
                intent_id=intent_id,
                observation_nonce=intent_nonce,
                token=intent_token,
            )
            if row.completion_id is not None:
                if row.completion_xid != row.writer_xid:
                    raise ValueError
                session.rollback()
                raise CleanupReceiptUnknown("cleanup receipt unconfirmed")
            xid = _xid(session)
            execution, attempt = worker_cleanup_v4._lock_receipt(
                session,
                frozen.execution_id,
                frozen.worker_id,
                V4AttemptAuthority(frozen.credential, frozen.instance_id, frozen.generation),
                frozen.receipt,
                frozen.cleanup_token,
            )
            worker_cleanup_v4._mark_cleanup_completed(execution, attempt)
            if slot.active_attempt_id == attempt.id:
                if slot.fencing_token != attempt.fencing_token:
                    raise ValueError
                slot.active_attempt_id = None
                slot.lease_expires_at = None
            _, _, now = _snapshot(session, frozen)
            if attempt.cleanup_settled_at is None:
                attempt.cleanup_settled_at = now
            row.completion_id = uuid4()
            row.completion_xid = row.writer_xid = xid
            session.flush()
            if execution.status in execution_service.TERMINAL_STATUSES:
                execution_service.release_execution_leases(session, execution.id)
            candidate, before, now = _snapshot(session, frozen)
            _require_attestation(
                row,
                frozen,
                candidate,
                before,
                now,
                intent_id=intent_id,
                observation_nonce=intent_nonce,
                token=intent_token,
            )
            session.commit()
    except Exception:
        raise CleanupReceiptUnknown("cleanup receipt unavailable") from None
    raise CleanupReceiptUnknown("cleanup receipt unconfirmed")


def observe_committed_cleanup(
    factory: sessionmaker[Session],
    context: CleanupIntentContext,
    *,
    intent_id: UUID,
    intent_nonce: UUID,
    intent_token: str,
    receipt_nonce: UUID,
    control: ResultControlFence,
) -> str:
    """Sign only after separately observing the committed cleanup settlement."""
    try:
        frozen = _frozen(context)
        if type(receipt_nonce) is not UUID or receipt_nonce.int == 0:
            raise ValueError
        with factory() as session:
            gate, authority = _control_authority(session, control)
            frozen = replace(frozen, authority=authority)
            # Match Claim's admission -> Execution -> Attempt -> Slot ->
            # Worker order. The normal receipt helper starts at Execution and
            # locks Worker; taking Slot after it would invert that order.
            locked_execution = lock_execution_in_admission_order(session, frozen.execution_id)
            if locked_execution is None:
                raise ValueError
            lock_execution_attempts(session, frozen.execution_id)
            slot = session.get(
                AdapterExecutionSlot,
                (locked_execution.adapter_id, 0),
                with_for_update=True,
                populate_existing=True,
            )
            if slot is None:
                raise ValueError
            row = _authorized_intent(
                session,
                frozen,
                intent_id=intent_id,
                observation_nonce=intent_nonce,
                token=intent_token,
            )
            execution, attempt = worker_cleanup_v4._lock_receipt(
                session,
                frozen.execution_id,
                frozen.worker_id,
                V4AttemptAuthority(frozen.credential, frozen.instance_id, frozen.generation),
                frozen.receipt,
                frozen.cleanup_token,
            )
            candidate, before, now = _snapshot(session, frozen)
            _require_attestation(
                row,
                frozen,
                candidate,
                before,
                now,
                intent_id=intent_id,
                observation_nonce=intent_nonce,
                token=intent_token,
            )
            if (
                type(row.completion_id) is not UUID
                or row.completion_id.int == 0
                or row.completion_xid != row.writer_xid
                or attempt.cleanup_settled_at is None
                or slot.active_attempt_id == attempt.id
                or not isinstance(attempt.cleanup_summary, dict)
                or attempt.cleanup_summary.get("workspace_cleanup_status") != "completed"
                or (
                    attempt.attempt_no == execution.attempt_count
                    and execution.workspace_cleanup_status != "completed"
                )
            ):
                raise ValueError
            payload = RECEIPT_WIRE.pack(
                RECEIPT_VERSION,
                hashlib.sha256(intent_token.encode("ascii")).digest(),
                row.completion_id.bytes,
                receipt_nonce.bytes,
                _timestamp(now),
            )
            signature = Ed25519PrivateKey.from_private_bytes(authority.key).sign(
                RECEIPT_CONTEXT + payload
            )
            token = base64.urlsafe_b64encode(payload + signature).decode("ascii")
            candidate, before, checked_now = _snapshot(session, frozen)
            _require_attestation(
                row,
                frozen,
                candidate,
                before,
                checked_now,
                intent_id=intent_id,
                observation_nonce=intent_nonce,
                token=intent_token,
            )
            gate.check()
            session.rollback()
            return token
    except Exception:
        raise CleanupReceiptUnknown("cleanup receipt unavailable") from None
