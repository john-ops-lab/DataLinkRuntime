"""Transaction-scoped Worker instance leases for the identity protocol.

Callers must commit or roll back the session; this module never releases the
credential and Worker row locks before the identity decision is persisted.
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from dlr.common.worker_protocol_v4 import INSTANCE_LEASE_SECONDS
from dlr.control.models import Worker
from dlr.control.models.worker_instance_history import WorkerInstanceHistory
from dlr.control.services import worker_availability, worker_credentials
from dlr.control.services.adapter import domain_error


def register_instance(
    session: Session, *, worker_id: int, credential: str, instance_id: UUID, protocol_version: int
) -> Worker:
    """Create one fenced instance, or idempotently read a live instance back.

    A new generation starts in reconciliation and cannot use an old capacity
    report. A retry never extends the lease: only a valid heartbeat can do so.
    """
    if not isinstance(instance_id, UUID) or instance_id.int == 0:
        raise domain_error(409, "worker_instance_invalid", "Worker instance is invalid")
    if type(protocol_version) is not int or protocol_version != 4:
        raise domain_error(422, "worker_protocol_incompatible", "Worker protocol is incompatible")
    worker = worker_credentials.authenticate_runtime_credential(session, worker_id, credential)
    now = worker_availability.current_time(session)
    worker_credentials._confirm_runtime_credential_at_locked(session, worker_id, credential, now)
    old_id = worker.instance_id
    old_until = worker.instance_lease_until
    if old_id is not None and old_until is not None:
        if old_id == instance_id:
            if old_until <= now:
                raise domain_error(409, "worker_instance_expired", "Worker instance has expired")
            history = session.get(WorkerInstanceHistory, (worker_id, instance_id))
            if history is None or history.generation != worker.instance_generation:
                raise domain_error(
                    409, "worker_instance_history_inconsistent", "Instance history invalid"
                )
            if worker.protocol_version != 4:
                raise domain_error(
                    409, "worker_protocol_incompatible", "Worker protocol is incompatible"
                )
            _confirm_prior_lease(
                session,
                worker_id,
                credential,
                instance_id,
                worker.instance_generation,
                old_until,
                ending=False,
            )
            return worker
        if old_until > now:
            raise domain_error(409, "worker_instance_conflict", "Worker identity is in use")

    if session.get(WorkerInstanceHistory, (worker_id, instance_id)) is not None:
        raise domain_error(409, "worker_instance_expired", "Worker instance has expired")
    worker.instance_id = instance_id
    worker.instance_generation += 1
    session.add(
        WorkerInstanceHistory(
            worker_id=worker_id,
            instance_id=instance_id,
            generation=worker.instance_generation,
        )
    )
    worker.protocol_version = 4
    worker.instance_lease_until = now + timedelta(seconds=INSTANCE_LEASE_SECONDS)
    worker.capacity_state = "reconciling"
    # Capability and preflight facts belong to the old instance, not to the
    # stable Worker identity. A new generation must report and validate its
    # own environment before any future transition to ready.
    worker.capabilities = []
    worker.isolation_capabilities = {}
    worker.isolation_preflight_status = "unknown"
    worker.isolation_preflight_at = None
    worker.rabbitmq_execution_v3 = False
    worker.status = "online"
    worker.last_heartbeat = now
    session.flush()
    assert worker.instance_lease_until is not None
    _confirm_prior_lease(
        session,
        worker_id,
        credential,
        instance_id,
        worker.instance_generation,
        worker.instance_lease_until,
        ending=False,
    )
    return worker


def renew_instance(
    session: Session,
    *,
    worker_id: int,
    credential: str,
    instance_id: UUID,
    generation: int,
) -> Worker:
    """Renew only the currently live generation; never revive an expired one."""
    worker, now = _live_instance(session, worker_id, credential, instance_id, generation)
    previous_until = worker.instance_lease_until
    assert previous_until is not None
    worker.instance_lease_until = now + timedelta(seconds=INSTANCE_LEASE_SECONDS)
    worker.last_heartbeat = now
    worker.status = "online"
    session.flush()
    _confirm_prior_lease(
        session, worker_id, credential, instance_id, generation, previous_until, ending=False
    )
    return worker


def _live_instance(
    session: Session, worker_id: int, credential: str, instance_id: UUID, generation: int
) -> tuple[Worker, datetime]:
    if (
        not isinstance(instance_id, UUID)
        or instance_id.int == 0
        or type(generation) is not int
        or generation <= 0
    ):
        raise domain_error(409, "worker_instance_invalid", "Worker instance is invalid")
    worker = worker_credentials.authenticate_runtime_credential(session, worker_id, credential)
    now = worker_availability.current_time(session)
    worker_credentials._confirm_runtime_credential_at_locked(session, worker_id, credential, now)
    if (
        worker.protocol_version != 4
        or worker.instance_id != instance_id
        or worker.instance_generation != generation
        or worker.instance_lease_until is None
        or worker.instance_lease_until <= now
    ):
        raise domain_error(409, "worker_instance_expired", "Worker instance is no longer valid")
    return worker, now


def _confirm_prior_lease(
    session: Session,
    worker_id: int,
    credential: str,
    instance_id: UUID,
    generation: int,
    previous_until: datetime,
    *,
    ending: bool,
) -> datetime:
    """Recheck the original lease and credential after potentially slow writes.

    The caller must retain the transaction locks and roll back on failure.
    This check alone cannot prove that a later commit finished before expiry.
    """
    worker = worker_credentials.authenticate_runtime_credential(session, worker_id, credential)
    now = worker_availability.current_time(session)
    worker_credentials._confirm_runtime_credential_at_locked(session, worker_id, credential, now)
    if (
        worker.protocol_version != 4
        or worker.instance_id != instance_id
        or worker.instance_generation != generation
        or previous_until <= now
        or (
            not ending
            and (worker.instance_lease_until is None or worker.instance_lease_until <= now)
        )
    ):
        raise domain_error(409, "worker_instance_expired", "Worker instance is no longer valid")
    return now


def mark_instance_offline(
    session: Session,
    *,
    worker_id: int,
    credential: str,
    instance_id: UUID,
    generation: int,
) -> Worker:
    """Fence the reporting instance without claiming its processes have stopped."""
    worker, now = _live_instance(session, worker_id, credential, instance_id, generation)
    previous_until = worker.instance_lease_until
    assert previous_until is not None
    worker.instance_lease_until = now
    worker.status = "offline"
    session.flush()
    _confirm_prior_lease(
        session, worker_id, credential, instance_id, generation, previous_until, ending=True
    )
    return worker
