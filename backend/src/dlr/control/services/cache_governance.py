"""PostgreSQL authority for Worker cache references and destructive guards."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from dlr.control.models import (
    Adapter,
    AdapterExecutionSlot,
    AdapterVersion,
    Execution,
    ExecutionArtifactHold,
    ExecutionAttempt,
    ExecutionInfrastructureIncident,
    Worker,
    WorkerCacheGuard,
    WorkerCacheOperation,
    WorkerCleanupRequest,
)
from dlr.control.services.adapter import domain_error
from dlr.control.services.input_config import database_now
from dlr.control.services.worker_protocol import token_matches

ACTIVE_EXECUTIONS = frozenset({"queued", "running", "retry_wait"})
ACTIVE_ATTEMPTS = frozenset({"claimed", "running"})
MAX_REFERENCE_RECORDS = 1_024


@dataclass(frozen=True)
class CacheReferenceFacts:
    protected: bool
    reasons: tuple[str, ...]


def _ensure_guard(
    session: Session, *, worker_id: int, adapter_id: int, version_id: int
) -> WorkerCacheGuard:
    session.execute(
        pg_insert(WorkerCacheGuard)
        .values(worker_id=worker_id, version_id=version_id, adapter_id=adapter_id)
        .on_conflict_do_nothing(
            index_elements=[WorkerCacheGuard.worker_id, WorkerCacheGuard.version_id]
        )
    )
    guard = session.scalar(
        select(WorkerCacheGuard)
        .where(
            WorkerCacheGuard.worker_id == worker_id,
            WorkerCacheGuard.version_id == version_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if guard is None:  # pragma: no cover - insert/select are one transaction
        raise RuntimeError("cache guard is unavailable")
    if guard.adapter_id != adapter_id:
        raise domain_error(
            409, "cache_guard_identity_conflict", "Cache guard identity is inconsistent"
        )
    return guard


def ensure_reference_allowed(
    session: Session, *, worker_id: int, adapter_id: int, version_id: int
) -> WorkerCacheGuard:
    """Lock the stable key before creating or reactivating a reference."""

    guard = _ensure_guard(
        session, worker_id=worker_id, adapter_id=adapter_id, version_id=version_id
    )
    if guard.phase != "idle":
        raise domain_error(
            409,
            "cache_reclamation_in_progress",
            "Cache reclamation is in progress",
            {"retry_after": 1},
        )
    return guard


def reference_facts(
    session: Session,
    *,
    worker_id: int,
    adapter_id: int,
    version_id: int,
    excluded_cleanup_id: int | None = None,
) -> CacheReferenceFacts:
    """Return complete durable reasons that keep one Worker/version cache alive."""

    now = database_now(session)
    worker_attempt = select(ExecutionAttempt.id).where(
        ExecutionAttempt.execution_id == Execution.id,
        ExecutionAttempt.worker_id == worker_id,
    )
    active_worker_attempt = worker_attempt.where(ExecutionAttempt.status.in_(ACTIVE_ATTEMPTS))
    incomplete_worker_attempt = worker_attempt.where(
        func.coalesce(
            ExecutionAttempt.cleanup_summary["workspace_cleanup_status"].astext,
            "",
        )
        != "completed"
    )
    attempt_count = (
        select(func.count(ExecutionAttempt.id))
        .where(ExecutionAttempt.execution_id == Execution.id)
        .correlate(Execution)
        .scalar_subquery()
    )
    open_incident = select(ExecutionInfrastructureIncident.id).where(
        ExecutionInfrastructureIncident.execution_id == Execution.id,
        ExecutionInfrastructureIncident.status == "open",
    )
    active_hold = select(ExecutionArtifactHold.id).where(
        ExecutionArtifactHold.execution_id == Execution.id,
        ExecutionArtifactHold.purged_at.is_(None),
        ExecutionArtifactHold.expires_at > now,
    )
    direct_responsibility_clause = or_(
        Execution.target_worker_id_snapshot == worker_id,
        Execution.target_worker_id == worker_id,
        Execution.worker_id == worker_id,
    )
    actual_responsibility_clause = or_(Execution.worker_id == worker_id, worker_attempt.exists())
    responsibility = or_(direct_responsibility_clause, worker_attempt.exists())
    protective_candidate = or_(
        Execution.adapter_id != adapter_id,
        Execution.status.in_(ACTIVE_EXECUTIONS),
        active_worker_attempt.exists(),
        incomplete_worker_attempt.exists(),
        and_(
            ~Execution.status.in_(ACTIVE_EXECUTIONS),
            actual_responsibility_clause,
            func.coalesce(Execution.workspace_cleanup_status, "") != "completed",
        ),
        open_incident.exists(),
        active_hold.exists(),
        Execution.attempt_count != attempt_count,
        and_(
            Execution.attempt_count > 0,
            direct_responsibility_clause,
            ~worker_attempt.exists(),
        ),
    )
    execution_rows = list(
        session.execute(
            select(
                Execution.id,
                Execution.adapter_id,
                Execution.status,
                Execution.worker_id,
                Execution.target_worker_id_snapshot,
                Execution.target_worker_id,
                Execution.attempt_count,
                Execution.workspace_cleanup_status,
            )
            .where(
                Execution.version_id == version_id,
                responsibility,
                protective_candidate,
            )
            .order_by(Execution.id)
            .limit(MAX_REFERENCE_RECORDS + 1)
        )
    )
    reasons: set[str] = set()
    if len(execution_rows) > MAX_REFERENCE_RECORDS:
        return CacheReferenceFacts(True, ("reference_query_truncated",))
    execution_ids = [int(row.id) for row in execution_rows]
    attempt_rows = (
        list(
            session.execute(
                select(
                    ExecutionAttempt.id,
                    ExecutionAttempt.execution_id,
                    ExecutionAttempt.worker_id,
                    ExecutionAttempt.status,
                    ExecutionAttempt.cleanup_summary,
                )
                .where(ExecutionAttempt.execution_id.in_(execution_ids))
                .order_by(ExecutionAttempt.id)
                .limit(MAX_REFERENCE_RECORDS + 1)
            )
        )
        if execution_ids
        else []
    )
    if len(attempt_rows) > MAX_REFERENCE_RECORDS:
        return CacheReferenceFacts(True, ("reference_query_truncated",))
    attempts_by_execution: dict[int, list[Any]] = {}
    for attempt in attempt_rows:
        attempts_by_execution.setdefault(int(attempt.execution_id), []).append(attempt)
    open_incident_execution_ids = (
        set(
            session.scalars(
                select(ExecutionInfrastructureIncident.execution_id)
                .where(
                    ExecutionInfrastructureIncident.execution_id.in_(execution_ids),
                    ExecutionInfrastructureIncident.status == "open",
                )
                .distinct()
            )
        )
        if execution_ids
        else set()
    )
    active_hold_execution_ids = (
        set(
            session.scalars(
                select(ExecutionArtifactHold.execution_id)
                .where(
                    ExecutionArtifactHold.execution_id.in_(execution_ids),
                    ExecutionArtifactHold.purged_at.is_(None),
                    ExecutionArtifactHold.expires_at > now,
                )
                .distinct()
            )
        )
        if execution_ids
        else set()
    )
    for execution in execution_rows:
        if execution.adapter_id != adapter_id:
            reasons.add("reference_identity_conflict")
            continue
        if execution.status in ACTIVE_EXECUTIONS:
            reasons.add(f"execution_{execution.status}")
        all_attempts = attempts_by_execution.get(int(execution.id), [])
        attempts = [attempt for attempt in all_attempts if attempt.worker_id == worker_id]
        if len(all_attempts) != execution.attempt_count:
            reasons.add("attempt_history_unknown")
        direct_responsibility = worker_id in {
            execution.target_worker_id_snapshot,
            execution.target_worker_id,
            execution.worker_id,
        }
        if execution.attempt_count > 0 and direct_responsibility and not attempts:
            reasons.add("attempt_identity_unknown")
        for attempt in attempts:
            if attempt.status in ACTIVE_ATTEMPTS:
                reasons.add("attempt_active")
            elif not isinstance(attempt.cleanup_summary, dict):
                reasons.add("attempt_cleanup_unknown")
            elif attempt.cleanup_summary.get("workspace_cleanup_status") != "completed":
                reasons.add("attempt_cleanup_incomplete")
        actual_responsibility = execution.worker_id == worker_id or bool(attempts)
        if (
            actual_responsibility
            and execution.status not in ACTIVE_EXECUTIONS
            and execution.workspace_cleanup_status != "completed"
        ):
            reasons.add("workspace_cleanup_incomplete")
        if execution.id in open_incident_execution_ids:
            reasons.add("incident_open")
        if execution.id in active_hold_execution_ids:
            reasons.add("recovery_material_active")
    cleanup_query = select(WorkerCleanupRequest.id).where(
        WorkerCleanupRequest.worker_id == worker_id,
        WorkerCleanupRequest.adapter_id == adapter_id,
        WorkerCleanupRequest.status != "completed",
    )
    if excluded_cleanup_id is not None:
        cleanup_query = cleanup_query.where(WorkerCleanupRequest.id != excluded_cleanup_id)
    cleanup = session.scalar(cleanup_query.limit(1))
    if cleanup is not None:
        reasons.add("adapter_cleanup_incomplete")
    ordered = tuple(sorted(reasons))
    return CacheReferenceFacts(bool(ordered), ordered)


def _replacement_reference_facts(
    session: Session,
    *,
    worker_id: int,
    adapter_id: int,
    version_id: int,
    current_execution: Execution,
    current_attempt: ExecutionAttempt,
) -> CacheReferenceFacts:
    """Allow only future unclaimed references with the exact builtin snapshot."""

    now = database_now(session)
    worker_attempt = select(ExecutionAttempt.id).where(
        ExecutionAttempt.execution_id == Execution.id,
        ExecutionAttempt.worker_id == worker_id,
    )
    active_worker_attempt = worker_attempt.where(ExecutionAttempt.status.in_(ACTIVE_ATTEMPTS))
    incomplete_worker_attempt = worker_attempt.where(
        func.coalesce(
            ExecutionAttempt.cleanup_summary["workspace_cleanup_status"].astext,
            "",
        )
        != "completed"
    )
    attempt_count = (
        select(func.count(ExecutionAttempt.id))
        .where(ExecutionAttempt.execution_id == Execution.id)
        .correlate(Execution)
        .scalar_subquery()
    )
    open_incident = select(ExecutionInfrastructureIncident.id).where(
        ExecutionInfrastructureIncident.execution_id == Execution.id,
        ExecutionInfrastructureIncident.status == "open",
    )
    active_hold = select(ExecutionArtifactHold.id).where(
        ExecutionArtifactHold.execution_id == Execution.id,
        ExecutionArtifactHold.purged_at.is_(None),
        ExecutionArtifactHold.expires_at > now,
    )
    direct_responsibility = or_(
        Execution.target_worker_id_snapshot == worker_id,
        Execution.target_worker_id == worker_id,
        Execution.worker_id == worker_id,
    )
    responsibility = or_(direct_responsibility, worker_attempt.exists())
    # First discard ordinary completed history.  The bounded materialization below
    # then limits only records which can protect replacement or prove a conflict.
    candidate = or_(
        Execution.id == current_execution.id,
        and_(Execution.status.in_(ACTIVE_EXECUTIONS), responsibility),
        active_worker_attempt.exists(),
        incomplete_worker_attempt.exists(),
        and_(
            ~Execution.status.in_(ACTIVE_EXECUTIONS),
            or_(Execution.worker_id == worker_id, worker_attempt.exists()),
            func.coalesce(Execution.workspace_cleanup_status, "") != "completed",
        ),
        and_(responsibility, open_incident.exists()),
        and_(responsibility, active_hold.exists()),
        and_(responsibility, Execution.attempt_count != attempt_count),
        and_(Execution.attempt_count > 0, direct_responsibility, ~worker_attempt.exists()),
    )
    execution_rows = list(
        session.execute(
            select(
                Execution.id,
                Execution.adapter_id,
                Execution.status,
                Execution.worker_id,
                Execution.target_worker_id_snapshot,
                Execution.target_worker_id,
                Execution.attempt_count,
                Execution.workspace_cleanup_status,
                Execution.builtin_package_snapshot,
            )
            .where(Execution.version_id == version_id, candidate)
            .order_by(Execution.id)
            .limit(MAX_REFERENCE_RECORDS + 1)
        )
    )
    if len(execution_rows) > MAX_REFERENCE_RECORDS:
        return CacheReferenceFacts(True, ("reference_query_truncated",))
    execution_ids = [int(row.id) for row in execution_rows]
    attempt_rows = list(
        session.execute(
            select(
                ExecutionAttempt.id,
                ExecutionAttempt.execution_id,
                ExecutionAttempt.worker_id,
                ExecutionAttempt.status,
                ExecutionAttempt.cleanup_summary,
            )
            .where(ExecutionAttempt.execution_id.in_(execution_ids))
            .order_by(ExecutionAttempt.id)
            .limit(MAX_REFERENCE_RECORDS + 1)
        )
    )
    if len(attempt_rows) > MAX_REFERENCE_RECORDS:
        return CacheReferenceFacts(True, ("reference_query_truncated",))
    attempts_by_execution: dict[int, list[Any]] = {}
    for attempt in attempt_rows:
        attempts_by_execution.setdefault(int(attempt.execution_id), []).append(attempt)
    incident_ids = set(
        session.scalars(
            select(ExecutionInfrastructureIncident.execution_id)
            .where(
                ExecutionInfrastructureIncident.execution_id.in_(execution_ids),
                ExecutionInfrastructureIncident.status == "open",
            )
            .distinct()
        )
    )
    hold_ids = set(
        session.scalars(
            select(ExecutionArtifactHold.execution_id)
            .where(
                ExecutionArtifactHold.execution_id.in_(execution_ids),
                ExecutionArtifactHold.purged_at.is_(None),
                ExecutionArtifactHold.expires_at > now,
            )
            .distinct()
        )
    )
    reasons: set[str] = set()
    for execution in execution_rows:
        if execution.adapter_id != adapter_id:
            reasons.add("reference_identity_conflict")
            continue
        rows = attempts_by_execution.get(int(execution.id), [])
        if len(rows) != execution.attempt_count:
            reasons.add("attempt_history_unknown")
        direct_row_responsibility = worker_id in {
            execution.target_worker_id_snapshot,
            execution.target_worker_id,
            execution.worker_id,
        }
        if (
            execution.attempt_count > 0
            and direct_row_responsibility
            and not any(row.worker_id == worker_id for row in rows)
        ):
            reasons.add("attempt_identity_unknown")
        if execution.id in incident_ids:
            reasons.add("incident_open")
        if execution.id in hold_ids:
            reasons.add("recovery_material_active")
        if execution.id == current_execution.id:
            if any(row.id != current_attempt.id for row in rows):
                for row in rows:
                    if row.id != current_attempt.id and (
                        row.status in ACTIVE_ATTEMPTS
                        or not isinstance(row.cleanup_summary, dict)
                        or row.cleanup_summary.get("workspace_cleanup_status") != "completed"
                    ):
                        reasons.add("attempt_cleanup_incomplete")
            continue
        if execution.status not in {"queued", "retry_wait"}:
            if execution.status == "running":
                reasons.add("execution_running")
            for row in rows:
                if row.status in ACTIVE_ATTEMPTS:
                    reasons.add("attempt_active")
                elif (
                    not isinstance(row.cleanup_summary, dict)
                    or row.cleanup_summary.get("workspace_cleanup_status") != "completed"
                ):
                    reasons.add("attempt_cleanup_incomplete")
            if (
                execution.worker_id == worker_id or any(row.worker_id == worker_id for row in rows)
            ) and execution.workspace_cleanup_status != "completed":
                reasons.add("workspace_cleanup_incomplete")
            continue
        if execution.builtin_package_snapshot != current_execution.builtin_package_snapshot:
            reasons.add("builtin_snapshot_conflict")
        if any(row.status in ACTIVE_ATTEMPTS for row in rows):
            reasons.add("attempt_active")
        for row in rows:
            if row.status not in ACTIVE_ATTEMPTS and (
                not isinstance(row.cleanup_summary, dict)
                or row.cleanup_summary.get("workspace_cleanup_status") != "completed"
            ):
                reasons.add("attempt_cleanup_incomplete")
        if rows and execution.workspace_cleanup_status != "completed":
            reasons.add("workspace_cleanup_incomplete")
    cleanup = session.scalar(
        select(WorkerCleanupRequest.id)
        .where(
            WorkerCleanupRequest.worker_id == worker_id,
            WorkerCleanupRequest.adapter_id == adapter_id,
            WorkerCleanupRequest.status != "completed",
        )
        .limit(1)
    )
    if cleanup is not None:
        reasons.add("adapter_cleanup_incomplete")
    return CacheReferenceFacts(bool(reasons), tuple(sorted(reasons)))


def _operation_matches_request(
    operation: WorkerCacheOperation,
    *,
    worker_id: int,
    adapter_id: int,
    version_id: int,
    operation_kind: str,
    cleanup_context: dict[str, Any] | None,
    observed_identity: dict[str, Any] | None,
    replacement_binding: dict[str, Any] | None,
) -> bool:
    return not (
        operation.worker_id != worker_id
        or operation.adapter_id != adapter_id
        or operation.version_id != version_id
        or operation.operation_kind != operation_kind
        or operation.cleanup_id != (cleanup_context or {}).get("cleanup_id")
        or operation.cleanup_claim_attempt != (cleanup_context or {}).get("claim_attempt")
        or operation.observed_identity != observed_identity
        or operation.replacement_context != replacement_binding
    )


def _validate_replacement_authority(
    session: Session,
    *,
    worker_id: int,
    adapter_id: int,
    version_id: int,
    adapter: Adapter | None,
    replacement_context: dict[str, Any],
) -> None:
    # Execution -> Attempt is the established receipt/recovery lock order.
    execution = session.get(
        Execution,
        replacement_context["execution_id"],
        with_for_update=True,
        populate_existing=True,
    )
    attempt = session.get(
        ExecutionAttempt,
        replacement_context["attempt_id"],
        with_for_update=True,
        populate_existing=True,
    )
    slot = session.get(
        AdapterExecutionSlot,
        (adapter_id, 0),
        with_for_update=True,
        populate_existing=True,
    )
    old_identity = replacement_context.get("old_identity")
    target_language = replacement_context.get("target_language")
    now = database_now(session)
    if (
        attempt is None
        or execution is None
        or slot is None
        or adapter is None
        or not isinstance(old_identity, dict)
        or old_identity.get("language") != adapter.language
        or target_language != adapter.language
        or attempt.execution_id != execution.id
        or attempt.worker_id != worker_id
        or attempt.status != "running"
        or execution.status != "running"
        or execution.adapter_id != adapter_id
        or execution.version_id != version_id
        or execution.worker_id != worker_id
        or execution.cancel_requested
        or attempt.fencing_token != replacement_context["fencing_token"]
        or not token_matches(replacement_context["claim_token"], attempt.claim_token_hash)
        or attempt.lease_expires_at <= now
        or slot.active_attempt_id != attempt.id
        or slot.fencing_token != attempt.fencing_token
        or slot.lease_expires_at is None
        or slot.lease_expires_at <= now
    ):
        raise domain_error(409, "cache_replacement_stale", "Replacement ownership is stale")
    facts = _replacement_reference_facts(
        session,
        worker_id=worker_id,
        adapter_id=adapter_id,
        version_id=version_id,
        current_execution=execution,
        current_attempt=attempt,
    )
    if facts.protected:
        raise domain_error(
            409,
            "cache_reference_active",
            "Cache is retained by durable runtime responsibility",
            {"reasons": list(facts.reasons)},
        )


def acquire_guard(
    session: Session,
    *,
    worker_id: int,
    adapter_id: int,
    version_id: int,
    operation_id: uuid.UUID,
    cleanup_context: dict[str, Any] | None = None,
    observed_identity: dict[str, Any] | None = None,
    replacement_context: dict[str, Any] | None = None,
) -> WorkerCacheOperation:
    worker = session.get(Worker, worker_id)
    if worker is None:
        raise domain_error(404, "worker_not_found", "Worker not found")
    if worker.isolation_capabilities.get("cache_governance_v1") is not True:
        raise domain_error(
            409, "cache_governance_unsupported", "Worker cache governance is unavailable"
        )
    if replacement_context is not None and cleanup_context is not None:
        raise domain_error(409, "cache_guard_identity_conflict", "Guard purpose is inconsistent")
    operation_kind = (
        "replacement"
        if replacement_context is not None
        else "cleanup"
        if cleanup_context is not None
        else "gc"
    )
    replacement_binding = None
    if replacement_context is not None:
        replacement_binding = {
            key: value for key, value in replacement_context.items() if key != "claim_token"
        }
    adapter = session.get(Adapter, adapter_id, with_for_update=True, populate_existing=True)
    previous_operation = session.get(WorkerCacheOperation, operation_id)
    if previous_operation is not None and not _operation_matches_request(
        previous_operation,
        worker_id=worker_id,
        adapter_id=adapter_id,
        version_id=version_id,
        operation_kind=operation_kind,
        cleanup_context=cleanup_context,
        observed_identity=observed_identity,
        replacement_binding=replacement_binding,
    ):
        raise domain_error(
            409, "cache_guard_identity_conflict", "Cache guard identity is inconsistent"
        )
    existing = session.get(WorkerCacheGuard, (worker_id, version_id))
    version_owner = session.scalar(
        select(AdapterVersion.adapter_id).where(AdapterVersion.id == version_id)
    )
    if adapter is not None:
        version = session.scalar(
            select(AdapterVersion.id).where(
                AdapterVersion.id == version_id, AdapterVersion.adapter_id == adapter_id
            )
        )
        if version is None:
            raise domain_error(409, "cache_guard_identity_conflict", "Revision is unavailable")
    elif existing is None:
        if observed_identity is None or version_owner is not None:
            raise domain_error(404, "cache_guard_not_found", "Cache guard is unavailable")
    elif existing.adapter_id != adapter_id:
        raise domain_error(409, "cache_guard_identity_conflict", "Cache guard is inconsistent")
    guard = _ensure_guard(
        session, worker_id=worker_id, adapter_id=adapter_id, version_id=version_id
    )
    previous_operation = session.get(WorkerCacheOperation, operation_id, populate_existing=True)
    if previous_operation is not None:
        if not _operation_matches_request(
            previous_operation,
            worker_id=worker_id,
            adapter_id=adapter_id,
            version_id=version_id,
            operation_kind=operation_kind,
            cleanup_context=cleanup_context,
            observed_identity=observed_identity,
            replacement_binding=replacement_binding,
        ):
            raise domain_error(
                409, "cache_guard_identity_conflict", "Cache guard identity is inconsistent"
            )
        if previous_operation.phase != "acquired":
            raise domain_error(
                409,
                "cache_guard_operation_finished",
                "Cache guard operation is already finished",
            )
        if (
            guard.operation_id != operation_id
            or guard.phase != "acquired"
            or guard.generation != previous_operation.generation
        ):
            raise domain_error(
                409, "cache_guard_identity_conflict", "Cache guard identity is inconsistent"
            )
        if replacement_context is not None:
            _validate_replacement_authority(
                session,
                worker_id=worker_id,
                adapter_id=adapter_id,
                version_id=version_id,
                adapter=adapter,
                replacement_context=replacement_context,
            )
        session.commit()
        session.refresh(previous_operation)
        return previous_operation
    excluded_cleanup_id: int | None = None
    if cleanup_context is not None:
        cleanup = session.scalar(
            select(WorkerCleanupRequest)
            .where(WorkerCleanupRequest.id == cleanup_context["cleanup_id"])
            .with_for_update()
        )
        if (
            cleanup is None
            or cleanup.worker_id != worker_id
            or cleanup.adapter_id != adapter_id
            or cleanup.status != "running"
            or cleanup.attempts != cleanup_context["claim_attempt"]
            or adapter is not None
        ):
            raise domain_error(409, "cleanup_stale_claim", "Cleanup claim is stale")
        excluded_cleanup_id = cleanup.id
    if guard.phase != "idle":
        raise domain_error(
            409,
            "cache_reclamation_in_progress",
            "Cache reclamation is in progress",
            {"retry_after": 1},
        )
    if replacement_context is not None:
        _validate_replacement_authority(
            session,
            worker_id=worker_id,
            adapter_id=adapter_id,
            version_id=version_id,
            adapter=adapter,
            replacement_context=replacement_context,
        )
        facts = CacheReferenceFacts(False, ())
    else:
        facts = reference_facts(
            session,
            worker_id=worker_id,
            adapter_id=adapter_id,
            version_id=version_id,
            excluded_cleanup_id=excluded_cleanup_id,
        )
    if facts.protected:
        raise domain_error(
            409,
            "cache_reference_active",
            "Cache is retained by durable runtime responsibility",
            {"reasons": list(facts.reasons)},
        )
    guard.generation += 1
    guard.operation_id = operation_id
    guard.phase = "acquired"
    operation = WorkerCacheOperation(
        operation_id=operation_id,
        worker_id=worker_id,
        adapter_id=adapter_id,
        version_id=version_id,
        generation=guard.generation,
        phase="acquired",
        operation_kind=operation_kind,
        cleanup_id=(cleanup_context or {}).get("cleanup_id"),
        cleanup_claim_attempt=(cleanup_context or {}).get("claim_attempt"),
        observed_identity=observed_identity,
        replacement_context=replacement_binding,
    )
    session.add(operation)
    session.commit()
    session.refresh(operation)
    return operation


def check_guard(
    session: Session, *, worker_id: int, operation_id: uuid.UUID
) -> WorkerCacheOperation:
    operation = session.scalar(
        select(WorkerCacheOperation).where(
            WorkerCacheOperation.worker_id == worker_id,
            WorkerCacheOperation.operation_id == operation_id,
        )
    )
    if operation is None:
        raise domain_error(404, "cache_guard_not_found", "Cache guard is unavailable")
    return operation


def finish_guard(
    session: Session,
    *,
    worker_id: int,
    operation_id: uuid.UUID,
    generation: int,
    outcome: str,
) -> WorkerCacheOperation:
    operation = session.scalar(
        select(WorkerCacheOperation)
        .where(
            WorkerCacheOperation.worker_id == worker_id,
            WorkerCacheOperation.operation_id == operation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if operation is None:
        raise domain_error(404, "cache_guard_not_found", "Cache guard is unavailable")
    if operation.generation != generation:
        raise domain_error(409, "cache_guard_stale", "Cache guard generation is stale")
    if operation.phase != "acquired":
        if operation.phase != outcome:
            raise domain_error(409, "cache_guard_stale", "Cache guard result is inconsistent")
        return operation
    guard = session.scalar(
        select(WorkerCacheGuard)
        .where(
            WorkerCacheGuard.worker_id == worker_id,
            WorkerCacheGuard.version_id == operation.version_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        guard is None
        or guard.generation != generation
        or guard.operation_id != operation_id
        or guard.phase != "acquired"
    ):
        raise domain_error(409, "cache_guard_stale", "Cache guard generation is stale")
    guard.operation_id = None
    guard.phase = "idle"
    operation.phase = outcome
    operation.finished_at = database_now(session)
    session.commit()
    session.refresh(operation)
    return operation


def list_active_guards(
    session: Session,
    *,
    worker_id: int,
    after_version_id: int | None = None,
    limit: int = 100,
) -> tuple[list[WorkerCacheGuard], int | None]:
    if session.get(Worker, worker_id) is None:
        raise domain_error(404, "worker_not_found", "Worker not found")
    bounded_limit = max(1, min(int(limit), 100))
    query = select(WorkerCacheGuard).where(
        WorkerCacheGuard.worker_id == worker_id, WorkerCacheGuard.phase == "acquired"
    )
    if after_version_id is not None:
        query = query.where(WorkerCacheGuard.version_id > after_version_id)
    rows = list(
        session.scalars(query.order_by(WorkerCacheGuard.version_id).limit(bounded_limit + 1))
    )
    has_more = len(rows) > bounded_limit
    items = rows[:bounded_limit]
    next_after = items[-1].version_id if has_more and items else None
    return items, next_after


def resolve_reference(
    session: Session, *, worker_id: int, execution_id: int, attempt_id: int | None
) -> tuple[int, int]:
    if session.get(Worker, worker_id) is None:
        raise domain_error(404, "worker_not_found", "Worker not found")
    execution = session.get(Execution, execution_id)
    if execution is None:
        raise domain_error(404, "cache_reference_not_found", "Cache reference is unavailable")
    responsible = worker_id in {
        execution.target_worker_id_snapshot,
        execution.target_worker_id,
        execution.worker_id,
    }
    if attempt_id is not None:
        attempt = session.get(ExecutionAttempt, attempt_id)
        if (
            attempt is None
            or attempt.execution_id != execution.id
            or attempt.worker_id != worker_id
        ):
            raise domain_error(409, "cache_reference_mismatch", "Cache reference is inconsistent")
        responsible = True
    if not responsible:
        responsible = (
            session.scalar(
                select(ExecutionAttempt.id)
                .where(
                    ExecutionAttempt.execution_id == execution.id,
                    ExecutionAttempt.worker_id == worker_id,
                )
                .limit(1)
            )
            is not None
        )
    if not responsible:
        raise domain_error(409, "cache_reference_mismatch", "Cache reference is inconsistent")
    return execution.adapter_id, execution.version_id
