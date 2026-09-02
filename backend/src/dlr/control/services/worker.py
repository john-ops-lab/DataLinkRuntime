"""Domain service for Worker registration, heartbeat and task claiming.

Claiming uses ``FOR UPDATE SKIP LOCKED`` so concurrent Workers can never
claim the same Execution, while different Executions are claimed in
parallel. Long polling simply retries the atomic claim until the deadline.
"""

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, BinaryIO, cast

from fastapi import HTTPException
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.common.managed_input import MANAGED_INPUT_FILE_EXTENSION_SET
from dlr.control.input_errors import ManagedInputErrorCode
from dlr.control.models import (
    Adapter,
    AdapterVersion,
    Execution,
    ExecutionAttempt,
    ExecutionInputArtifactLease,
    ManagedInputArtifact,
    ManagedInputArtifactStatus,
    Worker,
    WorkerCleanupRequest,
)
from dlr.control.schemas.worker import (
    CleanupResult,
    CleanupTaskPayload,
    TaskInputFile,
    TaskPayload,
    WorkerRegister,
    isolation_capabilities_ready,
)
from dlr.control.services import package_source as package_source_service
from dlr.control.services import secrets as secrets_service
from dlr.control.services import worker_availability
from dlr.control.services.adapter import domain_error
from dlr.control.services.artifact_store import ArtifactStoreError, LocalFileArtifactStore
from dlr.control.services.worker_protocol import (
    generate_token,
    hash_token,
    require_claim_token,
    require_cleanup_token,
    token_matches,
)

MAX_CLAIM_WAIT_SECONDS = 30
CLAIM_POLL_INTERVAL_SECONDS = 1.0
READABLE_ARTIFACT_STATUSES = frozenset(
    {
        ManagedInputArtifactStatus.READY,
        ManagedInputArtifactStatus.PENDING_DELETE,
        ManagedInputArtifactStatus.DELETING,
        ManagedInputArtifactStatus.DELETE_FAILED,
    }
)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
DEFAULT_CONTENT_TYPE = "application/octet-stream"
V3_TERMINAL_ATTEMPT_STATUSES = frozenset(
    {"succeeded", "failed", "timed_out", "cancelled", "worker_lost", "resource_exceeded"}
)
V3_ACTIVE_ATTEMPT_STATUSES = frozenset({"claimed", "running"})


def _as_utc(value: datetime) -> datetime:
    """Normalize PostgreSQL and injected timestamps before deadline comparison."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _database_now(session: Session) -> datetime:
    """Sample the authoritative decision time after the candidate row is locked."""
    return _as_utc(worker_availability.current_time(session))


def _claim_deadline_expression() -> Any:
    """Return the explicit or legacy effective claim deadline SQL expression."""
    legacy_deadline = Execution.created_at + func.make_interval(
        0,
        0,
        0,
        0,
        0,
        0,
        settings.execution_claim_timeout_seconds,
    )
    return func.coalesce(Execution.claim_deadline_at, legacy_deadline)


def _effective_claim_deadline(execution: Execution) -> datetime:
    """Resolve a historical NULL deadline without rejuvenating the row."""
    deadline = execution.claim_deadline_at
    if deadline is None:
        deadline = execution.created_at + timedelta(
            seconds=settings.execution_claim_timeout_seconds
        )
    return _as_utc(deadline)


def _claim_deadline_is_open(execution: Execution, *, now: datetime) -> bool:
    """Only a deadline strictly after the post-lock DB time remains claimable."""
    return _effective_claim_deadline(execution) > _as_utc(now)


def _safe_content_type(value: object) -> str:
    """Return a bounded header-safe MIME value from persisted metadata."""
    if not isinstance(value, str) or not value or len(value) > 256:
        return DEFAULT_CONTENT_TYPE
    if not all(0x20 <= ord(character) < 0x7F for character in value):
        return DEFAULT_CONTENT_TYPE
    return value


def _controlled_input_mount_name(ordinal: int, original_filename: str) -> str:
    """Keep only a known lowercase type suffix in the opaque workspace name."""
    suffix = (
        f".{original_filename.rsplit('.', 1)[-1].casefold()}" if "." in original_filename else ""
    )
    if suffix not in MANAGED_INPUT_FILE_EXTENSION_SET:
        suffix = ""
    return f"input-{ordinal:02d}{suffix}"


@dataclass(frozen=True)
class WorkerInputDownload:
    """Verified Artifact handle and safe HTTP metadata for one Worker stream."""

    stream: BinaryIO
    size_bytes: int
    content_type: str


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
    if execution is None or execution.worker_id != worker_id or execution.status != "running":
        raise domain_error(
            422,
            "execution_claim_token_invalid",
            "A valid Claim Token is required",
        )
    if execution.dispatch_backend == "rabbitmq":
        attempt = session.scalar(
            select(ExecutionAttempt).where(
                ExecutionAttempt.execution_id == execution.id,
                ExecutionAttempt.attempt_no == execution.attempt_count,
                ExecutionAttempt.worker_id == worker_id,
                ExecutionAttempt.status.in_(V3_ACTIVE_ATTEMPT_STATUSES),
            )
        )
        if attempt is None:
            raise domain_error(
                422,
                "execution_claim_token_invalid",
                "A valid Claim Token is required",
            )
        require_claim_token(claim_token, attempt.claim_token_hash)
    else:
        if execution.claim_token_hash is None:
            raise domain_error(
                422,
                "execution_claim_token_invalid",
                "A valid Claim Token is required",
            )
        require_claim_token(claim_token, execution.claim_token_hash)
    return execution


def open_input_artifact_for_download(
    session: Session,
    worker_id: int,
    execution_id: int,
    artifact_id: int,
    claim_token: str | None,
    *,
    store: LocalFileArtifactStore | None = None,
) -> WorkerInputDownload:
    """Authorize, verify, and open one leased Artifact for streaming.

    The database Lease is checked before the Artifact row is exposed to the
    caller.  The object is hashed through its already-open descriptor before
    the descriptor is returned, so a stale or tampered Blob never becomes an
    HTTP stream.  Error details intentionally collapse missing, unleased,
    unreadable, and unavailable objects into stable machine codes.
    """
    execution = validate_claim_for_route(session, worker_id, execution_id, claim_token)
    artifact = session.scalar(
        select(ManagedInputArtifact)
        .join(
            ExecutionInputArtifactLease,
            ExecutionInputArtifactLease.artifact_id == ManagedInputArtifact.id,
        )
        .where(
            ExecutionInputArtifactLease.execution_id == execution.id,
            ManagedInputArtifact.id == artifact_id,
        )
    )
    if (
        artifact is None
        or artifact.status not in READABLE_ARTIFACT_STATUSES
        or artifact.size_bytes < 0
        or artifact.sha256 is None
        or SHA256_PATTERN.fullmatch(artifact.sha256) is None
    ):
        raise domain_error(
            422,
            ManagedInputErrorCode.ARTIFACT_NOT_READY.value,
            "Input Artifact is not available for this Execution",
        )

    stream: BinaryIO | None = None
    try:
        artifact_store = store or LocalFileArtifactStore(settings.artifact_store_root)
        stream = artifact_store.open(artifact.storage_key)
        digest = hashlib.sha256()
        actual_size = 0
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            actual_size += len(chunk)
            digest.update(chunk)
        if actual_size != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
            stream.close()
            raise domain_error(
                422,
                ManagedInputErrorCode.CHECKSUM_INVALID.value,
                "Input Artifact content does not match its metadata",
            )
        stream.seek(0)
    except HTTPException:
        raise
    except (ArtifactStoreError, OSError, ValueError) as error:
        if stream is not None:
            stream.close()
        # Do not include storage_key, object paths, or exception text in the
        # response or log.  The caller can safely retry this stable state.
        raise domain_error(
            422,
            ManagedInputErrorCode.ARTIFACT_NOT_READY.value,
            "Input Artifact is not available for this Execution",
        ) from error
    return WorkerInputDownload(
        stream=stream,
        size_bytes=artifact.size_bytes,
        content_type=_safe_content_type(artifact.content_type),
    )


def apply_cleanup_receipt(
    session: Session,
    execution_id: int,
    cleanup_token: str | None,
) -> Execution:
    """Advance only cleanup state after a valid v2 or v3 receipt.

    The row lock and capability-token check make retries after a lost HTTP
    response idempotent.  No business result, timestamp, or Lease is changed
    here.
    """
    execution = (
        session.query(Execution)
        .filter(Execution.id == execution_id)
        .with_for_update()
        .one_or_none()
    )
    if execution is None:
        raise domain_error(
            422,
            "execution_cleanup_token_invalid",
            "A valid Cleanup Token is required",
        )
    matched_attempt: ExecutionAttempt | None = None
    if execution.cleanup_receipt_token_hash is not None:
        require_cleanup_token(cleanup_token, execution.cleanup_receipt_token_hash)
    elif execution.dispatch_backend == "rabbitmq":
        attempts = list(
            session.scalars(
                select(ExecutionAttempt)
                .where(ExecutionAttempt.execution_id == execution_id)
                .order_by(ExecutionAttempt.attempt_no)
                .with_for_update()
            )
        )
        matched_attempt = next(
            (
                attempt
                for attempt in attempts
                if token_matches(cleanup_token, attempt.cleanup_token_hash)
            ),
            None,
        )
        if matched_attempt is None:
            raise domain_error(
                422,
                "execution_cleanup_token_invalid",
                "A valid Cleanup Token is required",
            )
    else:
        raise domain_error(
            422,
            "execution_cleanup_token_invalid",
            "A valid Cleanup Token is required",
        )
    if matched_attempt is None:
        allowed_statuses = {"succeeded", "failed", "timeout", "cancelled"}
    else:
        if matched_attempt.status not in V3_TERMINAL_ATTEMPT_STATUSES:
            raise domain_error(
                422,
                "workspace_cleanup_transition_invalid",
                "Workspace cleanup is not available before the Attempt is terminal",
            )
        # A receipt for an older Attempt can arrive while the next Attempt is
        # running. It is independently idempotent and must not complete the
        # current Attempt's Execution cleanup state.
        allowed_statuses = {
            "queued",
            "running",
            "retry_wait",
            "succeeded",
            "dead_letter",
            "cancelled",
            "expired",
        }
    if execution.status not in allowed_statuses:
        raise domain_error(
            422,
            "workspace_cleanup_transition_invalid",
            "Workspace cleanup is not available in the current Execution state",
        )
    if matched_attempt is not None:
        summary = dict(matched_attempt.cleanup_summary or {})
        summary["workspace_cleanup_status"] = "completed"
        summary.pop("workspace_cleanup_error_code", None)
        matched_attempt.cleanup_summary = summary
        if matched_attempt.attempt_no == execution.attempt_count:
            if execution.workspace_cleanup_status == "deferred":
                execution.workspace_cleanup_status = "completed"
                execution.workspace_cleanup_error_code = None
            elif execution.workspace_cleanup_status != "completed":
                raise domain_error(
                    422,
                    "workspace_cleanup_transition_invalid",
                    "Workspace cleanup state cannot transition to completed",
                )
    elif execution.workspace_cleanup_status == "deferred":
        execution.workspace_cleanup_status = "completed"
        execution.workspace_cleanup_error_code = None
    elif execution.workspace_cleanup_status != "completed":
        raise domain_error(
            422,
            "workspace_cleanup_transition_invalid",
            "Workspace cleanup state cannot transition to completed",
        )
    session.commit()
    session.refresh(execution)
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
    protocol_version = int(data.protocol_version or 1)
    preflight_ready = protocol_version >= 3 and isolation_capabilities_ready(
        data.isolation_capabilities
    )
    preflight_status = (
        "passed" if preflight_ready else ("failed" if protocol_version >= 3 else "unknown")
    )
    if worker is None:
        worker = Worker(
            name=data.name,
            status="online",
            last_heartbeat=func.now(),
            capabilities=data.capabilities,
            protocol_version=protocol_version,
            isolation_capabilities=data.isolation_capabilities,
            isolation_preflight_status=preflight_status,
            isolation_preflight_at=func.now() if protocol_version >= 3 else None,
            rabbitmq_execution_v3=preflight_ready,
        )
        session.add(worker)
    else:
        worker.status = "online"
        worker.last_heartbeat = func.now()
        worker.capabilities = data.capabilities
        worker.protocol_version = protocol_version
        worker.isolation_capabilities = cast(dict[str, object], data.isolation_capabilities)
        worker.isolation_preflight_status = preflight_status
        worker.isolation_preflight_at = func.now() if protocol_version >= 3 else None
        worker.rabbitmq_execution_v3 = preflight_ready
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


def heartbeat(
    session: Session,
    worker_id: int,
    isolation_capabilities: dict[str, bool] | None = None,
) -> None:
    worker = get_worker(session, worker_id)
    worker.status = "online"
    worker.last_heartbeat = func.now()
    if isolation_capabilities is not None:
        worker.isolation_capabilities = cast(dict[str, object], isolation_capabilities)
        if int(worker.protocol_version or 1) >= 3:
            ready = isolation_capabilities_ready(isolation_capabilities)
            worker.isolation_preflight_status = "passed" if ready else "failed"
            worker.isolation_preflight_at = func.now()
            worker.rabbitmq_execution_v3 = ready
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
                mount_name=_controlled_input_mount_name(lease.ordinal, artifact.original_filename),
                original_filename=artifact.original_filename,
                content_type=artifact.content_type,
                size_bytes=artifact.size_bytes,
                sha256=artifact.sha256,
            )
            for lease, artifact in rows
        ]
        snapshot_artifacts = execution.input_snapshot.get("artifacts", [])
        if not isinstance(snapshot_artifacts, list) or len(input_files) != len(snapshot_artifacts):
            raise domain_error(
                409,
                "execution_input_lease_unavailable",
                "Execution input Lease is unavailable",
            )
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
        secrets=secrets_service.resolve_execution_secrets(session, execution),
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

    lease_rejected_execution_ids: set[int] = set()
    expired_execution_ids: set[int] = set()
    while True:
        deadline_is_open = _claim_deadline_expression() > func.clock_timestamp()
        candidate_query = (
            select(Execution)
            .join(Adapter, Adapter.id == Execution.adapter_id)
            .where(
                # Batch 1 deliberately keeps the HTTP long-poll Claim path
                # legacy-only.  RabbitMQ dispatch rows wait for the later v3
                # Consumer/Claim batch and must never be silently downgraded.
                Execution.dispatch_backend == "legacy",
                Execution.status == "pending",
                Execution.cancel_requested.is_(False),
                Adapter.language.in_(worker.capabilities),
                or_(
                    Execution.target_worker_id.is_(None),
                    Execution.target_worker_id == worker_id,
                ),
                deadline_is_open,
            )
        )
        if protocol_version < 2:
            candidate_query = candidate_query.where(
                Execution.input_source_type.in_(("none", "json"))
            )
        excluded_execution_ids = lease_rejected_execution_ids | expired_execution_ids
        if excluded_execution_ids:
            candidate_query = candidate_query.where(Execution.id.not_in(excluded_execution_ids))
        candidate_query = (
            candidate_query.order_by(Execution.created_at.asc(), Execution.id.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        execution = session.scalar(candidate_query)
        if execution is None:
            if lease_rejected_execution_ids:
                session.rollback()
                raise domain_error(
                    409,
                    "execution_input_lease_unavailable",
                    "Execution input Lease is unavailable",
                )
            if protocol_version < 2:
                incompatible_exists = session.scalar(
                    select(Execution.id)
                    .join(Adapter, Adapter.id == Execution.adapter_id)
                    .where(
                        Execution.status == "pending",
                        Execution.dispatch_backend == "legacy",
                        Execution.cancel_requested.is_(False),
                        Execution.input_source_type.not_in(("none", "json")),
                        Adapter.language.in_(worker.capabilities),
                        or_(
                            Execution.target_worker_id.is_(None),
                            Execution.target_worker_id == worker_id,
                        ),
                        _claim_deadline_expression() > func.clock_timestamp(),
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
                rabbitmq_exists = session.scalar(
                    select(Execution.id)
                    .join(Adapter, Adapter.id == Execution.adapter_id)
                    .where(
                        Execution.dispatch_backend == "rabbitmq",
                        Execution.status == "queued",
                        Execution.cancel_requested.is_(False),
                        Adapter.language.in_(worker.capabilities),
                        or_(
                            Execution.target_worker_id.is_(None),
                            Execution.target_worker_id == worker_id,
                        ),
                    )
                    .order_by(Execution.created_at.asc(), Execution.id.asc())
                    .limit(1)
                )
                if rabbitmq_exists is not None:
                    session.rollback()
                    raise domain_error(
                        409,
                        "worker_protocol_incompatible",
                        "RabbitMQ Executions require the v3 Consumer protocol",
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
        now = _database_now(session)
        if not _claim_deadline_is_open(execution, now=now):
            # The SQL prefilter avoids locking work already known to be stale;
            # this post-lock check closes the query-to-lock boundary.  Leave
            # terminalization, cleanup facts and Lease release to the
            # reconciler's single convergence path.
            expired_execution_ids.add(int(execution.id))
            session.rollback()
            continue
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
        try:
            # Build the response while the claim is still uncommitted. A
            # historical managed-files row may predate the C0 Lease table; a
            # structured rejection leaves it pending and lets this same claim
            # attempt continue to a later valid candidate.
            payload = build_task_payload(
                session,
                execution,
                worker=worker,
                claim_token=claim_token,
                cleanup_token=cleanup_token,
            )
        except HTTPException as exc:
            detail = exc.detail
            if (
                exc.status_code == 409
                and isinstance(detail, dict)
                and detail.get("code") == "execution_input_lease_unavailable"
            ):
                lease_rejected_execution_ids.add(int(execution.id))
                session.rollback()
                continue
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        session.commit()
        return payload


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
