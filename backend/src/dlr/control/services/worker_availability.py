"""Authoritative effective-online semantics for registered Workers.

The persisted ``Worker.status`` remains the Worker's most recent explicit
register/heartbeat/offline declaration. Availability-sensitive Control paths
must use this module so a stale stored-online row is never mistaken for a
currently usable Worker.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import Select, SQLColumnExpression, and_, func, or_, select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.common.worker_protocol_v4 import ADAPTER_CLEANUP_CAPABILITY
from dlr.control.models import ExecutionAttempt, Worker, WorkerMaintenanceOperation

# A decision can sample the DB clock just before another transaction's
# heartbeat becomes visible. This small tolerance bounds that read race; it
# does not establish clock synchronization or a v4 lease proof.
_HEARTBEAT_FUTURE_TOLERANCE_SECONDS = 1.0


def current_time(session: Session) -> datetime:
    """Capture one wall-clock timestamp for an entire business decision.

    PostgreSQL ``now()`` is fixed at transaction start, which may precede a
    row-lock wait in Test/Start. ``clock_timestamp()`` captures the actual
    decision time after that lock is acquired; callers still reuse this one
    value for every Worker considered by the operation.
    """
    now = session.scalar(select(func.pg_catalog.clock_timestamp()))
    if not isinstance(now, datetime):
        raise RuntimeError("database did not return a current timestamp")
    return now


def force_stop_operation_locked(
    session: Session, worker: Worker, *, attempt: ExecutionAttempt | None = None
) -> UUID | None:
    """Read the current maintenance intent under the caller's Worker lock."""
    if worker.scheduling_state != "disabled":
        return None
    query = select(WorkerMaintenanceOperation.request_id).where(
        WorkerMaintenanceOperation.worker_id == worker.id,
        WorkerMaintenanceOperation.result_version == worker.maintenance_version,
        WorkerMaintenanceOperation.action == "force_stop",
    )
    if attempt is not None:
        query = query.where(
            WorkerMaintenanceOperation.recovery_binding == attempt_reference(attempt)
        )
    return session.scalar(query)


def attempt_reference(attempt: ExecutionAttempt) -> dict[str, object]:
    """Stable ownership fields shared by maintenance and historical receipts."""
    return {
        "attempt_id": attempt.id,
        "fencing_token": attempt.fencing_token,
        "worker_identity_id": str(attempt.worker_identity_id),
        "original_instance_id": str(attempt.worker_instance_id),
        "original_instance_generation": attempt.worker_instance_generation,
    }


def v4_adapter_cleanup_ready(worker: Worker) -> bool:
    flags = worker.isolation_capabilities
    return worker.capacity_state == "ready" and (
        (worker.v4_storage_profile == "ephemeral" and flags.get(ADAPTER_CLEANUP_CAPABILITY) is True)
        or (
            worker.v4_storage_profile == "managed_cache"
            and flags.get("cache_governance_v1") is True
        )
    )


def has_unreconciled_v4_attempt(session: Session, worker: Worker, *, now: datetime) -> bool:
    """Conservatively retain a Worker's unfinished v4 physical responsibility.

    The caller holds the Worker row lock. The read takes no other row locks:
    supported Claim, result and recovery writers also serialize on Worker.
    A terminal Attempt stops blocking only after Control has settled its
    cleanup. An inconsistent Worker ID and identity snapshot blocks both
    possible owners even if a settlement marker is present.
    """
    return session.scalar(unreconciled_v4_attempt_query(worker, now=now)) is not None


def unreconciled_v4_attempt_query(
    worker: Worker | type[Worker], *, now: datetime | SQLColumnExpression[datetime]
) -> Select[tuple[int]]:
    """Share the read predicate with the single-snapshot admin observation.

    This query does not grant authority. Business callers still hold Worker;
    an administrator's unlocked projection is only a historical observation.
    """
    from dlr.control.services.attempt import ACTIVE_ATTEMPT_STATUSES

    return (
        select(ExecutionAttempt.id)
        .where(
            ExecutionAttempt.worker_identity_id.is_not(None),
            or_(
                and_(
                    ExecutionAttempt.worker_id == worker.id,
                    or_(
                        ExecutionAttempt.worker_identity_id != worker.identity_id,
                        and_(
                            ExecutionAttempt.cleanup_settled_at.is_(None),
                            or_(
                                ExecutionAttempt.status.not_in(ACTIVE_ATTEMPT_STATUSES),
                                ExecutionAttempt.worker_instance_id != worker.instance_id,
                                ExecutionAttempt.worker_instance_generation
                                != worker.instance_generation,
                                ExecutionAttempt.lease_expires_at <= now,
                            ),
                        ),
                        and_(
                            ExecutionAttempt.cleanup_settled_at.is_not(None),
                            ExecutionAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
                        ),
                    ),
                ),
                and_(
                    ExecutionAttempt.worker_identity_id == worker.identity_id,
                    ExecutionAttempt.worker_id != worker.id,
                ),
            ),
        )
        .limit(1)
    )


def is_effectively_online(worker: Worker, *, now: datetime) -> bool:
    """Return whether the Worker is stored-online with a fresh heartbeat.

    The inclusive cutoff fixes the boundary contract: a heartbeat whose age
    equals the configured timeout is still effective-online. A timestamp far
    ahead of the decision time cannot prove a fresh heartbeat.
    """
    heartbeat_age_seconds = (
        now.astimezone(UTC) - worker.last_heartbeat.astimezone(UTC)
    ).total_seconds()
    future_tolerance = min(
        _HEARTBEAT_FUTURE_TOLERANCE_SECONDS, settings.worker_heartbeat_timeout_seconds
    )
    return (
        worker.status == "online"
        and -future_tolerance <= heartbeat_age_seconds <= settings.worker_heartbeat_timeout_seconds
        and (
            worker.protocol_version != 4
            or (
                worker.instance_id is not None
                and worker.instance_lease_until is not None
                and worker.instance_lease_until.astimezone(UTC) > now.astimezone(UTC)
            )
        )
    )


def effective_status(worker: Worker, *, now: datetime) -> Literal["online", "offline"]:
    """Map the authoritative predicate to the existing API status values."""
    return "online" if is_effectively_online(worker, now=now) else "offline"


def list_effectively_online_workers(session: Session, *, now: datetime) -> list[Worker]:
    """Return every effective-online Worker in deterministic registration order."""
    stored_online = session.scalars(
        select(Worker).where(Worker.status == "online").order_by(Worker.id.asc())
    ).all()
    return [worker for worker in stored_online if is_effectively_online(worker, now=now)]
