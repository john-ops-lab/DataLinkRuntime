"""PostgreSQL-authoritative Worker v3 Claim, Attempt and retry services.

The RabbitMQ delivery is deliberately a short-lived transport envelope.  This
module owns the durable state transition after a delivery reaches Control:
Execution -> Adapter -> Slot 0 -> Attempt.  Every terminal path uses the same
locks and fencing checks so a duplicate delivery, late result, or lease
recovery cannot create a second active run or release a reservation twice.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from dlr.common.bigfields import truncate_utf8
from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    AdapterExecutionSlot,
    AdapterVersion,
    Execution,
    ExecutionArtifactHold,
    ExecutionAttempt,
    ExecutionIncidentDisposition,
    ExecutionInfrastructureIncident,
    ExecutionInputArtifactLease,
    ExecutionOutbox,
    ManagedInputArtifact,
    ManagedInputArtifactStatus,
    RuntimeReconciliationCursor,
    Worker,
)
from dlr.control.schemas.execution import ExecutionResultReport
from dlr.control.schemas.reliable_runtime import (
    AttemptActionBody,
    AttemptPrepareFailedBody,
    AttemptProgressBody,
    AttemptRenewBody,
    AttemptResultBody,
    AttemptStartBody,
    AttemptSummary,
    ClaimDecision,
    IncidentDispositionReceipt,
    InfrastructureIncidentSummary,
    ReliableExecutionDetail,
    ReplayResponse,
    ResourceProfile,
    V3TaskPayload,
)
from dlr.control.services import admission, cache_governance, outbox
from dlr.control.services import execution as execution_service
from dlr.control.services.adapter import domain_error
from dlr.control.services.dispatch import deserialize_dispatch_message
from dlr.control.services.execution_cancellation import (
    CANCELLATION_ERROR_CODE,
    lock_execution_attempts,
    lock_incidents_and_outbox,
)
from dlr.control.services.input_config import database_now
from dlr.control.services.worker import build_task_payload
from dlr.control.services.worker_protocol import generate_token, hash_token, token_matches

logger = logging.getLogger("dlr.control.attempt")

ACTIVE_ATTEMPT_STATUSES = frozenset({"claimed", "running"})
TERMINAL_ATTEMPT_STATUSES = frozenset(
    {"succeeded", "failed", "timed_out", "cancelled", "worker_lost", "resource_exceeded"}
)
RETRYABLE_ERROR_CLASSES = frozenset({"platform_transient", "worker_lost"})
ATTEMPT_METRICS: Counter[str] = Counter()
_asyncio_to_thread = asyncio.to_thread
_asyncio_sleep = asyncio.sleep
RECONCILIATION_CURSOR_NAME = "expired_attempts"
RECONCILIATION_LOCK_TIMEOUT_MS = 1_000
RECONCILIATION_ROW_ERROR_CODES = frozenset({"resource_profile_invalid", "retry_policy_invalid"})
RECONCILIATION_MISSING_CODES = frozenset(
    {"adapter_not_found", "attempt_not_found", "execution_not_found"}
)


@dataclass(frozen=True)
class RecoveryCandidateReservation:
    """One committed cursor segment, safe to process without holding its row lock."""

    attempt_ids: tuple[int, ...]
    after_id: int
    upper_id: int
    wrapped: bool


class ReconciliationRowError(Exception):
    """A closed, non-sensitive validation failure isolated to one Attempt row."""

    def __init__(self, code: str, *, attempt_id: int, execution_id: int) -> None:
        if code not in RECONCILIATION_ROW_ERROR_CODES:
            raise ValueError("unsupported reconciliation row error code")
        super().__init__(code)
        self.code = code
        self.attempt_id = attempt_id
        self.execution_id = execution_id


def metrics_snapshot() -> dict[str, int]:
    """Return only stable, low-cardinality Attempt/lease counters."""

    names = (
        "claim_execute",
        "claim_reject_dispatch_payload_invalid",
        "claim_reject_dispatch_target_mismatch",
        "claim_reject_execution_not_found",
        "claim_reject_execution_backend_mismatch",
        "claim_reject_future_generation",
        "claim_reject_target_worker_mismatch",
        "claim_reject_dispatch_fact_mismatch",
        "claim_reject_resource_class_mismatch",
        "claim_reject_adapter_not_found",
        "claim_reject_worker_not_found",
        "terminal_succeeded",
        "terminal_failed",
        "terminal_timed_out",
        "terminal_cancelled",
        "terminal_worker_lost",
        "terminal_resource_exceeded",
        "lease_recovered",
        "retry_dispatched",
    )
    return {name: int(ATTEMPT_METRICS.get(name, 0)) for name in names}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _retry_policy(execution: Execution) -> dict[str, Any]:
    value = execution.retry_policy_snapshot
    if not isinstance(value, dict):
        raise domain_error(409, "retry_policy_invalid", "Execution Retry Policy is invalid")
    raw = cast(dict[str, Any], value)
    required = {
        "max_attempts",
        "initial_backoff_seconds",
        "multiplier",
        "max_backoff_seconds",
        "jitter_ratio",
        "retryable_error_classes",
    }
    if set(raw) != required:
        raise domain_error(409, "retry_policy_invalid", "Execution Retry Policy is invalid")
    max_attempts_raw = raw["max_attempts"]
    numeric_raw = tuple(
        raw[name]
        for name in (
            "initial_backoff_seconds",
            "multiplier",
            "max_backoff_seconds",
            "jitter_ratio",
        )
    )
    if (
        isinstance(max_attempts_raw, bool)
        or not isinstance(max_attempts_raw, int)
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in numeric_raw
        )
    ):
        raise domain_error(409, "retry_policy_invalid", "Execution Retry Policy is invalid")
    max_attempts = max_attempts_raw
    initial, multiplier, maximum, jitter = (float(value) for value in numeric_raw)
    classes = raw["retryable_error_classes"]
    if (
        max_attempts < 1
        or max_attempts > 100
        or not 0 < initial <= maximum <= 3_600
        or not 1 <= multiplier <= 10
        or not 0 <= jitter <= 0.2
        or not isinstance(classes, list)
        or not classes
        or any(not isinstance(item, str) or item not in RETRYABLE_ERROR_CLASSES for item in classes)
        or len(set(classes)) != len(classes)
    ):
        raise domain_error(409, "retry_policy_invalid", "Execution Retry Policy is invalid")
    return {
        "max_attempts": max_attempts,
        "initial_backoff_seconds": initial,
        "multiplier": multiplier,
        "max_backoff_seconds": maximum,
        "jitter_ratio": jitter,
        "retryable_error_classes": list(classes),
    }


def retry_delay_seconds(execution: Execution, attempt_no: int) -> float:
    """Calculate a finite snapshot-based delay with bounded jitter."""
    policy = _retry_policy(execution)
    base = min(
        policy["max_backoff_seconds"],
        policy["initial_backoff_seconds"] * policy["multiplier"] ** max(0, attempt_no - 1),
    )
    ratio = policy["jitter_ratio"]
    return float(
        max(
            0.0,
            min(
                float(policy["max_backoff_seconds"]),
                random.uniform(base * (1 - ratio), base * (1 + ratio)),
            ),
        )
    )


def _decision(
    decision: str,
    reason: str,
    *,
    retry_after_seconds: int | None = None,
    attempt_id: int | None = None,
    payload: V3TaskPayload | None = None,
    cancel_requested: bool = False,
) -> ClaimDecision:
    return ClaimDecision(
        decision=cast(Any, decision),
        reason=reason,
        retry_after_seconds=retry_after_seconds,
        attempt_id=attempt_id,
        cancel_requested=cancel_requested,
        payload=payload,
    )


def _record_incident(
    session: Session,
    *,
    kind: str,
    message_id: uuid.UUID | None = None,
    execution_id: int | None = None,
    dispatch_generation: int | None = None,
    error: str | None = None,
) -> None:
    """Upsert a low-cardinality infrastructure fact and commit it."""
    query = select(ExecutionInfrastructureIncident).where(
        ExecutionInfrastructureIncident.kind == kind,
        ExecutionInfrastructureIncident.status == "open",
    )
    if message_id is not None:
        query = query.where(ExecutionInfrastructureIncident.message_id == message_id)
    elif execution_id is not None:
        query = query.where(
            ExecutionInfrastructureIncident.execution_id == execution_id,
            ExecutionInfrastructureIncident.dispatch_generation == dispatch_generation,
        )
    incident = session.scalar(query.with_for_update())
    if incident is None:
        incident = ExecutionInfrastructureIncident(
            kind=kind[:64],
            message_id=message_id,
            execution_id=execution_id,
            dispatch_generation=dispatch_generation,
            attempts=1,
            last_error=(error or kind)[:256],
        )
        session.add(incident)
    else:
        incident.attempts += 1
        incident.last_error = (error or kind)[:256]
    session.commit()


def _reject_dispatch(
    session: Session,
    *,
    kind: str,
    message_id: uuid.UUID | None = None,
    execution_id: int | None = None,
    dispatch_generation: int | None = None,
) -> ClaimDecision:
    ATTEMPT_METRICS[f"claim_reject_{kind}"] += 1
    _record_incident(
        session,
        kind=kind,
        message_id=message_id,
        execution_id=execution_id,
        dispatch_generation=dispatch_generation,
    )
    return _decision("REJECT_DLQ", kind)


def _load_profile(execution: Execution) -> ResourceProfile:
    try:
        return ResourceProfile.model_validate(execution.resource_profile_snapshot)
    except ValidationError:
        raise domain_error(
            409, "resource_profile_invalid", "Execution Resource Profile is invalid"
        ) from None


def _build_v3_payload(
    session: Session,
    execution: Execution,
    worker: Worker,
    attempt: ExecutionAttempt,
    claim_token: str,
    cleanup_token: str,
) -> V3TaskPayload:
    profile = _load_profile(execution)
    legacy_payload = build_task_payload(
        session,
        execution,
        worker=worker,
        claim_token=claim_token,
        cleanup_token=cleanup_token,
    )
    return V3TaskPayload(
        execution_id=execution.id,
        attempt_id=attempt.id,
        attempt_no=attempt.attempt_no,
        fencing_token=attempt.fencing_token,
        lease_expires_at=attempt.lease_expires_at,
        lease_seconds=settings.attempt_lease_seconds,
        renew_seconds=settings.attempt_renew_seconds,
        claim_token=claim_token,
        cleanup_token=cleanup_token,
        adapter_id=legacy_payload.adapter_id,
        version_id=legacy_payload.version_id,
        language=cast(Any, legacy_payload.language),
        code=legacy_payload.code,
        requirements=legacy_payload.requirements,
        runtime_config=legacy_payload.runtime_config,
        input=legacy_payload.input,
        latest_version_id=legacy_payload.latest_version_id,
        execution_timeout_seconds=legacy_payload.execution_timeout_seconds,
        secrets=legacy_payload.secrets,
        index_url=legacy_payload.index_url,
        builtin_package_snapshot=legacy_payload.builtin_package_snapshot,
        dependency_check=legacy_payload.dependency_check,
        locale=cast(Any, legacy_payload.locale),
        resource_profile=profile,
        credential_bindings=list(execution.credential_bindings_snapshot),
        input_source_type=cast(Any, execution.input_source_type),
        input_snapshot=dict(execution.input_snapshot),
        input_files=legacy_payload.input_files,
        recovery_grace_seconds_snapshot=execution.recovery_grace_seconds_snapshot,
        workspace_cleanup_attempt_timeout_seconds_snapshot=(
            execution.workspace_cleanup_attempt_timeout_seconds_snapshot
        ),
        workspace_cleanup_total_timeout_seconds_snapshot=(
            execution.workspace_cleanup_total_timeout_seconds_snapshot
        ),
    )


def _slot(session: Session, adapter_id: int) -> AdapterExecutionSlot:
    slot = session.scalar(
        select(AdapterExecutionSlot)
        .where(AdapterExecutionSlot.adapter_id == adapter_id, AdapterExecutionSlot.slot_no == 0)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if slot is None:
        slot = AdapterExecutionSlot(adapter_id=adapter_id, slot_no=0)
        session.add(slot)
        session.flush()
    return slot


def claim_dispatch(
    session: Session, worker_id: int, raw_dispatch: Mapping[str, Any] | object
) -> ClaimDecision:
    """Validate one delivery and atomically create its Attempt, if eligible."""
    try:
        message = deserialize_dispatch_message(cast(Any, raw_dispatch))
    except (TypeError, ValueError, ValidationError):
        session.rollback()
        return _reject_dispatch(session, kind="dispatch_payload_invalid")

    worker = session.get(Worker, worker_id)
    if worker is None:
        return _decision("PAUSE_CONSUMER", "worker_not_registered", retry_after_seconds=5)
    if int(worker.protocol_version or 1) != 3:
        return _decision("PAUSE_CONSUMER", "worker_protocol_incompatible", retry_after_seconds=30)
    if not bool(worker.rabbitmq_execution_v3):
        return _decision("PAUSE_CONSUMER", "isolation_capability_missing", retry_after_seconds=30)
    if message.target_worker_id != worker_id:
        return _reject_dispatch(
            session, kind="dispatch_target_mismatch", message_id=message.message_id
        )

    identity = session.execute(
        select(
            Execution.adapter_id,
            Execution.dispatch_backend,
            Execution.version_id,
            Execution.target_worker_id_snapshot,
        ).where(Execution.id == message.execution_id)
    ).one_or_none()
    if identity is None:
        return _reject_dispatch(
            session,
            kind="execution_not_found",
            message_id=message.message_id,
            execution_id=message.execution_id,
            dispatch_generation=message.dispatch_generation,
        )
    adapter_id, dispatch_backend, version_id, target_worker_id_snapshot = identity
    if dispatch_backend == "rabbitmq":
        admission_scope = admission.lock_admission_scope(session, int(adapter_id))
        adapter = session.get(Adapter, int(adapter_id)) if admission_scope is not None else None
    else:
        adapter = session.get(Adapter, int(adapter_id), with_for_update=True)
    if adapter is None:
        return _reject_dispatch(
            session,
            kind="adapter_not_found",
            message_id=message.message_id,
            execution_id=message.execution_id,
            dispatch_generation=message.dispatch_generation,
        )
    try:
        cache_governance.ensure_reference_allowed(
            session,
            worker_id=int(target_worker_id_snapshot or worker_id),
            adapter_id=int(adapter_id),
            version_id=int(version_id),
        )
    except HTTPException as error:
        if _http_error_code(error) != "cache_reclamation_in_progress":
            raise
        session.rollback()
        return _decision("PAUSE_CONSUMER", "cache_reclamation_in_progress", retry_after_seconds=1)
    execution = session.get(
        Execution, message.execution_id, with_for_update=True, populate_existing=True
    )
    if execution is None:
        return _reject_dispatch(
            session,
            kind="execution_not_found",
            message_id=message.message_id,
            execution_id=message.execution_id,
            dispatch_generation=message.dispatch_generation,
        )
    now = database_now(session)
    if execution.dispatch_backend != "rabbitmq":
        return _reject_dispatch(
            session,
            kind="execution_backend_mismatch",
            message_id=message.message_id,
            execution_id=execution.id,
            dispatch_generation=message.dispatch_generation,
        )
    if message.dispatch_generation < execution.dispatch_generation:
        session.rollback()
        return _decision("ACK_NOOP", "stale_generation")
    if message.dispatch_generation > execution.dispatch_generation:
        return _reject_dispatch(
            session,
            kind="future_generation",
            message_id=message.message_id,
            execution_id=execution.id,
            dispatch_generation=message.dispatch_generation,
        )
    if execution.status in {"cancelled", "succeeded", "dead_letter", "expired"}:
        session.rollback()
        return _decision("ACK_NOOP", "cancelled" if execution.status == "cancelled" else "terminal")
    if (
        execution.builtin_package_snapshot is not None
        and worker.isolation_capabilities.get("builtin_packages_v1") is not True
    ):
        session.rollback()
        return _decision(
            "PAUSE_CONSUMER", "builtin_worker_upgrade_required", retry_after_seconds=30
        )
    if execution.status == "retry_wait":
        next_attempt_at = execution.next_attempt_at
        if next_attempt_at is None:
            delay = 1
        else:
            due = _utc(next_attempt_at) <= _utc(now)
            delay = 1 if due else max(1, int((_utc(next_attempt_at) - _utc(now)).total_seconds()))
        session.rollback()
        return _decision("ACK_NOOP", "retry_not_due", retry_after_seconds=min(delay, 86_400))
    if execution.status != "queued":
        session.rollback()
        return _decision("ACK_NOOP", "execution_not_queued")
    if execution.target_worker_id != worker_id or execution.target_worker_id_snapshot != worker_id:
        return _reject_dispatch(
            session,
            kind="target_worker_mismatch",
            message_id=message.message_id,
            execution_id=execution.id,
            dispatch_generation=message.dispatch_generation,
        )
    if execution.adapter_id != message.adapter_id or adapter.language != message.language:
        return _reject_dispatch(
            session,
            kind="dispatch_fact_mismatch",
            message_id=message.message_id,
            execution_id=execution.id,
            dispatch_generation=message.dispatch_generation,
        )
    if execution.resource_class != message.resource_class:
        return _reject_dispatch(
            session,
            kind="resource_class_mismatch",
            message_id=message.message_id,
            execution_id=execution.id,
            dispatch_generation=message.dispatch_generation,
        )
    if execution.next_attempt_at is not None and _utc(execution.next_attempt_at) > _utc(now):
        delay = max(1, int((_utc(execution.next_attempt_at) - _utc(now)).total_seconds()))
        session.rollback()
        return _decision("ACK_NOOP", "retry_not_due", retry_after_seconds=min(delay, 86_400))

    locked_attempts = lock_execution_attempts(session, execution.id)
    slot = _slot(session, adapter.id)
    if execution.cancel_requested:
        active_attempt = next(
            (attempt for attempt in locked_attempts if attempt.status in ACTIVE_ATTEMPT_STATUSES),
            None,
        )
        if active_attempt is not None:
            session.rollback()
            return _decision(
                "ACK_NOOP",
                "cancel_requested",
                attempt_id=active_attempt.id,
                cancel_requested=True,
            )
        incidents, _outbox_rows = lock_incidents_and_outbox(session, execution.id)
        execution.status = "cancelled"
        execution.ended_at = now
        execution.error_code = CANCELLATION_ERROR_CODE
        execution.last_error_code = CANCELLATION_ERROR_CODE
        admission.release_admission_once(session, execution, now=now)
        execution_service.release_execution_leases(session, execution.id)
        from dlr.control.services.incident_disposition import (
            converge_terminal_dispositions_locked,
        )

        converge_terminal_dispositions_locked(session, execution, incidents, now=now)
        session.commit()
        return _decision("ACK_NOOP", "cancelled")

    if slot.active_attempt_id is not None:
        active = next(
            (attempt for attempt in locked_attempts if attempt.id == slot.active_attempt_id),
            None,
        )
        if active is None:
            # Do not lock or clear another Execution's active Attempt after Slot.
            active = session.get(ExecutionAttempt, slot.active_attempt_id)
        if active is not None and active.status in ACTIVE_ATTEMPT_STATUSES:
            if _utc(active.lease_expires_at) > _utc(now):
                session.rollback()
                return _decision("DEFER", "adapter_slot_busy", retry_after_seconds=1)
            session.rollback()
            return _decision("DEFER", "slot_recovery_pending", retry_after_seconds=1)
        slot.active_attempt_id = None
        slot.lease_expires_at = None
    try:
        policy = _retry_policy(execution)
    except Exception:
        session.rollback()
        raise
    attempt_no = int(execution.attempt_count) + 1
    if attempt_no > int(policy["max_attempts"]):
        incidents, _outbox_rows = lock_incidents_and_outbox(session, execution.id)
        execution.status = "dead_letter"
        execution.last_error_code = "retry_exhausted"
        execution.ended_at = now
        _create_holds_locked(session, execution, now=now)
        execution_service.release_execution_leases(session, execution.id)
        admission.release_admission_once(session, execution, now=now)
        from dlr.control.services.incident_disposition import (
            converge_terminal_dispositions_locked,
        )

        converge_terminal_dispositions_locked(session, execution, incidents, now=now)
        session.commit()
        return _decision("ACK_NOOP", "retry_exhausted")

    # Execution cleanup fields describe the currently claimed v3 Attempt.
    # The authoritative raw token remains on that Attempt, so a previous
    # deferred journal can be acknowledged later without changing this state.
    execution.workspace_cleanup_status = "pending"
    execution.workspace_cleanup_error_code = None
    claim_token = generate_token()
    cleanup_token = generate_token()
    fence = max(int(slot.fencing_token) + 1, 1)
    attempt = ExecutionAttempt(
        execution_id=execution.id,
        adapter_id=adapter.id,
        attempt_no=attempt_no,
        worker_id=worker_id,
        fencing_token=fence,
        claim_token_hash=hash_token(claim_token),
        cleanup_token_hash=hash_token(cleanup_token),
        lease_expires_at=now + timedelta(seconds=settings.attempt_lease_seconds),
        status="claimed",
        claimed_at=now,
    )
    session.add(attempt)
    session.flush()
    execution.attempt_count = attempt_no
    execution.status = "running"
    execution.worker_id = worker_id
    execution.started_at = now
    execution.next_attempt_at = None
    slot.active_attempt_id = attempt.id
    slot.lease_expires_at = attempt.lease_expires_at
    slot.fencing_token = fence
    try:
        payload = _build_v3_payload(session, execution, worker, attempt, claim_token, cleanup_token)
    except Exception:
        session.rollback()
        raise
    session.commit()
    ATTEMPT_METRICS["claim_execute"] += 1
    return _decision("EXECUTE", "claimed", attempt_id=attempt.id, payload=payload)


def _lock_attempt_context(
    session: Session, worker_id: int, attempt_id: int
) -> tuple[Execution, Adapter, ExecutionAttempt, AdapterExecutionSlot]:
    execution_id = session.scalar(
        select(ExecutionAttempt.execution_id).where(ExecutionAttempt.id == attempt_id)
    )
    if execution_id is None:
        raise domain_error(404, "attempt_not_found", "Attempt not found")
    identity = session.execute(
        select(Execution.adapter_id, Execution.dispatch_backend).where(Execution.id == execution_id)
    ).one_or_none()
    if identity is None:
        raise domain_error(404, "execution_not_found", "Execution not found")
    adapter_id, dispatch_backend = identity
    if (
        dispatch_backend == "rabbitmq"
        and admission.lock_admission_scope(session, int(adapter_id)) is None
    ):
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    execution = session.get(Execution, execution_id, with_for_update=True, populate_existing=True)
    if execution is None:
        raise domain_error(404, "execution_not_found", "Execution not found")
    adapter = session.get(
        Adapter, execution.adapter_id, with_for_update=True, populate_existing=True
    )
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    # Lock every Attempt in ascending order before Slot.  This preserves the
    # shared cancellation/disposition order and refreshes any auth-read cache.
    attempts = lock_execution_attempts(session, execution.id)
    attempt = next((row for row in attempts if row.id == attempt_id), None)
    if attempt is None:
        raise domain_error(404, "attempt_not_found", "Attempt not found")
    slot = _slot(session, adapter.id)
    if attempt.worker_id != worker_id:
        raise domain_error(409, "attempt_not_owned", "Attempt is assigned to another Worker")
    return execution, adapter, attempt, slot


def _validate_action(
    execution: Execution,
    attempt: ExecutionAttempt,
    payload: AttemptActionBody,
    now: datetime,
) -> None:
    if int(payload.attempt_id) != int(attempt.id):
        raise domain_error(409, "attempt_id_mismatch", "Attempt identity does not match")
    if not token_matches(payload.claim_token, attempt.claim_token_hash):
        raise domain_error(422, "attempt_token_invalid", "Attempt Claim Token is invalid")
    if int(payload.fencing_token) != int(attempt.fencing_token):
        raise domain_error(409, "attempt_stale_fence", "Attempt fencing token is stale")
    if attempt.status in ACTIVE_ATTEMPT_STATUSES and _utc(attempt.lease_expires_at) <= _utc(now):
        raise domain_error(409, "attempt_lease_expired", "Attempt Lease has expired")
    if execution.worker_id != attempt.worker_id:
        raise domain_error(409, "attempt_stale_fence", "Attempt ownership is stale")


def start_attempt(
    session: Session, worker_id: int, attempt_id: int, payload: AttemptStartBody
) -> ClaimDecision:
    execution, _adapter, attempt, _slot_row = _lock_attempt_context(session, worker_id, attempt_id)
    now = database_now(session)
    _validate_action(execution, attempt, payload, now)
    if execution.cancel_requested and attempt.status == "claimed":
        result = _apply_terminal_locked(
            session,
            execution,
            attempt,
            _slot_row,
            status="cancelled",
            error_code=CANCELLATION_ERROR_CODE,
            error_class="cancelled",
            error="Execution was cancelled before Adapter start",
            now=now,
        )
        session.commit()
        del result
        return _decision(
            "ACK_NOOP",
            "cancel_requested",
            attempt_id=attempt.id,
            cancel_requested=True,
        )
    if execution.cancel_requested:
        session.rollback()
        return _decision(
            "ACK_NOOP", "cancel_requested", attempt_id=attempt.id, cancel_requested=True
        )
    if attempt.status == "running":
        session.rollback()
        return _decision("ACK_NOOP", "already_started", attempt_id=attempt.id)
    if attempt.status in TERMINAL_ATTEMPT_STATUSES:
        session.rollback()
        return _decision("ACK_NOOP", "already_terminal", attempt_id=attempt.id)
    if attempt.status != "claimed":
        raise domain_error(409, "attempt_transition_invalid", "Attempt cannot start")
    attempt.status = "running"
    attempt.started_at = now
    session.commit()
    return _decision("ACK_NOOP", "started", attempt_id=attempt.id)


def renew_attempt(
    session: Session, worker_id: int, attempt_id: int, payload: AttemptRenewBody
) -> ClaimDecision:
    execution, _adapter, attempt, slot = _lock_attempt_context(session, worker_id, attempt_id)
    now = database_now(session)
    _validate_action(execution, attempt, payload, now)
    if attempt.status in TERMINAL_ATTEMPT_STATUSES:
        session.rollback()
        return _decision("ACK_NOOP", "already_terminal", attempt_id=attempt.id)
    if execution.cancel_requested:
        session.rollback()
        return _decision(
            "ACK_NOOP", "cancel_requested", attempt_id=attempt.id, cancel_requested=True
        )
    lease = now + timedelta(seconds=settings.attempt_lease_seconds)
    attempt.lease_expires_at = lease
    if slot.active_attempt_id == attempt.id and slot.fencing_token == attempt.fencing_token:
        slot.lease_expires_at = lease
    session.commit()
    return _decision("ACK_NOOP", "renewed", attempt_id=attempt.id)


def progress_attempt(
    session: Session, worker_id: int, attempt_id: int, payload: AttemptProgressBody
) -> ClaimDecision:
    execution, _adapter, attempt, _slot_row = _lock_attempt_context(session, worker_id, attempt_id)
    now = database_now(session)
    _validate_action(execution, attempt, payload, now)
    if attempt.status in TERMINAL_ATTEMPT_STATUSES:
        session.rollback()
        return _decision("ACK_NOOP", "already_terminal", attempt_id=attempt.id)
    if execution.cancel_requested:
        session.rollback()
        return _decision(
            "ACK_NOOP", "cancel_requested", attempt_id=attempt.id, cancel_requested=True
        )
    # Attempt rows intentionally retain only bounded summaries. Live output is
    # owned by the existing Execution progress contract, not an unbounded JSON
    # array on the Attempt table.
    if payload.stdout_chunk or payload.stderr_chunk:
        execution.stdout, execution.stdout_truncated = execution_service.append_stream(
            execution.stdout, execution.stdout_truncated, payload.stdout_chunk
        )
        execution.stderr, execution.stderr_truncated = execution_service.append_stream(
            execution.stderr, execution.stderr_truncated, payload.stderr_chunk
        )
        attempt.output_summary = {
            "progress": True,
            "stdout_bytes": len(payload.stdout_chunk.encode()),
            "stderr_bytes": len(payload.stderr_chunk.encode()),
        }
    session.commit()
    return _decision("ACK_NOOP", "progressed", attempt_id=attempt.id)


def _release_slot_locked(slot: AdapterExecutionSlot, attempt: ExecutionAttempt) -> bool:
    if slot.active_attempt_id != attempt.id or slot.fencing_token != attempt.fencing_token:
        return False
    slot.active_attempt_id = None
    slot.lease_expires_at = None
    return True


def _create_holds_locked(session: Session, execution: Execution, *, now: datetime) -> int:
    if execution.input_source_type != "managed_files":
        return 0
    artifacts = list(
        session.scalars(
            select(ManagedInputArtifact)
            .join(
                ExecutionInputArtifactLease,
                ExecutionInputArtifactLease.artifact_id == ManagedInputArtifact.id,
            )
            .where(ExecutionInputArtifactLease.execution_id == execution.id)
            .order_by(ExecutionInputArtifactLease.ordinal)
        )
    )
    created = 0
    expires = now + timedelta(seconds=settings.dead_letter_hold_seconds)
    for artifact in artifacts:
        artifact_id = artifact.id
        existing = session.scalar(
            select(ExecutionArtifactHold).where(
                ExecutionArtifactHold.execution_id == execution.id,
                ExecutionArtifactHold.artifact_id == artifact_id,
                ExecutionArtifactHold.reason == "dead_letter_replay",
            )
        )
        if existing is not None:
            if existing.purged_at is None and existing.held_bytes == 0 and artifact.size_bytes > 0:
                existing.held_bytes = artifact.size_bytes
            if existing.purged_at is None and _utc(existing.expires_at) < _utc(expires):
                existing.expires_at = expires
            continue
        session.add(
            ExecutionArtifactHold(
                execution_id=execution.id,
                artifact_id=artifact_id,
                reason="dead_letter_replay",
                expires_at=expires,
                held_bytes=artifact.size_bytes,
            )
        )
        created += 1
    return created


def _apply_terminal_locked(
    session: Session,
    execution: Execution,
    attempt: ExecutionAttempt,
    slot: AdapterExecutionSlot,
    *,
    status: str,
    error_code: str,
    error_class: str,
    error: str | None,
    output: Any = None,
    output_size: int | None = None,
    output_truncated: bool = False,
    output_preview: str | None = None,
    stdout: str = "",
    stderr: str = "",
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    workspace_cleanup_status: str | None = None,
    workspace_cleanup_error_code: str | None = None,
    resource_usage: dict[str, Any] | None = None,
    cleanup_summary: dict[str, Any] | None = None,
    now: datetime,
) -> ClaimDecision:
    if attempt.status in TERMINAL_ATTEMPT_STATUSES:
        return _decision("ACK_NOOP", "already_terminal", attempt_id=attempt.id)
    incidents, _outbox_rows = lock_incidents_and_outbox(session, execution.id)
    attempt.status = status
    attempt.ended_at = now
    attempt.error_code = error_code[:64]
    attempt.resource_usage_json = dict(resource_usage or {})
    attempt.cleanup_summary = dict(cleanup_summary or {})
    attempt.output_summary = {
        "output_size": output_size,
        "output_truncated": bool(output_truncated),
        "has_output": output is not None,
    }
    execution.last_error_code = error_code[:64]
    execution.error_code = error_code[:64]
    execution.error = error
    execution.output = output
    execution.output_size = output_size
    execution.output_truncated = output_truncated
    execution.output_preview = output_preview
    capped_stdout, stdout_capped = truncate_utf8(
        stdout.encode(), settings.execution_stream_max_bytes
    )
    capped_stderr, stderr_capped = truncate_utf8(
        stderr.encode(), settings.execution_stream_max_bytes
    )
    execution.stdout = capped_stdout.decode("utf-8", errors="replace")
    execution.stderr = capped_stderr.decode("utf-8", errors="replace")
    execution.stdout_truncated = stdout_truncated or stdout_capped
    execution.stderr_truncated = stderr_truncated or stderr_capped
    if execution.dispatch_backend == "rabbitmq":
        # v3 reports the local cleanup result with the Attempt result.  A
        # completed result is still idempotently receipt-acknowledged by
        # Control; a deferred result remains the durable recovery boundary.
        effective_cleanup_status = workspace_cleanup_status or "deferred"
        effective_cleanup_error_code = (
            workspace_cleanup_error_code if effective_cleanup_status == "deferred" else None
        )
        if effective_cleanup_status == "deferred" and effective_cleanup_error_code is None:
            effective_cleanup_error_code = "workspace_cleanup_failed"
        execution.workspace_cleanup_status = effective_cleanup_status
        execution.workspace_cleanup_error_code = effective_cleanup_error_code
        attempt.cleanup_summary["workspace_cleanup_status"] = effective_cleanup_status
        if effective_cleanup_error_code is not None:
            attempt.cleanup_summary["workspace_cleanup_error_code"] = effective_cleanup_error_code
        else:
            attempt.cleanup_summary.pop("workspace_cleanup_error_code", None)
    if output_preview is not None:
        preview_bytes = output_preview.encode()[: settings.execution_output_preview_max_bytes]
        output_preview = preview_bytes.decode("utf-8", errors="ignore")
        execution.output_preview = output_preview
    if status == "cancelled":
        attempt.error_code = CANCELLATION_ERROR_CODE
    if execution.cancel_requested or status == "cancelled":
        execution.status = "cancelled"
        execution.error_code = CANCELLATION_ERROR_CODE
        execution.last_error_code = CANCELLATION_ERROR_CODE
        final = True
    elif status == "succeeded":
        execution.status = "succeeded"
        execution.error = None
        execution.error_code = None
        execution.last_error_code = None
        final = True
    else:
        policy = _retry_policy(execution)
        can_retry = error_class in policy["retryable_error_classes"] and attempt.attempt_no < int(
            policy["max_attempts"]
        )
        if can_retry:
            execution.status = "retry_wait"
            execution.next_attempt_at = now + timedelta(
                seconds=retry_delay_seconds(execution, attempt.attempt_no)
            )
            final = False
        else:
            execution.status = "dead_letter"
            execution.next_attempt_at = None
            _create_holds_locked(session, execution, now=now)
            final = True
    if final:
        execution.next_attempt_at = None
        execution.ended_at = now
        if execution.started_at is not None:
            execution.duration_ms = max(
                0,
                int((_utc(now) - _utc(execution.started_at)).total_seconds() * 1000),
            )
        execution_service.release_execution_leases(session, execution.id)
        admission.release_admission_once(session, execution, now=now)
    _release_slot_locked(slot, attempt)
    if final:
        from dlr.control.services.incident_disposition import (
            converge_terminal_dispositions_locked,
        )

        converge_terminal_dispositions_locked(session, execution, incidents, now=now)
    ATTEMPT_METRICS[f"terminal_{status}"] += 1
    return _decision("ACK_NOOP", "terminal_recorded", attempt_id=attempt.id)


def finish_attempt(
    session: Session, worker_id: int, attempt_id: int, payload: AttemptResultBody
) -> ClaimDecision:
    execution, _adapter, attempt, slot = _lock_attempt_context(session, worker_id, attempt_id)
    now = database_now(session)
    _validate_action(execution, attempt, payload, now)
    if attempt.status in TERMINAL_ATTEMPT_STATUSES:
        session.rollback()
        return _decision("ACK_NOOP", "already_terminal", attempt_id=attempt.id)
    if payload.status == "succeeded":
        error_class = ""
    elif payload.status == "cancelled":
        error_class = "cancelled"
    elif payload.status == "timed_out":
        error_class = "timeout"
    elif payload.status == "resource_exceeded":
        error_class = "resource_exceeded"
    else:
        error_class = payload.error_class or "business_error"
    error_code = payload.error_code or (
        "execution_succeeded" if payload.status == "succeeded" else "attempt_failed"
    )
    normalized_output, normalized_size, normalized_truncated, normalized_preview = (
        execution_service._normalize_output(cast(ExecutionResultReport, payload))
    )
    result = _apply_terminal_locked(
        session,
        execution,
        attempt,
        slot,
        status=payload.status,
        error_code=error_code,
        error_class=error_class,
        error=payload.error,
        output=normalized_output,
        output_size=normalized_size,
        output_truncated=normalized_truncated,
        output_preview=normalized_preview,
        stdout=payload.stdout,
        stderr=payload.stderr,
        stdout_truncated=payload.stdout_truncated,
        stderr_truncated=payload.stderr_truncated,
        workspace_cleanup_status=payload.workspace_cleanup_status,
        workspace_cleanup_error_code=payload.workspace_cleanup_error_code,
        resource_usage=payload.resource_usage,
        cleanup_summary=payload.cleanup_summary,
        now=now,
    )
    session.commit()
    return result


def prepare_failed(
    session: Session, worker_id: int, attempt_id: int, payload: AttemptPrepareFailedBody
) -> ClaimDecision:
    execution, _adapter, attempt, slot = _lock_attempt_context(session, worker_id, attempt_id)
    now = database_now(session)
    _validate_action(execution, attempt, payload, now)
    result = _apply_terminal_locked(
        session,
        execution,
        attempt,
        slot,
        status="failed",
        error_code=payload.error_code,
        error_class=payload.error_class,
        error=payload.error,
        now=now,
    )
    session.commit()
    return result


def _http_error_code(error: HTTPException) -> str | None:
    detail = error.detail
    if not isinstance(detail, dict):
        return None
    code = detail.get("code")
    return code if isinstance(code, str) else None


def _active_recovery_upper_id(session: Session) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.max(ExecutionAttempt.id), 0)).where(
                ExecutionAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES)
            )
        )
        or 0
    )


def _recovery_candidate_ids(
    session: Session, *, after_id: int, upper_id: int, limit: int
) -> tuple[int, ...]:
    if upper_id <= after_id:
        return ()
    return tuple(
        int(value)
        for value in session.scalars(
            select(ExecutionAttempt.id)
            .where(
                ExecutionAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
                ExecutionAttempt.id > after_id,
                ExecutionAttempt.id <= upper_id,
            )
            .order_by(ExecutionAttempt.id)
            .limit(limit)
        )
    )


def _set_reconciliation_lock_timeout(session: Session) -> None:
    """Bound only this reconciliation transaction's PostgreSQL lock waits."""
    session.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": f"{RECONCILIATION_LOCK_TIMEOUT_MS}ms"},
    )


def reserve_recovery_candidates(
    session: Session, *, limit: int = 100
) -> RecoveryCandidateReservation:
    """Commit one bounded active-Attempt cursor segment before processing it."""
    batch_size = max(1, min(int(limit), 1_000))
    try:
        _set_reconciliation_lock_timeout(session)
        cursor = session.scalar(
            select(RuntimeReconciliationCursor)
            .where(RuntimeReconciliationCursor.name == RECONCILIATION_CURSOR_NAME)
            .with_for_update()
        )
        if cursor is None:
            raise RuntimeError("expired Attempt reconciliation cursor is missing")

        wrapped = False
        if int(cursor.upper_id) == 0:
            cursor.after_id = 0
            cursor.upper_id = _active_recovery_upper_id(session)
        attempt_ids = _recovery_candidate_ids(
            session,
            after_id=int(cursor.after_id),
            upper_id=int(cursor.upper_id),
            limit=batch_size,
        )
        if not attempt_ids:
            wrapped = True
            cursor.after_id = 0
            cursor.upper_id = _active_recovery_upper_id(session)
            attempt_ids = _recovery_candidate_ids(
                session,
                after_id=0,
                upper_id=int(cursor.upper_id),
                limit=batch_size,
            )
        if attempt_ids:
            cursor.after_id = attempt_ids[-1]
        reservation = RecoveryCandidateReservation(
            attempt_ids=attempt_ids,
            after_id=int(cursor.after_id),
            upper_id=int(cursor.upper_id),
            wrapped=wrapped,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    return reservation


def _validate_recovery_snapshots(execution: Execution, *, attempt_id: int) -> None:
    for validator in (_retry_policy, _load_profile):
        try:
            validator(execution)
        except HTTPException as error:
            code = _http_error_code(error)
            if code in RECONCILIATION_ROW_ERROR_CODES:
                raise ReconciliationRowError(
                    code,
                    attempt_id=attempt_id,
                    execution_id=execution.id,
                ) from None
            raise


def _recover_reserved_attempt(
    session: Session,
    attempt_id: int,
    *,
    now: datetime | None,
) -> bool:
    _set_reconciliation_lock_timeout(session)
    identity = session.execute(
        select(ExecutionAttempt.worker_id, ExecutionAttempt.execution_id).where(
            ExecutionAttempt.id == attempt_id
        )
    ).one_or_none()
    if identity is None:
        session.rollback()
        return False
    worker_id, _execution_id = identity
    try:
        execution, _adapter, attempt, slot = _lock_attempt_context(
            session, int(worker_id), attempt_id
        )
    except HTTPException as error:
        session.rollback()
        if _http_error_code(error) in RECONCILIATION_MISSING_CODES:
            return False
        raise
    locked_now = _utc(now or database_now(session))
    if attempt.status not in ACTIVE_ATTEMPT_STATUSES or _utc(attempt.lease_expires_at) > locked_now:
        session.rollback()
        return False
    _validate_recovery_snapshots(execution, attempt_id=attempt.id)
    _apply_terminal_locked(
        session,
        execution,
        attempt,
        slot,
        status="worker_lost",
        error_code="worker_lost",
        error_class="worker_lost",
        error="Worker Lease expired before a terminal result was accepted",
        now=locked_now,
    )
    session.commit()
    return True


def recover_expired_attempts(
    session: Session, *, limit: int = 100, now: datetime | None = None
) -> int:
    """Fence expired Attempts from one committed, restart-safe cursor segment."""
    reservation = reserve_recovery_candidates(session, limit=limit)
    recovered = 0
    for candidate_index, attempt_id in enumerate(reservation.attempt_ids, start=1):
        try:
            recovered += int(_recover_reserved_attempt(session, attempt_id, now=now))
        except ReconciliationRowError as error:
            session.rollback()
            metric = f"reconciliation_row_error_{error.code}"
            ATTEMPT_METRICS[metric] += 1
            logger.warning(
                "attempt reconciliation row skipped: attempt_id=%s execution_id=%s "
                "error_code=%s cursor_upper_id=%s wrapped=%s "
                "index=%s count=%s error_count=%s",
                error.attempt_id,
                error.execution_id,
                error.code,
                reservation.upper_id,
                reservation.wrapped,
                candidate_index,
                len(reservation.attempt_ids),
                ATTEMPT_METRICS[metric],
            )
        except Exception:
            session.rollback()
            raise
    if recovered:
        ATTEMPT_METRICS["lease_recovered"] += recovered
    return recovered


def retry_dispatcher_once(
    session: Session, *, limit: int = 100, now: datetime | None = None
) -> int:
    """Move due retry_wait rows to a unique next generation and Outbox."""
    effective_now = _utc(now or database_now(session))
    candidates = list(
        session.execute(
            select(
                Execution.id,
                Execution.adapter_id,
                Execution.version_id,
                Execution.target_worker_id_snapshot,
                Execution.target_worker_id,
            )
            .where(
                Execution.dispatch_backend == "rabbitmq",
                Execution.status == "retry_wait",
                Execution.next_attempt_at <= effective_now,
            )
            .order_by(Execution.next_attempt_at, Execution.id)
            .limit(max(1, min(int(limit), 1_000)))
        )
    )
    dispatched = 0
    for execution_id, adapter_id, version_id, target_snapshot, target_worker in candidates:
        adapter = session.scalar(
            select(Adapter).where(Adapter.id == adapter_id).with_for_update(skip_locked=True)
        )
        if adapter is None:
            session.rollback()
            continue
        try:
            cache_governance.ensure_reference_allowed(
                session,
                worker_id=int(target_snapshot or target_worker),
                adapter_id=int(adapter_id),
                version_id=int(version_id),
            )
        except HTTPException as error:
            if _http_error_code(error) != "cache_reclamation_in_progress":
                raise
            session.rollback()
            continue
        execution = session.scalar(
            select(Execution).where(Execution.id == execution_id).with_for_update(skip_locked=True)
        )
        if execution is None or execution.status != "retry_wait":
            session.rollback()
            continue
        if execution.adapter_id != adapter.id:
            session.rollback()
            continue
        # Keep the injected clock authoritative for deterministic reconciliation
        # and preserve the production path, where ``effective_now`` already
        # comes from PostgreSQL.
        locked_now = effective_now
        if execution.next_attempt_at is not None and _utc(execution.next_attempt_at) > _utc(
            locked_now
        ):
            session.rollback()
            continue
        execution.status = "queued"
        execution.dispatch_generation += 1
        execution.queued_at = locked_now
        execution.next_attempt_at = None
        outbox.create_dispatch_outbox(session, execution, available_at=locked_now)
        session.commit()
        dispatched += 1
    if dispatched:
        ATTEMPT_METRICS["retry_dispatched"] += dispatched
    return dispatched


def execution_detail(
    session: Session, execution_id: int, *, can_edit: bool = True
) -> ReliableExecutionDetail:
    execution = session.get(Execution, execution_id)
    if execution is None:
        raise domain_error(404, "execution_not_found", "Execution not found")
    attempts = list(
        session.scalars(
            select(ExecutionAttempt)
            .where(ExecutionAttempt.execution_id == execution.id)
            .order_by(ExecutionAttempt.attempt_no)
        )
    )
    incidents = list(
        session.scalars(
            select(ExecutionInfrastructureIncident)
            .where(ExecutionInfrastructureIncident.execution_id == execution.id)
            .order_by(ExecutionInfrastructureIncident.id)
        )
    )
    dispositions = list(
        session.scalars(
            select(ExecutionIncidentDisposition)
            .where(ExecutionIncidentDisposition.execution_id == execution.id)
            .order_by(
                ExecutionIncidentDisposition.created_at.desc(),
                ExecutionIncidentDisposition.id.desc(),
            )
        )
    )
    dispositions_by_incident: dict[int, list[ExecutionIncidentDisposition]] = {}
    for disposition in dispositions:
        dispositions_by_incident.setdefault(disposition.incident_id, []).append(disposition)
    current_outbox = session.scalar(
        select(ExecutionOutbox).where(
            ExecutionOutbox.execution_id == execution.id,
            ExecutionOutbox.dispatch_generation == execution.dispatch_generation,
        )
    )
    active_attempts = any(item.status in ACTIVE_ATTEMPT_STATUSES for item in attempts)
    from dlr.control.services.incident_disposition import inspect_incident_disposition

    replay_available, replay_reason = _replay_availability(session, execution)
    return ReliableExecutionDetail(
        execution_id=execution.id,
        dispatch_backend=cast(Any, execution.dispatch_backend),
        status=execution.status,
        attempts=[
            AttemptSummary(
                id=item.id,
                execution_id=item.execution_id,
                adapter_id=item.adapter_id,
                attempt_no=item.attempt_no,
                worker_id=item.worker_id,
                fencing_token=item.fencing_token,
                lease_expires_at=item.lease_expires_at,
                status=item.status,
                claimed_at=item.claimed_at,
                started_at=item.started_at,
                ended_at=item.ended_at,
                error_code=item.error_code,
                resource_usage_json=item.resource_usage_json,
                output_summary=item.output_summary,
                cleanup_summary=item.cleanup_summary,
            )
            for item in attempts
        ],
        incidents=[
            InfrastructureIncidentSummary(
                id=incident.id,
                execution_id=execution.id,
                dispatch_generation=incident.dispatch_generation,
                message_id=incident.message_id,
                kind=incident.kind,
                status=cast(Any, incident.status),
                attempts=incident.attempts,
                observation_count=incident.attempts,
                disposition_count=len(dispositions_by_incident.get(incident.id, [])),
                recovery_dispatch_count=sum(
                    item.outcome == "recovery_dispatched"
                    for item in dispositions_by_incident.get(incident.id, [])
                ),
                last_error=incident.last_error,
                created_at=incident.created_at,
                resolved_at=incident.resolved_at,
                recent_disposition=(
                    IncidentDispositionReceipt.model_validate(
                        dispositions_by_incident[incident.id][0]
                    )
                    if dispositions_by_incident.get(incident.id)
                    else None
                ),
                dispositions_url=(
                    f"/api/executions/{execution.id}/incidents/{incident.id}/dispositions"
                ),
                **inspect_incident_disposition(
                    execution,
                    incident,
                    active_attempts=active_attempts,
                    current_outbox=current_outbox,
                    can_edit=can_edit,
                ).__dict__,
            )
            for incident in incidents
        ],
        replay_available=replay_available,
        replay_reason=replay_reason,
    )


def _replay_availability(session: Session, execution: Execution) -> tuple[bool, str | None]:
    if execution.dispatch_backend != "rabbitmq" or execution.status != "dead_letter":
        return False, "execution_not_dead_letter"
    if execution.input_source_type != "managed_files":
        return True, None
    holds = list(
        session.scalars(
            select(ExecutionArtifactHold).where(
                ExecutionArtifactHold.execution_id == execution.id,
                ExecutionArtifactHold.reason == "dead_letter_replay",
                ExecutionArtifactHold.purged_at.is_(None),
            )
        )
    )
    now = _utc(database_now(session))
    if not holds or any(_utc(hold.expires_at) <= now for hold in holds):
        return False, "dead_letter_input_expired"
    artifacts = list(
        session.scalars(
            select(ManagedInputArtifact).where(
                ManagedInputArtifact.id.in_([hold.artifact_id for hold in holds])
            )
        )
    )
    if len(artifacts) != len(holds) or any(
        artifact.status == ManagedInputArtifactStatus.DELETED or artifact.sha256 is None
        for artifact in artifacts
    ):
        return False, "dead_letter_input_expired"
    return True, None


def replay_execution(session: Session, execution_id: int) -> ReplayResponse:
    """Create a new accepted RabbitMQ Execution from a dead-letter snapshot."""
    identity = session.execute(
        select(
            Execution.adapter_id,
            Execution.version_id,
            Execution.dependency_check,
            Execution.target_worker_id_snapshot,
        ).where(Execution.id == execution_id)
    ).one_or_none()
    if identity is None:
        raise domain_error(404, "execution_not_found", "Execution not found")
    adapter_id, version_id, dependency_check, target_snapshot = identity
    adapter_id = int(adapter_id)
    if admission.lock_admission_scope(session, adapter_id) is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    adapter = session.get(Adapter, adapter_id)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    replay_worker_id = target_snapshot if dependency_check else adapter.runtime_worker_id
    if replay_worker_id is None:
        raise domain_error(409, "worker_unavailable", "No Worker is assigned")
    cache_governance.ensure_reference_allowed(
        session,
        worker_id=int(replay_worker_id),
        adapter_id=adapter_id,
        version_id=int(version_id),
    )
    old = session.get(Execution, execution_id, with_for_update=True)
    if old is None:
        raise domain_error(404, "execution_not_found", "Execution not found")
    if old.adapter_id != adapter_id:
        raise domain_error(409, "execution_replay_invalid", "Execution cannot be replayed")
    available, reason = _replay_availability(session, old)
    if not available:
        code = (
            "dead_letter_input_expired"
            if reason == "dead_letter_input_expired"
            else "execution_replay_invalid"
        )
        raise domain_error(409, code, "Execution cannot be replayed")
    version = session.scalar(
        select(AdapterVersion).where(
            AdapterVersion.id == old.version_id,
            AdapterVersion.adapter_id == old.adapter_id,
        )
    )
    if version is None:
        raise domain_error(409, "execution_replay_invalid", "Execution Revision is unavailable")
    artifact_ids: tuple[int, ...] = ()
    if old.input_source_type == "managed_files":
        hold_ids = list(
            session.scalars(
                select(ExecutionArtifactHold.artifact_id)
                .where(
                    ExecutionArtifactHold.execution_id == old.id,
                    ExecutionArtifactHold.reason == "dead_letter_replay",
                    ExecutionArtifactHold.purged_at.is_(None),
                )
                .order_by(ExecutionArtifactHold.id)
            )
        )
        artifact_ids = tuple(int(value) for value in hold_ids)
    from dlr.control.services.reliable_execution import accept_execution

    new_execution = accept_execution(
        session,
        adapter,
        trigger=old.trigger,
        runtime_input=old.input,
        input_source_type=old.input_source_type,
        input_config_revision=old.input_config_revision,
        input_snapshot=dict(old.input_snapshot),
        artifact_ids=artifact_ids,
        scheduled_for=old.scheduled_for,
        schedule_policy_snapshot=(
            dict(old.schedule_policy_snapshot)
            if isinstance(old.schedule_policy_snapshot, dict)
            else None
        ),
        version_id=old.version_id,
        dependency_check=old.dependency_check,
        worker_id_override=(old.target_worker_id_snapshot if old.dependency_check else None),
    )
    new_execution.replay_of_execution_id = old.id
    session.commit()
    session.refresh(new_execution)
    return ReplayResponse(execution_id=new_execution.id, replay_of_execution_id=old.id)


def held_backlog(session: Session, *, now: datetime | None = None) -> tuple[int, int]:
    effective_now = _utc(now or database_now(session))
    count, total = session.execute(
        select(
            func.count(ExecutionArtifactHold.id),
            func.coalesce(func.sum(ExecutionArtifactHold.held_bytes), 0),
        ).where(
            ExecutionArtifactHold.purged_at.is_(None),
            ExecutionArtifactHold.expires_at > effective_now,
        )
    ).one()
    return int(count or 0), int(total or 0)


def ensure_managed_file_hold_capacity(session: Session) -> None:
    count, total = held_backlog(session)
    if count >= settings.dead_letter_hold_max_count or total >= settings.dead_letter_hold_max_bytes:
        raise domain_error(
            429,
            "dead_letter_hold_full",
            "Managed-file replay hold capacity is full",
            {"retry_after": 60},
        )


def purge_holds(session: Session, execution_id: int, *, actor: str, reason: str) -> int:
    if not actor or len(actor) > 128:
        raise domain_error(403, "account_admin_required", "Administrator privileges are required")
    old = session.get(Execution, execution_id, with_for_update=True)
    if old is None:
        raise domain_error(404, "execution_not_found", "Execution not found")
    if old.status != "dead_letter":
        raise domain_error(409, "execution_replay_invalid", "Only dead-letter holds can be purged")
    now = database_now(session)
    rows = list(
        session.scalars(
            select(ExecutionArtifactHold)
            .where(
                ExecutionArtifactHold.execution_id == execution_id,
                ExecutionArtifactHold.purged_at.is_(None),
            )
            .with_for_update()
        )
    )
    for row in rows:
        row.purged_at = now
        row.purged_by = actor
        row.purge_reason = reason[:256]
    session.commit()
    return len(rows)


def expire_holds(session: Session, *, limit: int = 100, now: datetime | None = None) -> int:
    effective_now = _utc(now or database_now(session))
    rows = list(
        session.scalars(
            select(ExecutionArtifactHold)
            .where(
                ExecutionArtifactHold.purged_at.is_(None),
                ExecutionArtifactHold.expires_at <= effective_now,
            )
            .order_by(ExecutionArtifactHold.expires_at, ExecutionArtifactHold.id)
            .limit(max(1, min(limit, 1_000)))
            .with_for_update(skip_locked=True)
        )
    )
    for row in rows:
        row.purged_at = effective_now
        row.purged_by = "system"
        row.purge_reason = "hold_expired"
    if rows:
        session.commit()
    else:
        session.rollback()
    return len(rows)


def attempt_reconciler_once(session: Session, *, limit: int = 100) -> dict[str, int]:
    recovered = recover_expired_attempts(session, limit=limit)
    dispatched = retry_dispatcher_once(session, limit=limit)
    holds = expire_holds(session, limit=limit)
    return {"recovered": recovered, "retry_dispatched": dispatched, "holds_expired": holds}


def _attempt_reconcile_tick() -> dict[str, int]:
    """Run blocking database reconciliation outside the asyncio event loop."""
    from dlr.control.db import SessionLocal

    with SessionLocal() as session:
        return attempt_reconciler_once(session)


async def attempt_reconciler_loop() -> None:
    """Run bounded Attempt/Retry/Hold reconciliation in fresh DB sessions."""
    while True:
        try:
            await _asyncio_to_thread(_attempt_reconcile_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("attempt reconciliation failed")
        await _asyncio_sleep(settings.attempt_reconcile_interval_seconds)
