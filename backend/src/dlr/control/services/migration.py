"""Explicit reliable-runtime migration, Cutover, and invariant tools.

The helpers are intentionally authenticated administrative operations rather
than startup work. Inventory, dry-run, preflight, and invariants are read-only.
Pending conversion locks one Adapter and one legacy Execution at a time, then
commits the converted row, its Admission reservation and its generation-1
Outbox together. A legacy running row is never converted in place. The final
legacy-index retirement is a separately confirmed, fail-closed operation.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    AdapterVersion,
    Execution,
    ExecutionCredentialBindingSnapshot,
    Worker,
)
from dlr.control.schemas.worker import isolation_capabilities_ready
from dlr.control.services import admission, outbox, rabbitmq, reliable_execution
from dlr.control.services import attempt as attempt_service
from dlr.control.services.adapter import domain_error
from dlr.control.services.execution import _persist_credential_binding_snapshots
from dlr.control.services.input_config import database_now

logger = logging.getLogger("dlr.control.migration")


def _execution_counts(session: Session) -> dict[str, dict[str, int]]:
    rows = session.execute(
        select(Execution.dispatch_backend, Execution.status, func.count(Execution.id)).group_by(
            Execution.dispatch_backend, Execution.status
        )
    ).all()
    result: dict[str, dict[str, int]] = {"legacy": {}, "rabbitmq": {}}
    for backend, status, count in rows:
        result.setdefault(str(backend), {})[str(status)] = int(count)
    return result


def _protocol_distribution(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(Worker.protocol_version, func.count(Worker.id)).group_by(Worker.protocol_version)
    ).all()
    return {str(int(version)): int(count) for version, count in rows}


def _old_active_index_present(session: Session) -> bool:
    return bool(
        session.scalar(
            text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND indexname = 'uq_executions_active_adapter'"
                ")"
            )
        )
    )


def _worker_readiness(session: Session) -> dict[str, object]:
    rows = list(session.scalars(select(Worker).order_by(Worker.id.asc())))
    v3_rows = [worker for worker in rows if int(worker.protocol_version or 1) == 3]
    ready_rows = [
        worker
        for worker in v3_rows
        if bool(worker.rabbitmq_execution_v3)
        and worker.isolation_preflight_status == "passed"
        and isolation_capabilities_ready(worker.isolation_capabilities)
    ]
    all_v3_workers_ready = bool(rows) and len(ready_rows) == len(rows)
    sandbox_gate_passed = bool(settings.cutover_sandbox_gate_passed and all_v3_workers_ready)
    return {
        "worker_count": len(rows),
        "protocol_v3_workers": len(v3_rows),
        "isolation_preflight_passed_workers": len(ready_rows),
        "all_v3_workers_ready": all_v3_workers_ready,
        # A persisted registration alone is not the exact-SHA target-Linux
        # evidence.  The operator attestation remains a separate fail-closed
        # deployment fact and only becomes effective when every row is ready.
        "sandbox_gate": "passed" if sandbox_gate_passed else "not_passed",
        "cutover_ready": sandbox_gate_passed,
    }


def inventory(session: Session) -> dict[str, object]:
    """Return a redacted, repeatable view of migration and dark-launch facts."""

    revision = session.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
    execution_counts = _execution_counts(session)
    legacy_counts = execution_counts.get("legacy", {})
    rabbit_counts = execution_counts.get("rabbitmq", {})
    legacy_running = int(
        session.scalar(
            select(func.count(Execution.id)).where(
                Execution.dispatch_backend == "legacy", Execution.status == "running"
            )
        )
        or 0
    )
    legacy_pending = int(
        session.scalar(
            select(func.count(Execution.id)).where(
                Execution.dispatch_backend == "legacy", Execution.status == "pending"
            )
        )
        or 0
    )
    worker_state = _worker_readiness(session)
    outbox_state = outbox.backlog_health(session)
    hold_count, hold_bytes = attempt_service.held_backlog(session)
    return {
        "schema_revision": str(revision) if revision is not None else None,
        "execution_counts": {"legacy": legacy_counts, "rabbitmq": rabbit_counts},
        "legacy": {
            "pending": legacy_pending,
            "running": legacy_running,
            "claim_enabled": bool(settings.legacy_execution_claim_enabled),
        },
        "protocol_distribution": _protocol_distribution(session),
        "minimum_worker_protocol": settings.min_worker_protocol_version,
        "rabbitmq": rabbitmq.runtime_health(session),
        "outbox": outbox_state,
        "managed_file_holds": {
            "active_count": hold_count,
            "active_bytes": hold_bytes,
            "max_count": settings.dead_letter_hold_max_count,
            "max_bytes": settings.dead_letter_hold_max_bytes,
        },
        "sandbox_readiness": worker_state,
        "cutover_gates": {
            "backup_restore_gate_passed": bool(settings.cutover_backup_restore_gate_passed),
            "sandbox_gate_passed": bool(settings.cutover_sandbox_gate_passed),
            "slot_gate_passed": bool(settings.cutover_slot_gate_passed),
        },
        "dark_launch": {
            "rabbitmq_production_ingress_enabled": bool(settings.rabbitmq_execution_enabled),
            "rabbitmq_canary_enabled": bool(settings.rabbitmq_execution_canary_enabled),
            "ordinary_new_traffic_backend": "legacy"
            if not settings.rabbitmq_execution_enabled
            else "rabbitmq",
            "old_active_index_present": _old_active_index_present(session),
            "legacy_claim_enabled": bool(settings.legacy_execution_claim_enabled),
        },
    }


def dry_run(session: Session) -> dict[str, object]:
    """Perform no writes and describe the pending migration boundary."""

    result = inventory(session)
    legacy = cast(dict[str, object], result["legacy"])
    pending = cast(int, legacy["pending"])
    running = cast(int, legacy["running"])
    result["dry_run"] = {
        "would_convert_pending": pending,
        "would_leave_running_untouched": running,
        "blocked_by_legacy_running": running > 0,
        "cutover_ready": running == 0 and pending == 0,
    }
    return result


def legacy_running_drain_status(session: Session) -> dict[str, object]:
    """Require a clean legacy-running boundary without modifying rows."""

    running = int(
        session.scalar(
            select(func.count(Execution.id)).where(
                Execution.dispatch_backend == "legacy", Execution.status == "running"
            )
        )
        or 0
    )
    if running:
        raise domain_error(
            409,
            "legacy_running_not_drained",
            "Legacy running Executions must drain before migration cutover",
            {"running": running},
        )
    return {"status": "drained", "legacy_running": 0}


def _current_or_snapshot_bindings(
    session: Session, execution: Execution, adapter_id: int
) -> list[dict[str, object]]:
    value = execution.credential_bindings_snapshot
    if isinstance(value, list) and value:
        rows = list(
            session.scalars(
                select(ExecutionCredentialBindingSnapshot)
                .where(ExecutionCredentialBindingSnapshot.execution_id == execution.id)
                .order_by(ExecutionCredentialBindingSnapshot.id.asc())
            )
        )
        if not rows:
            _persist_credential_binding_snapshots(session, execution, value)
        return list(value)
    snapshots = reliable_execution._credential_bindings_snapshot(session, adapter_id)
    execution.credential_bindings_snapshot = snapshots
    if snapshots:
        _persist_credential_binding_snapshots(session, execution, snapshots)
    return snapshots


def _policy_snapshot(execution: Execution) -> dict[str, object]:
    value = execution.retry_policy_snapshot
    if isinstance(value, Mapping) and value:
        try:
            return reliable_execution.validate_retry_policy(value)
        except ValueError:
            pass
    return reliable_execution.default_retry_policy()


def _resource_snapshot(execution: Execution, adapter: Adapter) -> dict[str, object]:
    value = execution.resource_profile_snapshot
    if isinstance(value, Mapping) and value:
        try:
            from dlr.control.schemas.reliable_runtime import ResourceProfile

            profile = ResourceProfile.model_validate(value)
            return cast(dict[str, object], profile.model_dump(mode="json"))
        except Exception:
            pass
    return reliable_execution.default_resource_profile(adapter.timeout_seconds)


def _convert_one_pending(session: Session, execution_id: int) -> bool:
    """Convert one row in the caller's transaction; return whether changed."""

    adapter_id = session.scalar(select(Execution.adapter_id).where(Execution.id == execution_id))
    if adapter_id is None:
        return False
    adapter = session.get(Adapter, adapter_id, with_for_update=True)
    if adapter is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    execution = session.get(Execution, execution_id, with_for_update=True)
    if execution is None:
        return False
    if execution.dispatch_backend == "rabbitmq":
        return False
    if execution.dispatch_backend != "legacy" or execution.status != "pending":
        return False
    worker = reliable_execution.resolve_queue_target_worker(session, adapter)
    version = session.scalar(
        select(AdapterVersion).where(
            AdapterVersion.id == execution.version_id,
            AdapterVersion.adapter_id == adapter.id,
        )
    )
    if version is None:
        raise domain_error(409, "execution_version_invalid", "Execution Revision is unavailable")
    logical_bytes = admission.logical_input_bytes(
        execution.input_source_type,
        execution.input,
        cast(dict[str, Any], execution.input_snapshot),
    )
    policy = _policy_snapshot(execution)
    resource_profile = _resource_snapshot(execution, adapter)
    _current_or_snapshot_bindings(session, execution, adapter.id)
    now = database_now(session)
    admission.reserve_admission(session, adapter.id, logical_bytes)
    execution.dispatch_backend = "rabbitmq"
    execution.status = "queued"
    execution.dispatch_generation = 1
    execution.queued_at = now
    execution.next_attempt_at = None
    execution.attempt_count = 0
    execution.max_attempts_snapshot = cast(int, policy["max_attempts"])
    execution.retry_policy_snapshot = policy
    execution.resource_profile_snapshot = resource_profile
    execution.resource_class = reliable_execution.RESOURCE_PROFILE_CLASS
    execution.target_worker_id = worker.id
    execution.target_worker_id_snapshot = worker.id
    execution.worker_id = None
    execution.started_at = None
    execution.ended_at = None
    execution.duration_ms = None
    execution.logical_input_bytes = logical_bytes
    execution.claim_deadline_at = now + timedelta(seconds=settings.execution_claim_timeout_seconds)
    outbox.create_dispatch_outbox(session, execution, available_at=now)
    outbox.require_outbox_capacity(session, additional_count=0, additional_bytes=0)
    return True


def migrate_legacy_pending(session: Session, *, limit: int = 100) -> dict[str, object]:
    """Convert a bounded page of legacy pending rows, never legacy running."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
        raise domain_error(422, "migration_limit_invalid", "Migration limit is invalid")
    ids = list(
        session.scalars(
            select(Execution.id)
            .where(Execution.dispatch_backend == "legacy", Execution.status == "pending")
            .order_by(Execution.created_at, Execution.id)
            .limit(limit)
        )
    )
    converted = 0
    for execution_id in ids:
        try:
            if _convert_one_pending(session, int(execution_id)):
                session.commit()
                converted += 1
            else:
                session.rollback()
        except Exception:
            session.rollback()
            raise
    remaining_pending = int(
        session.scalar(
            select(func.count(Execution.id)).where(
                Execution.dispatch_backend == "legacy", Execution.status == "pending"
            )
        )
        or 0
    )
    running = int(
        session.scalar(
            select(func.count(Execution.id)).where(
                Execution.dispatch_backend == "legacy", Execution.status == "running"
            )
        )
        or 0
    )
    return {
        "status": "completed",
        "converted": converted,
        "already_converted": 0,
        "legacy_pending_remaining": remaining_pending,
        "legacy_running": running,
        "cutover_ready": remaining_pending == 0 and running == 0,
    }


_INVARIANT_SAMPLE_LIMIT = 20


def _invariant_violation(
    session: Session,
    code: str,
    query: str,
) -> dict[str, object] | None:
    rows = session.execute(
        text(
            "SELECT violation_id, count(*) OVER() AS total "
            f"FROM ({query}) AS invariant_rows "
            "ORDER BY violation_id LIMIT :sample_limit"
        ),
        {"sample_limit": _INVARIANT_SAMPLE_LIMIT},
    ).all()
    if not rows:
        return None
    return {
        "code": code,
        "count": int(rows[0][1]),
        "sample_ids": [str(row[0]) for row in rows],
    }


def _structural_invariant_violations(session: Session) -> list[dict[str, object]]:
    """Return deterministic, bounded samples for the post-Cutover DB contract."""

    checks = (
        (
            "legacy_active_execution",
            "SELECT id AS violation_id FROM executions "
            "WHERE dispatch_backend = 'legacy' AND status IN ('pending', 'running')",
        ),
        (
            "queued_without_outbox_or_incident",
            "SELECT execution.id AS violation_id FROM executions AS execution "
            "WHERE execution.dispatch_backend = 'rabbitmq' "
            "AND execution.status = 'queued' "
            "AND (execution.dispatch_generation < 1 OR ("
            "NOT EXISTS (SELECT 1 FROM execution_outbox AS outbox "
            "WHERE outbox.execution_id = execution.id "
            "AND outbox.dispatch_generation = execution.dispatch_generation) "
            "AND NOT EXISTS (SELECT 1 FROM execution_infrastructure_incidents AS incident "
            "WHERE incident.execution_id = execution.id "
            "AND incident.dispatch_generation = execution.dispatch_generation "
            "AND incident.status = 'open'))) ",
        ),
        (
            "running_without_single_attempt_and_slot",
            "SELECT execution.id AS violation_id FROM executions AS execution "
            "WHERE execution.dispatch_backend = 'rabbitmq' "
            "AND execution.status = 'running' AND ("
            "(SELECT count(*) FROM execution_attempts AS attempt "
            "WHERE attempt.execution_id = execution.id "
            "AND attempt.status IN ('claimed', 'running')) <> 1 "
            "OR NOT EXISTS (SELECT 1 FROM execution_attempts AS attempt "
            "JOIN adapter_execution_slots AS slot "
            "ON slot.active_attempt_id = attempt.id "
            "WHERE attempt.execution_id = execution.id "
            "AND attempt.adapter_id = execution.adapter_id "
            "AND attempt.status IN ('claimed', 'running') "
            "AND slot.adapter_id = execution.adapter_id AND slot.slot_no = 0))",
        ),
        (
            "active_attempt_without_running_execution_and_slot",
            "SELECT attempt.id AS violation_id FROM execution_attempts AS attempt "
            "LEFT JOIN executions AS execution ON execution.id = attempt.execution_id "
            "WHERE attempt.status IN ('claimed', 'running') AND ("
            "execution.id IS NULL OR execution.dispatch_backend <> 'rabbitmq' "
            "OR execution.status <> 'running' OR execution.adapter_id <> attempt.adapter_id "
            "OR NOT EXISTS (SELECT 1 FROM adapter_execution_slots AS slot "
            "WHERE slot.active_attempt_id = attempt.id "
            "AND slot.adapter_id = attempt.adapter_id AND slot.slot_no = 0))",
        ),
        (
            "occupied_slot_without_matching_attempt",
            "SELECT slot.active_attempt_id AS violation_id "
            "FROM adapter_execution_slots AS slot "
            "LEFT JOIN execution_attempts AS attempt ON attempt.id = slot.active_attempt_id "
            "LEFT JOIN executions AS execution ON execution.id = attempt.execution_id "
            "WHERE slot.active_attempt_id IS NOT NULL AND ("
            "attempt.id IS NULL OR attempt.status NOT IN ('claimed', 'running') "
            "OR attempt.adapter_id <> slot.adapter_id "
            "OR execution.id IS NULL OR execution.status <> 'running' "
            "OR execution.dispatch_backend <> 'rabbitmq')",
        ),
        (
            "orphan_or_future_outbox",
            "SELECT outbox.id AS violation_id FROM execution_outbox AS outbox "
            "LEFT JOIN executions AS execution ON execution.id = outbox.execution_id "
            "WHERE execution.id IS NULL OR execution.dispatch_backend <> 'rabbitmq' "
            "OR outbox.dispatch_generation < 1 "
            "OR outbox.dispatch_generation > execution.dispatch_generation "
            "OR COALESCE(outbox.payload_json->>'execution_id', '') <> outbox.execution_id::text "
            "OR COALESCE(outbox.payload_json->>'dispatch_generation', '') "
            "<> outbox.dispatch_generation::text",
        ),
    )
    violations: list[dict[str, object]] = []
    for code, query in checks:
        violation = _invariant_violation(session, code, query)
        if violation is not None:
            violations.append(violation)
    return violations


def cutover_preflight(session: Session) -> dict[str, object]:
    """Read-only database/Broker readiness before any Cutover mutation."""

    facts = inventory(session)
    blockers: list[str] = []
    legacy = cast(dict[str, object], facts["legacy"])
    worker_state = cast(dict[str, object], facts["sandbox_readiness"])
    rabbit_state = cast(dict[str, object], facts["rabbitmq"])
    repair_state = cast(dict[str, object], rabbit_state.get("repair", {}))
    outbox_state = cast(dict[str, object], facts["outbox"])
    dark_launch = cast(dict[str, object], facts["dark_launch"])
    if cast(int, legacy["running"]) > 0:
        blockers.append("legacy_running_not_drained")
    if cast(int, legacy["pending"]) > 0:
        blockers.append("legacy_pending_not_drained_or_migrated")
    if not settings.cutover_backup_restore_gate_passed:
        blockers.append("backup_restore_gate_not_attested")
    if not bool(worker_state["all_v3_workers_ready"]):
        blockers.append("worker_v3_isolation_not_ready")
    if not settings.cutover_sandbox_gate_passed:
        blockers.append("sandbox_gate_not_attested")
    if not bool(repair_state.get("ready")):
        blockers.append("rabbitmq_repair_not_ready")
    if outbox_state.get("status") != "ok":
        blockers.append("outbox_backlog_degraded")
    if not bool(dark_launch["old_active_index_present"]):
        blockers.append("legacy_active_index_already_retired")
    return {
        "status": "ready" if not blockers else "blocked",
        "read_only": True,
        "backup_restore_evidence_required": True,
        "blockers": blockers,
        "inventory": facts,
    }


def _cutover_mutation_blockers(session: Session) -> list[str]:
    blockers: list[str] = []
    if not settings.cutover_backup_restore_gate_passed:
        blockers.append("backup_restore_gate_not_attested")
    if not settings.cutover_sandbox_gate_passed:
        blockers.append("sandbox_gate_not_attested")
    if not settings.cutover_slot_gate_passed:
        blockers.append("slot_gate_not_attested")
    if not settings.rabbitmq_execution_enabled:
        blockers.append("rabbitmq_ingress_not_enabled")
    if settings.min_worker_protocol_version != 3:
        blockers.append("minimum_worker_protocol_not_v3")
    if not settings.legacy_execution_claim_enabled:
        blockers.append("legacy_claim_closed_before_index_retirement")
    worker_state = _worker_readiness(session)
    if not bool(worker_state["all_v3_workers_ready"]):
        blockers.append("worker_v3_isolation_not_ready")
    if not rabbitmq.ingress_configuration_ready(session):
        blockers.append("rabbitmq_ingress_not_ready")
    if outbox.backlog_health(session).get("status") != "ok":
        blockers.append("outbox_backlog_degraded")
    blockers.extend(
        str(violation["code"]) for violation in _structural_invariant_violations(session)
    )
    try:
        dlq = rabbitmq.infrastructure_dlq_observation()
    except rabbitmq.RabbitMQTopologyError:
        blockers.append("infrastructure_dlq_unavailable")
    else:
        if dlq["messages_ready"] or dlq["messages_unacknowledged"]:
            blockers.append("infrastructure_dlq_not_empty")
    return blockers


def retire_legacy_active_index(
    session: Session,
    *,
    expected_schema_revision: str,
    backup_restore_evidence_id: str,
) -> dict[str, object]:
    """Run the explicit, guarded Cutover schema migration exactly once."""

    if not expected_schema_revision or not backup_restore_evidence_id:
        raise domain_error(
            422,
            "cutover_confirmation_invalid",
            "Cutover confirmation fields must be non-empty",
        )
    schema_revision = session.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
    if schema_revision is None or str(schema_revision) != expected_schema_revision:
        session.rollback()
        raise domain_error(
            409,
            "cutover_schema_revision_mismatch",
            "Reliable-runtime Cutover schema revision does not match preflight",
            {
                "expected_schema_revision": expected_schema_revision,
                "actual_schema_revision": str(schema_revision)
                if schema_revision is not None
                else None,
            },
        )
    blockers = _cutover_mutation_blockers(session)
    if blockers:
        session.rollback()
        raise domain_error(
            409,
            "cutover_precondition_failed",
            "Reliable-runtime Cutover preconditions are not satisfied",
            {"blockers": blockers},
        )
    session.execute(text("LOCK TABLE executions IN SHARE ROW EXCLUSIVE MODE"))
    locked_violations = _structural_invariant_violations(session)
    if locked_violations:
        session.rollback()
        raise domain_error(
            409,
            "cutover_precondition_failed",
            "Reliable-runtime Cutover preconditions changed while acquiring the lock",
            {"blockers": [str(item["code"]) for item in locked_violations]},
        )
    if not _old_active_index_present(session):
        session.rollback()
        logger.info(
            "Reliable-runtime Cutover index already retired; schema_revision=%s "
            "backup_restore_evidence_id=%s",
            expected_schema_revision,
            backup_restore_evidence_id,
        )
        return {
            "status": "completed",
            "schema_revision": expected_schema_revision,
            "backup_restore_evidence_id": backup_restore_evidence_id,
            "old_active_index_present": False,
            "changed": False,
        }
    session.execute(text("DROP INDEX uq_executions_active_adapter"))
    session.commit()
    logger.info(
        "Reliable-runtime Cutover index retired; schema_revision=%s backup_restore_evidence_id=%s",
        expected_schema_revision,
        backup_restore_evidence_id,
    )
    return {
        "status": "completed",
        "schema_revision": expected_schema_revision,
        "backup_restore_evidence_id": backup_restore_evidence_id,
        "old_active_index_present": False,
        "changed": True,
    }


def post_cutover_invariants(
    session: Session,
    *,
    include_broker: bool = True,
) -> dict[str, object]:
    """Verify the complete post-Cutover contract without modifying state."""

    violations = _structural_invariant_violations(session)

    def add(code: str, sample_ids: list[str] | None = None) -> None:
        violations.append(
            {"code": code, "count": len(sample_ids or []) or 1, "sample_ids": sample_ids or []}
        )

    if not settings.rabbitmq_execution_enabled:
        add("rabbitmq_ingress_not_enabled")
    if settings.min_worker_protocol_version != 3:
        add("minimum_worker_protocol_not_v3")
    if settings.legacy_execution_claim_enabled:
        add("legacy_claim_still_enabled")
    if _old_active_index_present(session):
        add("legacy_active_index_still_present")
    worker_state = _worker_readiness(session)
    if not bool(worker_state["all_v3_workers_ready"]):
        workers = list(session.scalars(select(Worker).order_by(Worker.id.asc())))
        not_ready = [
            worker.id
            for worker in workers
            if int(worker.protocol_version or 1) != 3
            or not bool(worker.rabbitmq_execution_v3)
            or worker.isolation_preflight_status != "passed"
            or not isolation_capabilities_ready(worker.isolation_capabilities)
        ][:_INVARIANT_SAMPLE_LIMIT]
        add("worker_v3_isolation_not_ready", [str(value) for value in not_ready])
    broker: dict[str, object]
    if include_broker:
        try:
            observation = rabbitmq.infrastructure_dlq_observation()
        except rabbitmq.RabbitMQTopologyError as error:
            broker = {"status": "unavailable", "error_code": error.code}
            add("infrastructure_dlq_unavailable")
        else:
            broker = {"status": "ready", **observation}
            if observation["messages_ready"] or observation["messages_unacknowledged"]:
                add("infrastructure_dlq_not_empty")
    else:
        broker = {"status": "not_checked"}
    violations.sort(key=lambda item: str(item["code"]))
    revision = session.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
    return {
        "status": "passed" if not violations else "failed",
        "read_only": True,
        "schema_revision": str(revision) if revision is not None else None,
        "worker_readiness": worker_state,
        "broker": broker,
        "violations": violations,
    }
