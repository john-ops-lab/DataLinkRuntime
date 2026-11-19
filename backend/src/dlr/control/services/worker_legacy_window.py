"""Database authorization for the one-time, fixed Worker mode A upgrade."""

from collections.abc import Callable
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.orm import Session, SessionTransaction

from dlr.common.config import settings
from dlr.control.models import Worker
from dlr.control.services.worker_availability import current_time

_KEY = "dlr_worker_legacy_window"
_LISTENING = "dlr_worker_legacy_window_listening"
_MAX_DURATION = timedelta(days=7)


class LegacyWindowUnavailable(Exception):
    """Fixed diagnostic for ineligible legacy access."""


def enabled_mode() -> bool:
    return (
        settings.worker_remote_v4_enabled
        and settings.worker_v4_deployment_mode == "A"
        and settings.worker_v4_control_slots == 1
    )


def is_legacy_worker(worker: Worker) -> bool:
    return (
        worker.protocol_version == 3
        and worker.identity_lifecycle == "legacy"
        and worker.identity_enrolled_at is None
    )


def _open_deadline(session: Session) -> datetime:
    if not enabled_mode():
        raise LegacyWindowUnavailable("legacy_window_unavailable")
    deadline = session.scalar(
        text("""
        SELECT deadline_at FROM worker_legacy_window
        WHERE id=1 AND opened_at IS NOT NULL AND closed_at IS NULL
        FOR SHARE
        """)
    )
    if not isinstance(deadline, datetime) or deadline <= current_time(session):
        raise LegacyWindowUnavailable("legacy_window_unavailable")
    return deadline


def gate(session: Session, *, check_control: Callable[[Session], None] | None = None) -> datetime:
    """Hold the window through the request's transaction and recheck on commit."""
    deadline = _open_deadline(session)
    if _LISTENING not in session.info:
        session.info[_LISTENING] = True
        event.listen(session, "before_commit", _before_commit)
        event.listen(session, "after_transaction_end", _after_transaction_end)
    if check_control is not None or _KEY not in session.info:
        session.info[_KEY] = check_control
    return deadline


def _before_commit(session: Session) -> None:
    if _KEY not in session.info:
        return
    session.flush()
    check_control = session.info[_KEY]
    if check_control is not None:
        check_control(session)
    _open_deadline(session)


def _after_transaction_end(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None and session.info.get(_KEY) is None:
        session.info.pop(_KEY, None)


def authorize_worker(session: Session, worker: Worker | None) -> None:
    """Call with the shared window lock held before reading the Worker."""
    if _KEY in session.info and (worker is None or not is_legacy_worker(worker)):
        raise LegacyWindowUnavailable("legacy_window_unavailable")


def target_allowed(session: Session, worker: Worker) -> bool:
    if not enabled_mode() or not is_legacy_worker(worker):
        return False
    try:
        gate(session)
    except LegacyWindowUnavailable:
        return False
    session.refresh(worker)
    return is_legacy_worker(worker)


def require_dispatch_target(session: Session, worker_id: int | None) -> None:
    """Gate producers that use an existing frozen Worker target."""
    if not settings.worker_remote_v4_enabled or worker_id is None:
        return
    worker = session.get(Worker, worker_id)
    if worker is not None and worker.protocol_version == 3 and not target_allowed(session, worker):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "worker_legacy_window_unavailable",
                "message": "Legacy Worker dispatch is unavailable",
            },
        )


def has_history(session: Session) -> bool:
    """Retain eligibility to read the upgraded database after v3 access closes."""
    return (
        session.scalar(text("SELECT opened_at IS NOT NULL FROM worker_legacy_window WHERE id=1"))
        is True
    )


def lock_cutover(session: Session) -> None:
    """Exclude legacy requests and admissions before locking a Worker row."""
    if (
        not enabled_mode()
        or session.scalar(
            text("""
        SELECT opened_at IS NOT NULL FROM worker_legacy_window WHERE id=1 FOR UPDATE
        """)
        )
        is not True
    ):
        raise LegacyWindowUnavailable("legacy_window_unavailable")


def has_unsettled_worker_work(session: Session, worker_id: int) -> bool:
    """Conservative database predicate. Physical stop is an operator prerequisite."""
    return (
        session.scalar(
            text("""
        SELECT EXISTS (
            SELECT 1 FROM executions e
            WHERE (e.target_worker_id=:worker OR e.target_worker_id_snapshot=:worker
                    OR e.worker_id=:worker)
              AND e.status IN ('queued','running','retry_wait')
              AND NOT (
                  e.placement_origin IS NOT DISTINCT FROM 'legacy_v3'
                    AND e.requires_worker_reservation AND e.target_worker_id IS NULL
                    AND e.target_worker_id_snapshot IS NOT DISTINCT FROM :worker
                    AND e.status IN ('queued','retry_wait')
                    AND EXISTS (
                        SELECT 1 FROM worker_placement_cutover WHERE id=1 AND mode='paused'
                    )
                    AND EXISTS (
                        SELECT 1 FROM execution_outbox o WHERE o.execution_id=e.id
                            AND o.dispatch_generation < e.dispatch_generation
                            AND o.status='superseded'
                            AND o.last_error_code='placement_cutover_generation'
                    )
              )
        ) OR EXISTS (
            SELECT 1 FROM execution_attempts a
            WHERE a.worker_id=:worker
              AND (a.status IN ('claimed','running')
                   OR a.cleanup_summary->>'workspace_cleanup_status' IS DISTINCT FROM 'completed')
        ) OR EXISTS (
            SELECT 1 FROM execution_outbox o JOIN executions e ON e.id=o.execution_id
            WHERE e.target_worker_id_snapshot=:worker AND o.status='pending'
        ) OR EXISTS (
            SELECT 1 FROM worker_cleanup_requests r
            WHERE r.worker_id=:worker AND r.status<>'completed'
        ) OR EXISTS (
            SELECT 1 FROM worker_cache_guards g WHERE g.worker_id=:worker AND g.phase <> 'idle'
        ) OR EXISTS (
            SELECT 1 FROM worker_cache_operations o
            WHERE o.worker_id=:worker AND o.finished_at IS NULL
        ) OR EXISTS (
            SELECT 1 FROM worker_cache_management_operations m
            WHERE m.worker_id=:worker AND m.status IN ('pending','running')
        ) OR EXISTS (
            SELECT 1 FROM event_receipts r
            JOIN subscription_activations a ON a.id=r.activation_id
            WHERE a.worker_id=:worker
              AND r.state NOT IN ('succeeded','skipped','superseded','abandoned')
        ) OR EXISTS (
            SELECT 1 FROM event_subscriptions s
            JOIN subscription_activations a ON a.id=s.activation_id
            WHERE a.worker_id=:worker AND s.desired_state='enabled' AND s.deleted_at IS NULL
        )
        """),
            {"worker": worker_id},
        )
        is not False
    )


def open_once(session: Session, deadline: datetime) -> None:
    """Called by the offline schema owner, inside the first authority transaction."""
    if not enabled_mode() or deadline.utcoffset() is None:
        raise LegacyWindowUnavailable("legacy_window_unavailable")
    row = session.execute(
        text("SELECT opened_at FROM worker_legacy_window WHERE id=1 FOR UPDATE")
    ).one_or_none()
    now = current_time(session)
    if (
        row is None
        or row.opened_at is not None
        or not now < deadline <= now + _MAX_DURATION
        or session.scalar(
            text("""
            SELECT EXISTS (
                SELECT 1 FROM workers WHERE protocol_version=3 AND identity_lifecycle='legacy'
                    AND identity_enrolled_at IS NULL
            )
            """)
        )
        is not True
    ):
        raise LegacyWindowUnavailable("legacy_window_unavailable")
    session.execute(
        text("UPDATE worker_legacy_window SET opened_at=:now, deadline_at=:deadline WHERE id=1"),
        {"now": now, "deadline": deadline},
    )


def close(session: Session) -> None:
    """Stop shared access and retain all Worker and business history."""
    if (
        not enabled_mode()
        or session.scalar(
            text("""
        UPDATE worker_legacy_window
        SET closed_at=COALESCE(closed_at, pg_catalog.clock_timestamp())
        WHERE id=1 AND opened_at IS NOT NULL
        RETURNING id
        """)
        )
        != 1
    ):
        raise LegacyWindowUnavailable("legacy_window_unavailable")
