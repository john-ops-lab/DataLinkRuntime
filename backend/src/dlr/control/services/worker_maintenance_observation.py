"""Secret-free administrator observations, never Worker execution authority."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, or_, select, text, true
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.models import ExecutionAttempt, Worker, WorkerMaintenanceOperation
from dlr.control.schemas.worker import REQUIRED_ISOLATION_CAPABILITIES
from dlr.control.security import Principal
from dlr.control.services import worker_availability
from dlr.control.services.attempt import ACTIVE_ATTEMPT_STATUSES
from dlr.control.services.worker_identity_reservation import _actor
from dlr.control.services.worker_placement_observation import worker_ledger


class ObservationNotFound(Exception):
    """The exact supported Worker/operation was not visible in this snapshot."""


def _same_actor(principal: Principal) -> ColumnElement[bool]:
    kind, actor_id = _actor(principal)
    return and_(
        WorkerMaintenanceOperation.actor_kind == kind,
        WorkerMaintenanceOperation.actor_id.is_(None)
        if actor_id is None
        else WorkerMaintenanceOperation.actor_id == actor_id,
    )


def _readonly(session: Session) -> None:
    # Fresh session, same finite management pool, no ORM writes or row locks.
    # Configuration statements do not read business data; the one SELECT below
    # is the sole data snapshot, even under READ COMMITTED.
    session.execute(text("SET TRANSACTION READ ONLY"))
    session.execute(text("SET LOCAL statement_timeout = '3000ms'"))
    session.execute(text("SET LOCAL lock_timeout = '1000ms'"))


def read_status(
    factory: sessionmaker[Session], worker_id: int, principal: Principal
) -> dict[str, object]:
    if not 0 < worker_id < 2**63:
        raise ObservationNotFound
    clock = select(func.statement_timestamp().label("observed_at")).cte("observation_clock")
    belongs = or_(
        ExecutionAttempt.worker_id == Worker.id,
        ExecutionAttempt.worker_identity_id == Worker.identity_id,
    )
    unsettled = (
        select(func.count())
        .select_from(ExecutionAttempt)
        .where(
            belongs,
            or_(
                ExecutionAttempt.cleanup_settled_at.is_(None),
                ExecutionAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
                ExecutionAttempt.worker_id != Worker.id,
                ExecutionAttempt.worker_identity_id.is_distinct_from(Worker.identity_id),
            ),
        )
        .correlate(Worker)
        .scalar_subquery()
    )
    active = (
        select(func.count())
        .select_from(ExecutionAttempt)
        .where(belongs, ExecutionAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES))
        .correlate(Worker)
        .scalar_subquery()
    )
    unreconciled = (
        worker_availability.unreconciled_v4_attempt_query(Worker, now=clock.c.observed_at)
        .correlate(Worker, clock)
        .exists()
    )
    op = WorkerMaintenanceOperation
    current_action = (
        select(op.action)
        .where(op.worker_id == Worker.id, op.result_version == Worker.maintenance_version)
        .correlate(Worker)
        .scalar_subquery()
    )
    current_drain = (
        select(
            func.jsonb_build_object(
                "request_id",
                op.request_id,
                "action",
                op.action,
                "expected_version",
                op.expected_version,
                "result_version",
                op.result_version,
                "same_actor",
                _same_actor(principal),
            )
        )
        .where(
            op.worker_id == Worker.id,
            op.result_version == Worker.maintenance_version,
            op.action == "drain",
        )
        .correlate(Worker)
        .scalar_subquery()
    )
    disposition = (
        select(
            func.jsonb_build_object(
                "operation_id",
                op.request_id,
                "disposition_id",
                op.recovery_disposition_id,
                "kind",
                op.recovery_disposition["kind"],
                "reason",
                op.recovery_disposition["reason"],
                "execution_id",
                op.recovery_binding["execution_id"],
                "attempt_id",
                op.recovery_binding["attempt"]["attempt_id"],
                "attempt_status",
                op.recovery_disposition["attempt_status"],
                "execution_status",
                op.recovery_disposition["execution_status"],
            )
        )
        .where(op.worker_id == Worker.id, op.recovery_disposition_id.is_not(None))
        .order_by(op.result_version.desc())
        .limit(1)
        .correlate(Worker)
        .scalar_subquery()
    )
    query = (
        select(
            Worker,
            clock.c.observed_at,
            unsettled,
            active,
            unreconciled,
            current_drain,
            disposition,
            current_action,
            worker_ledger(),
        )
        .select_from(Worker)
        .join(clock, true())
        .where(
            Worker.id == worker_id,
            Worker.identity_lifecycle == "reserved",
            Worker.protocol_version == 4,
        )
    )
    with factory() as session:
        _readonly(session)
        row = session.execute(query).one_or_none()
        if row is None:
            raise ObservationNotFound
        (
            worker,
            observed_at,
            unsettled_count,
            active_count,
            has_unreconciled,
            drain,
            last,
            action,
            ledger,
        ) = row
        return {
            "worker_id": worker.id,
            "worker_identity_id": str(worker.identity_id),
            "scheduling_state": worker.scheduling_state,
            "maintenance_version": worker.maintenance_version,
            "capacity_state": worker.capacity_state,
            "placement_ledger": ledger
            if ledger["capacity"] or ledger["reclaiming"]["slots"]
            else None,
            "online": worker_availability.is_effectively_online(worker, now=observed_at),
            "instance_id": str(worker.instance_id) if worker.instance_id is not None else None,
            "instance_generation": worker.instance_generation,
            "instance_lease_until": worker.instance_lease_until,
            "unsettled_attempts": unsettled_count,
            "blocked_reason": "worker_cleanup_unsettled" if unsettled_count else None,
            "last_recovery_disposition": last,
            "observation_schema": 1,
            "observed_at": observed_at,
            "protocol_version": worker.protocol_version,
            "enrolled": worker.identity_enrolled_at is not None,
            "isolation_preflight_status": worker.isolation_preflight_status,
            "isolation_preflight_at": worker.isolation_preflight_at,
            "isolation_capabilities": {
                key: (worker.isolation_capabilities or {}).get(key) is True
                for key in sorted(REQUIRED_ISOLATION_CAPABILITIES)
            },
            "capabilities": worker.capabilities,
            "active_attempts": active_count,
            "has_unreconciled_attempts": has_unreconciled,
            "current_drain_operation": drain,
            "current_maintenance_action": action,
            "force_stop_available": (
                action == "drain" and worker.scheduling_state == "draining" and active_count > 0
            ),
        }


def read_operation(
    factory: sessionmaker[Session], worker_id: int, request_id: UUID, principal: Principal
) -> dict[str, object]:
    if not 0 < worker_id < 2**63 or request_id.int == 0:
        raise ObservationNotFound
    op = WorkerMaintenanceOperation
    query = (
        select(
            op.request_id,
            op.worker_id,
            op.action,
            op.expected_version,
            op.result_version,
            op.created_at,
            _same_actor(principal),
            Worker.identity_id,
            Worker.maintenance_version,
            Worker.scheduling_state,
            func.statement_timestamp(),
        )
        .join(Worker, Worker.id == op.worker_id)
        .where(
            op.request_id == request_id,
            op.worker_id == worker_id,
            op.action.in_(("drain", "activate", "force_stop")),
            Worker.identity_lifecycle == "reserved",
            Worker.protocol_version == 4,
        )
    )
    with factory() as session:
        _readonly(session)
        row = session.execute(query).one_or_none()
        if row is None:
            raise ObservationNotFound
        (
            operation_id,
            owner,
            action,
            expected,
            result,
            created,
            same_actor,
            identity,
            version,
            state,
            observed,
        ) = row
        return {
            "operation": {
                "request_id": str(operation_id),
                "worker_id": owner,
                "action": action,
                "expected_version": expected,
                "result_version": result,
                "created_at": created,
                "same_actor": same_actor,
            },
            "current": {
                "worker_identity_id": str(identity),
                "maintenance_version": version,
                "scheduling_state": state,
                "observed_at": observed,
            },
            "applies_to_current_version": version == result,
        }
