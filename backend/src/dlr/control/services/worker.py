"""Domain service for Worker registration, heartbeat and task claiming.

Claiming uses ``FOR UPDATE SKIP LOCKED`` so concurrent Workers can never
claim the same Execution, while different Executions are claimed in
parallel. Long polling simply retries the atomic claim until the deadline.
"""

import time
from datetime import timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    AdapterVersion,
    Execution,
    ExecutionInputArtifactLease,
    ManagedInputArtifact,
    Worker,
    WorkerCleanupRequest,
)
from dlr.control.schemas.worker import (
    CleanupResult,
    CleanupTaskPayload,
    TaskInputFile,
    TaskPayload,
    WorkerRegister,
)
from dlr.control.services import package_source as package_source_service
from dlr.control.services import secrets as secrets_service
from dlr.control.services.adapter import domain_error
from dlr.control.services.worker_protocol import (
    generate_token,
    hash_token,
    require_claim_token,
    require_cleanup_token,
)

MAX_CLAIM_WAIT_SECONDS = 30
CLAIM_POLL_INTERVAL_SECONDS = 1.0


def get_worker(session: Session, worker_id: int) -> Worker:
    worker = session.get(Worker, worker_id)
    if worker is None:
        raise domain_error(404, "worker_not_found", "Worker not found")
    return worker


def validate_claim_for_route(
    session: Session, worker_id: int, execution_id: int, claim_token: str | None
) -> Execution:
    """Validate the C0 Claim credential without exposing row existence.

    Full Artifact content authorization and streaming are deliberately owned
    by the later Worker download wave; the C0 route skeleton still enforces
    the protocol credential boundary before returning its not-ready result.
    """
    execution = session.get(Execution, execution_id)
    if (
        execution is None
        or execution.worker_id != worker_id
        or execution.status != "running"
        or execution.claim_token_hash is None
    ):
        raise domain_error(
            422,
            "execution_claim_token_invalid",
            "A valid Claim Token is required",
        )
    require_claim_token(claim_token, execution.claim_token_hash)
    return execution


def validate_cleanup_for_route(
    session: Session, worker_id: int, execution_id: int, cleanup_token: str | None
) -> Execution:
    """Validate the C0 Cleanup credential for the receipt route skeleton."""
    execution = session.get(Execution, execution_id)
    if (
        execution is None
        or execution.worker_id != worker_id
        or execution.status not in {"succeeded", "failed", "timeout", "cancelled"}
        or execution.cleanup_receipt_token_hash is None
    ):
        raise domain_error(
            422,
            "execution_cleanup_token_invalid",
            "A valid Cleanup Token is required",
        )
    require_cleanup_token(cleanup_token, execution.cleanup_receipt_token_hash)
    return execution


def list_workers(session: Session) -> list[Worker]:
    """All registered Workers with stored state, oldest registration first."""
    return list(session.scalars(select(Worker).order_by(Worker.id.asc())).all())


def register_worker(session: Session, data: WorkerRegister) -> Worker:
    """Upsert by name: restarts reuse the existing row.

    A process can die after claiming an adapter cleanup but before reporting
    the result. Re-registration is the safe ownership boundary at which to
    return that request to the queue; no Control-side filesystem fallback is
    attempted while the Worker is offline.
    """
    worker = session.scalar(select(Worker).where(Worker.name == data.name).with_for_update())
    if worker is None:
        worker = Worker(
            name=data.name,
            status="online",
            last_heartbeat=func.now(),
            capabilities=data.capabilities,
            protocol_version=data.protocol_version,
        )
        session.add(worker)
    else:
        worker.status = "online"
        worker.last_heartbeat = func.now()
        worker.capabilities = data.capabilities
        worker.protocol_version = data.protocol_version
        session.execute(
            update(WorkerCleanupRequest)
            .where(
                WorkerCleanupRequest.worker_id == worker.id,
                WorkerCleanupRequest.status == "running",
            )
            .values(status="pending", error_code=None)
        )
    session.commit()
    session.refresh(worker)
    return worker


def heartbeat(session: Session, worker_id: int) -> None:
    worker = get_worker(session, worker_id)
    worker.status = "online"
    worker.last_heartbeat = func.now()
    session.commit()


def mark_offline(session: Session, worker_id: int) -> None:
    worker = get_worker(session, worker_id)
    worker.status = "offline"
    session.commit()


def build_task_payload(
    session: Session,
    execution: Execution,
    *,
    worker: Worker | None = None,
    claim_token: str | None = None,
    cleanup_token: str | None = None,
) -> TaskPayload:
    """Assemble the task payload from the immutable version snapshot.

    Bound credential fields are decrypted here at claim time and travel
    inside the payload as ``secrets`` — an Execution only ever receives the
    secrets its own Adapter bound. The language-specific platform default
    dependency-source URL travels as ``index_url``.
    """
    version = session.get(AdapterVersion, execution.version_id)
    adapter = session.get(Adapter, execution.adapter_id)
    # Guaranteed by the restrict-delete foreign keys; a missing row here is
    # an invariant violation, not a user error.
    if version is None or adapter is None:
        raise RuntimeError("execution references a missing adapter version")
    worker = worker or session.get(Worker, execution.worker_id)
    if worker is None:
        raise RuntimeError("execution references a missing worker")
    input_files: list[TaskInputFile] = []
    if execution.input_source_type == "managed_files":
        rows = session.execute(
            select(ExecutionInputArtifactLease, ManagedInputArtifact)
            .join(
                ManagedInputArtifact,
                ManagedInputArtifact.id == ExecutionInputArtifactLease.artifact_id,
            )
            .where(ExecutionInputArtifactLease.execution_id == execution.id)
            .order_by(ExecutionInputArtifactLease.ordinal)
        ).all()
        input_files = [
            TaskInputFile(
                id=lease.artifact_id,
                ordinal=lease.ordinal,
                mount_name=f"input-{lease.ordinal:02d}",
                original_filename=artifact.original_filename,
                content_type=artifact.content_type,
                size_bytes=artifact.size_bytes,
                sha256=artifact.sha256 or "",
            )
            for lease, artifact in rows
        ]
        snapshot_artifacts = execution.input_snapshot.get("artifacts", [])
        if not isinstance(snapshot_artifacts, list) or len(input_files) != len(snapshot_artifacts):
            raise RuntimeError("execution input Lease set is incomplete")
    timeout_seconds = execution.timeout_seconds_snapshot or (
        adapter.timeout_seconds
        if adapter.timeout_seconds is not None
        else settings.execution_timeout_seconds
    )
    return TaskPayload(
        execution_id=execution.id,
        adapter_id=execution.adapter_id,
        version_id=execution.version_id,
        language=adapter.language,
        code=version.code,
        requirements=version.requirements,
        runtime_config=version.runtime_config,
        input=execution.input,
        latest_version_id=adapter.latest_version_id,
        # M5.5.11: the Adapter-level timeout is authoritative for every
        # Execution (manual, schedule and webhook). The platform setting is
        # only a defensive fallback for rows predating the migration.
        execution_timeout_seconds=timeout_seconds,
        secrets=secrets_service.resolve_adapter_secrets(session, execution.adapter_id),
        index_url=package_source_service.resolve_default_index_url(
            session,
            {"python": "pypi", "javascript": "npm", "java": "maven"}[adapter.language],
        ),
        locale=execution.locale,
        protocol_version=worker.protocol_version,
        claim_deadline_at=execution.claim_deadline_at,
        execution_deadline_at=execution.execution_deadline_at,
        recovery_grace_seconds_snapshot=execution.recovery_grace_seconds_snapshot,
        workspace_cleanup_attempt_timeout_seconds_snapshot=(
            execution.workspace_cleanup_attempt_timeout_seconds_snapshot
        ),
        workspace_cleanup_total_timeout_seconds_snapshot=(
            execution.workspace_cleanup_total_timeout_seconds_snapshot
        ),
        input_files=input_files,
        claim_token=claim_token,
        cleanup_token=cleanup_token,
    )


def try_claim(session: Session, worker_id: int) -> TaskPayload | CleanupTaskPayload | None:
    """One atomic claim attempt; None when no task is free.

    M3.2 scheduling rules: Executions flagged for cancellation are never
    claimed, and an Execution with a scheduling target may only be claimed
    by that Worker (a NULL target stays claimable by any Worker, which keeps
    historical rows working).
    """
    worker = get_worker(session, worker_id)
    protocol_version = int(worker.protocol_version or 1)
    if protocol_version < settings.min_worker_protocol_version:
        raise domain_error(
            409,
            "worker_protocol_incompatible",
            "Worker protocol version is below the deployment minimum",
        )
    cleanup = session.scalar(
        select(WorkerCleanupRequest)
        .where(
            WorkerCleanupRequest.worker_id == worker_id,
            WorkerCleanupRequest.status == "pending",
        )
        .order_by(WorkerCleanupRequest.created_at.asc(), WorkerCleanupRequest.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if cleanup is not None:
        cleanup.status = "running"
        cleanup.attempts += 1
        cleanup.error_code = None
        session.commit()
        session.refresh(cleanup)
        return CleanupTaskPayload(cleanup_id=cleanup.id, adapter_id=cleanup.adapter_id)

    candidate_query = (
        select(Execution)
        .join(Adapter, Adapter.id == Execution.adapter_id)
        .where(
            Execution.status == "pending",
            Execution.cancel_requested.is_(False),
            Adapter.language.in_(worker.capabilities),
            or_(
                Execution.target_worker_id.is_(None),
                Execution.target_worker_id == worker_id,
            ),
        )
    )
    if protocol_version < 2:
        candidate_query = candidate_query.where(Execution.input_source_type.in_(("none", "json")))
    candidate_query = (
        candidate_query.order_by(Execution.created_at.asc(), Execution.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    execution = session.scalar(candidate_query)
    if execution is None:
        if protocol_version < 2:
            incompatible_exists = session.scalar(
                select(Execution.id)
                .join(Adapter, Adapter.id == Execution.adapter_id)
                .where(
                    Execution.status == "pending",
                    Execution.cancel_requested.is_(False),
                    Execution.input_source_type.not_in(("none", "json")),
                    Adapter.language.in_(worker.capabilities),
                    or_(
                        Execution.target_worker_id.is_(None),
                        Execution.target_worker_id == worker_id,
                    ),
                )
                .order_by(Execution.created_at.asc(), Execution.id.asc())
                .limit(1)
            )
            if incompatible_exists is not None:
                session.rollback()
                raise domain_error(
                    409,
                    "worker_protocol_incompatible",
                    "This Execution requires a newer Worker protocol",
                )
        # Release any snapshot state before the next poll iteration.
        session.rollback()
        return None
    if protocol_version < 2 and execution.input_source_type not in {"none", "json"}:
        # Defensive guard for rows introduced by a concurrent migration or a
        # future source type: do not transition an unclaimable Execution.
        session.rollback()
        raise domain_error(
            409,
            "worker_protocol_incompatible",
            "This Execution requires a newer Worker protocol",
        )
    now = session.scalar(select(func.clock_timestamp()))
    if now is None:  # pragma: no cover - PostgreSQL always returns a value
        raise RuntimeError("Database clock did not return a timestamp")
    claim_token: str | None = None
    cleanup_token: str | None = None
    execution.status = "running"
    execution.worker_id = worker_id
    execution.started_at = now
    if protocol_version >= 2:
        claim_token = generate_token()
        cleanup_token = generate_token()
        execution.claim_token_hash = hash_token(claim_token)
        execution.cleanup_receipt_token_hash = hash_token(cleanup_token)
        timeout_seconds = execution.timeout_seconds_snapshot
        if timeout_seconds is None:
            adapter = session.get(Adapter, execution.adapter_id)
            timeout_seconds = (
                adapter.timeout_seconds
                if adapter is not None
                else settings.execution_timeout_seconds
            )
        execution.execution_deadline_at = now + timedelta(seconds=timeout_seconds)
    session.commit()
    session.refresh(execution)
    return build_task_payload(
        session,
        execution,
        worker=worker,
        claim_token=claim_token,
        cleanup_token=cleanup_token,
    )


def claim_task(
    session: Session, worker_id: int, wait_seconds: int
) -> TaskPayload | CleanupTaskPayload | None:
    """Long-poll: retry the atomic claim until a task or the deadline."""
    get_worker(session, worker_id)
    wait_seconds = max(0, min(wait_seconds, MAX_CLAIM_WAIT_SECONDS))
    deadline = time.monotonic() + wait_seconds
    while True:
        payload = try_claim(session, worker_id)
        if payload is not None:
            return payload
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(CLAIM_POLL_INTERVAL_SECONDS, remaining))


def apply_cleanup_result(
    session: Session, worker_id: int, cleanup_id: int, report: CleanupResult
) -> WorkerCleanupRequest:
    """Record one secret-free Worker cleanup result.

    Failed filesystem cleanup is retried a bounded number of times. The
    Control Node never performs a fallback deletion because doing so would
    violate the Worker ownership and offline safety boundary.
    """
    cleanup = session.scalar(
        select(WorkerCleanupRequest).where(WorkerCleanupRequest.id == cleanup_id).with_for_update()
    )
    if cleanup is None:
        raise domain_error(404, "cleanup_not_found", "Worker cleanup request not found")
    if cleanup.worker_id != worker_id:
        raise domain_error(
            409,
            "cleanup_not_owned",
            "Worker cleanup request is assigned to another Worker",
        )
    if cleanup.status == "completed":
        return cleanup
    if cleanup.status != "running":
        raise domain_error(409, "cleanup_not_running", "Worker cleanup request is not running")

    if report.success:
        cleanup.status = "completed"
        cleanup.error_code = None
        cleanup.completed_at = func.now()
    else:
        cleanup.error_code = "cleanup_failed"
        cleanup.status = "pending" if cleanup.attempts < 3 else "failed"
    session.commit()
    session.refresh(cleanup)
    return cleanup
