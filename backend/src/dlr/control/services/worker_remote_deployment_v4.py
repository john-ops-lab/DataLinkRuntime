"""Reject accidental in-place activation of the restricted remote Worker stage."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from dlr.control.services import worker_legacy_window

# Recognize settled v3 history after the operator adopts the stopped Worker.
# The release never rewrites the original Attempt with a v4 instance identity.
_ADOPTED_LEGACY_ATTEMPT = """
    AND NOT EXISTS (
        SELECT 1
        FROM worker_identity_reservations r
        JOIN workers w ON w.id=r.worker_id AND w.name=r.name
        JOIN executions e ON e.id=a.execution_id AND e.adapter_id=a.adapter_id
        JOIN worker_legacy_window l ON l.id=1
        WHERE r.worker_id=a.worker_id
          AND l.closed_at IS NOT NULL AND r.created_at >= l.opened_at
          AND r.legacy_stop_evidence_sha256 <> repeat('0',64)
          AND a.claim_token_hash <> repeat('0',64) AND a.cleanup_token_hash <> repeat('0',64)
          AND a.status NOT IN ('claimed','running')
          AND a.claimed_at <= a.ended_at AND a.ended_at <= r.created_at
          AND a.cleanup_summary->>'workspace_cleanup_status' = 'completed'
          AND (a.attempt_no < e.attempt_count
               OR (a.attempt_no = e.attempt_count AND e.workspace_cleanup_status = 'completed'))
          AND e.dispatch_backend = 'rabbitmq' AND e.placement_origin = 'legacy_v3'
          AND e.route_snapshot #>> '{route,mode}' = 'fixed'
          AND e.route_snapshot #>> '{route,worker_id}' = a.worker_id::text
          AND e.target_worker_id_snapshot = a.worker_id
          AND (
              (NOT e.requires_worker_reservation AND e.target_worker_id = a.worker_id
               AND e.admission_released_at IS NOT NULL
               AND e.status IN ('succeeded','dead_letter','cancelled','expired'))
              OR (e.requires_worker_reservation
                  AND (e.target_worker_id IS NULL OR e.target_worker_id = a.worker_id)
                  AND EXISTS (
                      SELECT 1 FROM execution_outbox o
                      WHERE o.execution_id=e.id AND o.dispatch_generation<e.dispatch_generation
                        AND o.status='superseded'
                        AND o.last_error_code='placement_cutover_generation'
                  ))
          )
    )
"""


class RemoteDeploymentUnavailable(RuntimeError):
    """A stable diagnostic which contains no database or credential details."""


def require_dedicated_deployment(session: Session, *, readonly: bool = False) -> None:
    """Check persistent provenance before disabling the legacy Worker APIs.

    Valid stage data remains valid across ordinary Control restarts. An empty
    queue alone is insufficient. Legacy history requires confirmed adoption,
    cleanup and preserved custody. Role fencing and the deployment operator
    establish whether external writers and local Worker journals have stopped.
    """
    session.execute(text("SET LOCAL lock_timeout = '500ms'"))
    session.execute(text("SET LOCAL statement_timeout = '2000ms'"))
    if not readonly:
        session.execute(
            text(
                "LOCK TABLE workers, worker_identity_reservations, "
                "executions, execution_attempts IN SHARE MODE"
            )
        )
    if worker_legacy_window.enabled_mode() and worker_legacy_window.has_history(session):
        # An owner authorized this in-place upgrade. Historical v3 rows remain
        # readable after the deadline. The shared-token route separately gates
        # permissions using the current database time and exact Worker row.
        incompatible_workers = session.scalar(
            text("""
            SELECT EXISTS (
                SELECT 1 FROM workers w
                WHERE NOT (
                    (w.protocol_version=3 AND w.identity_lifecycle='legacy'
                        AND w.identity_enrolled_at IS NULL)
                    OR (w.protocol_version=4 AND w.identity_lifecycle='reserved'
                        AND EXISTS (
                            SELECT 1 FROM worker_identity_reservations r WHERE r.worker_id=w.id
                        ))
                )
            )
            """)
        )
        if incompatible_workers is not False:
            raise RemoteDeploymentUnavailable("worker_remote_deployment_incompatible")
        return
    # An owner may have paused with converted custody, or enabled placement
    # with an unassigned Pool head. Such a row legitimately has no current
    # Worker, including when Control restarts before its first placement.
    placement_installed = session.scalar(
        text("SELECT to_regclass('public.worker_placement_cutover') IS NOT NULL")
    )
    unassigned_custody = ""
    if placement_installed and session.scalar(
        text("SELECT mode FROM worker_placement_cutover WHERE id=1")
    ) in {"paused", "reserved"}:
        unassigned_custody = """
            AND NOT (
                e.requires_worker_reservation AND e.target_worker_id IS NULL
                AND e.dispatch_backend = 'rabbitmq'
                AND (e.route_pool_id IS NOT NULL OR EXISTS (
                    SELECT 1 FROM worker_identity_reservations r
                    WHERE r.worker_id = e.target_worker_id_snapshot
                ))
                AND (
                    (e.status = 'queued' AND e.admission_released_at IS NULL AND EXISTS (
                        SELECT 1 FROM execution_outbox o WHERE o.execution_id=e.id
                            AND o.dispatch_generation=e.dispatch_generation
                            AND o.status='awaiting_target'
                    ))
                    OR (e.status = 'retry_wait' AND e.admission_released_at IS NULL
                        AND e.next_attempt_at IS NOT NULL)
                    OR (e.status IN ('succeeded','dead_letter','cancelled','expired')
                        AND e.admission_released_at IS NOT NULL)
                )
            )
        """
    incompatible = session.scalar(
        text(f"""
        SELECT EXISTS (
            SELECT 1 FROM workers w
            WHERE w.protocol_version <> 4 OR w.identity_lifecycle <> 'reserved'
               OR NOT EXISTS (
                   SELECT 1 FROM worker_identity_reservations r WHERE r.worker_id = w.id
               )
        ) OR EXISTS (
            SELECT 1 FROM executions e
            WHERE NOT EXISTS (
                SELECT 1 FROM worker_identity_reservations r
                WHERE r.worker_id = e.target_worker_id
            )
            {unassigned_custody}
        ) OR EXISTS (
            SELECT 1 FROM execution_attempts a WHERE a.worker_identity_id IS NULL
            {_ADOPTED_LEGACY_ATTEMPT if placement_installed else ""}
        )
    """)
    )
    if incompatible is not False:
        raise RemoteDeploymentUnavailable("worker_remote_deployment_incompatible")
