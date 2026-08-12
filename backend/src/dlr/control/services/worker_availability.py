"""Authoritative effective-online semantics for registered Workers.

The persisted ``Worker.status`` remains the Worker's most recent explicit
register/heartbeat/offline declaration. Availability-sensitive Control paths
must use this module so a stale stored-online row is never mistaken for a
currently usable Worker.
"""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import Worker


def current_time(session: Session) -> datetime:
    """Capture one wall-clock timestamp for an entire business decision.

    PostgreSQL ``now()`` is fixed at transaction start, which may precede a
    row-lock wait in Test/Start. ``clock_timestamp()`` captures the actual
    decision time after that lock is acquired; callers still reuse this one
    value for every Worker considered by the operation.
    """
    now = session.scalar(select(func.clock_timestamp()))
    if not isinstance(now, datetime):
        raise RuntimeError("database did not return a current timestamp")
    return now


def is_effectively_online(worker: Worker, *, now: datetime) -> bool:
    """Return whether the Worker is stored-online with a fresh heartbeat.

    The inclusive cutoff fixes the boundary contract: a heartbeat whose age
    equals the configured timeout is still effective-online.
    """
    heartbeat_age_seconds = (
        now.astimezone(UTC) - worker.last_heartbeat.astimezone(UTC)
    ).total_seconds()
    return (
        worker.status == "online"
        and heartbeat_age_seconds <= settings.worker_heartbeat_timeout_seconds
    )


def effective_status(worker: Worker, *, now: datetime) -> str:
    """Map the authoritative predicate to the existing API status values."""
    return "online" if is_effectively_online(worker, now=now) else "offline"


def list_effectively_online_workers(session: Session, *, now: datetime) -> list[Worker]:
    """Return every effective-online Worker in deterministic registration order."""
    stored_online = session.scalars(
        select(Worker).where(Worker.status == "online").order_by(Worker.id.asc())
    ).all()
    return [worker for worker in stored_online if is_effectively_online(worker, now=now)]
