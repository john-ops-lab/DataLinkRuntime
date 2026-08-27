"""Retryable Managed Input lifecycle governance (Issue #127 B3).

The database owns lifecycle state and charge accounting; the ArtifactStore is
called only after a short claim transaction commits.  This keeps a slow or
crashed filesystem operation from holding PostgreSQL locks and lets another
Control process reclaim an expired delete lease.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control import db
from dlr.control.models import (
    AdapterInputArtifactBinding,
    AdapterInputConfig,
    ArtifactDeletionJob,
    ManagedInputArtifact,
    ManagedInputArtifactStatus,
    ManagedInputCapacity,
    ManagedInputDeletionJobStatus,
    ManagedInputUploadReservation,
)
from dlr.control.services import input_config as input_config_service
from dlr.control.services import managed_input as policy_service
from dlr.control.services.artifact_store import (
    ArtifactAuditResult,
    ArtifactStoreError,
    LocalFileArtifactStore,
)
from dlr.control.services.managed_input_audit import record_audit_event

logger = logging.getLogger("dlr.control.managed_input_gc")

DELETE_LEASE_SECONDS = 60
DELETE_BACKOFF_BASE_SECONDS = 5
MAX_DELETE_BACKOFF_SECONDS = 300
GC_BATCH_SIZE = 100
ArtifactProtectionHook = Callable[[Session, int], bool]


@dataclass(frozen=True)
class ArtifactDeletionClaim:
    """The immutable facts needed to finish one Artifact delete attempt."""

    artifact_id: int
    adapter_id: int
    storage_key: str
    size_bytes: int
    attempt: int
    started_at: datetime
    lease_until: datetime


@dataclass(frozen=True)
class DeletionJobClaim:
    """The immutable facts needed to finish one detached deletion job."""

    job_id: int
    storage_key: str
    charged_bytes: int
    attempt: int
    lease_until: datetime


@dataclass(frozen=True)
class ArtifactDeletionReport:
    claimed: int = 0
    deleted: int = 0
    failed: int = 0


@dataclass(frozen=True)
class DeletionJobReport:
    claimed: int = 0
    completed: int = 0
    failed: int = 0


@dataclass(frozen=True)
class GarbageCollectionReport:
    reservations_expired: int = 0
    bindings_expired: int = 0
    staged_marked: int = 0
    artifacts_claimed: int = 0
    artifacts_deleted: int = 0
    artifacts_failed: int = 0
    deletion_jobs_claimed: int = 0
    deletion_jobs_completed: int = 0
    deletion_jobs_failed: int = 0


# Short names are useful to callers that use the Issue vocabulary directly.
GCReport = GarbageCollectionReport
ArtifactGCReport = ArtifactDeletionReport


def utcnow() -> datetime:
    """Return an aware UTC timestamp for lifecycle leases and transitions."""
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _same_timestamp(left: datetime | None, right: datetime | None) -> bool:
    left_utc = _as_utc(left)
    right_utc = _as_utc(right)
    return left_utc is not None and right_utc is not None and left_utc == right_utc


def _backoff_seconds(attempt: int) -> int:
    exponent = max(0, min(int(attempt) - 1, 8))
    return int(min(MAX_DELETE_BACKOFF_SECONDS, DELETE_BACKOFF_BASE_SECONDS * (2**exponent)))


def _artifact_expired(artifact: ManagedInputArtifact, now: datetime) -> bool:
    expires_at = _as_utc(artifact.expires_at)
    return expires_at is not None and expires_at <= now


def has_active_artifact_lease(session: Session, artifact_id: int) -> bool:
    """Default B3 protection hook; the C0 Lease provider is injected later.

    B3 deliberately does not create, inspect, or reference the C0 Lease
    schema.  The hook remains a replaceable seam: C0 supplies a database-backed
    provider through ``protection_hook`` once its Lease schema and lock order
    exist.  Returning ``False`` here is the B3-only unprotected fixture, not a
    claim that active Execution Leases have been queried.
    """
    _ = session, artifact_id
    return False


def _artifact_delete_due(
    artifact: ManagedInputArtifact,
    now: datetime,
    *,
    force: bool = False,
) -> bool:
    status = str(artifact.status)
    if force and status == ManagedInputArtifactStatus.STAGED:
        return True
    if status not in {
        ManagedInputArtifactStatus.PENDING_DELETE,
        ManagedInputArtifactStatus.DELETE_FAILED,
        ManagedInputArtifactStatus.DELETING,
    }:
        return False
    if force and status in {
        ManagedInputArtifactStatus.PENDING_DELETE,
        ManagedInputArtifactStatus.DELETE_FAILED,
    }:
        return True
    lease_until = _as_utc(artifact.delete_lease_until)
    return lease_until is None or lease_until <= now


def _artifact_candidate_ids(session: Session, now: datetime, limit: int) -> list[int]:
    if limit <= 0:
        return []
    due = or_(
        ManagedInputArtifact.delete_lease_until.is_(None),
        ManagedInputArtifact.delete_lease_until <= now,
    )
    return list(
        session.scalars(
            select(ManagedInputArtifact.id)
            .where(
                ManagedInputArtifact.status.in_(
                    (
                        ManagedInputArtifactStatus.PENDING_DELETE,
                        ManagedInputArtifactStatus.DELETE_FAILED,
                        ManagedInputArtifactStatus.DELETING,
                    )
                ),
                due,
            )
            .order_by(ManagedInputArtifact.id)
            .limit(limit)
        )
    )


def claim_artifact_deletion(
    session: Session,
    artifact_id: int,
    *,
    adapter_id: int | None = None,
    now: datetime | None = None,
    force: bool = False,
    lease_seconds: int = DELETE_LEASE_SECONDS,
    protection_hook: ArtifactProtectionHook | None = None,
) -> ArtifactDeletionClaim | None:
    """Claim one eligible Artifact without holding a DB lock over file I/O.

    The optional protection hook is the schema-independent B3 seam.  C0 can
    provide its database-backed Lease implementation without making this
    lifecycle module import or inspect the C0 schema.
    """
    effective_now = _as_utc(now) or utcnow()
    artifact = session.scalar(
        select(ManagedInputArtifact)
        .where(ManagedInputArtifact.id == int(artifact_id))
        .with_for_update(skip_locked=True)
    )
    if (
        artifact is None
        or (adapter_id is not None and int(artifact.adapter_id) != int(adapter_id))
        or not _artifact_delete_due(artifact, effective_now, force=force)
    ):
        session.rollback()
        return None
    protected = (
        protection_hook(session, int(artifact.id))
        if protection_hook is not None
        else has_active_artifact_lease(session, int(artifact.id))
    )
    if protected:
        session.rollback()
        return None
    started_at = effective_now
    lease_until = effective_now + timedelta(seconds=max(10, int(lease_seconds)))
    artifact.delete_attempts = int(artifact.delete_attempts) + 1
    artifact.status = ManagedInputArtifactStatus.DELETING
    artifact.delete_started_at = started_at
    artifact.delete_lease_until = lease_until
    artifact.last_error_code = None
    session.commit()
    return ArtifactDeletionClaim(
        artifact_id=int(artifact.id),
        adapter_id=int(artifact.adapter_id),
        storage_key=artifact.storage_key,
        size_bytes=int(artifact.size_bytes),
        attempt=int(artifact.delete_attempts),
        started_at=started_at,
        lease_until=lease_until,
    )


def _release_actual(capacity: ManagedInputCapacity, charged_bytes: int) -> None:
    if charged_bytes < 0 or int(capacity.actual_bytes) < charged_bytes:
        raise RuntimeError("Managed Input actual accounting is inconsistent")
    capacity.actual_bytes -= charged_bytes


def finalize_artifact_deletion(
    session: Session,
    claim: ArtifactDeletionClaim,
    *,
    succeeded: bool,
    now: datetime | None = None,
    error_code: str = "input_artifact_delete_failed",
) -> bool:
    """Finish a claim only if it still owns the exact delete lease."""
    effective_now = _as_utc(now) or utcnow()
    artifact = session.scalar(
        select(ManagedInputArtifact)
        .where(ManagedInputArtifact.id == claim.artifact_id)
        .with_for_update()
    )
    if (
        artifact is None
        or artifact.status != ManagedInputArtifactStatus.DELETING
        or int(artifact.delete_attempts) != claim.attempt
        or not _same_timestamp(artifact.delete_started_at, claim.started_at)
        or not _same_timestamp(artifact.delete_lease_until, claim.lease_until)
    ):
        session.rollback()
        return False

    if succeeded:
        capacity = policy_service.get_capacity(session, for_update=True)
        _release_actual(capacity, int(artifact.size_bytes))
        artifact.status = ManagedInputArtifactStatus.DELETED
        artifact.deleted_at = effective_now
        artifact.delete_lease_until = None
        artifact.last_error_code = None
        session.commit()
        record_audit_event(
            "gc_delete",
            "success",
            adapter_id=int(artifact.adapter_id),
            artifact_id=int(artifact.id),
        )
        return True

    artifact.status = ManagedInputArtifactStatus.DELETE_FAILED
    artifact.last_error_code = error_code
    artifact.delete_lease_until = effective_now + timedelta(seconds=_backoff_seconds(claim.attempt))
    session.commit()
    logger.warning(
        "managed input artifact deletion failed adapter=%s artifact=%s code=%s "
        "retry_after_seconds=%s",
        int(artifact.adapter_id),
        int(artifact.id),
        error_code,
        _backoff_seconds(claim.attempt),
    )
    record_audit_event(
        "gc_delete",
        "failed",
        adapter_id=int(artifact.adapter_id),
        artifact_id=int(artifact.id),
        code=error_code,
    )
    return True


def process_artifact_deletions(
    session: Session,
    *,
    store: LocalFileArtifactStore | None = None,
    now: datetime | None = None,
    limit: int = GC_BATCH_SIZE,
    protection_hook: ArtifactProtectionHook | None = None,
) -> ArtifactDeletionReport:
    """Claim and process bounded Artifact deletes, retrying failures later."""
    if limit <= 0:
        return ArtifactDeletionReport()
    effective_now = _as_utc(now) or utcnow()
    store = store or LocalFileArtifactStore()
    claimed = deleted = failed = 0
    candidate_ids = _artifact_candidate_ids(session, effective_now, limit)
    for artifact_id in candidate_ids:
        claim = claim_artifact_deletion(
            session,
            int(artifact_id),
            now=effective_now,
            protection_hook=protection_hook,
        )
        if claim is None:
            continue
        claimed += 1
        try:
            store.delete(claim.storage_key)
        except (ArtifactStoreError, OSError):
            if finalize_artifact_deletion(
                session,
                claim,
                succeeded=False,
                now=effective_now,
            ):
                failed += 1
            continue
        if finalize_artifact_deletion(session, claim, succeeded=True, now=effective_now):
            deleted += 1
    return ArtifactDeletionReport(claimed=claimed, deleted=deleted, failed=failed)


def mark_expired_staged_artifacts(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = GC_BATCH_SIZE,
) -> int:
    """Move unbound, expired STAGED rows into the ordinary GC state machine."""
    if limit <= 0:
        return 0
    effective_now = _as_utc(now) or utcnow()
    candidate_ids = list(
        session.scalars(
            select(ManagedInputArtifact.id)
            .where(
                ManagedInputArtifact.status == ManagedInputArtifactStatus.STAGED,
                ManagedInputArtifact.expires_at.is_not(None),
                ManagedInputArtifact.expires_at <= effective_now,
                ~exists(
                    select(1).where(
                        AdapterInputArtifactBinding.artifact_id == ManagedInputArtifact.id
                    )
                ),
            )
            .order_by(ManagedInputArtifact.id)
            .limit(limit)
        )
    )
    marked = 0
    for artifact_id in candidate_ids:
        artifact = session.scalar(
            select(ManagedInputArtifact)
            .where(ManagedInputArtifact.id == int(artifact_id))
            .with_for_update(skip_locked=True)
        )
        if (
            artifact is None
            or artifact.status != ManagedInputArtifactStatus.STAGED
            or not _artifact_expired(artifact, effective_now)
            or session.scalar(
                select(exists().where(AdapterInputArtifactBinding.artifact_id == artifact.id))
            )
        ):
            session.rollback()
            continue
        artifact.status = ManagedInputArtifactStatus.PENDING_DELETE
        artifact.delete_lease_until = None
        artifact.last_error_code = None
        session.commit()
        record_audit_event(
            "artifact_ttl",
            "pending_delete",
            adapter_id=int(artifact.adapter_id),
            artifact_id=int(artifact.id),
        )
        marked += 1
    return marked


def expire_current_bindings(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = GC_BATCH_SIZE,
) -> int:
    """Delegate expiry/corruption transitions to the B2 system-only path.

    B2 owns the complete Adapter -> Schedule -> InputConfig -> Binding ->
    Artifact lock sequence and the revision/schedule mirror update.  B3 only
    selects a bounded set of affected Adapters, then lets B2 commit each
    lifecycle transaction before ordinary GC considers the unbound Artifacts.
    """
    if limit <= 0:
        return 0
    effective_now = _as_utc(now) or utcnow()
    checksum_invalid = or_(
        ManagedInputArtifact.sha256.is_(None),
        func.length(ManagedInputArtifact.sha256) != 64,
        ~func.lower(ManagedInputArtifact.sha256).op("~")(r"^[0-9a-f]{64}$"),
    )
    lifecycle_invalid = or_(
        ManagedInputArtifact.status != ManagedInputArtifactStatus.READY,
        and_(
            ManagedInputArtifact.expires_at.is_not(None),
            ManagedInputArtifact.expires_at <= effective_now,
        ),
        checksum_invalid,
    )
    adapter_ids = list(
        session.scalars(
            select(AdapterInputArtifactBinding.adapter_id)
            .join(
                ManagedInputArtifact,
                (ManagedInputArtifact.id == AdapterInputArtifactBinding.artifact_id)
                & (ManagedInputArtifact.adapter_id == AdapterInputArtifactBinding.adapter_id),
            )
            .where(lifecycle_invalid)
            .distinct()
            .order_by(AdapterInputArtifactBinding.adapter_id)
            .limit(limit)
        )
    )
    expired_count = 0
    for adapter_id in adapter_ids:
        before_ids = set(
            session.scalars(
                select(AdapterInputArtifactBinding.artifact_id).where(
                    AdapterInputArtifactBinding.adapter_id == int(adapter_id)
                )
            )
        )
        input_config_service.reconcile_current_bindings(
            session,
            int(adapter_id),
            now=effective_now,
        )
        after_ids = set(
            session.scalars(
                select(AdapterInputArtifactBinding.artifact_id).where(
                    AdapterInputArtifactBinding.adapter_id == int(adapter_id)
                )
            )
        )
        removed_ids = before_ids - after_ids
        for artifact_id in sorted(removed_ids):
            record_audit_event(
                "binding_expiry",
                "pending_delete",
                adapter_id=int(adapter_id),
                artifact_id=int(artifact_id),
                code="input_invalid",
            )
        expired_count += len(removed_ids)
        # B2 commits changed rows and leaves a no-op transaction open.  End
        # either read transaction here so the next Adapter can be processed
        # without retaining locks from the previous candidate.
        session.rollback()
    return expired_count


def has_active_upload(session: Session, adapter_id: int) -> bool:
    """Return whether an Adapter still has an active writer or reservation."""
    reservation = session.scalar(
        select(ManagedInputUploadReservation.id)
        .where(
            ManagedInputUploadReservation.adapter_id == int(adapter_id),
            ManagedInputUploadReservation.status == "ACTIVE",
        )
        .limit(1)
    )
    if reservation is not None:
        return True
    return (
        session.scalar(
            select(ManagedInputArtifact.id)
            .where(
                ManagedInputArtifact.adapter_id == int(adapter_id),
                ManagedInputArtifact.status == ManagedInputArtifactStatus.UPLOADING,
            )
            .limit(1)
        )
        is not None
    )


def ensure_no_active_upload(session: Session, adapter_id: int) -> None:
    """Reject Adapter deletion while the Adapter row is already locked."""
    if not has_active_upload(session, adapter_id):
        return
    record_audit_event(
        "adapter_delete",
        "rejected",
        adapter_id=int(adapter_id),
        code="adapter_upload_in_progress",
    )
    from dlr.control.services.adapter import domain_error

    raise domain_error(
        409,
        "adapter_upload_in_progress",
        "Adapter has an active input upload",
    )


def prepare_adapter_deletion(
    session: Session,
    adapter_id: int,
    *,
    now: datetime | None = None,
) -> list[int]:
    """Transfer charged Blob responsibility before Adapter metadata deletion.

    The caller must already hold the Adapter row lock.  No platform actual
    charge is released here: detached jobs retain the charge until they
    confirm the object is gone or missing.
    """
    _ = _as_utc(now) or utcnow()
    artifacts = list(
        session.scalars(
            select(ManagedInputArtifact)
            .where(ManagedInputArtifact.adapter_id == int(adapter_id))
            .order_by(ManagedInputArtifact.id)
            .with_for_update()
        )
    )
    charged_statuses = set(policy_service.CHARGED_ARTIFACT_STATUSES)
    existing_keys = set(
        session.scalars(
            select(ArtifactDeletionJob.storage_key).where(
                ArtifactDeletionJob.storage_key.in_(
                    [artifact.storage_key for artifact in artifacts]
                )
            )
        )
    )
    jobs: list[ArtifactDeletionJob] = []
    for artifact in artifacts:
        if artifact.status not in charged_statuses or artifact.storage_key in existing_keys:
            continue
        job = ArtifactDeletionJob(
            storage_key=artifact.storage_key,
            sha256=artifact.sha256,
            size_bytes=int(artifact.size_bytes),
            former_adapter_id=int(adapter_id),
            charged_bytes=int(artifact.size_bytes),
            status=ManagedInputDeletionJobStatus.PENDING,
        )
        session.add(job)
        jobs.append(job)
        existing_keys.add(artifact.storage_key)
    session.flush()

    # Delete dependent metadata only after the independent jobs have IDs.  The
    # deletion job table deliberately has no Adapter FK.
    session.execute(
        delete(AdapterInputArtifactBinding).where(
            AdapterInputArtifactBinding.adapter_id == int(adapter_id)
        )
    )
    session.execute(
        delete(ManagedInputArtifact).where(ManagedInputArtifact.adapter_id == int(adapter_id))
    )
    session.execute(
        delete(ManagedInputUploadReservation).where(
            ManagedInputUploadReservation.adapter_id == int(adapter_id)
        )
    )
    session.execute(
        delete(AdapterInputConfig).where(AdapterInputConfig.adapter_id == int(adapter_id))
    )
    return [int(job.id) for job in jobs]


def _job_delete_due(job: ArtifactDeletionJob, now: datetime) -> bool:
    status = str(job.status)
    if status not in {
        ManagedInputDeletionJobStatus.PENDING,
        ManagedInputDeletionJobStatus.DELETING,
        ManagedInputDeletionJobStatus.FAILED,
    }:
        return False
    lease_until = _as_utc(job.delete_lease_until)
    return lease_until is None or lease_until <= now


def _job_candidate_ids(session: Session, now: datetime, limit: int) -> list[int]:
    if limit <= 0:
        return []
    due = or_(
        ArtifactDeletionJob.delete_lease_until.is_(None),
        ArtifactDeletionJob.delete_lease_until <= now,
    )
    return list(
        session.scalars(
            select(ArtifactDeletionJob.id)
            .where(
                ArtifactDeletionJob.status.in_(
                    (
                        ManagedInputDeletionJobStatus.PENDING,
                        ManagedInputDeletionJobStatus.DELETING,
                        ManagedInputDeletionJobStatus.FAILED,
                    )
                ),
                due,
            )
            .order_by(ArtifactDeletionJob.id)
            .limit(limit)
        )
    )


def claim_deletion_job(
    session: Session,
    job_id: int,
    *,
    now: datetime | None = None,
    lease_seconds: int = DELETE_LEASE_SECONDS,
) -> DeletionJobClaim | None:
    """Claim one detached deletion job with an expiring database lease."""
    effective_now = _as_utc(now) or utcnow()
    job = session.scalar(
        select(ArtifactDeletionJob)
        .where(ArtifactDeletionJob.id == int(job_id))
        .with_for_update(skip_locked=True)
    )
    if job is None or not _job_delete_due(job, effective_now):
        session.rollback()
        return None
    attempt = int(job.attempts) + 1
    lease_until = effective_now + timedelta(seconds=max(10, int(lease_seconds)))
    job.status = ManagedInputDeletionJobStatus.DELETING
    job.attempts = attempt
    job.delete_lease_until = lease_until
    job.last_error_code = None
    session.commit()
    return DeletionJobClaim(
        job_id=int(job.id),
        storage_key=job.storage_key,
        charged_bytes=int(job.charged_bytes),
        attempt=attempt,
        lease_until=lease_until,
    )


def finalize_deletion_job(
    session: Session,
    claim: DeletionJobClaim,
    *,
    succeeded: bool,
    now: datetime | None = None,
    error_code: str = "input_artifact_delete_failed",
) -> bool:
    """Complete or back off a job only when its claim is still current."""
    effective_now = _as_utc(now) or utcnow()
    job = session.scalar(
        select(ArtifactDeletionJob).where(ArtifactDeletionJob.id == claim.job_id).with_for_update()
    )
    if (
        job is None
        or job.status != ManagedInputDeletionJobStatus.DELETING
        or int(job.attempts) != claim.attempt
        or not _same_timestamp(job.delete_lease_until, claim.lease_until)
    ):
        session.rollback()
        return False
    if succeeded:
        capacity = policy_service.get_capacity(session, for_update=True)
        if job.capacity_released_at is None:
            _release_actual(capacity, int(job.charged_bytes))
            job.capacity_released_at = effective_now
        job.status = ManagedInputDeletionJobStatus.COMPLETED
        job.completed_at = effective_now
        job.delete_lease_until = None
        job.last_error_code = None
        session.commit()
        record_audit_event(
            "deletion_job",
            "completed",
            deletion_job_id=int(job.id),
            code=None,
        )
        return True

    job.status = ManagedInputDeletionJobStatus.FAILED
    job.last_error_code = error_code
    job.delete_lease_until = effective_now + timedelta(seconds=_backoff_seconds(claim.attempt))
    session.commit()
    logger.warning(
        "managed input deletion job failed job=%s code=%s retry_after_seconds=%s",
        int(job.id),
        error_code,
        _backoff_seconds(claim.attempt),
    )
    record_audit_event(
        "deletion_job",
        "failed",
        deletion_job_id=int(job.id),
        code=error_code,
    )
    return True


def process_deletion_jobs(
    session: Session,
    *,
    store: LocalFileArtifactStore | None = None,
    now: datetime | None = None,
    limit: int = GC_BATCH_SIZE,
) -> DeletionJobReport:
    """Process detached jobs independently of the deleted Adapter."""
    if limit <= 0:
        return DeletionJobReport()
    effective_now = _as_utc(now) or utcnow()
    store = store or LocalFileArtifactStore()
    claimed = completed = failed = 0
    for job_id in _job_candidate_ids(session, effective_now, limit):
        claim = claim_deletion_job(session, int(job_id), now=effective_now)
        if claim is None:
            continue
        claimed += 1
        try:
            store.delete(claim.storage_key)
        except (ArtifactStoreError, OSError):
            if finalize_deletion_job(
                session,
                claim,
                succeeded=False,
                now=effective_now,
            ):
                failed += 1
            continue
        if finalize_deletion_job(session, claim, succeeded=True, now=effective_now):
            completed += 1
    return DeletionJobReport(claimed=claimed, completed=completed, failed=failed)


def run_orphan_audit(
    session: Session,
    *,
    store: LocalFileArtifactStore | None = None,
    older_than: datetime | None = None,
) -> ArtifactAuditResult:
    """Quarantine only old legal random objects absent from all DB metadata."""
    store = store or LocalFileArtifactStore()
    artifact_keys = set(
        session.scalars(
            select(ManagedInputArtifact.storage_key).where(
                ManagedInputArtifact.status != ManagedInputArtifactStatus.DELETED
            )
        )
    )
    deletion_keys = set(session.scalars(select(ArtifactDeletionJob.storage_key)))
    try:
        result = store.audit_orphans(artifact_keys | deletion_keys, older_than=older_than)
    except (ArtifactStoreError, OSError):
        record_audit_event("orphan_audit", "failed", code="artifact_store_unavailable")
        raise
    record_audit_event(
        "orphan_audit",
        "completed",
        code=None,
    )
    return result


def run_gc_cycle(
    session: Session,
    *,
    store: LocalFileArtifactStore | None = None,
    now: datetime | None = None,
    limit: int = GC_BATCH_SIZE,
    protection_hook: ArtifactProtectionHook | None = None,
) -> GarbageCollectionReport:
    """Run one bounded TTL, lifecycle, Artifact and detached-job cycle."""
    if limit <= 0:
        return GarbageCollectionReport()
    effective_now = _as_utc(now) or utcnow()
    store = store or LocalFileArtifactStore()
    from dlr.control.services.managed_input_upload import expire_upload_reservations

    reservations_expired = expire_upload_reservations(
        session,
        store=store,
        now=effective_now,
        limit=limit,
    )
    bindings_expired = expire_current_bindings(session, now=effective_now, limit=limit)
    staged_marked = mark_expired_staged_artifacts(session, now=effective_now, limit=limit)
    artifacts = process_artifact_deletions(
        session,
        store=store,
        now=effective_now,
        limit=limit,
        protection_hook=protection_hook,
    )
    jobs = process_deletion_jobs(
        session,
        store=store,
        now=effective_now,
        limit=limit,
    )
    return GarbageCollectionReport(
        reservations_expired=reservations_expired,
        bindings_expired=bindings_expired,
        staged_marked=staged_marked,
        artifacts_claimed=artifacts.claimed,
        artifacts_deleted=artifacts.deleted,
        artifacts_failed=artifacts.failed,
        deletion_jobs_claimed=jobs.claimed,
        deletion_jobs_completed=jobs.completed,
        deletion_jobs_failed=jobs.failed,
    )


def _gc_tick() -> None:
    with db.SessionLocal() as session:
        store = LocalFileArtifactStore(settings.artifact_store_root)
        run_gc_cycle(session, store=store)


def _audit_tick() -> None:
    with db.SessionLocal() as session:
        store = LocalFileArtifactStore(settings.artifact_store_root)
        run_orphan_audit(session, store=store)


async def artifact_gc_loop() -> None:
    """Keep TTL and deletion work retryable across Control restarts."""
    logger.info(
        "managed input GC loop started interval=%.2f batch_size=%s",
        settings.artifact_gc_interval_seconds,
        GC_BATCH_SIZE,
    )
    while True:
        try:
            await asyncio.to_thread(_gc_tick)
        except asyncio.CancelledError:
            raise
        except (ArtifactStoreError, OSError):
            record_audit_event("gc_cycle", "failed", code="artifact_store_unavailable")
            logger.warning("managed input GC cycle unavailable; retrying next interval")
        except Exception:  # noqa: BLE001 - one failed cycle must not stop governance
            record_audit_event("gc_cycle", "failed", code="gc_cycle_failed")
            logger.warning(
                "managed input GC cycle failed; retrying next interval",
                exc_info=True,
            )
        await asyncio.sleep(settings.artifact_gc_interval_seconds)


async def orphan_audit_loop() -> None:
    """Run the bounded low-frequency orphan audit without exposing object names."""
    logger.info(
        "managed input orphan audit loop started interval=%.2f",
        settings.artifact_audit_interval_seconds,
    )
    while True:
        try:
            await asyncio.to_thread(_audit_tick)
        except asyncio.CancelledError:
            raise
        except (ArtifactStoreError, OSError):
            record_audit_event("orphan_audit", "failed", code="artifact_store_unavailable")
            logger.warning("managed input orphan audit unavailable; retrying next interval")
        except Exception:  # noqa: BLE001 - one failed audit must not stop governance
            record_audit_event("orphan_audit", "failed", code="orphan_audit_failed")
            logger.warning(
                "managed input orphan audit failed; retrying next interval",
                exc_info=True,
            )
        await asyncio.sleep(settings.artifact_audit_interval_seconds)


# Explicit aliases keep the service discoverable for lifecycle callers.
gc_loop = artifact_gc_loop
audit_loop = orphan_audit_loop
run_artifact_gc = run_gc_cycle
process_artifact_gc = process_artifact_deletions
process_artifact_deletion_jobs = process_deletion_jobs
handoff_adapter_artifacts_for_deletion = prepare_adapter_deletion

__all__ = [
    "ArtifactDeletionClaim",
    "ArtifactDeletionReport",
    "ArtifactGCReport",
    "ArtifactProtectionHook",
    "DELETE_BACKOFF_BASE_SECONDS",
    "DELETE_LEASE_SECONDS",
    "DeletionJobClaim",
    "DeletionJobReport",
    "GarbageCollectionReport",
    "GCReport",
    "artifact_gc_loop",
    "audit_loop",
    "claim_artifact_deletion",
    "claim_deletion_job",
    "ensure_no_active_upload",
    "expire_current_bindings",
    "finalize_artifact_deletion",
    "finalize_deletion_job",
    "gc_loop",
    "has_active_artifact_lease",
    "has_active_upload",
    "handoff_adapter_artifacts_for_deletion",
    "mark_expired_staged_artifacts",
    "orphan_audit_loop",
    "prepare_adapter_deletion",
    "process_artifact_deletion_jobs",
    "process_artifact_deletions",
    "process_deletion_jobs",
    "record_audit_event",
    "run_artifact_gc",
    "run_gc_cycle",
    "run_orphan_audit",
    "utcnow",
]
