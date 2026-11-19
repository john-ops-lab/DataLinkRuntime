"""Transaction-scoped capacity for independently verified v4 Worker instances."""

from datetime import datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from dlr.common.cpu_accounting import CPU_ACCOUNTING_VERSION, CPU_MIN_MILLIS, attempt_cpu_millis
from dlr.control.models import ExecutionAttempt, Worker, WorkerCapacities, WorkerResourceReservation
from dlr.control.models.worker_capacity import OCCUPIED_RESERVATION_STATES, RESOURCE_DIMENSIONS
from dlr.control.schemas.reliable_runtime import ResourceProfile
from dlr.control.schemas.worker import isolation_capabilities_ready
from dlr.control.schemas.worker_pool import EnvironmentRule, Fingerprint
from dlr.control.services import worker_availability, worker_lease_candidates_v4

CAPACITY_FRESHNESS = timedelta(seconds=15)
HELD_TTL = timedelta(seconds=30)


class Resources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cpu_millis: StrictInt = Field(ge=0, lt=2**63)
    memory_bytes: StrictInt = Field(ge=0, lt=2**63)
    pids: StrictInt = Field(ge=0, lt=2**63)
    tmp_bytes: StrictInt = Field(ge=0, lt=2**63)
    slots: StrictInt = Field(ge=0, lt=2**63)


class VerifiedHostEnvelope(BaseModel):
    """Internal facts from a trusted verifier, with CPU rounded conservatively.

    The verifier floors host CPU and ceilings Agent/system headroom in millicores.
    Regular Worker registration and heartbeat never submit this object.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_identity_id: UUID
    instance_id: UUID
    instance_generation: StrictInt = Field(gt=0, lt=2**63)
    sampled_at: datetime
    envelope_fingerprint: Fingerprint
    total: Resources
    headroom: Resources
    nofile: StrictInt = Field(gt=0, lt=2**63)
    storage_ready: StrictBool
    environment: EnvironmentRule


def _fresh(observed_at: datetime, now: datetime) -> bool:
    return observed_at.utcoffset() is not None and now - CAPACITY_FRESHNESS <= observed_at <= now


def lock_capacity(session: Session, worker_id: int) -> WorkerCapacities | None:
    """Use only after locking Worker, which serializes the initial insert too."""
    return session.get(WorkerCapacities, worker_id, with_for_update=True, populate_existing=True)


def record_verified_capacity(
    session: Session, worker_id: int, envelope: VerifiedHostEnvelope
) -> WorkerCapacities:
    """Persist a fresh verified sample. The caller owns the enclosing transaction."""
    worker = session.get(Worker, worker_id, with_for_update=True, populate_existing=True)
    current = lock_capacity(session, worker_id) if worker is not None else None
    now = worker_availability.current_time(session)
    if (
        worker is None
        or worker.identity_lifecycle != "reserved"
        or worker.identity_id != envelope.worker_identity_id
        or worker.instance_id != envelope.instance_id
        or worker.instance_generation != envelope.instance_generation
        or worker.isolation_preflight_status != "passed"
        or not isolation_capabilities_ready(worker.isolation_capabilities)
        or not _fresh(envelope.sampled_at, now)
        or (current is not None and envelope.sampled_at <= current.sampled_at)
        or not worker_lease_candidates_v4.has_current_committed_lease(session, worker, now=now)
    ):
        raise ValueError("Worker capacity sample is unavailable")
    # Allow a genuine downshift below reserved headroom to record zero allocatable.
    allocatable = {
        name: max(0, getattr(envelope.total, name) - getattr(envelope.headroom, name))
        for name in RESOURCE_DIMENSIONS
    }
    if current is None:
        current = WorkerCapacities(worker_id=worker_id, capacity_version=0)
        session.add(current)
    current.worker_identity_id = envelope.worker_identity_id
    current.instance_id = envelope.instance_id
    current.instance_generation = envelope.instance_generation
    current.capacity_version += 1
    current.sampled_at = envelope.sampled_at
    current.received_at = now
    current.envelope_fingerprint = envelope.envelope_fingerprint
    current.verified_environment = envelope.environment.model_dump(mode="json")
    current.nofile = envelope.nofile
    current.storage_ready = envelope.storage_ready
    for name, value in allocatable.items():
        setattr(current, name, value)
    session.flush()
    return current


def charge(profile: ResourceProfile) -> Resources:
    return Resources(
        cpu_millis=attempt_cpu_millis(profile.cpu_cores),
        memory_bytes=profile.memory_bytes,
        pids=profile.pids,
        tmp_bytes=profile.tmp_bytes,
        slots=1,
    )


def eligible_environment(
    session: Session,
    worker: Worker,
    capacity: WorkerCapacities | None,
    profile: ResourceProfile,
    *,
    now: datetime,
) -> EnvironmentRule | None:
    if (
        capacity is None
        or worker.protocol_version != 4
        or worker.identity_lifecycle != "reserved"
        or worker.scheduling_state != "active"
        or worker.capacity_state != "ready"
        or worker.isolation_preflight_status != "passed"
        or not isolation_capabilities_ready(worker.isolation_capabilities)
        or not worker_availability.is_effectively_online(worker, now=now)
        or capacity.worker_identity_id != worker.identity_id
        or capacity.instance_id != worker.instance_id
        or capacity.instance_generation != worker.instance_generation
        or not _fresh(capacity.sampled_at, now)
        or not _fresh(capacity.received_at, now)
        or not capacity.storage_ready
        or profile.nofile > capacity.nofile
        or not worker_lease_candidates_v4.has_current_committed_lease(session, worker, now=now)
        or worker_availability.has_unreconciled_v4_attempt(session, worker, now=now)
    ):
        return None
    try:
        return EnvironmentRule.model_validate(capacity.verified_environment)
    except ValidationError:
        return None


def occupied_resources(session: Session, worker_id: int) -> tuple[int, ...] | None:
    """Read all occupied charges under Worker/Capacity locks, including expired held."""
    reservation = WorkerResourceReservation
    used = session.execute(
        select(
            func.count().filter(
                or_(
                    reservation.cpu_accounting_version.is_distinct_from(CPU_ACCOUNTING_VERSION),
                    reservation.cpu_millis < CPU_MIN_MILLIS,
                )
            ),
            *(
                func.coalesce(func.sum(getattr(reservation, name)), 0)
                for name in RESOURCE_DIMENSIONS
            ),
        ).where(
            reservation.worker_id == worker_id,
            reservation.status.in_(OCCUPIED_RESERVATION_STATES),
        )
    ).one()
    if used[0]:
        return None

    # An old W1 Attempt may hold physical responsibility without a ledger charge.
    # The cutover must drain it before any capacity can be allocated alongside it.
    uncharged = session.scalar(
        select(ExecutionAttempt.id)
        .where(
            ExecutionAttempt.worker_id == worker_id,
            ExecutionAttempt.cleanup_settled_at.is_(None),
            or_(
                ExecutionAttempt.worker_identity_id.is_not(None),
                ExecutionAttempt.status.in_(("claimed", "running")),
            ),
            ~select(reservation.id)
            .where(
                reservation.attempt_id == ExecutionAttempt.id,
                reservation.status.in_(OCCUPIED_RESERVATION_STATES),
            )
            .exists(),
        )
        .limit(1)
    )
    return None if uncharged is not None else tuple(int(value) for value in used[1:])
