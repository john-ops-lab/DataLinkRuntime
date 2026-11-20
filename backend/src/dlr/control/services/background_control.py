"""Short, fenced transactions for replicated Control work."""

from sqlalchemy import Select, text
from sqlalchemy.orm import InstrumentedAttribute, Session

from dlr.control.models import RuntimeReconciliationCursor
from dlr.control.services.control_ha_capacity import assert_local_control


def reserve_page(
    session: Session,
    name: str,
    candidates: Select[tuple[int]],
    key: InstrumentedAttribute[int],
    *,
    limit: int,
) -> tuple[int, ...]:
    """Release the scan lock before any candidate's business transaction."""
    try:
        session.execute(text("SET LOCAL statement_timeout = '1s'"))
        assert_local_control(session)
        cursor = session.get(
            RuntimeReconciliationCursor,
            name,
            with_for_update={"skip_locked": True},
            populate_existing=True,
        )
        if cursor is None:
            session.rollback()
            return ()
        if cursor.upper_id == 0:
            cursor.upper_id = session.scalar(candidates.order_by(key.desc()).limit(1)) or 0
        page = candidates.order_by(key).limit(limit)
        ids = tuple(session.scalars(page.where(key > cursor.after_id, key <= cursor.upper_id)))
        if not ids and cursor.upper_id:
            cursor.after_id = 0
            cursor.upper_id = session.scalar(candidates.order_by(key.desc()).limit(1)) or 0
            ids = tuple(session.scalars(page.where(key <= cursor.upper_id)))
        if ids:
            cursor.after_id = ids[-1]
        session.commit()
        return ids
    except Exception:
        session.rollback()
        raise


def cluster_diagnostics(session: Session) -> dict[str, int]:
    """Read deployment-wide custody counts without disclosing owner identities."""
    values = session.execute(
        text("""
        WITH observed AS (SELECT pg_catalog.clock_timestamp() AS now)
        SELECT
          (SELECT count(*) FROM event_subscriptions s
             LEFT JOIN subscription_leases l ON l.subscription_id = s.id
             LEFT JOIN control_process_instances p ON p.instance_id = l.control_instance_id
             WHERE s.desired_state = 'enabled' AND s.deleted_at IS NULL
               AND (l.lease_until IS NULL OR l.lease_until <= observed.now
                 OR p.instance_id IS NULL OR p.revoked OR p.lease_until <= observed.now)),
          (SELECT coalesce(max(greatest(0, extract(epoch FROM
                    (observed.now - l.lease_until)) + 30)), 0)
             FROM subscription_leases l JOIN control_process_instances p
               ON p.instance_id = l.control_instance_id
             WHERE l.lease_until > observed.now AND NOT p.revoked AND p.lease_until > observed.now),
          (SELECT count(*) FROM worker_resource_reservations r
             WHERE r.status = 'held' AND r.held_until <= observed.now),
          (SELECT count(*) FROM worker_resource_reservations r
             JOIN execution_attempts a ON a.id = r.attempt_id
             WHERE r.status = 'reclaiming' AND a.recovery_not_before <= observed.now)
        FROM observed
    """)
    ).one()
    return dict(
        zip(
            (
                "source_owner_unavailable",
                "oldest_active_owner_renewal_seconds",
                "expired_worker_holds",
                "overdue_worker_reclamations",
            ),
            map(int, values),
            strict=True,
        )
    )
