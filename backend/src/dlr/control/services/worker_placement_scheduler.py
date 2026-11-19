"""Bounded placement passes that revisit each Adapter's queued head."""

import asyncio
import logging
import threading
import time
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from dlr.control.models import Execution, RuntimeReconciliationCursor
from dlr.control.services import worker_placement_cutover, worker_placement_wait, worker_reservation

_PLACEMENT_CURSOR = "worker_placement"
_EXPIRATION_CURSOR = "worker_placement_expiration"
_MAX_DECISIONS = 100
_TICK_SECONDS = 0.050
_POLL_SECONDS = 1.0
_wake = threading.Event()
logger = logging.getLogger(__name__)

_HEAD = text("""
    SELECT a.id AS adapter_id, head.id AS execution_id, head.target_worker_id,
           wake.changed_at
    FROM adapters a
    JOIN LATERAL (
        SELECT e.id, e.target_worker_id, e.placement_deadline_at, e.next_placement_at,
               e.placement_hold_count, e.placement_checked_at,
               e.target_worker_id_snapshot, e.route_pool_id
        FROM executions e
        WHERE e.adapter_id = a.id AND e.status = 'queued' AND e.requires_worker_reservation
        ORDER BY e.id LIMIT 1
    ) head ON TRUE
    LEFT JOIN LATERAL (
        SELECT MAX(c.placement_changed_at) AS changed_at
        FROM worker_capacities c
        WHERE c.placement_changed_at > head.placement_checked_at
          AND (c.worker_id = head.target_worker_id_snapshot OR EXISTS (
              SELECT 1 FROM worker_pool_members m
              WHERE m.pool_id = head.route_pool_id AND m.worker_id = c.worker_id AND m.enabled
          ))
    ) wake ON TRUE
    WHERE a.id > :after
      AND (
          head.target_worker_id IS NOT NULL
          OR (
              head.placement_hold_count < 10
              AND (
                  head.next_placement_at IS NULL OR head.next_placement_at <= clock_timestamp()
                  OR wake.changed_at IS NOT NULL
              )
          )
      )
    ORDER BY a.id LIMIT 1
""")


def notify_capacity() -> None:
    _wake.set()


def _next(session: Session) -> tuple[int, int, bool, datetime | None] | None:
    """Commit cursor advancement before entering any business transaction."""
    session.execute(text("SET LOCAL statement_timeout = '100ms'"))
    if worker_placement_cutover.mode(session) != "reserved":
        session.rollback()
        return None
    cursor = session.get(
        RuntimeReconciliationCursor,
        _PLACEMENT_CURSOR,
        with_for_update={"skip_locked": True},
        populate_existing=True,
    )
    if cursor is None:
        session.rollback()
        return None
    next_head = session.execute(_HEAD, {"after": cursor.after_id}).one_or_none()
    if next_head is None:
        cursor.after_id = cursor.upper_id = 0
        session.commit()
        return None
    adapter_id, execution_id, target_worker_id, changed_at = next_head
    cursor.after_id = cursor.upper_id = adapter_id
    session.commit()
    return adapter_id, execution_id, target_worker_id is not None, changed_at


def _next_expired(session: Session) -> int | None:
    session.execute(text("SET LOCAL statement_timeout = '100ms'"))
    if worker_placement_cutover.mode(session) != "reserved":
        session.rollback()
        return None
    cursor = session.get(
        RuntimeReconciliationCursor,
        _EXPIRATION_CURSOR,
        with_for_update={"skip_locked": True},
        populate_existing=True,
    )
    if cursor is None:
        session.rollback()
        return None
    execution_id = session.scalar(
        select(Execution.id)
        .where(
            Execution.id > cursor.after_id,
            Execution.requires_worker_reservation,
            Execution.status == "queued",
            Execution.placement_deadline_at <= func.clock_timestamp(),
        )
        .order_by(Execution.id)
        .limit(1)
    )
    cursor.after_id = cursor.upper_id = execution_id or 0
    session.commit()
    return execution_id


def _expire_pass(session: Session, deadline: float) -> tuple[int, int]:
    """Expire queued custody independently of the per-Adapter dispatch head."""
    selected = changed = 0
    while selected < 25 and time.monotonic() < deadline:
        try:
            execution_id = _next_expired(session)
            if execution_id is None:
                break
            selected += 1
            session.execute(text("SET LOCAL statement_timeout = '100ms'"))
            if worker_placement_cutover.mode(session, hold=True) != "reserved":
                session.rollback()
                break
            changed += int(worker_placement_wait.expire_queued(session, execution_id))
            session.commit()
        except OperationalError as error:
            session.rollback()
            if getattr(error.orig, "sqlstate", None) not in {"55P03", "57014"}:
                raise
            break
    return selected, changed


def reconcile_once(session: Session) -> int:
    """Start at most 100 decisions within one 50 ms scheduling budget."""
    started = time.monotonic()
    end = started + _TICK_SECONDS
    expired, changed = _expire_pass(session, started + _TICK_SECONDS / 2)
    adapters: set[int] = set()
    while expired + len(adapters) < _MAX_DECISIONS and time.monotonic() < end:
        try:
            next_head = _next(session)
            if next_head is None:
                break
            adapter_id, execution_id, assigned, changed_at = next_head
            if adapter_id in adapters:
                break
            adapters.add(adapter_id)
            session.execute(text("SET LOCAL statement_timeout = '100ms'"))
            if worker_placement_cutover.mode(session, hold=True) != "reserved":
                session.rollback()
                break
            if assigned:
                changed += int(worker_reservation.invalidate_held(session, execution_id))
            else:
                changed += int(
                    worker_reservation.reserve_execution(
                        session, execution_id, observed_change_at=changed_at
                    )
                    is not None
                )
            session.commit()
        except OperationalError as error:
            session.rollback()
            if getattr(error.orig, "sqlstate", None) not in {"55P03", "57014"}:
                raise
            break
    return changed


def _tick() -> None:
    from dlr.control.db import SessionLocal

    with SessionLocal() as session:
        reconcile_once(session)


def _await_capacity() -> None:
    _wake.wait(timeout=_POLL_SECONDS)
    _wake.clear()


async def run() -> None:
    while True:
        try:
            await asyncio.to_thread(_tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker placement reconciliation failed")
        await asyncio.to_thread(_await_capacity)
