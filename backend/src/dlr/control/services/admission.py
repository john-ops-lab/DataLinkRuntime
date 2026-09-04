"""Atomic RabbitMQ business Admission counters and reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.common.jcs import canonicalize
from dlr.control.models import (
    Adapter,
    AdapterExecutionAdmission,
    Execution,
    GlobalExecutionAdmission,
)
from dlr.control.services.adapter import domain_error

RABBITMQ_OUTSTANDING_STATUSES = ("queued", "running", "retry_wait")
RABBITMQ_TERMINAL_STATUSES = ("succeeded", "dead_letter", "cancelled", "expired")
_admission_reconcile_cursor: tuple[int | None, int | None] = (None, None)


@dataclass(frozen=True)
class AdmissionReconcileReport:
    adapters_checked: int
    adapter_count_delta: int
    adapter_bytes_delta: int
    global_count_delta: int
    global_bytes_delta: int
    next_adapter_id: int | None = None
    next_execution_id: int | None = None
    complete: bool = True


def lock_admission_scope(
    session: Session, adapter_id: int
) -> tuple[AdapterExecutionAdmission, GlobalExecutionAdmission] | None:
    """Lock one Adapter's Admission scope in the canonical order.

    RabbitMQ cancellation and permanent stop/delete must acquire the same
    ``Adapter -> AdapterAdmission -> Global`` locks as ingress and repair
    before they lock an Execution or its Outbox rows.  Returning ``None`` for
    a deleted Adapter lets a caller re-read the authoritative Execution after
    the Adapter lock without manufacturing a partial release.
    """
    if session.get(Adapter, adapter_id, with_for_update=True) is None:
        return None
    return _ensure_adapter_counter(session, adapter_id), _ensure_global_counter(session)


def logical_input_bytes(
    source_type: str,
    runtime_input: Any,
    input_snapshot: dict[str, Any] | None = None,
) -> int:
    """Compute the immutable business charge without counting Outbox bytes."""

    if source_type == "none":
        return 0
    if source_type == "managed_files":
        artifacts = (input_snapshot or {}).get("artifacts", [])
        if not isinstance(artifacts, list):
            raise ValueError("managed-files input snapshot artifacts must be a list")
        total = 0
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise ValueError("managed-files input snapshot artifact is invalid")
            size = artifact.get("size_bytes")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ValueError("managed-files input snapshot size is invalid")
            total += size
        return total
    if source_type in {"json", "webhook"}:
        return len(canonicalize(runtime_input))
    raise ValueError("unsupported input source type")


def _ensure_adapter_counter(session: Session, adapter_id: int) -> AdapterExecutionAdmission:
    counter = session.scalar(
        select(AdapterExecutionAdmission)
        .where(AdapterExecutionAdmission.adapter_id == adapter_id)
        .with_for_update()
    )
    if counter is None:
        counter = AdapterExecutionAdmission(adapter_id=adapter_id)
        session.add(counter)
        session.flush()
    return counter


def _ensure_global_counter(session: Session) -> GlobalExecutionAdmission:
    # The migration seeds the singleton, but an existing installation or a
    # repair after an interrupted bootstrap may not have the row.  Upsert it
    # before taking the row lock so two concurrent first reservations cannot
    # race into duplicate-key failures.
    session.execute(
        pg_insert(GlobalExecutionAdmission)
        .values(singleton_key="global")
        .on_conflict_do_nothing(index_elements=[GlobalExecutionAdmission.singleton_key])
    )
    counter = session.scalar(
        select(GlobalExecutionAdmission)
        .where(GlobalExecutionAdmission.singleton_key == "global")
        .with_for_update()
    )
    if counter is None:
        raise RuntimeError("global admission counter is unavailable")
    return counter


def reserve_admission(
    session: Session,
    adapter_id: int,
    logical_bytes: int,
    *,
    outbox_additional_bytes: int = 0,
) -> None:
    """Reserve one RabbitMQ outstanding unit under Adapter→Global locks."""

    if isinstance(logical_bytes, bool) or not isinstance(logical_bytes, int) or logical_bytes < 0:
        raise ValueError("logical input bytes must be a non-negative integer")
    # Keep the same lock order as the ingress transaction even when this
    # helper is called directly: Adapter -> Adapter counter -> Global.
    if session.get(Adapter, adapter_id, with_for_update=True) is None:
        raise domain_error(404, "adapter_not_found", "Adapter not found")
    adapter = _ensure_adapter_counter(session, adapter_id)
    if (
        adapter.outstanding_count + 1 > settings.admission_adapter_max_count
        or adapter.outstanding_bytes + logical_bytes > settings.admission_adapter_max_bytes
    ):
        raise domain_error(
            429,
            "adapter_queue_full",
            "Adapter reliable queue capacity is full",
            {"retry_after": 1},
        )
    global_counter = _ensure_global_counter(session)
    if (
        global_counter.outstanding_count + 1 > settings.admission_global_max_count
        or global_counter.outstanding_bytes + logical_bytes > settings.admission_global_max_bytes
    ):
        raise domain_error(
            503,
            "runtime_capacity_full",
            "Runtime reliable capacity is full",
            {"retry_after": 1},
        )
    # The argument is intentionally accepted so the unified ingress can
    # document that Outbox bytes are checked separately; it is never charged
    # to Business Outstanding.
    del outbox_additional_bytes
    adapter.outstanding_count += 1
    adapter.outstanding_bytes += logical_bytes
    global_counter.outstanding_count += 1
    global_counter.outstanding_bytes += logical_bytes


def release_admission_once(
    session: Session, execution: Execution, *, now: datetime | None = None
) -> bool:
    """Release an Execution charge exactly once after terminal transition."""

    if execution.dispatch_backend != "rabbitmq" or execution.admission_released_at is not None:
        return False
    if execution.status not in RABBITMQ_TERMINAL_STATUSES:
        return False
    adapter = _ensure_adapter_counter(session, execution.adapter_id)
    global_counter = _ensure_global_counter(session)
    logical_bytes = execution.logical_input_bytes
    if (
        adapter.outstanding_count < 1
        or global_counter.outstanding_count < 1
        or adapter.outstanding_bytes < logical_bytes
        or global_counter.outstanding_bytes < logical_bytes
    ):
        raise RuntimeError("admission counter drift prevents terminal release")
    adapter.outstanding_count -= 1
    adapter.outstanding_bytes -= logical_bytes
    global_counter.outstanding_count -= 1
    global_counter.outstanding_bytes -= logical_bytes
    if now is not None:
        execution.admission_released_at = now
    else:
        from dlr.control.services.input_config import database_now

        execution.admission_released_at = database_now(session)
    return True


def _reconcile_limit(batch_size: int | None) -> int:
    limit = settings.admission_reconcile_batch_size if batch_size is None else batch_size
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("admission reconcile batch size must be a positive integer")
    return min(limit, 1_000)


def _select_reconcile_adapter_ids(
    session: Session,
    *,
    adapter_id: int | None,
    after_adapter_id: int | None,
    after_execution_id: int | None,
    limit: int,
) -> list[int]:
    """Select at most one bounded Adapter page in ascending lock order."""

    if adapter_id is not None:
        query = select(Adapter.id).where(Adapter.id == adapter_id)
    elif after_execution_id is not None and after_adapter_id is not None:
        # An execution page overflowed while processing this Adapter. Finish
        # that Adapter before advancing the Adapter cursor, otherwise a later
        # page could leave terminal release markers behind indefinitely.
        query = select(Adapter.id).where(Adapter.id == after_adapter_id)
        query = query.order_by(Adapter.id.asc()).limit(limit).with_for_update()
        selected = list(session.scalars(query))
        if selected:
            return selected
        # A deleted Adapter has no remaining responsibility.  Continue with
        # the next Adapter rather than treating a stale cursor as end-of-data.
        query = select(Adapter.id).where(Adapter.id > after_adapter_id)
    else:
        query = select(Adapter.id)
        if after_adapter_id is not None:
            query = query.where(Adapter.id > after_adapter_id)
    query = query.order_by(Adapter.id.asc()).limit(limit).with_for_update()
    return list(session.scalars(query))


def reconcile_admission(
    session: Session,
    *,
    adapter_id: int | None = None,
    batch_size: int | None = None,
    after_adapter_id: int | None = None,
    after_execution_id: int | None = None,
) -> AdmissionReconcileReport:
    """Repair a bounded page without changing RabbitMQ Execution states.

    The full reconciler is cursor-driven: one transaction locks at most one
    Adapter page, its counters, the Global singleton and one bounded terminal
    Execution page.  Locks are acquired as Adapter -> AdapterAdmission ->
    Global -> Execution/Outbox, matching ingress, cancellation and
    stop/delete.  A continuation cursor is returned when either page has
    more work, so a large installation cannot hold every Adapter/Execution
    lock for one polling cycle.
    """

    limit = _reconcile_limit(batch_size)
    if adapter_id is not None and after_adapter_id is not None and after_adapter_id != adapter_id:
        raise ValueError("targeted admission reconciliation cursor does not match adapter_id")
    if after_execution_id is not None and after_adapter_id is None:
        raise ValueError("execution cursor requires an adapter cursor")

    # Lock every selected Adapter first.  Do not use the old expected/counter
    # union: a stale zero-responsibility counter is repaired when its Adapter
    # reaches the page, while the page remains bounded and resumable.
    selected_ids = _select_reconcile_adapter_ids(
        session,
        adapter_id=adapter_id,
        after_adapter_id=after_adapter_id,
        after_execution_id=after_execution_id,
        limit=limit,
    )
    if not selected_ids:
        # A targeted call for a concurrently deleted Adapter has no local
        # scope, but still repairs the singleton from the authoritative set.
        global_counter = _ensure_global_counter(session)
        global_count, global_bytes = session.execute(
            select(
                func.count(Execution.id),
                func.coalesce(func.sum(Execution.logical_input_bytes), 0),
            ).where(
                Execution.dispatch_backend == "rabbitmq",
                Execution.status.in_(RABBITMQ_OUTSTANDING_STATUSES),
            )
        ).one()
        global_count_delta = int(global_count or 0) - global_counter.outstanding_count
        global_bytes_delta = int(global_bytes or 0) - global_counter.outstanding_bytes
        global_counter.outstanding_count = int(global_count or 0)
        global_counter.outstanding_bytes = int(global_bytes or 0)
        session.commit()
        return AdmissionReconcileReport(0, 0, 0, global_count_delta, global_bytes_delta)

    # The initial SELECT used FOR UPDATE, but read the rows again in a sorted
    # set to make the lock order explicit and to avoid manufacturing a counter
    # for a row deleted between a cursor read and this transaction.
    adapters = list(
        session.scalars(
            select(Adapter)
            .where(Adapter.id.in_(selected_ids))
            .order_by(Adapter.id.asc())
            .with_for_update()
        ).all()
    )
    selected_ids = [adapter.id for adapter in adapters]
    counters = {
        current_id: _ensure_adapter_counter(session, current_id) for current_id in selected_ids
    }
    # All Adapter and AdapterAdmission locks are held before the Global lock.
    global_counter = _ensure_global_counter(session)

    expected: dict[int, tuple[int, int]] = {}
    if selected_ids:
        rows = session.execute(
            select(
                Execution.adapter_id,
                func.count(Execution.id),
                func.coalesce(func.sum(Execution.logical_input_bytes), 0),
            )
            .where(
                Execution.adapter_id.in_(selected_ids),
                Execution.dispatch_backend == "rabbitmq",
                Execution.status.in_(RABBITMQ_OUTSTANDING_STATUSES),
            )
            .group_by(Execution.adapter_id)
        ).all()
        expected = {int(row[0]): (int(row[1]), int(row[2] or 0)) for row in rows}

    adapter_count_delta = 0
    adapter_bytes_delta = 0
    for current_id in selected_ids:
        counter = counters[current_id]
        target_count, target_bytes = expected.get(current_id, (0, 0))
        adapter_count_delta += target_count - counter.outstanding_count
        adapter_bytes_delta += target_bytes - counter.outstanding_bytes
        counter.outstanding_count = target_count
        counter.outstanding_bytes = target_bytes

    # The Global lock serializes RabbitMQ ingress/cancel for every Adapter;
    # only now is the all-Adapter aggregate read.  It cannot overwrite an
    # increment committed after an ingress transaction acquired this lock.
    global_count, global_bytes = session.execute(
        select(
            func.count(Execution.id),
            func.coalesce(func.sum(Execution.logical_input_bytes), 0),
        ).where(
            Execution.dispatch_backend == "rabbitmq",
            Execution.status.in_(RABBITMQ_OUTSTANDING_STATUSES),
        )
    ).one()
    global_count_delta = int(global_count or 0) - global_counter.outstanding_count
    global_bytes_delta = int(global_bytes or 0) - global_counter.outstanding_bytes
    global_counter.outstanding_count = int(global_count or 0)
    global_counter.outstanding_bytes = int(global_bytes or 0)

    # Terminal release markers are repaired only after the counter locks.  A
    # bounded extra row is used solely as a lookahead; at most ``limit`` rows
    # are modified in this transaction.  The lock follows Global, preserving
    # the canonical order for cancellation and stop/delete.
    rows_to_repair: list[Execution] = []
    next_adapter_id: int | None = None
    next_execution_id: int | None = None
    remaining = limit
    for index, current_id in enumerate(selected_ids):
        if remaining == 0:
            # ``next_adapter_id`` is the last fully processed Adapter.  The
            # next call's ``>`` cursor therefore cannot skip the following
            # Adapter, even when its Execution IDs are interleaved globally.
            next_adapter_id = selected_ids[index - 1]
            break
        terminal_query = select(Execution).where(
            Execution.adapter_id == current_id,
            Execution.dispatch_backend == "rabbitmq",
            Execution.status.in_(RABBITMQ_TERMINAL_STATUSES),
            Execution.admission_released_at.is_(None),
        )
        if after_execution_id is not None and current_id == after_adapter_id:
            terminal_query = terminal_query.where(Execution.id > after_execution_id)
        terminal_rows = list(
            session.scalars(
                terminal_query.order_by(Execution.id.asc()).limit(remaining + 1).with_for_update()
            ).all()
        )
        if len(terminal_rows) > remaining:
            rows_to_repair.extend(terminal_rows[:remaining])
            next_adapter_id = current_id
            next_execution_id = rows_to_repair[-1].id
            remaining = 0
            break
        rows_to_repair.extend(terminal_rows)
        remaining -= len(terminal_rows)
        if remaining == 0:
            # The current Adapter is the last one whose rows were examined.
            # Keep it as the composite cursor; the next page starts at the
            # next Adapter rather than assuming Execution IDs are contiguous.
            next_adapter_id = current_id
            break
    if rows_to_repair:
        from dlr.control.services.input_config import database_now

        release_now = database_now(session)
        for execution in rows_to_repair:
            execution.admission_released_at = release_now

    if next_adapter_id is None and adapter_id is None and selected_ids:
        last_adapter_id = selected_ids[-1]
        has_more = session.scalar(select(exists().where(Adapter.id > last_adapter_id)))
        if has_more:
            next_adapter_id = last_adapter_id
    complete = next_adapter_id is None
    session.commit()
    return AdmissionReconcileReport(
        adapters_checked=len(selected_ids),
        adapter_count_delta=adapter_count_delta,
        adapter_bytes_delta=adapter_bytes_delta,
        global_count_delta=global_count_delta,
        global_bytes_delta=global_bytes_delta,
        next_adapter_id=next_adapter_id,
        next_execution_id=next_execution_id,
        complete=complete,
    )


def _admission_tick() -> AdmissionReconcileReport:
    """Reconcile counters in a short-lived Control database session."""

    from dlr.control import db

    global _admission_reconcile_cursor
    cursor = _admission_reconcile_cursor
    with db.SessionLocal() as session:
        report = reconcile_admission(
            session,
            after_adapter_id=cursor[0],
            after_execution_id=cursor[1],
        )
    _admission_reconcile_cursor = (
        (report.next_adapter_id, report.next_execution_id) if not report.complete else (None, None)
    )
    return report


async def admission_reconciler_loop() -> None:
    """Periodically repair counter drift without changing Execution state."""

    import asyncio
    import logging

    logger = logging.getLogger("dlr.control.admission")
    while True:
        try:
            await asyncio.to_thread(_admission_tick)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next tick retries governance
            logger.warning("admission reconciliation cycle failed", exc_info=True)
        await asyncio.sleep(settings.schedule_poll_seconds)
