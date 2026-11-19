"""Assignment and lifecycle of one prepaid dispatch generation."""

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from dlr.common import builtin_wheel_v4, managed_files_v4
from dlr.common.cpu_accounting import CPU_ACCOUNTING_VERSION
from dlr.control.models import (
    Adapter,
    AdapterExecutionSlot,
    AttemptLeaseCandidateV4,
    Execution,
    ExecutionAttempt,
    ExecutionOutbox,
    Worker,
    WorkerCapacities,
    WorkerPool,
    WorkerPoolMember,
    WorkerPoolRevision,
    WorkerResourceReservation,
)
from dlr.control.models.worker_capacity import RESOURCE_DIMENSIONS
from dlr.control.schemas.reliable_runtime import ResourceProfile
from dlr.control.schemas.worker_pool import EnvironmentRule, FixedRoute, RouteSnapshot
from dlr.control.services import (
    admission,
    cache_governance,
    outbox,
    rabbitmq,
    worker_availability,
    worker_capacity,
    worker_placement_wait,
    worker_pool,
)
from dlr.control.services.business_credentials_v4 import execution_bindings
from dlr.control.services.execution_cancellation import (
    lock_execution,
    lock_execution_attempts,
    lock_execution_in_admission_order,
    lock_incidents_and_outbox,
    lock_reserved_subscription_scope,
)
from dlr.control.services.worker_stage_capability_v4 import execution_block_reason

MAX_CANDIDATES = 100


def lock_pool(session: Session, execution: Execution) -> WorkerPool | None:
    if execution.requires_worker_reservation and execution.route_pool_id is not None:
        return session.get(
            WorkerPool, execution.route_pool_id, with_for_update=True, populate_existing=True
        )
    return None


def lock_generation(session: Session, execution: Execution) -> WorkerResourceReservation | None:
    return session.scalar(
        select(WorkerResourceReservation)
        .where(
            WorkerResourceReservation.execution_id == execution.id,
            WorkerResourceReservation.dispatch_generation == execution.dispatch_generation,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def lock_target(
    session: Session, execution: Execution
) -> tuple[
    WorkerPool | None, Worker | None, WorkerCapacities | None, WorkerResourceReservation | None
]:
    """Acquire the reserved target suffix after Execution, Attempts and Slot."""
    if not execution.requires_worker_reservation or execution.target_worker_id is None:
        return None, None, None, None
    pool = lock_pool(session, execution)
    worker = session.get(
        Worker, execution.target_worker_id, with_for_update=True, populate_existing=True
    )
    capacity = worker_capacity.lock_capacity(session, worker.id) if worker is not None else None
    return pool, worker, capacity, lock_generation(session, execution)


def _candidate_page(
    session: Session, snapshot: RouteSnapshot, after_worker_id: int | None
) -> tuple[list[int], int | None]:
    route = snapshot.route
    if isinstance(route, FixedRoute):
        return [route.worker_id], None
    ids = list(
        session.scalars(
            select(WorkerPoolMember.worker_id)
            .where(
                WorkerPoolMember.pool_id == route.pool_id,
                WorkerPoolMember.enabled,
                WorkerPoolMember.worker_id > (after_worker_id or 0),
            )
            .order_by(WorkerPoolMember.worker_id)
            .limit(MAX_CANDIDATES + 1)
        )
    )
    if len(ids) <= MAX_CANDIDATES:
        return ids, None
    page = ids[:MAX_CANDIDATES]
    return page, page[-1]


def _ineligible_reason(
    session: Session,
    execution: Execution,
    snapshot: RouteSnapshot,
    pool: WorkerPool | None,
    worker: Worker,
    environment: EnvironmentRule,
) -> str | None:
    language = snapshot.required_language
    if language not in worker.capabilities or language not in environment.toolchains:
        return "environment_mismatch"
    route = snapshot.route
    if isinstance(route, FixedRoute):
        return (
            None
            if route.worker_id == execution.target_worker_id_snapshot == worker.id
            else "no_compatible_worker"
        )
    if (
        pool is None
        or pool.id != route.pool_id
        or not isinstance(snapshot.environment, EnvironmentRule)
    ):
        return "no_compatible_worker"
    revision = session.get(WorkerPoolRevision, (pool.id, route.pool_revision))
    if revision is None or worker_pool.pool_route(revision) != route:
        return "no_compatible_worker"
    member = session.get(WorkerPoolMember, (pool.id, worker.id), populate_existing=True)
    scopes = set()
    if execution.input_source_type == "managed_files":
        scopes.add(managed_files_v4.CAPABILITY)
    if execution.builtin_package_snapshot is not None:
        scopes.add(builtin_wheel_v4.CAPABILITY)
    if not scopes.issubset(environment.builtin_materials):
        return "environment_mismatch"
    reason = worker_pool.member_rule_reason(
        pool,
        revision,
        worker,
        member,
        expected_environment=snapshot.environment,
        verified_environment=environment,
        language=language,
        credential_ids=frozenset(
            ref.credential_id for ref in execution_bindings(session, execution.id)
        ),
        material_scopes=frozenset(scopes),
    )
    return (
        {
            "pool_placement_not_authorized": "scope_denied",
            "worker_environment_incompatible": "environment_mismatch",
            "worker_environment_unknown": "capacity_unknown",
        }.get(reason, "no_compatible_worker")
        if reason is not None
        else None
    )


def _profile(execution: Execution) -> ResourceProfile:
    return ResourceProfile.model_validate(execution.resource_profile_snapshot)


def _lock_slot(session: Session, execution: Execution) -> AdapterExecutionSlot | None:
    lock_execution_attempts(session, execution.id)
    return session.get(
        AdapterExecutionSlot,
        (execution.adapter_id, 0),
        with_for_update=True,
        populate_existing=True,
    )


def _current_outbox(
    rows: tuple[ExecutionOutbox, ...], execution: Execution
) -> ExecutionOutbox | None:
    return next((r for r in rows if r.dispatch_generation == execution.dispatch_generation), None)


def reserve_execution(
    session: Session, execution_id: int, *, observed_change_at: datetime | None = None
) -> UUID | None:
    """Place an awaiting Execution. The caller commits or rolls back the whole decision."""
    session.execute(text("SET LOCAL lock_timeout = '3s'"))
    adapter_id = session.scalar(
        select(Execution.adapter_id).where(
            Execution.id == execution_id, Execution.requires_worker_reservation
        )
    )
    if adapter_id is None:
        return None
    adapter = session.get(Adapter, adapter_id, with_for_update=True, populate_existing=True)
    if adapter is None:
        return None
    lock_reserved_subscription_scope(session, execution_id)
    if admission.lock_admission_scope(session, adapter.id) is None:
        return None
    hint = session.execute(
        select(
            Execution.version_id, Execution.route_snapshot, Execution.placement_after_worker_id
        ).where(
            Execution.id == execution_id,
            Execution.status == "queued",
            ~Execution.cancel_requested,
            Execution.target_worker_id.is_(None),
        )
    ).one_or_none()
    if hint is None:
        return None
    snapshot = worker_pool.stored_snapshot(hint.route_snapshot)
    if snapshot is None or snapshot.required_language != adapter.language:
        return None
    candidates = []
    page, resume_at = _candidate_page(session, snapshot, hint.placement_after_worker_id)
    for worker_id in page:
        if session.get(WorkerCapacities, worker_id) is None:
            continue
        # Existing cache writers acquire this guard before Execution/Worker.
        try:
            cache_governance.ensure_reference_allowed(
                session, worker_id=worker_id, adapter_id=adapter.id, version_id=hint.version_id
            )
        except HTTPException as error:
            if (
                not isinstance(error.detail, dict)
                or error.detail.get("code") != "cache_reclamation_in_progress"
            ):
                raise
        else:
            candidates.append(worker_id)

    execution = lock_execution(session, execution_id)
    if (
        execution is None
        or execution.status != "queued"
        or execution.cancel_requested
        or execution.target_worker_id is not None
        or execution.resource_class != "standard"
    ):
        return None
    now = worker_availability.current_time(session)
    tracks_placement = execution.placement_origin is not None
    capacity_changed = observed_change_at is not None and (
        execution.placement_checked_at is None
        or observed_change_at > execution.placement_checked_at
    )
    if tracks_placement and not worker_placement_wait.can_place(
        execution, now=now, capacity_changed=capacity_changed
    ):
        return None
    slot = _lock_slot(session, execution)
    if slot is None or slot.active_attempt_id is not None:
        if tracks_placement:
            worker_placement_wait.defer(execution, now=now, reason="adapter_slot_busy")
        return None
    execution.placement_after_worker_id = resume_at
    pool = lock_pool(session, execution)
    profile = _profile(execution)
    charge = worker_capacity.charge(profile)
    locked_workers = []
    for worker_id in candidates:
        worker = session.get(Worker, worker_id, with_for_update=True, populate_existing=True)
        if worker is not None:
            locked_workers.append((worker, worker_capacity.lock_capacity(session, worker.id)))
    if not locked_workers:
        if tracks_placement:
            worker_placement_wait.defer(
                execution,
                now=now,
                reason="capacity_unknown" if page else "no_compatible_worker",
                page_remaining=resume_at is not None,
            )
        return None
    broker_targets = rabbitmq.ingress_ready_targets(
        session, [worker.id for worker, _ in locked_workers]
    )
    ranked = []
    reason = "no_compatible_worker"
    for worker, capacity in locked_workers:
        if worker.id not in broker_targets:
            reason = "broker_target_unverified"
            continue
        now = worker_availability.current_time(session)
        environment = worker_capacity.eligible_environment(
            session, worker, capacity, profile, now=now
        )
        if environment is None or capacity is None:
            reason = (
                "worker_draining" if worker.scheduling_state != "active" else "capacity_unknown"
            )
            continue
        blocked_route = _ineligible_reason(session, execution, snapshot, pool, worker, environment)
        if blocked_route is not None:
            reason = blocked_route
            continue
        blocked = execution_block_reason(session, adapter, execution, candidate_worker_id=worker.id)
        if blocked is not None:
            reason = (
                "scope_denied" if blocked.startswith("credentials_") else "environment_mismatch"
            )
            continue
        used = worker_capacity.occupied_resources(session, worker.id)
        if used is None:
            reason = "capacity_unknown"
            continue
        alloc = tuple(getattr(capacity, name) for name in RESOURCE_DIMENSIONS)
        if any(
            occupied + getattr(charge, name) > available
            for name, occupied, available in zip(RESOURCE_DIMENSIONS, used, alloc, strict=True)
        ):
            reason = "capacity_insufficient"
            continue
        ratio = max(
            Fraction(occupied, available) for occupied, available in zip(used, alloc, strict=True)
        )
        assigned = capacity.last_assignment_at or datetime.min.replace(tzinfo=UTC)
        ranked.append(((ratio, assigned, worker.id), worker, capacity))
    if not ranked:
        if tracks_placement:
            worker_placement_wait.defer(
                execution, now=now, reason=reason, page_remaining=resume_at is not None
            )
        return None
    if lock_generation(session, execution) is not None:
        return None
    now = worker_availability.current_time(session)
    if tracks_placement and worker_placement_wait.expired(execution, now=now):
        return None
    for _, worker, capacity in sorted(ranked, key=lambda item: item[0]):
        if (
            worker_capacity.eligible_environment(session, worker, capacity, profile, now=now)
            is None
        ):
            continue
        reservation = WorkerResourceReservation(
            execution_id=execution.id,
            dispatch_generation=execution.dispatch_generation,
            worker_id=worker.id,
            worker_identity_id=worker.identity_id,
            instance_id=capacity.instance_id,
            instance_generation=capacity.instance_generation,
            capacity_version=capacity.capacity_version,
            envelope_fingerprint=capacity.envelope_fingerprint,
            cpu_accounting_version=CPU_ACCOUNTING_VERSION,
            status="held",
            held_until=now + worker_capacity.HELD_TTL,
            **charge.model_dump(),
        )
        session.add(reservation)
        session.flush()
        _, rows = lock_incidents_and_outbox(session, execution.id)
        row = _current_outbox(rows, execution)
        if row is None or row.status != "awaiting_target":
            raise ValueError("Worker reservation lacks its prepaid dispatch intent")
        execution.target_worker_id = worker.id
        execution.placement_after_worker_id = None
        if tracks_placement:
            worker_placement_wait.record_hold(execution)
        capacity.last_assignment_at = now
        outbox.bind_reservation(session, execution, row, reservation)
        session.flush()
        return reservation.id
    if tracks_placement:
        worker_placement_wait.defer(
            execution, now=now, reason="capacity_unknown", page_remaining=resume_at is not None
        )
    return None


def held_valid(
    session: Session,
    execution: Execution,
    worker: Worker,
    capacity: WorkerCapacities | None,
    reservation: WorkerResourceReservation | None,
    pool: WorkerPool | None,
    *,
    now: datetime,
) -> bool:
    if (
        reservation is None
        or capacity is None
        or reservation.status != "held"
        or worker_placement_wait.expired(execution, now=now)
        or reservation.held_until <= now
        or reservation.execution_id != execution.id
        or reservation.dispatch_generation != execution.dispatch_generation
        or execution.target_worker_id != worker.id
        or reservation.worker_id != worker.id
        or reservation.worker_identity_id != worker.identity_id
        or reservation.instance_id != worker.instance_id
        or reservation.instance_generation != worker.instance_generation
        or reservation.capacity_version > capacity.capacity_version
        or reservation.cpu_accounting_version != CPU_ACCOUNTING_VERSION
        or (
            reservation.capacity_version == capacity.capacity_version
            and reservation.envelope_fingerprint != capacity.envelope_fingerprint
        )
    ):
        return False
    snapshot = worker_pool.stored_snapshot(execution.route_snapshot)
    profile = _profile(execution)
    charge = worker_capacity.charge(profile)
    if any(getattr(reservation, name) != getattr(charge, name) for name in RESOURCE_DIMENSIONS):
        return False
    environment = worker_capacity.eligible_environment(session, worker, capacity, profile, now=now)
    return (
        snapshot is not None
        and environment is not None
        and _ineligible_reason(session, execution, snapshot, pool, worker, environment) is None
    )


def claim_fits(session: Session, worker: Worker, capacity: WorkerCapacities | None) -> bool:
    """A downshift cannot authorize a new Claim or erase existing charges."""
    used = worker_capacity.occupied_resources(session, worker.id)
    return (
        capacity is not None
        and used is not None
        and all(
            occupied <= getattr(capacity, name)
            for name, occupied in zip(RESOURCE_DIMENSIONS, used, strict=True)
        )
    )


def invalidate_held(session: Session, execution_id: int) -> bool:
    """Fence the old generation and restore prepaid custody if the hold is no longer valid."""
    session.execute(text("SET LOCAL lock_timeout = '3s'"))
    execution = lock_execution_in_admission_order(session, execution_id)
    if (
        execution is None
        or not execution.requires_worker_reservation
        or execution.status != "queued"
        or execution.cancel_requested
        or execution.target_worker_id is None
        or _lock_slot(session, execution) is None
    ):
        return False
    pool, worker, capacity, reservation = lock_target(session, execution)
    if reservation is None or reservation.status != "held" or worker is None:
        return False
    now = worker_availability.current_time(session)
    adapter = session.get(Adapter, execution.adapter_id)
    if (
        held_valid(session, execution, worker, capacity, reservation, pool, now=now)
        and adapter is not None
        and execution_block_reason(session, adapter, execution) is None
    ):
        return False
    _, rows = lock_incidents_and_outbox(session, execution.id)
    row = _current_outbox(rows, execution)
    if (
        row is None
        or row.reservation_id != reservation.id
        or row.status not in {"pending", "published"}
    ):
        return False
    release_held(reservation, now=now, capacity=capacity)
    outbox.supersede(row, "worker_reservation_invalidated")
    execution.target_worker_id = None
    execution.dispatch_generation += 1
    if execution.placement_origin is not None:
        worker_placement_wait.reopen_after_revocation(execution, now=now)
    # This returns already accepted custody to the backlog even if it is now
    # above the new-admission line. Further ingress then fails the normal gate.
    outbox.create_dispatch_outbox(session, execution, available_at=now)
    return True


def release_held(
    reservation: WorkerResourceReservation | None,
    *,
    now: datetime,
    capacity: WorkerCapacities | None = None,
) -> None:
    if reservation is not None and reservation.status == "held":
        reservation.status = "released"
        reservation.released_at = now
        if capacity is not None:
            worker_capacity.signal_capacity_change(capacity, now=now)


def release_cleaned_attempt(
    session: Session, execution: Execution, attempt: ExecutionAttempt
) -> None:
    """Use only while the verified cleanup transaction holds the Worker row."""
    if not execution.requires_worker_reservation:
        return
    capacity = worker_capacity.lock_capacity(session, attempt.worker_id)
    reservation = session.scalar(
        select(WorkerResourceReservation)
        .where(WorkerResourceReservation.attempt_id == attempt.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        reservation is None
        or reservation.execution_id != execution.id
        or reservation.worker_id != attempt.worker_id
        or reservation.fencing_token != attempt.fencing_token
        or attempt.cleanup_settled_at is None
    ):
        raise ValueError("Worker reservation cleanup binding unavailable")
    if reservation.status in {"claimed", "reclaiming"}:
        reservation.status = "released"
        reservation.released_at = attempt.cleanup_settled_at
        if capacity is not None:
            worker_capacity.signal_capacity_change(capacity, now=attempt.cleanup_settled_at)
        _, rows = lock_incidents_and_outbox(session, execution.id)
        for row in rows:
            if row.reservation_id == reservation.id and row.status == "pending":
                outbox.supersede(row, "worker_reservation_cleanup_settled")


def fence_lost_attempt(
    session: Session, execution: Execution, attempt: ExecutionAttempt, slot: AdapterExecutionSlot
) -> datetime | None:
    """Fence execution authority while retaining the original physical charge."""
    from dlr.control.services.attempt_lease_candidates_v4 import (
        AttemptLeaseCandidateError,
        _already_committed,
    )

    _, worker, _, reservation = lock_target(session, execution)
    if (
        worker is None
        or reservation is None
        or reservation.status != "claimed"
        or reservation.attempt_id != attempt.id
        or reservation.worker_identity_id != worker.identity_id
        or reservation.fencing_token != attempt.fencing_token
        or slot.active_attempt_id != attempt.id
        or slot.fencing_token != attempt.fencing_token
        or execution.status != "running"
        or execution.attempt_count != attempt.attempt_no
    ):
        return None
    candidate = session.get(
        AttemptLeaseCandidateV4, attempt.id, with_for_update=True, populate_existing=True
    )
    if candidate is None or candidate.nominal_until != attempt.lease_expires_at:
        return None
    try:
        _already_committed(session, candidate)
    except AttemptLeaseCandidateError:
        return None
    now = worker_availability.current_time(session)
    lease_end = max(candidate.nominal_until, candidate.guard_until)
    grace = execution.recovery_grace_seconds_snapshot
    if grace is None or grace <= 0 or now < lease_end:
        return None
    attempt.recovery_not_before = lease_end + timedelta(seconds=grace)
    reservation.status = "reclaiming"
    if worker.instance_id == reservation.instance_id:
        worker.capacity_state = "reconciling"
    return now


def retain_terminal_charge(
    session: Session,
    execution: Execution,
    attempt: ExecutionAttempt,
    *,
    now: datetime,
    quarantine: bool,
) -> None:
    """Keep a terminal Attempt's charge until its independent physical settlement."""
    _, worker, capacity, reservation = lock_target(session, execution)
    if (
        worker is None
        or capacity is None
        or reservation is None
        or reservation.status not in {"claimed", "reclaiming"}
        or reservation.attempt_id != attempt.id
        or reservation.fencing_token != attempt.fencing_token
        or reservation.instance_id != attempt.worker_instance_id
        or (quarantine and worker.instance_id != attempt.worker_instance_id)
    ):
        raise ValueError("Terminal Worker reservation binding unavailable")
    reservation.status = "reclaiming"
    if quarantine:
        worker.capacity_state = "quarantined"
        cutoff = now + timedelta(seconds=30)
        capacity.quarantine_until = max(capacity.quarantine_until or cutoff, cutoff)
        worker_capacity.signal_capacity_change(capacity, now=now)
