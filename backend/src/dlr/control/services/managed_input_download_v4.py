"""A bounded, already-admitted file read; never a new execution authority.

Only the existing Control child may hold this grant and its storage key.
Admission uses both current proofs under the original business lock order.
Subsequent chunks retain the original deadline, while checking mutable
ownership/revocation without requiring the original proof to remain current
after an ordinary renewal. Callers fence each short transaction separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.common import managed_files_v4 as contract
from dlr.common.managed_input import controlled_input_mount_name
from dlr.control.models import (
    AdapterExecutionSlot,
    AttemptLeaseCandidateV4,
    Execution,
    ExecutionAttempt,
    ExecutionInputArtifactLease,
    ManagedInputArtifact,
    Worker,
    WorkerLeaseCandidateV4,
)
from dlr.control.schemas.reliable_runtime import AttemptActionBody
from dlr.control.services import attempt_lease_candidates_v4 as candidates
from dlr.control.services import worker_availability, worker_credentials
from dlr.control.services.worker import READABLE_ARTIFACT_STATUSES
from dlr.control.services.worker_lease_candidates_v4 import ProofAuthority


class InputDownloadUnavailable(Exception):
    """Stable refusal; no database, file or credential details are public."""


@dataclass(frozen=True)
class InputReadGrant:
    stream_id: UUID
    worker_id: int
    worker_identity_id: UUID
    instance_id: UUID
    generation: int
    execution_id: int
    attempt_id: int
    fencing_token: int
    file: contract.InputFile
    original_until: datetime
    storage_key: str = field(repr=False)
    credential: str = field(repr=False)


def admit(
    session: Session,
    *,
    worker_id: int,
    execution_id: int,
    artifact_id: int,
    credential: str,
    instance_id: UUID,
    generation: int,
    instance_proof: str,
    attempt_proof: str,
    action: AttemptActionBody,
    authority: ProofAuthority,
) -> tuple[InputReadGrant, float]:
    """Admit only a running, finalized Attempt's exact frozen file.

    The caller must end this transaction before opening storage or waiting
    for network I/O. The returned time budget is applied to the monotonic
    timestamp taken *before* admission, so query delay never extends it.
    """
    if (
        type(execution_id) is not int
        or not 0 < execution_id < 2**63
        or type(artifact_id) is not int
        or not 0 < artifact_id < 2**63
    ):
        raise InputDownloadUnavailable("input_download_unavailable")
    lease = candidates.validate_current_attempt_proof(
        session,
        worker_id=worker_id,
        attempt_id=action.attempt_id,
        credential=credential,
        instance_id=instance_id,
        generation=generation,
        instance_proof=instance_proof,
        attempt_proof=attempt_proof,
        action=action,
        authority=authority,
    )
    attempt = session.get(ExecutionAttempt, action.attempt_id)
    execution = session.get(Execution, execution_id)
    worker = session.get(Worker, worker_id)
    instance = session.get(WorkerLeaseCandidateV4, worker_id)
    if (
        attempt is None
        or execution is None
        or worker is None
        or instance is None
        or attempt.execution_id != execution_id
        or attempt.status != "running"
        or attempt.started_at is None
        or execution.status != "running"
        or execution.cancel_requested
        or execution.input_source_type != "managed_files"
        or type(execution.input_snapshot) is not dict
        or (worker.isolation_capabilities or {}).get(contract.CAPABILITY) is not True
        or type(execution.timeout_seconds_snapshot) is not int
        or execution.timeout_seconds_snapshot <= 0
    ):
        raise InputDownloadUnavailable("input_download_unavailable")
    rows = session.execute(
        select(ExecutionInputArtifactLease, ManagedInputArtifact)
        .join(
            ManagedInputArtifact, ManagedInputArtifact.id == ExecutionInputArtifactLease.artifact_id
        )
        .where(ExecutionInputArtifactLease.execution_id == execution_id)
        .order_by(ExecutionInputArtifactLease.ordinal)
        .limit(contract.MAX_FILES + 1)
    ).all()
    metadata = []
    for item, artifact in rows:
        if (
            artifact.adapter_id != execution.adapter_id
            or artifact.status not in READABLE_ARTIFACT_STATUSES
        ):
            raise InputDownloadUnavailable("input_download_unavailable")
        metadata.append(
            {
                "id": artifact.id,
                "ordinal": item.ordinal,
                "mount_name": controlled_input_mount_name(item.ordinal, artifact.original_filename),
                "original_filename": artifact.original_filename,
                "content_type": artifact.content_type,
                "size_bytes": artifact.size_bytes,
                "sha256": artifact.sha256,
            }
        )
    files = contract.descriptors(metadata)
    frozen = [{k: v for k, v in item.items() if k not in {"id", "mount_name"}} for item in metadata]
    if frozen != execution.input_snapshot.get("artifacts"):
        raise InputDownloadUnavailable("input_download_unavailable")
    index = next((i for i, item in enumerate(files) if item.artifact_id == artifact_id), None)
    if index is None:
        raise InputDownloadUnavailable("input_download_unavailable")
    expires = candidates._credential_expiry(session, worker_id, credential)
    now = worker_availability.current_time(session)
    until = min(
        lease.nominal_until,
        lease.guard_until,
        instance.nominal_until,
        instance.guard_until,
        attempt.claimed_at + timedelta(seconds=contract.FILE_TIMEOUT_SECONDS),
        attempt.started_at + timedelta(seconds=execution.timeout_seconds_snapshot),
    )
    if expires is not None:
        until = min(until, expires)
    until = min(until, now + timedelta(seconds=contract.FILE_TIMEOUT_SECONDS))
    remaining = min(contract.FILE_TIMEOUT_SECONDS, (until - now).total_seconds())
    if remaining <= 0:
        raise InputDownloadUnavailable("input_download_unavailable")
    grant = InputReadGrant(
        uuid4(),
        worker_id,
        worker.identity_id,
        instance_id,
        generation,
        execution_id,
        attempt.id,
        attempt.fencing_token,
        files[index],
        until,
        rows[index][1].storage_key,
        credential,
    )
    return grant, remaining


def recheck(session: Session, grant: InputReadGrant) -> None:
    """Check one read-only chunk boundary; never extend the admitted grant."""
    worker_credentials.prescreen_runtime_credential(session, grant.worker_id, grant.credential)
    now = worker_availability.current_time(session)
    if now >= grant.original_until:
        raise InputDownloadUnavailable("input_download_unavailable")
    found = session.scalar(
        select(ExecutionAttempt.id)
        .join(Execution, Execution.id == ExecutionAttempt.execution_id)
        .join(Worker, Worker.id == ExecutionAttempt.worker_id)
        .join(WorkerLeaseCandidateV4, WorkerLeaseCandidateV4.worker_id == Worker.id)
        .join(AttemptLeaseCandidateV4, AttemptLeaseCandidateV4.attempt_id == ExecutionAttempt.id)
        .join(
            AdapterExecutionSlot,
            AdapterExecutionSlot.active_attempt_id == ExecutionAttempt.id,
        )
        .join(ExecutionInputArtifactLease, ExecutionInputArtifactLease.execution_id == Execution.id)
        .join(
            ManagedInputArtifact, ManagedInputArtifact.id == ExecutionInputArtifactLease.artifact_id
        )
        .where(
            ExecutionAttempt.id == grant.attempt_id,
            ExecutionAttempt.execution_id == grant.execution_id,
            ExecutionAttempt.worker_id == grant.worker_id,
            ExecutionAttempt.worker_identity_id == grant.worker_identity_id,
            ExecutionAttempt.worker_instance_id == grant.instance_id,
            ExecutionAttempt.worker_instance_generation == grant.generation,
            ExecutionAttempt.fencing_token == grant.fencing_token,
            ExecutionAttempt.status == "running",
            ExecutionAttempt.lease_expires_at > now,
            Execution.status == "running",
            Execution.input_source_type == "managed_files",
            Execution.cancel_requested.is_(False),
            AdapterExecutionSlot.adapter_id == Execution.adapter_id,
            AdapterExecutionSlot.slot_no == 0,
            AdapterExecutionSlot.fencing_token == grant.fencing_token,
            AdapterExecutionSlot.lease_expires_at == ExecutionAttempt.lease_expires_at,
            AdapterExecutionSlot.lease_expires_at > now,
            Worker.identity_id == grant.worker_identity_id,
            Worker.protocol_version == 4,
            Worker.identity_lifecycle == "reserved",
            Worker.instance_id == grant.instance_id,
            Worker.instance_generation == grant.generation,
            Worker.instance_lease_until == WorkerLeaseCandidateV4.nominal_until,
            Worker.scheduling_state.in_(("active", "draining")),
            WorkerLeaseCandidateV4.worker_identity_id == grant.worker_identity_id,
            WorkerLeaseCandidateV4.instance_id == grant.instance_id,
            WorkerLeaseCandidateV4.instance_generation == grant.generation,
            WorkerLeaseCandidateV4.retired.is_(False),
            WorkerLeaseCandidateV4.nominal_until > now,
            WorkerLeaseCandidateV4.guard_until > now,
            AttemptLeaseCandidateV4.worker_id == grant.worker_id,
            AttemptLeaseCandidateV4.worker_identity_id == grant.worker_identity_id,
            AttemptLeaseCandidateV4.instance_id == grant.instance_id,
            AttemptLeaseCandidateV4.instance_generation == grant.generation,
            AttemptLeaseCandidateV4.fencing_token == grant.fencing_token,
            AttemptLeaseCandidateV4.nominal_until == ExecutionAttempt.lease_expires_at,
            AttemptLeaseCandidateV4.nominal_until > now,
            AttemptLeaseCandidateV4.guard_until > now,
            ExecutionInputArtifactLease.artifact_id == grant.file.artifact_id,
            ExecutionInputArtifactLease.ordinal == grant.file.ordinal,
            ManagedInputArtifact.adapter_id == Execution.adapter_id,
            ManagedInputArtifact.storage_key == grant.storage_key,
            ManagedInputArtifact.sha256 == grant.file.sha256,
            ManagedInputArtifact.size_bytes == grant.file.size_bytes,
            ManagedInputArtifact.status.in_(READABLE_ARTIFACT_STATUSES),
        )
    )
    if found != grant.attempt_id:
        raise InputDownloadUnavailable("input_download_unavailable")
