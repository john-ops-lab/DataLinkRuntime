"""Unwired exchange for a newly reserved Worker; never migrate a legacy Worker.

The caller owns the transaction and must not expose the returned secret before
commit succeeds. An uncertain result requires explicit administrator recovery.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from dlr.control.models import WorkerCredential
from dlr.control.models.worker_identity_reservation import WorkerIdentityReservation
from dlr.control.services import worker_credentials as credentials


@dataclass(frozen=True)
class EnrollmentExchange:
    worker_id: int
    worker_identity_id: UUID
    credential_record_id: int
    enrollment_record_id: int
    enrollment_expires_at: datetime
    secret: str = field(repr=False)


def exchange(session: Session, *, worker_id: int, enrollment_secret: str) -> EnrollmentExchange:
    if type(worker_id) is not int or not 0 < worker_id <= 2**63 - 1:
        raise credentials.WorkerCredentialError("worker_credential_invalid")
    if session.scalar(text("SHOW transaction_isolation")) != "read committed":
        raise credentials.WorkerCredentialError("worker_enrollment_unavailable")
    digest = credentials._digest(enrollment_secret)
    try:
        worker = credentials._lock_worker(session, worker_id)
    except credentials.WorkerCredentialError:
        raise credentials.WorkerCredentialError("worker_credential_invalid") from None
    reservation = session.scalar(
        select(WorkerIdentityReservation)
        .where(WorkerIdentityReservation.worker_id == worker_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    enrollment = credentials._lock_by_digest(session, worker_id, "enrollment", digest)
    # Worker serializes all credential issuance/recovery for this identity.
    prior_runtime = session.scalar(
        select(WorkerCredential.id)
        .where(WorkerCredential.worker_id == worker_id, WorkerCredential.kind == "runtime")
        .limit(1)
    )
    now = credentials._now(session)  # sampled after the last lock wait
    matches = credentials._digest_matches(enrollment, digest)
    if (
        worker.identity_lifecycle != "reserved"
        or not isinstance(worker.identity_id, UUID)
        or worker.identity_id.int == 0
        or reservation is None
        or reservation.name != worker.name
        or worker.protocol_version != 4
        or worker.identity_enrolled_at is not None
        or worker.instance_id is not None
        or worker.instance_generation != 0
        or worker.instance_lease_until is not None
        or worker.scheduling_state != "disabled"
        or prior_runtime is not None
        or not matches
        or enrollment is None
        or enrollment.consumed_at is not None
        or enrollment.revoked_at is not None
        or enrollment.expires_at is None
        or enrollment.expires_at <= now
        or enrollment.expires_at > now + timedelta(seconds=credentials.MAX_ENROLLMENT_SECONDS)
    ):
        raise credentials.WorkerCredentialError("worker_credential_invalid")
    enrollment.consumed_at = now
    worker.identity_enrolled_at = now
    issued = credentials._issue(session, worker_id, "runtime", None)
    # The insert may wait on an index or other database work. Recheck the
    # enrollment expiry after that wait before allowing the caller to commit.
    if enrollment.expires_at <= credentials._now(session):
        raise credentials.WorkerCredentialError("worker_credential_invalid")
    return EnrollmentExchange(
        worker.id,
        worker.identity_id,
        issued.credential_id,
        enrollment.id,
        enrollment.expires_at,
        issued.secret,
    )
