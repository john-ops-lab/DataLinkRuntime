"""Reject accidental in-place activation of the restricted remote Worker stage."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from dlr.control.services import worker_legacy_window


class RemoteDeploymentUnavailable(RuntimeError):
    """A stable diagnostic which contains no database or credential details."""


def require_dedicated_deployment(session: Session, *, readonly: bool = False) -> None:
    """Check persistent provenance before disabling the legacy Worker APIs.

    Valid stage data remains valid across ordinary Control restarts. An empty
    queue alone is insufficient: retained legacy identities and executions also
    prevent switching. This check does not prove external writers or local
    Worker journals have stopped; role fencing and Worker startup own those.
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
    incompatible = session.scalar(
        text("""
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
        ) OR EXISTS (
            SELECT 1 FROM execution_attempts a WHERE a.worker_identity_id IS NULL
        )
    """)
    )
    if incompatible is not False:
        raise RemoteDeploymentUnavailable("worker_remote_deployment_incompatible")
