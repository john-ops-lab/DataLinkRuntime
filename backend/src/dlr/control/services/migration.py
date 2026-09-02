"""Explicit, additive Batch 2 migration inventory and rehearsal tools.

The migration helpers are intentionally administrative operations rather than
startup work.  Inventory and dry-run are read-only.  Pending conversion locks
one Adapter and one legacy Execution at a time, then commits the converted row,
its Admission reservation and its generation-1 Outbox together.  A legacy
running row is never converted in place.
"""

from __future__ import annotations

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
from dlr.control.services import admission, outbox, rabbitmq, reliable_execution
from dlr.control.services import attempt as attempt_service
from dlr.control.services.adapter import domain_error
from dlr.control.services.execution import _persist_credential_binding_snapshots
from dlr.control.services.input_config import database_now


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
    v3_rows = [worker for worker in rows if int(worker.protocol_version or 1) >= 3]
    ready_rows = [worker for worker in v3_rows if bool(worker.rabbitmq_execution_v3)]
    return {
        "worker_count": len(rows),
        "protocol_v3_workers": len(v3_rows),
        "isolation_preflight_passed_workers": len(ready_rows),
        "all_v3_workers_ready": bool(v3_rows) and len(ready_rows) == len(v3_rows),
        # Batch 2 must not claim the Linux sandbox gate.  The persisted
        # registration fact only says that a future v3 Worker reported all
        # required capabilities; the actual enforcement gate is Batch 3.
        "sandbox_gate": "not_run",
        "cutover_ready": False,
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
            "claim_enabled": True,
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
        "dark_launch": {
            "rabbitmq_production_ingress_enabled": bool(settings.rabbitmq_execution_enabled),
            "rabbitmq_canary_enabled": bool(settings.rabbitmq_execution_canary_enabled),
            "ordinary_new_traffic_backend": "legacy"
            if not settings.rabbitmq_execution_enabled
            else "rabbitmq",
            "old_active_index_present": _old_active_index_present(session),
            "legacy_claim_enabled": True,
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
