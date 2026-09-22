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
    AdapterVersion,
    Execution,
    ExecutionArtifactHold,
    ExecutionAttempt,
    ExecutionInfrastructureIncident,
    Worker,
    WorkerCacheGuard,
    WorkerCleanupRequest,
)
from dlr.control.services.adapter import domain_error
from dlr.control.services.input_config import database_now

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
    session: Session, *, worker_id: int, adapter_id: int, version_id: int
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
    ordered = tuple(sorted(reasons))
    return CacheReferenceFacts(bool(ordered), ordered)


def acquire_guard(
    session: Session,
    *,
    worker_id: int,
    adapter_id: int,
    version_id: int,
    operation_id: uuid.UUID,
) -> WorkerCacheGuard:
    worker = session.get(Worker, worker_id)
    if worker is None:
        raise domain_error(404, "worker_not_found", "Worker not found")
    if worker.isolation_capabilities.get("cache_governance_v1") is not True:
        raise domain_error(
            409, "cache_governance_unsupported", "Worker cache governance is unavailable"
        )
    adapter = session.get(Adapter, adapter_id, with_for_update=True, populate_existing=True)
    existing = session.get(WorkerCacheGuard, (worker_id, version_id))
    if adapter is not None:
        version = session.scalar(
            select(AdapterVersion.id).where(
                AdapterVersion.id == version_id, AdapterVersion.adapter_id == adapter_id
            )
        )
        if version is None:
            raise domain_error(409, "cache_guard_identity_conflict", "Revision is unavailable")
    elif existing is None or existing.adapter_id != adapter_id:
        raise domain_error(404, "cache_guard_not_found", "Cache guard is unavailable")
    guard = _ensure_guard(
        session, worker_id=worker_id, adapter_id=adapter_id, version_id=version_id
    )
    if guard.operation_id == operation_id and guard.phase == "acquired":
        session.commit()
        session.refresh(guard)
        return guard
    if guard.phase != "idle":
        raise domain_error(
            409,
            "cache_reclamation_in_progress",
            "Cache reclamation is in progress",
            {"retry_after": 1},
        )
    facts = reference_facts(
        session, worker_id=worker_id, adapter_id=adapter_id, version_id=version_id
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
    session.commit()
    session.refresh(guard)
    return guard


def check_guard(session: Session, *, worker_id: int, operation_id: uuid.UUID) -> WorkerCacheGuard:
    guard = session.scalar(
        select(WorkerCacheGuard).where(
            WorkerCacheGuard.worker_id == worker_id,
            WorkerCacheGuard.operation_id == operation_id,
        )
    )
    if guard is None:
        raise domain_error(404, "cache_guard_not_found", "Cache guard is unavailable")
    return guard


def finish_guard(
    session: Session,
    *,
    worker_id: int,
    operation_id: uuid.UUID,
    generation: int,
) -> WorkerCacheGuard:
    guard = session.scalar(
        select(WorkerCacheGuard)
        .where(
            WorkerCacheGuard.worker_id == worker_id,
            WorkerCacheGuard.operation_id == operation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if guard is None:
        raise domain_error(404, "cache_guard_not_found", "Cache guard is unavailable")
    if guard.generation != generation or guard.phase != "acquired":
        raise domain_error(409, "cache_guard_stale", "Cache guard generation is stale")
    guard.operation_id = None
    guard.phase = "idle"
    session.commit()
    session.refresh(guard)
    return guard


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
