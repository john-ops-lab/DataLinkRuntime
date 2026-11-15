"""Private Worker enrollment and runtime credential lifecycle.

These functions do not authorize administrators, commit, expose routes, or
terminate an active Worker. Callers must enforce administrator authorization
for issuance, rotation, revocation and recovery, hold the returned secret
privately until commit succeeds, and deliver it only once. Recovery revokes
all prior runtime credentials but does not change identity, instance, or
Attempt state; those continue to converge through their leases and fences.

All operations lock the Worker first and its credentials second. The locks
remain held until the caller commits or rolls back the enclosing transaction.
This lock order is suitable only for paths without an earlier business-lock
prefix. Claim must not acquire this lock in a router dependency before the
Admission/Execution/Slot locks: it must obtain those existing locks first,
then lock Worker and credentials and repeat authoritative authentication.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.common.worker_protocol_v4 import WORKER_CREDENTIAL_PREFIX, is_worker_credential
from dlr.control.models import Worker, WorkerCredential, WorkerCredentialAudit

MAX_ENROLLMENT_SECONDS = 600
MAX_ROTATION_OVERLAP_SECONDS = 600


class WorkerCredentialError(Exception):
    """A stable, non-sensitive error for the future API boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class IssuedWorkerCredential:
    credential_id: int
    secret: str = field(repr=False)
    expires_at: datetime | None = None


@dataclass(frozen=True)
class RotatedWorkerCredential:
    issued: IssuedWorkerCredential
    previous_credential_record_id: int
    previous_expires_at: datetime


def record_audit(
    session: Session,
    *,
    worker_id: int,
    credential_record_id: int,
    related_credential_record_id: int | None,
    event_kind: str,
    actor_kind: str,
    actor_id: int | None,
) -> None:
    """Persist a bounded lifecycle fact in the caller's credential transaction."""
    session.add(
        WorkerCredentialAudit(
            worker_id=worker_id,
            credential_record_id=credential_record_id,
            related_credential_record_id=related_credential_record_id,
            event_kind=event_kind,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
    )
    session.flush()


def _lock_worker(session: Session, worker_id: int) -> Worker:
    worker = session.scalar(
        select(Worker)
        .where(Worker.id == worker_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if worker is None:
        raise WorkerCredentialError("worker_not_found")
    return worker


def _lock_credentials(
    session: Session, worker_id: int, *, kind: str, active_only: bool = False
) -> list[WorkerCredential]:
    query = select(WorkerCredential).where(
        WorkerCredential.worker_id == worker_id,
        WorkerCredential.kind == kind,
        WorkerCredential.revoked_at.is_(None),
    )
    if active_only:
        query = query.where(
            or_(
                WorkerCredential.expires_at.is_(None),
                WorkerCredential.expires_at > func.pg_catalog.clock_timestamp(),
            )
        )
    elif kind == "enrollment":
        query = query.where(WorkerCredential.consumed_at.is_(None))
    return list(
        session.scalars(
            query.order_by(WorkerCredential.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


def _lock_by_digest(
    session: Session, worker_id: int, kind: str, digest: bytes
) -> WorkerCredential | None:
    return session.scalar(
        select(WorkerCredential)
        .where(
            WorkerCredential.secret_digest == digest,
            WorkerCredential.worker_id == worker_id,
            WorkerCredential.kind == kind,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _digest_matches(row: WorkerCredential | None, digest: bytes) -> bool:
    # Always do the fixed-length comparison, including for an absent row.
    equal = hmac.compare_digest(row.secret_digest if row is not None else bytes(32), digest)
    return row is not None and equal


def _now(session: Session) -> datetime:
    # Unlike transaction_timestamp(), this is sampled after any lock wait.
    value = session.scalar(select(func.pg_catalog.clock_timestamp()))
    assert isinstance(value, datetime) and value.tzinfo is not None
    return value


def _digest(secret: str) -> bytes:
    if not is_worker_credential(secret):
        raise WorkerCredentialError("worker_credential_invalid")
    try:
        raw = bytes.fromhex(secret[len(WORKER_CREDENTIAL_PREFIX) :])
    except ValueError:
        raise WorkerCredentialError("worker_credential_invalid") from None
    if len(raw) != 32 or secret != WORKER_CREDENTIAL_PREFIX + raw.hex():
        raise WorkerCredentialError("worker_credential_invalid")
    return hashlib.sha256(raw).digest()


def _valid(row: WorkerCredential, now: datetime) -> bool:
    return row.revoked_at is None and (row.expires_at is None or row.expires_at > now)


def _check_local_secret_candidate(secret: str) -> None:
    """Reject a candidate identical to this process's current shared token.

    This is a local safeguard, not a cluster configuration or old-binary fence.
    """
    _digest(secret)
    for configured in (settings.worker_token, settings.admin_token):
        if (
            configured is not None
            and configured.isascii()
            and secrets.compare_digest(configured, secret)
        ):
            raise WorkerCredentialError("worker_credential_unavailable")


def _issue(
    session: Session, worker_id: int, kind: str, expires_at: datetime | None
) -> IssuedWorkerCredential:
    secret = WORKER_CREDENTIAL_PREFIX + secrets.token_hex(32)
    _check_local_secret_candidate(secret)
    row = WorkerCredential(
        worker_id=worker_id, kind=kind, secret_digest=_digest(secret), expires_at=expires_at
    )
    session.add(row)
    session.flush()
    return IssuedWorkerCredential(credential_id=row.id, secret=secret, expires_at=expires_at)


def issue_enrollment(
    session: Session, worker_id: int, *, ttl_seconds: int = MAX_ENROLLMENT_SECONDS
) -> IssuedWorkerCredential:
    """Issue one new enrollment token for an identity not yet enrolled.

    Reissuing invalidates earlier enrollment tokens. Enrollment lifetime is
    at most 10 minutes, measured using database time after the worker lock.
    """
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= MAX_ENROLLMENT_SECONDS:
        raise WorkerCredentialError("worker_enrollment_ttl_invalid")
    worker = _lock_worker(session, worker_id)
    credentials = _lock_credentials(session, worker_id, kind="enrollment")
    now = _now(session)
    if worker.identity_enrolled_at is not None:
        raise WorkerCredentialError("worker_already_enrolled")
    for row in credentials:
        if row.kind == "enrollment" and row.revoked_at is None and row.consumed_at is None:
            row.revoked_at = now
    return _issue(session, worker_id, "enrollment", now + timedelta(seconds=ttl_seconds))


def exchange_enrollment(session: Session, worker_id: int, secret: str) -> IssuedWorkerCredential:
    """Consume one valid enrollment and issue the first runtime credential."""
    digest = _digest(secret)
    worker = _lock_worker(session, worker_id)
    match = _lock_by_digest(session, worker_id, "enrollment", digest)
    now = _now(session)
    matches = _digest_matches(match, digest)
    if (
        worker.identity_enrolled_at is not None
        or not matches
        or match is None
        or match.consumed_at is not None
        or not _valid(match, now)
    ):
        raise WorkerCredentialError("worker_credential_invalid")
    match.consumed_at = now
    worker.identity_enrolled_at = now
    return _issue(session, worker_id, "runtime", None)


def authenticate_runtime_credential(session: Session, worker_id: int, secret: str) -> Worker:
    """Verify one credential while retaining its Worker lock for the caller."""
    digest = _digest(secret)
    worker = _lock_worker(session, worker_id)
    match = _lock_by_digest(session, worker_id, "runtime", digest)
    now = _now(session)
    matches = _digest_matches(match, digest)
    if (
        worker.identity_enrolled_at is None
        or not matches
        or match is None
        or not _valid(match, now)
    ):
        raise WorkerCredentialError("worker_credential_invalid")
    return worker


def _confirm_runtime_credential_at_locked(
    session: Session, worker_id: int, secret: str, observed_at: datetime
) -> None:
    """Recheck at a later DB sample while the authentication locks are held.

    Callers must already have authenticated in this transaction, retaining the
    Worker and credential locks before taking ``observed_at``. This check
    aligns credential expiry with the proof's actual observation time.
    """
    if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
        raise WorkerCredentialError("worker_credential_invalid")
    digest = _digest(secret)
    match = _lock_by_digest(session, worker_id, "runtime", digest)
    if not _digest_matches(match, digest) or match is None or not _valid(match, observed_at):
        raise WorkerCredentialError("worker_credential_invalid")


def prescreen_runtime_credential(session: Session, worker_id: int, secret: str) -> None:
    """Reject obvious invalid credentials without locks; callers must reauthenticate.

    This is deliberately non-authoritative: rotation or revocation can race
    this read. Do not use it to authorize a mutation or disclose Attempt facts.
    """
    digest = _digest(secret)
    enrolled = session.scalar(select(Worker.identity_enrolled_at).where(Worker.id == worker_id))
    now = _now(session)
    row = session.execute(
        select(
            WorkerCredential.secret_digest, WorkerCredential.revoked_at, WorkerCredential.expires_at
        ).where(
            WorkerCredential.secret_digest == digest,
            WorkerCredential.worker_id == worker_id,
            WorkerCredential.kind == "runtime",
        )
    ).one_or_none()
    matches = hmac.compare_digest(row.secret_digest if row is not None else bytes(32), digest)
    if (
        enrolled is None
        or not matches
        or row is None
        or row.revoked_at is not None
        or (row.expires_at is not None and row.expires_at <= now)
    ):
        raise WorkerCredentialError("worker_credential_invalid")


def rotate_runtime_credential(
    session: Session, worker_id: int, *, overlap_seconds: int = MAX_ROTATION_OVERLAP_SECONDS
) -> IssuedWorkerCredential:
    """Keep only the newest current credential for at most one bounded overlap."""
    return rotate_runtime_credential_with_previous(
        session, worker_id, overlap_seconds=overlap_seconds
    ).issued


def rotate_runtime_credential_with_previous(
    session: Session, worker_id: int, *, overlap_seconds: int = MAX_ROTATION_OVERLAP_SECONDS
) -> RotatedWorkerCredential:
    """Return the actual previous credential's deadline under the same locks."""
    if type(overlap_seconds) is not int or not 0 <= overlap_seconds <= MAX_ROTATION_OVERLAP_SECONDS:
        raise WorkerCredentialError("worker_rotation_overlap_invalid")
    worker = _lock_worker(session, worker_id)
    credentials = _lock_credentials(session, worker_id, kind="runtime", active_only=True)
    now = _now(session)
    active = [row for row in credentials if row.kind == "runtime" and _valid(row, now)]
    if worker.identity_enrolled_at is None or not active:
        raise WorkerCredentialError("worker_runtime_recovery_required")
    previous = active[-1]
    for row in active[:-1]:
        row.revoked_at = now
    deadline = now + timedelta(seconds=overlap_seconds)
    if previous.expires_at is None or previous.expires_at > deadline:
        previous.expires_at = deadline
    assert previous.expires_at is not None
    return RotatedWorkerCredential(
        issued=_issue(session, worker_id, "runtime", None),
        previous_credential_record_id=previous.id,
        previous_expires_at=previous.expires_at,
    )


def revoke_runtime_credential(session: Session, worker_id: int, credential_id: int) -> None:
    """Revoke one credential without changing active execution ownership."""
    _lock_worker(session, worker_id)
    row = session.scalar(
        select(WorkerCredential)
        .where(
            WorkerCredential.id == credential_id,
            WorkerCredential.worker_id == worker_id,
            WorkerCredential.kind == "runtime",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = _now(session)
    if row is None:
        raise WorkerCredentialError("worker_credential_invalid")
    if row.revoked_at is None:
        row.revoked_at = now


def recover_runtime_credential(session: Session, worker_id: int) -> IssuedWorkerCredential:
    """Explicit administrator recovery: revoke all old runtime credentials."""
    worker = _lock_worker(session, worker_id)
    if worker.identity_enrolled_at is None:
        raise WorkerCredentialError("worker_not_enrolled")
    # Expired records must also remain revoked if the database clock moves
    # backward. The Worker lock serializes all credential lifecycle operations;
    # perform the historical update in SQL without loading the rows in Python.
    session.execute(
        update(WorkerCredential)
        .where(
            WorkerCredential.worker_id == worker_id,
            WorkerCredential.kind == "runtime",
            WorkerCredential.revoked_at.is_(None),
        )
        .values(revoked_at=func.pg_catalog.clock_timestamp())
    )
    return _issue(session, worker_id, "runtime", None)
