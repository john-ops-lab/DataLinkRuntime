"""A database fence shared by Wave 2 producers and Claim.

Mode changes never acquire business row locks while holding the exclusive
gate. The paused phase reconciles each admitted Execution separately.
"""

from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import Adapter, Execution, WorkerPlacementCutover
from dlr.control.services.adapter import domain_error
from dlr.control.services.input_config import database_now

PlacementMode = Literal["fixed", "paused", "reserved"]
_LOCK_NAMESPACE = 0x444C5250


def _lock(session: Session, *, exclusive: bool) -> None:
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    session.execute(
        text(f"SELECT pg_catalog.{function}(:namespace, 1)"), {"namespace": _LOCK_NAMESPACE}
    )


def mode(session: Session, *, hold: bool = False) -> PlacementMode:
    if hold:
        _lock(session, exclusive=False)
    value = session.scalar(
        select(WorkerPlacementCutover.mode).where(WorkerPlacementCutover.id == 1)
    )
    if value == "fixed":
        return "fixed"
    if value == "paused":
        return "paused"
    if value == "reserved" and settings.worker_remote_v4_enabled:
        return "reserved"
    raise domain_error(503, "placement_authority_unavailable", "Worker placement is unavailable")


def accepting_mode(session: Session) -> PlacementMode:
    """Fence a new acceptance until its enclosing transaction ends."""
    value = mode(session, hold=True)
    if value == "paused":
        raise domain_error(
            503,
            "placement_cutover_paused",
            "New responsibilities are paused for Worker cutover",
            {"retry_after": 1},
        )
    return value


def _unavailable(code: str) -> Exception:
    return domain_error(409, code, "Worker cutover requirements have not been met")


def pause(session: Session) -> str:
    """Owner entry point. Close new business admission and Claim together."""
    _lock(session, exclusive=True)
    row = session.get(WorkerPlacementCutover, 1, populate_existing=True)
    if not settings.worker_remote_v4_enabled or row is None or row.mode == "reserved":
        raise _unavailable("placement_authority_unavailable")
    if row.mode == "fixed":
        row.mode = "paused"
        row.paused_at = database_now(session)
    return row.mode


def _live_responsibilities(session: Session) -> bool:
    return bool(
        session.scalar(
            text("""
        SELECT EXISTS (
            SELECT 1 FROM execution_attempts WHERE status IN ('claimed','running')
               OR (worker_identity_id IS NOT NULL AND cleanup_settled_at IS NULL)
        ) OR EXISTS (
            SELECT 1 FROM adapter_execution_slots WHERE active_attempt_id IS NOT NULL
        ) OR EXISTS (
            SELECT 1 FROM attempt_result_intents_v4 WHERE finalization_id IS NULL
        ) OR EXISTS (
            SELECT 1 FROM worker_cleanup_intents_v4 WHERE completion_id IS NULL
        )
    """)
        )
    )


def convert_waiting(session: Session, execution_id: int) -> bool:
    """Owner entry point. Preserve accepted custody and supersede the old wire."""
    from dlr.control.services import admission, outbox, worker_placement_wait, worker_pool
    from dlr.control.services.execution_cancellation import (
        lock_execution,
        lock_execution_tail,
        lock_reserved_subscription_scope,
    )

    adapter_id = session.scalar(select(Execution.adapter_id).where(Execution.id == execution_id))
    if adapter_id is None or session.get(Adapter, adapter_id, with_for_update=True) is None:
        return False
    lock_reserved_subscription_scope(session, execution_id)
    if admission.lock_admission_scope(session, adapter_id) is None:
        return False
    execution = lock_execution(session, execution_id)
    if (
        execution is None
        or execution.requires_worker_reservation
        or execution.status not in {"queued", "retry_wait"}
    ):
        return False
    if mode(session, hold=True) != "paused" or _live_responsibilities(session):
        raise _unavailable("placement_cutover_not_drained")
    tail = lock_execution_tail(session, execution)
    snapshot = worker_pool.stored_snapshot(execution.route_snapshot)
    if (
        snapshot is None
        or snapshot.route.mode != "fixed"
        or snapshot.route.worker_id != execution.target_worker_id_snapshot
        or execution.admission_released_at is not None
        or tail.slot.active_attempt_id is not None
    ):
        raise _unavailable("placement_cutover_history_unverified")
    moved = next(
        (
            row
            for row in tail.outbox_rows
            if row.dispatch_generation == execution.dispatch_generation
        ),
        None,
    )
    if moved is None or moved.payload_json is None or moved.status not in {"pending", "published"}:
        raise _unavailable("placement_cutover_dispatch_unverified")
    now = database_now(session)
    execution.requires_worker_reservation = True
    execution.target_worker_id = None
    execution.dispatch_generation += 1
    execution.placement_origin = execution.placement_origin or "legacy_v4"
    timeout = (
        None
        if execution.placement_origin == "legacy_v3"
        else settings.worker_placement_wait_timeout_seconds
    )
    if execution.status == "queued":
        worker_placement_wait.start_cycle(execution, now=now, timeout_seconds=timeout)
        outbox.create_dispatch_outbox(session, execution, available_at=now)
    else:
        execution.placement_wait_timeout_seconds = timeout
    outbox.supersede(moved, "placement_cutover_generation")
    session.flush()
    return True


def activate(session: Session) -> str:
    """Owner entry point. Enable reservations only after the durable handoff."""
    _lock(session, exclusive=True)
    row = session.get(WorkerPlacementCutover, 1, populate_existing=True)
    if row is None or not settings.worker_remote_v4_enabled or row.mode == "fixed":
        raise _unavailable("placement_cutover_not_paused")
    if row.mode == "reserved":
        return row.mode
    if (
        _live_responsibilities(session)
        or session.scalar(
            select(Execution.id)
            .where(
                Execution.status.in_(("queued", "retry_wait", "running")),
                ~Execution.requires_worker_reservation,
            )
            .limit(1)
        )
        is not None
    ):
        raise _unavailable("placement_cutover_not_drained")
    # A host may change after this independent read. Placement and Claim
    # recheck the latest instance, capacity and scope under their usual locks.
    ready = session.scalar(
        text("""
        SELECT EXISTS (
            SELECT 1 FROM workers w WHERE w.scheduling_state = 'active'
        ) AND NOT EXISTS (
            SELECT 1 FROM workers w
            LEFT JOIN worker_capacities c ON c.worker_id = w.id
            LEFT JOIN worker_capacity_verifiers v ON v.worker_id = w.id
            LEFT JOIN worker_lease_candidates_v4 l ON l.worker_id = w.id
            WHERE w.scheduling_state = 'active' AND (
                w.protocol_version <> 4 OR w.identity_lifecycle <> 'reserved'
                OR w.capacity_state <> 'ready' OR w.status <> 'online'
                OR w.isolation_preflight_status <> 'passed'
                OR v.worker_identity_id IS DISTINCT FROM w.identity_id
                OR c.worker_identity_id IS DISTINCT FROM w.identity_id
                OR c.instance_id IS DISTINCT FROM w.instance_id
                OR c.instance_generation IS DISTINCT FROM w.instance_generation
                OR c.received_at IS NULL OR c.received_at < :paused
                OR c.sampled_at < clock_timestamp() - INTERVAL '15 seconds'
                OR c.received_at < clock_timestamp() - INTERVAL '15 seconds'
                OR c.storage_ready IS DISTINCT FROM TRUE
                OR c.quarantine_until IS NOT NULL
                OR l.retired IS DISTINCT FROM FALSE
                OR l.instance_id IS DISTINCT FROM w.instance_id
                OR l.instance_generation IS DISTINCT FROM w.instance_generation
                OR l.nominal_until IS DISTINCT FROM w.instance_lease_until
                OR LEAST(l.nominal_until, l.guard_until, l.proof_before) <= clock_timestamp()
            )
        )
    """),
        {"paused": row.paused_at},
    )
    if ready is not True:
        raise _unavailable("placement_cutover_capacity_unverified")
    row.mode = "reserved"
    row.enabled_at = database_now(session)
    return row.mode
