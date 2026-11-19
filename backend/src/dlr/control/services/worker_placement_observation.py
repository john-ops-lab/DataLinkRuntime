"""Placement observations contain accounting facts and closed reason codes."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, ColumnElement, and_, case, func, literal_column, select
from sqlalchemy.orm import Session

from dlr.control.models import (
    Execution,
    Worker,
    WorkerCapacities,
    WorkerLeaseCandidateV4,
    WorkerResourceReservation,
)
from dlr.control.models.worker_capacity import OCCUPIED_RESERVATION_STATES, RESOURCE_DIMENSIONS
from dlr.control.services.worker_capacity import CAPACITY_FRESHNESS
from dlr.control.services.worker_placement_cutover import mode

_WAIT_REASONS = (
    "adapter_slot_busy",
    "no_compatible_worker",
    "worker_draining",
    "capacity_unknown",
    "capacity_insufficient",
    "environment_mismatch",
    "scope_denied",
    "placement_limit_reached",
    "broker_target_unverified",
)


def _available(now: ColumnElement[datetime]) -> ColumnElement[bool]:
    capacity, lease = WorkerCapacities, WorkerLeaseCandidateV4
    freshness: timedelta = CAPACITY_FRESHNESS
    snapshot_xmax = literal_column(
        "pg_snapshot_xmax(pg_current_snapshot())::text::bigint", type_=BigInteger()
    )
    return and_(
        Worker.scheduling_state == "active",
        Worker.capacity_state == "ready",
        Worker.identity_lifecycle == "reserved",
        Worker.protocol_version == 4,
        capacity.worker_identity_id == Worker.identity_id,
        capacity.instance_id == Worker.instance_id,
        capacity.instance_generation == Worker.instance_generation,
        capacity.storage_ready,
        capacity.quarantine_until.is_(None),
        capacity.sampled_at.between(now - freshness, now),
        capacity.received_at.between(now - freshness, now),
        lease.instance_id == capacity.instance_id,
        lease.instance_generation == capacity.instance_generation,
        ~lease.retired,
        Worker.instance_lease_until == lease.nominal_until,
        now < func.least(lease.nominal_until, lease.guard_until, lease.proof_before),
        # Observation uses a read-only transaction; a visible current tuple
        # must also carry the expected persisted writer transaction.
        literal_column("worker_lease_candidates_v4.xmin::text::bigint")
        == lease.writer_xid % (2**32),
        lease.writer_xid < snapshot_xmax,
        snapshot_xmax - lease.writer_xid < 2**31,
    )


def _summary(
    columns: type[WorkerCapacities] | type[WorkerResourceReservation], *, sum_rows: bool
) -> Any:
    fields: list[Any] = []
    for name in RESOURCE_DIMENSIONS:
        value = getattr(columns, name)
        fields.extend((name, func.coalesce(func.sum(value), 0) if sum_rows else value))
    return func.jsonb_build_object(*fields)


def worker_ledger() -> ColumnElement[dict[str, Any]]:
    """Project the ledger inside the same Worker maintenance observation."""
    capacity, reservation = WorkerCapacities, WorkerResourceReservation
    now = func.statement_timestamp()
    envelope = (
        select(
            func.jsonb_build_object(
                "allocatable",
                case((_available(now), _summary(capacity, sum_rows=False))),
                "sampled_at",
                capacity.sampled_at,
                "quarantine_until",
                capacity.quarantine_until,
            )
        )
        .select_from(capacity)
        .outerjoin(WorkerLeaseCandidateV4, WorkerLeaseCandidateV4.worker_id == capacity.worker_id)
        .where(capacity.worker_id == Worker.id)
        .correlate(Worker)
        .scalar_subquery()
    )
    fields: list[Any] = ["capacity", envelope]
    for state in OCCUPIED_RESERVATION_STATES:
        totals = (
            select(_summary(reservation, sum_rows=True))
            .select_from(reservation)
            .where(reservation.worker_id == Worker.id, reservation.status == state)
            .correlate(Worker)
            .scalar_subquery()
        )
        fields.extend((state, totals))
    return func.jsonb_build_object(*fields)


def cluster_metrics(session: Session) -> dict[str, object]:
    placement_mode = mode(session)
    if placement_mode == "fixed":
        return {"mode": placement_mode}
    now = func.statement_timestamp()
    capacity, reservation = WorkerCapacities, WorkerResourceReservation
    allocatable = session.scalar(
        select(_summary(capacity, sum_rows=True))
        .select_from(capacity)
        .join(Worker, Worker.id == capacity.worker_id)
        .join(WorkerLeaseCandidateV4, WorkerLeaseCandidateV4.worker_id == Worker.id)
        .where(_available(now))
    )
    occupied = {
        state: totals
        for state, totals in session.execute(
            select(reservation.status, _summary(reservation, sum_rows=True))
            .where(reservation.status.in_(OCCUPIED_RESERVATION_STATES))
            .group_by(reservation.status)
        )
    }
    reason = case(
        (Execution.placement_reason.in_(_WAIT_REASONS), Execution.placement_reason),
        else_="capacity_unknown",
    )
    rows = session.execute(
        select(reason, func.count(), func.min(Execution.queued_at))
        .where(
            Execution.requires_worker_reservation,
            Execution.status == "queued",
            Execution.target_worker_id.is_(None),
        )
        .group_by(reason)
    ).all()
    observed_at = session.scalar(select(now))
    oldest = min((queued for _, _, queued in rows if queued is not None), default=None)
    return {
        "mode": placement_mode,
        "allocatable": allocatable,
        **{
            state: occupied.get(state, dict.fromkeys(RESOURCE_DIMENSIONS, 0))
            for state in OCCUPIED_RESERVATION_STATES
        },
        "waiting": {name: count for name, count, _ in rows},
        "oldest_wait_seconds": (
            max(0, int((observed_at - oldest).total_seconds()))
            if oldest is not None and observed_at is not None
            else None
        ),
    }
