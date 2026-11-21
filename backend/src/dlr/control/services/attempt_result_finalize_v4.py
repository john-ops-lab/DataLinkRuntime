"""Result finalization and separate committed observation for a v4 Worker.

This transaction logic does not prove physical stop or bound HTTP parsing.
Only a bounded request path may call it, after the trusted Worker has verified
its own process and local I/O have stopped.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from sqlalchemy import BigInteger, event, literal_column, select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.result_receipt_proof_v4 import (
    ATTEMPT_STATUS_CODES,
    EXECUTION_STATUS_CODES,
    RESULT_RECEIPT_CONTEXT,
    RESULT_RECEIPT_STRUCT,
    RESULT_RECEIPT_VERSION,
)
from dlr.control.models import (
    AttemptLeaseCandidateV4,
    AttemptResultIntentV4,
    Execution,
    ExecutionAttempt,
    WorkerLeaseCandidateV4,
)
from dlr.control.schemas.reliable_runtime import AttemptResultBody
from dlr.control.services import (
    attempt,
    worker_availability,
    worker_candidate_storage_preflight_v4,
    worker_credentials,
    worker_proof_authority_v4,
    worker_token_fence_v4,
)
from dlr.control.services.attempt_lease_candidates_v4 import (
    AttemptLeaseCandidateError,
    _binding,
    _check_instance_at,
)
from dlr.control.services.attempt_lease_candidates_v4 import (
    _already_committed as _attempt_already_committed,
)
from dlr.control.services.attempt_lease_proof_v4 import verify_attempt_lease_proof
from dlr.control.services.attempt_result_intent_v4 import (
    ResultIntentAttestation,
    ResultIntentContext,
    ResultIntentError,
    ResultIntentRef,
    _already_committed,
    _body_mac,
    _IntentChecks,
    _matches,
    _same_responsibility,
    attestation_from_token,
    verify_result_attestation,
)
from dlr.control.services.control_declaration_v4 import DeploymentDeclaration
from dlr.control.services.worker_attempt_identity import validate_v4_attempt_locked
from dlr.control.services.worker_lease_candidates_v4 import (
    ProofAuthority,
    _xid,
    validate_current_proof,
)
from dlr.control.services.worker_lease_proof_v4 import _timestamp


class ResultFinalizeRejected(Exception):
    """The uncommitted mutation was rejected and rollback was confirmed."""


class ResultFinalizeUnknown(Exception):
    """No separately confirmed result receipt was obtained."""


class ResultReceiptUnavailable(Exception):
    """The committed result receipt could not be safely confirmed."""


@dataclass(frozen=True)
class ResultReceipt:
    attempt_id: int
    intent_id: UUID
    finalization_id: UUID
    attempt_status: str
    execution_status: str
    token: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ResultControlFence:
    """Configuration for acquiring a fresh Control fence in each transaction."""

    deployment: DeploymentDeclaration = field(repr=False)
    instance_id: UUID
    instance_token: str = field(repr=False)
    local_check: Callable[[], None] = field(repr=False, compare=False)


@dataclass(frozen=True)
class _CheckedFence:
    gate: worker_token_fence_v4.Fence
    local_check: Callable[[], None] = field(repr=False, compare=False)

    def check(self) -> None:
        self.local_check()
        self.gate.check()
        self.local_check()


def _control_authority(
    session: Session, control: ResultControlFence
) -> tuple[_CheckedFence, ProofAuthority]:
    if type(control) is not ResultControlFence or not callable(control.local_check):
        raise ValueError("result unavailable")
    control.local_check()
    gate = worker_token_fence_v4.acquire(
        session,
        control.deployment.authority,
        instance_id=control.instance_id,
        instance_token=control.instance_token,
        mode="operation",
    )
    if (
        worker_candidate_storage_preflight_v4.inspect_candidate_storage(
            session, expected_schema="public"
        ).state
        != "observed"
    ):
        raise ValueError("result unavailable")
    authority = worker_proof_authority_v4.for_transaction(session, control.deployment)
    checked = _CheckedFence(gate, control.local_check)
    checked.check()
    return checked, authority


def _freeze_action(context: ResultIntentContext, max_body_bytes: int) -> AttemptResultBody:
    """Check the budget before copying using the existing Python JSON semantics."""
    _body_mac(context, max_body_bytes)
    body = {name: getattr(context.action, name) for name in AttemptResultBody.model_fields}
    frozen = json.loads(
        json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    )
    return AttemptResultBody.model_validate(frozen)


def _sign_receipt(
    receipt: ResultReceipt,
    attestation: ResultIntentAttestation,
    authority: ProofAuthority,
    *,
    observation_nonce: UUID,
    observed_at: datetime,
) -> str:
    if (
        type(observation_nonce) is not UUID
        or observation_nonce.int == 0
        or type(attestation.token) is not str
        or receipt.attempt_status not in ATTEMPT_STATUS_CODES
        or receipt.execution_status not in EXECUTION_STATUS_CODES
    ):
        raise ValueError("result receipt unavailable")
    payload = RESULT_RECEIPT_STRUCT.pack(
        RESULT_RECEIPT_VERSION,
        hashlib.sha256(attestation.token.encode("ascii")).digest(),
        receipt.finalization_id.bytes,
        observation_nonce.bytes,
        ATTEMPT_STATUS_CODES[receipt.attempt_status],
        EXECUTION_STATUS_CODES[receipt.execution_status],
        _timestamp(observed_at),
    )
    signature = Ed25519PrivateKey.from_private_bytes(authority.key).sign(
        RESULT_RECEIPT_CONTEXT + payload
    )
    return base64.urlsafe_b64encode(payload + signature).decode("ascii")


def _receipt_matches_after_heartbeat(
    row: AttemptResultIntentV4,
    context: ResultIntentContext,
    lease: AttemptLeaseCandidateV4,
    instance: WorkerLeaseCandidateV4,
    body_mac: bytes,
) -> bool:
    """Allow a current proof for the same generation without extending the intent."""
    return bool(
        _same_responsibility(row, context, lease, instance, body_mac)
        and row.attempt_revision == lease.revision
        and row.attempt_candidate_id == lease.candidate_id
        and instance.revision >= row.instance_revision
        and (
            instance.revision > row.instance_revision
            or instance.candidate_id == row.instance_candidate_id
        )
        and row.proof_before <= min(lease.nominal_until, lease.guard_until)
    )


def finalize_unconfirmed_result(
    factory: sessionmaker[Session],
    context: ResultIntentContext,
    ref: ResultIntentRef,
    attestation: ResultIntentAttestation,
    *,
    max_body_bytes: int,
    _control: ResultControlFence | None = None,
) -> None:
    """Finalize a timely-bound result; the commit still needs separate observation."""
    try:
        if (
            type(context) is not ResultIntentContext
            or type(context.action) is not AttemptResultBody
            or type(_control) is not ResultControlFence
        ):
            raise ResultIntentError("result unavailable")
        # The private copy prevents concurrent mutation; it is not a complete
        # production HTTP, parser or process-memory gate.
        action = _freeze_action(context, max_body_bytes)
        frozen = replace(context, action=action)
    except Exception:
        raise ResultFinalizeRejected("result unavailable") from None

    try:
        with factory() as session:
            gate = None
            if _control is not None:
                gate, authority = _control_authority(session, _control)
                frozen = replace(frozen, authority=authority)
            commit_started = False
            locked: (
                tuple[AttemptResultIntentV4, AttemptLeaseCandidateV4, WorkerLeaseCandidateV4] | None
            ) = None

            def on_commit(_session: Session) -> None:
                nonlocal commit_started
                commit_started = True
                if gate is not None:
                    gate.check()

            event.listen(session, "before_commit", on_commit)
            checks: _IntentChecks | None = None

            def check() -> None:
                nonlocal locked, checks
                if type(ref) is not ResultIntentRef or ref.attempt_id != frozen.attempt_id:
                    raise ResultIntentError("result unavailable")
                if checks is None:
                    checks = _IntentChecks(session, frozen)
                else:
                    checks.recheck()
                row = session.scalar(
                    select(AttemptResultIntentV4)
                    .where(AttemptResultIntentV4.attempt_id == frozen.attempt_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                body_mac = _body_mac(frozen, max_body_bytes)
                if row is None or row.intent_id != ref.intent_id:
                    raise ResultIntentError("result unavailable")
                _already_committed(session, row)
                lease, instance, _, now = checks.recheck()
                if not _matches(row, frozen, lease, instance, body_mac) or not (
                    verify_result_attestation(row, frozen.authority, attestation, now=now)
                ):
                    raise ResultIntentError("result unavailable")
                locked = row, lease, instance

            def check_after_terminal() -> None:
                if locked is None:
                    raise ResultIntentError("result unavailable")
                row, lease, instance = locked
                if row.finalization_id is not None:
                    raise ResultIntentError("result unavailable")
                terminal = session.get(ExecutionAttempt, frozen.attempt_id)
                execution = (
                    session.get(Execution, terminal.execution_id) if terminal is not None else None
                )
                if (
                    terminal is None
                    or execution is None
                    or terminal.status not in attempt.TERMINAL_ATTEMPT_STATUSES
                    or execution.status
                    not in {"succeeded", "dead_letter", "cancelled", "retry_wait"}
                ):
                    raise ResultIntentError("result unavailable")
                finalization_xid = _xid(session)
                row.finalization_id = uuid4()
                row.finalization_xid = finalization_xid
                row.writer_xid = finalization_xid
                row.final_attempt_status = terminal.status
                row.final_execution_status = execution.status
                session.flush()
                body_mac = _body_mac(frozen, max_body_bytes)
                now = worker_availability.current_time(session)
                try:
                    worker_credentials._confirm_runtime_credential_at_locked(
                        session, frozen.worker_id, frozen.credential, now
                    )
                    _check_instance_at(instance, frozen.authority, frozen.instance_proof, now)
                    if (
                        not _matches(row, frozen, lease, instance, body_mac)
                        or not verify_attempt_lease_proof(
                            token=frozen.attempt_proof,
                            key=frozen.authority.key,
                            binding=_binding(lease, frozen.authority),
                            database_now=now,
                        )
                        or not verify_result_attestation(
                            row, frozen.authority, attestation, now=now
                        )
                    ):
                        raise ResultIntentError("result unavailable")
                except (worker_credentials.WorkerCredentialError, AttemptLeaseCandidateError):
                    raise ResultIntentError("result unavailable") from None

            try:
                check()
                attempt.finish_attempt(
                    session,
                    frozen.worker_id,
                    frozen.attempt_id,
                    frozen.action,
                    authority=attempt.V4AttemptAuthority(
                        frozen.credential, frozen.instance_id, frozen.generation
                    ),
                    _proof_check=check,
                    _proof_after_terminal=check_after_terminal,
                )
            except (ResultIntentError, HTTPException):
                if commit_started:
                    with suppress(Exception):
                        session.rollback()
                    raise ResultFinalizeUnknown("result unavailable") from None
                try:
                    session.rollback()
                except Exception:
                    raise ResultFinalizeUnknown("result unavailable") from None
                raise ResultFinalizeRejected("result unavailable") from None
            except Exception:
                with suppress(Exception):
                    session.rollback()
                raise ResultFinalizeUnknown("result unavailable") from None
    except (ResultFinalizeRejected, ResultFinalizeUnknown):
        raise
    except Exception:
        raise ResultFinalizeUnknown("result unavailable") from None
    # A committed terminal transition is not a confirmed Worker receipt. A
    # future observer must prove the exact committed terminal identity and all
    # remaining authorization conditions before this can become an ACK.
    raise ResultFinalizeUnknown("result receipt unconfirmed")


def observe_committed_result(
    factory: sessionmaker[Session],
    context: ResultIntentContext,
    ref: ResultIntentRef,
    attestation: ResultIntentAttestation,
    *,
    max_body_bytes: int,
    _control: ResultControlFence | None = None,
    observation_nonce: UUID | None = None,
) -> ResultReceipt:
    """Observe one exact terminal transaction, never ACK or release resources."""
    try:
        if (
            type(context) is not ResultIntentContext
            or type(context.action) is not AttemptResultBody
            or type(ref) is not ResultIntentRef
            or ref.attempt_id != context.attempt_id
            or type(_control) is not ResultControlFence
        ):
            raise ValueError
        action = _freeze_action(context, max_body_bytes)
        frozen = replace(context, action=action)
        with factory() as session:
            gate = None
            if _control is not None:
                gate, authority = _control_authority(session, _control)
                frozen = replace(frozen, authority=authority)
            execution, _adapter, terminal, slot = attempt._lock_attempt_context(
                session, frozen.worker_id, frozen.attempt_id, existing_only=True
            )
            state = validate_v4_attempt_locked(
                session,
                execution=execution,
                attempt=terminal,
                slot=slot,
                worker_id=frozen.worker_id,
                credential=frozen.credential,
                instance_id=frozen.instance_id,
                instance_generation=frozen.generation,
                action=frozen.action,
            )
            if state != "terminal":
                raise ValueError
            instance = validate_current_proof(
                session,
                worker_id=frozen.worker_id,
                credential=frozen.credential,
                instance_id=frozen.instance_id,
                generation=frozen.generation,
                token=frozen.instance_proof,
                authority=frozen.authority,
            )
            lease = session.scalar(
                select(AttemptLeaseCandidateV4)
                .where(AttemptLeaseCandidateV4.attempt_id == frozen.attempt_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if lease is None:
                raise ValueError
            _attempt_already_committed(session, lease)
            row = session.scalar(
                select(AttemptResultIntentV4)
                .where(AttemptResultIntentV4.attempt_id == frozen.attempt_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                row is None
                or row.intent_id != ref.intent_id
                or type(row.finalization_id) is not UUID
                or row.finalization_id.int == 0
                or type(row.finalization_xid) is not int
                or row.finalization_xid != row.writer_xid
                or row.final_attempt_status != terminal.status
                or row.final_execution_status != execution.status
                or terminal.lease_expires_at != lease.nominal_until
            ):
                raise ValueError
            _already_committed(session, row)
            terminal_xid = session.scalar(
                select(literal_column("execution_attempts.xmin::text::bigint", type_=BigInteger()))
                .select_from(ExecutionAttempt)
                .where(ExecutionAttempt.id == terminal.id)
            )
            execution_xid = session.scalar(
                select(literal_column("executions.xmin::text::bigint", type_=BigInteger()))
                .select_from(Execution)
                .where(Execution.id == execution.id)
            )
            body_mac = _body_mac(frozen, max_body_bytes)
            now = worker_availability.current_time(session)
            worker_credentials._confirm_runtime_credential_at_locked(
                session, frozen.worker_id, frozen.credential, now
            )
            _check_instance_at(instance, frozen.authority, frozen.instance_proof, now)
            if (
                terminal_xid != row.finalization_xid % 2**32
                or execution_xid != row.finalization_xid % 2**32
                or not _receipt_matches_after_heartbeat(row, frozen, lease, instance, body_mac)
                or not verify_attempt_lease_proof(
                    token=frozen.attempt_proof,
                    key=frozen.authority.key,
                    binding=_binding(lease, frozen.authority),
                    database_now=now,
                )
                or not verify_result_attestation(row, frozen.authority, attestation, now=now)
            ):
                raise ValueError
            receipt = ResultReceipt(
                terminal.id,
                row.intent_id,
                row.finalization_id,
                terminal.status,
                execution.status,
            )
            if observation_nonce is not None:
                if gate is None:
                    raise ValueError("result receipt unavailable")
                receipt = replace(
                    receipt,
                    token=_sign_receipt(
                        receipt,
                        attestation,
                        frozen.authority,
                        observation_nonce=observation_nonce,
                        observed_at=now,
                    ),
                )
            if gate is not None:
                gate.check()
            session.rollback()
            return receipt
    except Exception:
        raise ResultReceiptUnavailable("result receipt unavailable") from None


def finalize_unconfirmed_result_from_token(
    factory: sessionmaker[Session],
    context: ResultIntentContext,
    ref: ResultIntentRef,
    token: object,
    *,
    observation_nonce: UUID,
    control: ResultControlFence,
    max_body_bytes: int,
) -> None:
    """Parse a signed intent and use a fresh transaction-bound Control fence."""
    try:
        if type(context) is not ResultIntentContext or type(ref) is not ResultIntentRef:
            raise ValueError
        if type(control) is not ResultControlFence:
            raise ValueError
        attestation = attestation_from_token(
            token,
            context.authority,
            intent_id=ref.intent_id,
            observation_nonce=observation_nonce,
        )
    except Exception:
        raise ResultFinalizeRejected("result unavailable") from None
    finalize_unconfirmed_result(
        factory,
        context,
        ref,
        attestation,
        max_body_bytes=max_body_bytes,
        _control=control,
    )


def observe_committed_result_from_token(
    factory: sessionmaker[Session],
    context: ResultIntentContext,
    ref: ResultIntentRef,
    token: object,
    *,
    observation_nonce: UUID,
    receipt_nonce: UUID,
    control: ResultControlFence,
    max_body_bytes: int,
) -> ResultReceipt:
    """Observe the exact finalization under a fresh Control fence and nonce."""
    try:
        if type(context) is not ResultIntentContext or type(ref) is not ResultIntentRef:
            raise ValueError
        if type(control) is not ResultControlFence:
            raise ValueError
        attestation = attestation_from_token(
            token,
            context.authority,
            intent_id=ref.intent_id,
            observation_nonce=observation_nonce,
        )
        return observe_committed_result(
            factory,
            context,
            ref,
            attestation,
            max_body_bytes=max_body_bytes,
            _control=control,
            observation_nonce=receipt_nonce,
        )
    except Exception:
        raise ResultReceiptUnavailable("result receipt unavailable") from None
