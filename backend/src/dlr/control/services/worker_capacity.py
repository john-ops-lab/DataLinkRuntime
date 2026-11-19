"""Transaction-scoped capacity for independently verified v4 Worker instances."""

from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from dlr.common.cpu_accounting import CPU_ACCOUNTING_VERSION, CPU_MIN_MILLIS, attempt_cpu_millis
from dlr.common.worker_capacity_report import (
    CapacityObservation,
    Resources,
    SignedCapacityObservation,
    verify_observation,
)
from dlr.control.models import (
    ExecutionAttempt,
    Worker,
    WorkerCapacities,
    WorkerCapacityVerifier,
    WorkerEnvironmentClass,
    WorkerResourceReservation,
)
from dlr.control.models.worker_capacity import OCCUPIED_RESERVATION_STATES, RESOURCE_DIMENSIONS
from dlr.control.schemas.reliable_runtime import ResourceProfile
from dlr.control.schemas.worker import isolation_capabilities_ready
from dlr.control.schemas.worker_pool import EnvironmentRule
from dlr.control.services import worker_availability, worker_lease_candidates_v4

CAPACITY_FRESHNESS = timedelta(seconds=15)
HELD_TTL = timedelta(seconds=30)


class VerifiedHostEnvelope(CapacityObservation):
    """Internal facts from a trusted verifier, with CPU rounded conservatively.

    The verifier floors host CPU and ceilings Agent/system headroom in millicores.
    Regular Worker registration and heartbeat never submit this object.
    """

    environment: EnvironmentRule


def _fresh(observed_at: datetime, now: datetime) -> bool:
    return observed_at.utcoffset() is not None and now - CAPACITY_FRESHNESS <= observed_at <= now


def lock_capacity(session: Session, worker_id: int) -> WorkerCapacities | None:
    """Use only after locking Worker, which serializes the initial insert too."""
    return session.get(WorkerCapacities, worker_id, with_for_update=True, populate_existing=True)


def approve_host_verifier(
    session: Session, worker_id: int, environment_name: str, public_key: bytes
) -> None:
    """Owner entry point for a disabled, reserved identity's deployed verifier."""
    worker = session.get(Worker, worker_id, with_for_update=True, populate_existing=True)
    if (
        worker is None
        or worker.identity_lifecycle != "reserved"
        or len(public_key) != 32
        or public_key == bytes(32)
        or session.get(WorkerEnvironmentClass, environment_name) is None
    ):
        raise ValueError("Worker capacity authority is unavailable")
    prior = session.get(WorkerCapacityVerifier, worker_id)
    if prior is not None:
        if (
            prior.worker_identity_id != worker.identity_id
            or prior.public_key != public_key
            or prior.environment_name != environment_name
        ):
            raise ValueError("Worker capacity authority conflicts")
        return
    if worker.scheduling_state != "disabled":
        raise ValueError("Worker must be disabled before approving its capacity verifier")
    session.add(
        WorkerCapacityVerifier(
            worker_id=worker_id,
            worker_identity_id=worker.identity_id,
            public_key=public_key,
            environment_name=environment_name,
        )
    )


def record_attested_capacity(
    session: Session, worker: Worker, report: SignedCapacityObservation
) -> WorkerCapacities:
    """Validate independently enrolled host authority under the Worker lock."""
    approved = session.get(WorkerCapacityVerifier, worker.id, populate_existing=True)
    if (
        approved is None
        or approved.worker_identity_id != worker.identity_id
        or not verify_observation(report, approved.public_key)
    ):
        raise ValueError("Worker capacity observation is unauthorized")
    entry = session.get(WorkerEnvironmentClass, approved.environment_name)
    if entry is None:
        raise ValueError("Worker environment is unavailable")
    environment = EnvironmentRule.model_validate(entry, from_attributes=True)
    envelope = VerifiedHostEnvelope.model_validate(
        report.observation.model_dump() | {"environment": environment}
    )
    capacity = record_verified_capacity(session, worker.id, envelope)
    quarantine = capacity.quarantine_until
    if (
        quarantine is not None
        and capacity.sampled_at >= quarantine
        and capacity.storage_ready
        and worker.isolation_preflight_at is not None
        and worker.isolation_preflight_at >= quarantine
        and not worker_availability.has_unreconciled_v4_attempt(
            session, worker, now=capacity.received_at
        )
    ):
        capacity.quarantine_until = None
        worker.capacity_state = "ready"
        signal_capacity_change(capacity, now=capacity.received_at)
    return capacity


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
    environment = envelope.environment.model_dump(mode="json")
    changed = (
        current is None
        or current.placement_changed_at is None
        or not _fresh(current.sampled_at, now)
        or not _fresh(current.received_at, now)
        or current.instance_id != envelope.instance_id
        or current.verified_environment != environment
        or current.storage_ready != envelope.storage_ready
        or current.nofile != envelope.nofile
        or any(getattr(current, name) != value for name, value in allocatable.items())
    )
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
    current.verified_environment = environment
    current.nofile = envelope.nofile
    current.storage_ready = envelope.storage_ready
    for name, value in allocatable.items():
        setattr(current, name, value)
    if changed:
        signal_capacity_change(current, now=now)
    session.flush()
    return current


def signal_capacity_change(capacity: WorkerCapacities, *, now: datetime) -> None:
    """Persist a wakeup fact while retaining the Worker capacity lock."""
    from dlr.control.services.worker_placement_scheduler import notify_capacity

    capacity.placement_changed_at = now
    notify_capacity()


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
        or capacity.quarantine_until is not None
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
