"""v4 Claim for W1 fixed dispatch and staged reservation-bound dispatch."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StrictBool,
    StrictInt,
    TypeAdapter,
    model_validator,
)
from sqlalchemy import BigInteger, case, func, select, text
from sqlalchemy import cast as sql_cast
from sqlalchemy.orm import Session, defer

from dlr.common.business_credentials_v4 import BusinessCredentialError
from dlr.common.config import settings
from dlr.common.managed_input import controlled_input_mount_name
from dlr.common.worker_languages_v4 import valid_capabilities
from dlr.control.models import (
    Adapter,
    AdapterExecutionAdmission,
    AdapterExecutionSlot,
    AdapterVersion,
    Execution,
    ExecutionAttempt,
    ExecutionInputArtifactLease,
    GlobalExecutionAdmission,
    ManagedInputArtifact,
    WorkerCacheGuard,
)
from dlr.control.models.worker_instance_history import WorkerInstanceHistory
from dlr.control.schemas.reliable_runtime import V4TaskInputFile, V4TaskPayload
from dlr.control.schemas.worker import isolation_capabilities_ready
from dlr.control.services import business_credentials_v4 as business_credentials
from dlr.control.services import package_source as package_source_service
from dlr.control.services import secrets as secrets_service
from dlr.control.services import (
    worker_availability,
    worker_capacity,
    worker_credentials,
    worker_reservation,
)
from dlr.control.services.adapter import domain_error
from dlr.control.services.attempt import (
    ACTIVE_ATTEMPT_STATUSES,
    V4AttemptAuthority,
    _load_profile,
    _retry_policy,
)
from dlr.control.services.attempt_lease_candidates_v4 import (
    AttemptCandidateRef,
    AttemptLeaseCandidateError,
    stage_initial_attempt_candidate,
)
from dlr.control.services.dispatch import (
    DispatchMessage,
    deserialize_dispatch_message,
    serialize_dispatch_message,
    worker_routing_key,
)
from dlr.control.services.execution_cancellation import (
    lock_execution_attempts,
    lock_incidents_and_outbox,
    lock_reserved_subscription_scope,
)
from dlr.control.services.worker_lease_candidates_v4 import (
    LeaseCandidateError,
    ProofAuthority,
    validate_current_proof,
)
from dlr.control.services.worker_protocol import generate_token, hash_token


class V4ClaimDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    _wire_bytes: bytes | None = PrivateAttr(default=None)
    _attempt_candidate: AttemptCandidateRef | None = PrivateAttr(default=None)

    decision: Literal["EXECUTE", "ACK_NOOP", "DEFER", "REJECT_DLQ", "PAUSE_CONSUMER"]
    reason: str = Field(min_length=1, max_length=64)
    retry_after_seconds: StrictInt | None = Field(default=None, ge=0, le=86400)
    attempt_id: StrictInt | None = Field(default=None, gt=0)
    cancel_requested: StrictBool = False
    payload: V4TaskPayload | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> V4ClaimDecision:
        if self.decision == "EXECUTE":
            if self.payload is None or self.attempt_id != self.payload.attempt_id:
                raise ValueError("EXECUTE requires a matching Attempt payload")
        elif self.payload is not None:
            raise ValueError("non-EXECUTE cannot contain a payload")
        if self.decision == "DEFER" and self.retry_after_seconds is None:
            raise ValueError("DEFER requires a retry interval")
        return self


_CLAIM_ADAPTER = TypeAdapter(V4ClaimDecision)


def _serialize_claim(decision: V4ClaimDecision) -> bytes:
    """Build the exact HTTP body while the Claim transaction is uncommitted."""
    return _CLAIM_ADAPTER.dump_json(decision)


@dataclass(frozen=True)
class ClaimWireBudget:
    """Experimental exact-wire ceiling, not a memory or deployment capability proof.

    Only an unmounted HTTP probe can supply this value. The eventual protocol
    must first establish local accounting, bounded memory handling, and a
    trusted all-Control capability fence before a Worker consumer can use it.
    """

    max_response_bytes: int

    def __post_init__(self) -> None:
        if type(self.max_response_bytes) is not int or not 0 < self.max_response_bytes < 2**63:
            raise ValueError("invalid claim wire budget")


@dataclass(frozen=True)
class ClaimProofContext:
    """Explicit test-only proof context; not a cluster rollout gate."""

    authority: ProofAuthority
    instance_proof: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.authority) is not ProofAuthority or type(self.instance_proof) is not str:
            raise ValueError("invalid claim proof context")


@dataclass(frozen=True)
class ClaimProposal:
    """Private tentative values; no Attempt exists until finalization commits."""

    attempt_id: int
    attempt_no: int
    fencing_token: int
    lease_expires_at: datetime
    claim_token: str = field(repr=False)
    cleanup_token: str = field(repr=False)
    wire_digest: bytes = field(repr=False)


def proposal_from_decision(decision: V4ClaimDecision) -> ClaimProposal:
    """Freeze the exact tentative body for a later locked revalidation."""
    if (
        type(decision) is not V4ClaimDecision
        or decision.decision != "EXECUTE"
        or decision.payload is None
        or type(decision._wire_bytes) is not bytes
        or decision._attempt_candidate is None
    ):
        raise ValueError("claim proposal unavailable")
    payload = decision.payload
    return ClaimProposal(
        attempt_id=payload.attempt_id,
        attempt_no=payload.attempt_no,
        fencing_token=payload.fencing_token,
        lease_expires_at=payload.lease_expires_at,
        claim_token=payload.claim_token,
        cleanup_token=payload.cleanup_token,
        wire_digest=hashlib.sha256(decision._wire_bytes).digest(),
    )


def _pause(session: Session, reason: str) -> V4ClaimDecision:
    session.rollback()
    return V4ClaimDecision(decision="PAUSE_CONSUMER", reason=reason, retry_after_seconds=30)


def _lock_source_lower_bound(session: Session, *, adapter_id: int, version_id: int) -> int | None:
    """Lock the Revision and obtain only a lower bound, never an acceptance bound.

    The payload emits both source strings unchanged. JSON escaping does not
    shorten their UTF-8 encoding. PostgreSQL may itself decompress the values;
    this query is not a database or full-response memory bound.
    """
    if session.scalar(text("SHOW server_encoding")) != "UTF8":
        return None
    value = session.scalar(
        select(
            sql_cast(func.octet_length(AdapterVersion.code), BigInteger)
            + sql_cast(func.octet_length(AdapterVersion.requirements), BigInteger)
        )
        .where(AdapterVersion.id == version_id, AdapterVersion.adapter_id == adapter_id)
        .with_for_update(read=True)
    )
    return int(value) if value is not None else None


def _locked_input_lower_bound(session: Session, execution_id: int) -> int:
    """Minimum wire bytes for a locked top-level array, object or UTF-8 string.

    This is a rejection check, not an acceptance or process-memory bound.
    The caller has already verified UTF-8 database encoding. Object keys and
    direct string values contribute only their unescaped UTF-8 bytes; JSON
    escaping can enlarge, but cannot shorten, that lower bound.
    """
    length = sql_cast(func.jsonb_array_length(Execution.input), BigInteger)
    string_bytes = sql_cast(
        func.octet_length(Execution.input.op("#>>")(text("'{}'::text[]"))), BigInteger
    )
    entry = func.pg_catalog.jsonb_each(Execution.input).table_valued("key", "value").alias("entry")
    entry_string_bytes = sql_cast(
        func.pg_catalog.octet_length(entry.c.value.op("#>>")(text("'{}'::text[]"))),
        BigInteger,
    )
    entry_value = case(
        (func.pg_catalog.jsonb_typeof(entry.c.value) == "string", entry_string_bytes + 2),
        else_=1,
    )
    object_bytes = (
        select(
            2
            + func.coalesce(
                func.sum(
                    sql_cast(func.pg_catalog.octet_length(entry.c.key), BigInteger)
                    + 3
                    + entry_value
                ),
                0,
            )
            + func.greatest(func.count() - 1, 0)
        )
        .select_from(entry)
        .correlate(Execution)
        .scalar_subquery()
    )
    value = session.scalar(
        select(
            case(
                (
                    func.jsonb_typeof(Execution.input) == "array",
                    case((length == 0, 2), else_=2 * length + 1),
                ),
                (func.jsonb_typeof(Execution.input) == "string", string_bytes),
                (func.jsonb_typeof(Execution.input) == "object", object_bytes),
                else_=0,
            )
        ).where(Execution.id == execution_id)
    )
    if value is None:
        raise ValueError("locked execution unavailable")
    return int(value)


def _build_payload(
    session: Session,
    execution: Execution,
    adapter: Adapter,
    worker: Any,
    attempt: ExecutionAttempt,
    claim_token: str,
    cleanup_token: str,
) -> V4TaskPayload:
    if (
        execution.timeout_seconds_snapshot is None
        or execution.recovery_grace_seconds_snapshot is None
        or execution.workspace_cleanup_attempt_timeout_seconds_snapshot is None
        or execution.workspace_cleanup_total_timeout_seconds_snapshot is None
    ):
        raise domain_error(409, "execution_snapshot_invalid", "Execution snapshot unavailable")
    profile = _load_profile(execution)
    version = session.get(AdapterVersion, execution.version_id)
    if version is None or version.adapter_id != adapter.id:
        raise domain_error(409, "execution_revision_invalid", "Execution revision unavailable")
    files: list[V4TaskInputFile] = []
    if execution.input_source_type == "managed_files":
        rows = session.execute(
            select(ExecutionInputArtifactLease, ManagedInputArtifact)
            .join(
                ManagedInputArtifact,
                ManagedInputArtifact.id == ExecutionInputArtifactLease.artifact_id,
            )
            .where(ExecutionInputArtifactLease.execution_id == execution.id)
            .order_by(ExecutionInputArtifactLease.ordinal)
        ).all()
        expected = execution.input_snapshot.get("artifacts", [])
        if not isinstance(expected, list) or len(rows) != len(expected):
            raise domain_error(
                409, "execution_input_lease_unavailable", "Execution input Lease is unavailable"
            )
        for (lease, artifact), frozen in zip(rows, expected, strict=True):
            if not isinstance(frozen, dict) or frozen != {
                "ordinal": lease.ordinal,
                "original_filename": artifact.original_filename,
                "content_type": artifact.content_type,
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
            }:
                raise domain_error(
                    409, "execution_input_lease_unavailable", "Execution input Lease is unavailable"
                )
        files = [
            V4TaskInputFile(
                id=lease.artifact_id,
                ordinal=lease.ordinal,
                mount_name=controlled_input_mount_name(lease.ordinal, artifact.original_filename),
                original_filename=artifact.original_filename,
                content_type=artifact.content_type,
                size_bytes=artifact.size_bytes,
                sha256=artifact.sha256,
            )
            for lease, artifact in rows
        ]
    index_url = None
    if execution.builtin_package_snapshot is None and version.requirements:
        index_url = package_source_service.resolve_default_index_url(
            session,
            {
                "python": "pypi",
                "javascript": "npm",
                "java": "maven",
                "typescript": "npm",
                "go": "goproxy",
            }[adapter.language],
        )
    return V4TaskPayload(
        protocol_version=4,
        worker_identity_id=worker.identity_id,
        instance_id=worker.instance_id,
        instance_generation=worker.instance_generation,
        execution_id=execution.id,
        attempt_id=attempt.id,
        attempt_no=attempt.attempt_no,
        fencing_token=attempt.fencing_token,
        lease_expires_at=attempt.lease_expires_at,
        lease_seconds=settings.attempt_lease_seconds,
        renew_seconds=settings.attempt_renew_seconds,
        claim_token=claim_token,
        cleanup_token=cleanup_token,
        adapter_id=adapter.id,
        version_id=version.id,
        language=cast(Any, adapter.language),
        code=version.code,
        requirements=version.requirements,
        runtime_config=version.runtime_config,
        input=execution.input,
        latest_version_id=adapter.latest_version_id,
        execution_timeout_seconds=execution.timeout_seconds_snapshot,
        secrets={}
        if execution.dependency_check
        else (
            business_credentials.resolve_values(session, execution, worker)
            if settings.worker_remote_v4_enabled
            else secrets_service.resolve_execution_secrets(session, execution)
        ),
        builtin_package_snapshot=execution.builtin_package_snapshot,
        dependency_check=execution.dependency_check,
        index_url=index_url,
        locale=cast(Any, execution.locale),
        resource_profile=profile,
        credential_bindings=list(execution.credential_bindings_snapshot),
        input_source_type=cast(Any, execution.input_source_type),
        input_snapshot=dict(execution.input_snapshot),
        input_files=files,
        recovery_grace_seconds_snapshot=execution.recovery_grace_seconds_snapshot,
        workspace_cleanup_attempt_timeout_seconds_snapshot=execution.workspace_cleanup_attempt_timeout_seconds_snapshot,
        workspace_cleanup_total_timeout_seconds_snapshot=execution.workspace_cleanup_total_timeout_seconds_snapshot,
    )


def _claim_fixed(
    session: Session,
    worker_id: int,
    message: DispatchMessage,
    authority: V4AttemptAuthority,
    *,
    _wire_budget: ClaimWireBudget,
    _proof_context: ClaimProofContext,
    _proposal: ClaimProposal | None = None,
) -> V4ClaimDecision:
    """Prepare a dispatch Claim; the caller must fence and commit it."""
    if type(_wire_budget) is not ClaimWireBudget:
        raise ValueError("invalid claim wire budget")
    if type(_proof_context) is not ClaimProofContext:
        raise ValueError("invalid claim proof context")
    if _proposal is not None and type(_proposal) is not ClaimProposal:
        raise ValueError("invalid claim proposal")
    identity = session.execute(
        select(
            Execution.adapter_id, Execution.version_id, Execution.requires_worker_reservation
        ).where(Execution.id == message.execution_id)
    ).one_or_none()
    if identity is None:
        return _pause(session, "execution_unavailable")
    adapter_id, version_id, requires_reservation = identity
    adapter = session.get(Adapter, adapter_id, with_for_update=True, populate_existing=True)
    if adapter is None:
        return _pause(session, "admission_state_invalid")
    if requires_reservation:
        lock_reserved_subscription_scope(session, message.execution_id)
    adapter_counter = session.get(
        AdapterExecutionAdmission, adapter_id, with_for_update=True, populate_existing=True
    )
    global_counter = session.get(
        GlobalExecutionAdmission, "global", with_for_update=True, populate_existing=True
    )
    if adapter is None or adapter_counter is None or global_counter is None:
        return _pause(session, "admission_state_invalid")
    # Adapter is already locked. Acquire the Revision before the cache,
    # Execution and Worker suffix; do not transfer code/requirements here.
    source_lower = _lock_source_lower_bound(session, adapter_id=adapter_id, version_id=version_id)
    guard = session.get(
        WorkerCacheGuard, (worker_id, version_id), with_for_update=True, populate_existing=True
    )
    if guard is None or guard.adapter_id != adapter_id:
        return _pause(session, "cache_guard_unavailable")
    deferred = (
        defer(Execution.input),
        defer(Execution.input_snapshot),
        defer(Execution.credential_bindings_snapshot),
        defer(Execution.resource_profile_snapshot),
        defer(Execution.schedule_policy_snapshot),
        defer(Execution.webhook_response_snapshot),
        defer(Execution.output),
        defer(Execution.output_preview),
        defer(Execution.stdout),
        defer(Execution.stderr),
        defer(Execution.error),
    )
    execution = session.get(
        Execution,
        message.execution_id,
        with_for_update=True,
        populate_existing=True,
        options=deferred,
    )
    if execution is None:
        return _pause(session, "execution_unavailable")
    attempts = lock_execution_attempts(session, execution.id)
    slot = session.get(
        AdapterExecutionSlot, (adapter_id, 0), with_for_update=True, populate_existing=True
    )
    if slot is None:
        return _pause(session, "attempt_slot_invalid")
    pool = worker_reservation.lock_pool(session, execution)
    try:
        worker = worker_credentials.authenticate_runtime_credential(
            session, worker_id, authority.credential
        )
    except worker_credentials.WorkerCredentialError:
        raise domain_error(
            401, "worker_credential_invalid", "Worker credential is invalid"
        ) from None
    now = worker_availability.current_time(session)
    try:
        worker_credentials._confirm_runtime_credential_at_locked(
            session, worker_id, authority.credential, now
        )
    except worker_credentials.WorkerCredentialError:
        raise domain_error(
            401, "worker_credential_invalid", "Worker credential is invalid"
        ) from None
    if (
        worker.protocol_version != 4
        or worker.instance_id != authority.instance_id
        or worker.instance_generation != authority.instance_generation
        or worker.instance_lease_until is None
        or worker.instance_lease_until <= now
    ):
        raise domain_error(409, "worker_instance_expired", "Worker instance is no longer valid")
    history = session.get(WorkerInstanceHistory, (worker_id, authority.instance_id))
    if history is None or history.generation != authority.instance_generation:
        return _pause(session, "worker_instance_history_invalid")
    capacity = reservation = None
    if (
        requires_reservation
        and message.dispatch_generation == execution.dispatch_generation
        and execution.target_worker_id == worker_id
    ):
        capacity = worker_capacity.lock_capacity(session, worker_id)
        reservation = worker_reservation.lock_generation(session, execution)
    _, outbox_rows = lock_incidents_and_outbox(session, execution.id)
    now = worker_availability.current_time(session)
    if worker.instance_lease_until is None or worker.instance_lease_until <= now:
        raise domain_error(409, "worker_instance_expired", "Worker instance is no longer valid")
    try:
        worker_credentials.authenticate_runtime_credential(session, worker_id, authority.credential)
    except worker_credentials.WorkerCredentialError:
        raise domain_error(
            401, "worker_credential_invalid", "Worker credential is invalid"
        ) from None
    try:
        validate_current_proof(
            session,
            worker_id=worker_id,
            credential=authority.credential,
            instance_id=authority.instance_id,
            generation=authority.instance_generation,
            token=_proof_context.instance_proof,
            authority=_proof_context.authority,
        )
    except LeaseCandidateError:
        raise domain_error(409, "worker_lease_unavailable", "Worker lease is unavailable") from None
    row = next(
        (r for r in outbox_rows if r.dispatch_generation == message.dispatch_generation), None
    )
    if (
        row is None
        or row.status not in {"pending", "published", "superseded"}
        or row.payload_json is None
    ):
        return _pause(session, "dispatch_generation_not_active")
    try:
        saved = deserialize_dispatch_message(row.payload_json)
        same = (
            saved == message
            and row.message_id == message.message_id
            and row.routing_key == worker_routing_key(worker_id)
            and row.payload_bytes == len(serialize_dispatch_message(saved))
        )
    except (ValueError, TypeError):
        same = False
    if not same:
        return _pause(session, "dispatch_identity_invalid")
    if (
        execution.dispatch_backend != "rabbitmq"
        or execution.adapter_id != message.adapter_id
        or execution.version_id != version_id
        or adapter.language != message.language
        or execution.resource_class != message.resource_class
        or message.target_worker_id != worker_id
        or (
            not requires_reservation
            and (
                execution.target_worker_id != worker_id
                or execution.target_worker_id_snapshot != worker_id
            )
        )
    ):
        return _pause(session, "dispatch_identity_invalid")
    if message.dispatch_generation < execution.dispatch_generation:
        session.rollback()
        return V4ClaimDecision(decision="ACK_NOOP", reason="stale_generation")
    if message.dispatch_generation > execution.dispatch_generation:
        return _pause(session, "future_generation")
    if requires_reservation:
        if (
            execution.target_worker_id != worker_id
            or reservation is None
            or row.reservation_id != reservation.id
        ):
            return _pause(session, "worker_reservation_unavailable")
        if execution.status in {"succeeded", "dead_letter", "cancelled", "expired"}:
            session.rollback()
            return V4ClaimDecision(decision="ACK_NOOP", reason="terminal")
        if reservation.attempt_id is not None:
            session.rollback()
            return V4ClaimDecision(decision="ACK_NOOP", reason="execution_not_queued")
    if row.status == "superseded":
        return _pause(session, "dispatch_generation_not_active")
    if not valid_capabilities(worker.capabilities) or adapter.language not in worker.capabilities:
        return _pause(session, "worker_task_capability_unavailable")
    if (
        worker.scheduling_state != "active"
        or worker.capacity_state != "ready"
        or not worker_availability.is_effectively_online(worker, now=now)
        or worker.isolation_preflight_status != "passed"
        or not isolation_capabilities_ready(worker.isolation_capabilities)
    ):
        return _pause(session, "worker_not_ready")
    if worker_availability.has_unreconciled_v4_attempt(session, worker, now=now):
        return _pause(session, "worker_recovery_unverified")
    if guard.phase != "idle":
        return _pause(session, "cache_reclamation_in_progress")
    if requires_reservation and (
        not worker_reservation.claim_fits(session, worker, capacity)
        or not worker_reservation.held_valid(
            session,
            execution,
            worker,
            capacity,
            reservation,
            pool,
            now=worker_availability.current_time(session),
        )
    ):
        return _pause(session, "worker_reservation_unavailable")
    if execution.status in {"succeeded", "dead_letter", "cancelled", "expired"}:
        session.rollback()
        return V4ClaimDecision(decision="ACK_NOOP", reason="terminal")
    if execution.cancel_requested:
        return _pause(session, "cancel_requested")
    if execution.status == "running":
        session.rollback()
        return V4ClaimDecision(decision="ACK_NOOP", reason="execution_not_queued")
    from dlr.control.services.worker_stage_capability_v4 import execution_block_reason

    blocked = execution_block_reason(session, adapter, execution, lock_credentials=True)
    if blocked is not None:
        return _pause(
            session,
            blocked if blocked.startswith("credentials_") else "worker_task_capability_unavailable",
        )
    # A lost instance's process and local resources cannot be presumed gone
    # from expiry or a new lease. Until physical reconciliation exists, do not
    # execute even when a retry generation is already queued.
    if any(a.status == "worker_lost" and a.worker_identity_id is not None for a in attempts):
        return _pause(session, "worker_recovery_unverified")
    if execution.status != "queued" or (
        execution.next_attempt_at is not None and execution.next_attempt_at > now
    ):
        return _pause(session, "execution_not_queued")
    if (
        any(a.status in ACTIVE_ATTEMPT_STATUSES for a in attempts)
        or slot.active_attempt_id is not None
    ):
        session.rollback()
        return V4ClaimDecision(decision="DEFER", reason="adapter_slot_busy", retry_after_seconds=1)
    policy = _retry_policy(execution)
    attempt_no = execution.attempt_count + 1
    if attempt_no > policy["max_attempts"]:
        return _pause(session, "retry_exhausted")
    input_lower = 0
    if source_lower is not None and source_lower <= _wire_budget.max_response_bytes:
        input_lower = _locked_input_lower_bound(session, execution.id)
    if source_lower is not None and source_lower + input_lower > _wire_budget.max_response_bytes:
        # A confirmed lower bound can prove rejection only. Recheck the
        # authenticated authority after potentially slow database work.
        try:
            worker_credentials.authenticate_runtime_credential(
                session, worker_id, authority.credential
            )
        except worker_credentials.WorkerCredentialError:
            raise domain_error(
                401, "worker_credential_invalid", "Worker credential is invalid"
            ) from None
        now = worker_availability.current_time(session)
        try:
            worker_credentials._confirm_runtime_credential_at_locked(
                session, worker_id, authority.credential, now
            )
        except worker_credentials.WorkerCredentialError:
            raise domain_error(
                401, "worker_credential_invalid", "Worker credential is invalid"
            ) from None
        if worker.instance_lease_until is None or worker.instance_lease_until <= now:
            raise domain_error(409, "worker_instance_expired", "Worker instance is no longer valid")
        if not worker_availability.is_effectively_online(worker, now=now):
            return _pause(session, "worker_not_ready")
        if worker_availability.has_unreconciled_v4_attempt(session, worker, now=now):
            return _pause(session, "worker_recovery_unverified")
        return _pause(session, "claim_response_budget_exceeded")
    fence = max(int(slot.fencing_token) + 1, 1)
    if _proposal is None:
        token, cleanup = generate_token(), generate_token()
        lease_expires_at = now + timedelta(seconds=settings.attempt_lease_seconds)
    else:
        if (
            _proposal.attempt_no != attempt_no
            or _proposal.fencing_token != fence
            or _proposal.lease_expires_at <= now
            or type(_proposal.wire_digest) is not bytes
            or len(_proposal.wire_digest) != 32
        ):
            return _pause(session, "claim_snapshot_changed")
        token, cleanup = _proposal.claim_token, _proposal.cleanup_token
        lease_expires_at = _proposal.lease_expires_at
    attempt = ExecutionAttempt(
        id=_proposal.attempt_id if _proposal is not None else None,
        execution_id=execution.id,
        adapter_id=adapter.id,
        attempt_no=attempt_no,
        worker_id=worker_id,
        worker_identity_id=worker.identity_id,
        worker_instance_id=worker.instance_id,
        worker_instance_generation=worker.instance_generation,
        fencing_token=fence,
        claim_token_hash=hash_token(token),
        cleanup_token_hash=hash_token(cleanup),
        lease_expires_at=lease_expires_at,
        status="claimed",
        claimed_at=now,
    )
    try:
        candidate = stage_initial_attempt_candidate(
            session,
            execution=execution,
            slot=slot,
            attempt=attempt,
            credential=authority.credential,
            instance_proof=_proof_context.instance_proof,
            authority=_proof_context.authority,
            expected_attempt_id=_proposal.attempt_id if _proposal is not None else None,
        )
    except AttemptLeaseCandidateError:
        raise domain_error(
            409, "attempt_lease_unavailable", "Attempt lease is unavailable"
        ) from None
    execution.attempt_count = attempt_no
    execution.status = "running"
    execution.worker_id = worker_id
    execution.started_at = now
    execution.next_attempt_at = None
    execution.workspace_cleanup_status = "pending"
    execution.workspace_cleanup_error_code = None
    slot.active_attempt_id = attempt.id
    slot.lease_expires_at = attempt.lease_expires_at
    slot.fencing_token = fence
    try:
        payload = _build_payload(session, execution, adapter, worker, attempt, token, cleanup)
    except BusinessCredentialError as error:
        return _pause(session, error.reason)
    decision = V4ClaimDecision(
        decision="EXECUTE", reason="claimed", attempt_id=attempt.id, payload=payload
    )
    decision._attempt_candidate = candidate
    try:
        wire = _serialize_claim(decision)
        decision._wire_bytes = wire
    except Exception:
        session.rollback()
        raise domain_error(
            409, "worker_claim_snapshot_invalid", "Worker claim snapshot invalid"
        ) from None
    # Serialization itself may take time; revalidate the final authority only
    # after the exact response bytes have been constructed.
    try:
        worker_credentials.authenticate_runtime_credential(session, worker_id, authority.credential)
    except worker_credentials.WorkerCredentialError:
        raise domain_error(
            401, "worker_credential_invalid", "Worker credential is invalid"
        ) from None
    now = worker_availability.current_time(session)
    try:
        worker_credentials._confirm_runtime_credential_at_locked(
            session, worker_id, authority.credential, now
        )
    except worker_credentials.WorkerCredentialError:
        raise domain_error(
            401, "worker_credential_invalid", "Worker credential is invalid"
        ) from None
    if (
        worker.instance_lease_until is None
        or worker.instance_lease_until <= now
        or attempt.lease_expires_at <= now
    ):
        raise domain_error(409, "worker_instance_expired", "Worker instance is no longer valid")
    if not worker_availability.is_effectively_online(worker, now=now):
        return _pause(session, "worker_not_ready")
    if worker_availability.has_unreconciled_v4_attempt(session, worker, now=now):
        return _pause(session, "worker_recovery_unverified")
    if len(wire) > _wire_budget.max_response_bytes:
        return _pause(session, "claim_response_budget_exceeded")
    if _proposal is not None and not hmac.compare_digest(
        hashlib.sha256(wire).digest(), _proposal.wire_digest
    ):
        return _pause(session, "claim_snapshot_changed")
    try:
        validate_current_proof(
            session,
            worker_id=worker_id,
            credential=authority.credential,
            instance_id=authority.instance_id,
            generation=authority.instance_generation,
            token=_proof_context.instance_proof,
            authority=_proof_context.authority,
        )
    except LeaseCandidateError:
        raise domain_error(409, "worker_lease_unavailable", "Worker lease is unavailable") from None
    if reservation is not None:
        if not worker_reservation.held_valid(
            session,
            execution,
            worker,
            capacity,
            reservation,
            pool,
            now=worker_availability.current_time(session),
        ):
            return _pause(session, "worker_reservation_unavailable")
        reservation.status = "claimed"
        reservation.attempt_id = attempt.id
        reservation.fencing_token = fence
    return decision


def prepare_claim(
    session: Session,
    worker_id: int,
    message: DispatchMessage,
    authority: V4AttemptAuthority,
    *,
    wire_budget: ClaimWireBudget,
    proof_context: ClaimProofContext,
    check_authority: Callable[[], None],
) -> tuple[V4ClaimDecision, ClaimProposal | None]:
    """Build tentative bytes, then confirm rollback before they can be offered."""
    decision = _claim_fixed(
        session,
        worker_id,
        message,
        authority,
        _wire_budget=wire_budget,
        _proof_context=proof_context,
    )
    proposal = proposal_from_decision(decision) if decision.decision == "EXECUTE" else None
    if proposal is not None:
        check_authority()
    # Even a matching proposal has no committed Attempt or business authority.
    session.rollback()
    return decision, proposal


def finalize_claim(
    session: Session,
    worker_id: int,
    message: DispatchMessage,
    authority: V4AttemptAuthority,
    *,
    wire_budget: ClaimWireBudget,
    proof_context: ClaimProofContext,
    proposal: ClaimProposal,
) -> V4ClaimDecision:
    """Recreate a prepared Claim only if its locked snapshot still matches.

    The caller must resolve the proposal from its own bounded, private
    preparation state, then fence and commit this transaction. Worker input
    alone must never be accepted as a proposal or execution authority.
    """
    if type(proposal) is not ClaimProposal:
        raise ValueError("invalid claim proposal")
    return _claim_fixed(
        session,
        worker_id,
        message,
        authority,
        _wire_budget=wire_budget,
        _proof_context=proof_context,
        _proposal=proposal,
    )
