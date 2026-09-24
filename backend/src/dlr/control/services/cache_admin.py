"""Durable bounded cache administration commands and latest observations."""

import hashlib
import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.control.models import (
    Adapter,
    Worker,
    WorkerCacheManagementChild,
    WorkerCacheManagementOperation,
    WorkerCacheOperation,
    WorkerCacheSnapshot,
    WorkerCacheSnapshotItem,
    WorkerCleanupRequest,
)
from dlr.control.schemas.cache_admin import (
    CacheAdminView,
    CacheCommandClaim,
    CacheCommandResult,
    CacheOperationCreate,
    CacheOperationPage,
    CacheOperationResponse,
    CacheProtectSelection,
    CacheSnapshotItem,
    CacheSnapshotUpload,
    FailedCleanupItem,
    FailedGuardItem,
)
from dlr.control.security import Principal
from dlr.control.services.adapter import domain_error

MAX_TERMINAL_PER_WORKER = 1000
TERMINAL_RETENTION_DAYS = 90
SNAPSHOT_STALE_SECONDS = 300
_SENSITIVE_KEYS = {"path", "url", "token", "source_scope", "code", "log", "error"}


def _canonical(data: object) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _request_payload(request: CacheOperationCreate) -> dict[str, object]:
    return request.model_dump(mode="json", exclude={"idempotency_key"}, exclude_none=True)


def _request_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def _actor(principal: Principal) -> dict[str, object]:
    if principal.kind == "superadmin":
        return {"kind": "superadmin"}
    return {"kind": "account", "user_id": principal.user_id or 0}


def _worker_locked(session: Session, worker_id: int) -> Worker:
    worker = session.scalar(select(Worker).where(Worker.id == worker_id).with_for_update())
    if worker is None:
        raise domain_error(404, "worker_not_found", "Worker not found")
    return worker


def _governance_ready(worker: Worker) -> bool:
    return (
        int(worker.protocol_version or 0) == 3
        and worker.isolation_capabilities.get("cache_governance_v1") is True
    )


def _snapshot_item_map(
    session: Session, worker_id: int
) -> dict[tuple[int, int], WorkerCacheSnapshotItem]:
    return {
        (row.adapter_id, row.version_id): row
        for row in session.scalars(
            select(WorkerCacheSnapshotItem).where(WorkerCacheSnapshotItem.worker_id == worker_id)
        )
    }


def _validate_bound_keys(session: Session, worker_id: int, request: CacheOperationCreate) -> None:
    snapshot = session.get(WorkerCacheSnapshot, worker_id)
    if (
        snapshot is None
        or snapshot.state == "owner_unconfirmed"
        or snapshot.received_at < datetime.now(UTC) - timedelta(seconds=SNAPSHOT_STALE_SECONDS)
    ):
        raise domain_error(409, "cache_snapshot_unavailable", "Cache snapshot is unavailable")
    observed = _snapshot_item_map(session, worker_id)
    selections = request.protect if request.kind == "protect" else request.keys
    for selection in selections or []:
        row = observed.get((selection.adapter_id, selection.version_id))
        if row is None:
            raise domain_error(
                409, "cache_snapshot_key_missing", "Cache key is not in the latest snapshot"
            )
        if request.kind == "protect" and (
            row.kind != "version"
            or row.identity is None
            or row.digest is None
            or row.digest != cast(CacheProtectSelection, selection).digest
            or row.identity
            != cast(CacheProtectSelection, selection).identity.model_dump(mode="json")
        ):
            raise domain_error(409, "cache_snapshot_changed", "Cache observation changed")


def _validate_retry(
    session: Session,
    worker_id: int,
    request: CacheOperationCreate,
    operation_id: uuid.UUID,
) -> tuple[str, uuid.UUID | None, int | None]:
    if request.cleanup_id is not None:
        candidate = session.get(WorkerCleanupRequest, request.cleanup_id)
        if candidate is None:
            raise domain_error(404, "cleanup_not_found", "Worker cleanup request not found")
        previous_retry = candidate.retry_operation_id
        if previous_retry is not None:
            previous = session.scalar(
                select(WorkerCacheManagementOperation)
                .where(WorkerCacheManagementOperation.operation_id == previous_retry)
                .with_for_update()
            )
            if previous is None or previous.status in {"pending", "running"}:
                raise domain_error(
                    409, "cleanup_retry_active", "Cleanup already has a retry operation"
                )
        cleanup = session.scalar(
            select(WorkerCleanupRequest)
            .where(WorkerCleanupRequest.id == request.cleanup_id)
            .with_for_update()
        )
        if cleanup is None:
            raise domain_error(404, "cleanup_not_found", "Worker cleanup request not found")
        if cleanup.worker_id != worker_id or cleanup.status != "failed":
            raise domain_error(409, "cleanup_retry_unavailable", "Cleanup cannot be retried")
        if session.get(Adapter, cleanup.adapter_id) is not None:
            raise domain_error(409, "cleanup_adapter_exists", "Cleanup target Adapter still exists")
        if cleanup.retry_operation_id != previous_retry:
            raise domain_error(409, "cleanup_retry_changed", "Cleanup retry association changed")
        cleanup.retry_operation_id = operation_id
        cleanup.status = "pending"
        cleanup.error_code = None
        return "cleanup", None, cleanup.id
    if request.management_operation_id is not None:
        target = session.scalar(
            select(WorkerCacheManagementOperation)
            .where(WorkerCacheManagementOperation.operation_id == request.management_operation_id)
            .with_for_update()
        )
        if target is None or target.worker_id != worker_id or target.status != "failed":
            raise domain_error(409, "cache_retry_target_unavailable", "Operation cannot be retried")
        children = list(
            session.execute(
                select(
                    WorkerCacheManagementChild.generation.label("linked_generation"),
                    WorkerCacheOperation.generation.label("guard_generation"),
                    WorkerCacheOperation.phase.label("guard_phase"),
                )
                .select_from(WorkerCacheManagementChild)
                .outerjoin(
                    WorkerCacheOperation,
                    WorkerCacheOperation.operation_id
                    == WorkerCacheManagementChild.guard_operation_id,
                )
                .where(WorkerCacheManagementChild.management_operation_id == target.operation_id)
            )
        )
        if children and all(
            linked_generation == guard_generation and guard_phase in {"completed", "aborted"}
            for linked_generation, guard_generation, guard_phase in children
        ):
            raise domain_error(
                409,
                "cache_retry_target_resolved",
                "Operation children are already resolved",
            )
        return "management", target.operation_id, None
    assert request.guard_operation_id is not None
    guard_operation = session.get(WorkerCacheOperation, request.guard_operation_id)
    if (
        guard_operation is None
        or guard_operation.worker_id != worker_id
        or guard_operation.phase != "acquired"
        or guard_operation.operation_kind != "gc"
    ):
        raise domain_error(
            409, "cache_retry_target_unavailable", "Guard operation cannot be retried"
        )
    managed = session.scalar(
        select(WorkerCacheManagementChild.guard_operation_id).where(
            WorkerCacheManagementChild.guard_operation_id == guard_operation.operation_id
        )
    )
    if managed is not None:
        raise domain_error(
            409,
            "cache_retry_target_managed",
            "Guard operation must be retried through its management operation",
        )
    snapshot = session.get(WorkerCacheSnapshot, worker_id)
    failed_items = [] if snapshot is None else snapshot.summary.get("failed_guard_items", [])
    if not isinstance(failed_items, list) or not any(
        isinstance(item, dict)
        and item.get("guard_operation_id") == str(guard_operation.operation_id)
        and item.get("generation") == guard_operation.generation
        and item.get("local_phase") == "failed"
        for item in failed_items
    ):
        raise domain_error(
            409, "cache_retry_observation_unavailable", "Failed guard observation is unavailable"
        )
    return "guard", guard_operation.operation_id, None


def create_operation(
    session: Session, worker_id: int, request: CacheOperationCreate, principal: Principal
) -> WorkerCacheManagementOperation:
    worker = _worker_locked(session, worker_id)
    if not _governance_ready(worker):
        raise domain_error(
            409, "cache_worker_unsupported", "Worker does not support cache governance"
        )
    payload = _request_payload(request)
    digest = _request_hash(payload)
    existing = session.scalar(
        select(WorkerCacheManagementOperation).where(
            WorkerCacheManagementOperation.worker_id == worker_id,
            WorkerCacheManagementOperation.idempotency_key == request.idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_hash != digest:
            raise domain_error(
                409, "cache_idempotency_conflict", "Idempotency key was used for another request"
            )
        session.rollback()
        return existing
    if request.kind == "protect":
        now = datetime.now(UTC)
        for selection in request.protect or []:
            if selection.valid_until is not None:
                valid_until = selection.valid_until.astimezone(UTC)
                if valid_until <= now or (valid_until - now).total_seconds() > 86_400:
                    raise domain_error(
                        422,
                        "cache_proof_validity_invalid",
                        "valid_until must be within the next 24 hours",
                    )
    active = session.scalar(
        select(WorkerCacheManagementOperation.operation_id).where(
            WorkerCacheManagementOperation.worker_id == worker_id,
            WorkerCacheManagementOperation.status.in_(("pending", "running")),
        )
    )
    if active is not None:
        raise domain_error(
            409, "cache_operation_active", "Worker already has an active cache operation"
        )
    operation_id = uuid.uuid4()
    target_kind = None
    target_operation_id = None
    target_cleanup_id = None
    if request.kind == "retry":
        target_kind, target_operation_id, target_cleanup_id = _validate_retry(
            session, worker_id, request, operation_id
        )
    else:
        _validate_bound_keys(session, worker_id, request)
    operation = WorkerCacheManagementOperation(
        operation_id=operation_id,
        worker_id=worker_id,
        kind=request.kind,
        status="running" if target_kind == "cleanup" else "pending",
        idempotency_key=request.idempotency_key,
        request_hash=digest,
        request_payload=payload,
        actor=_actor(principal),
        target_kind=target_kind,
        target_operation_id=target_operation_id,
        target_cleanup_id=target_cleanup_id,
    )
    session.add(operation)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise domain_error(
            409, "cache_operation_active", "Worker already has an active cache operation"
        ) from error
    session.refresh(operation)
    return operation


def claim_command(session: Session, worker_id: int) -> CacheCommandClaim | None:
    worker = _worker_locked(session, worker_id)
    if not _governance_ready(worker):
        session.rollback()
        return None
    operation = session.scalar(
        select(WorkerCacheManagementOperation)
        .where(
            WorkerCacheManagementOperation.worker_id == worker_id,
            WorkerCacheManagementOperation.status == "pending",
            or_(
                WorkerCacheManagementOperation.target_kind.is_(None),
                WorkerCacheManagementOperation.target_kind != "cleanup",
            ),
        )
        .order_by(
            WorkerCacheManagementOperation.created_at, WorkerCacheManagementOperation.operation_id
        )
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if operation is None:
        session.rollback()
        return None
    operation.status = "running"
    operation.claim_epoch += 1
    operation.claimed_at = func.now()
    payload = dict(operation.request_payload)
    if operation.target_kind == "management" and operation.target_operation_id is not None:
        target = session.get(WorkerCacheManagementOperation, operation.target_operation_id)
        if target is None or target.worker_id != worker_id:
            raise domain_error(409, "cache_retry_target_unavailable", "Retry target is unavailable")
        source = target
        seen = {source.operation_id}
        for _ in range(200):
            if source.kind != "retry" or source.target_kind != "management":
                break
            if source.target_operation_id is None or source.target_operation_id in seen:
                raise domain_error(
                    409, "cache_retry_target_unavailable", "Retry target is unavailable"
                )
            seen.add(source.target_operation_id)
            ancestor = session.get(WorkerCacheManagementOperation, source.target_operation_id)
            if ancestor is None or ancestor.worker_id != worker_id:
                raise domain_error(
                    409, "cache_retry_target_unavailable", "Retry target is unavailable"
                )
            source = ancestor
        else:
            raise domain_error(409, "cache_retry_target_unavailable", "Retry target is unavailable")
        payload["retry_command"] = {
            "kind": source.kind,
            "payload": source.request_payload,
            "request_hash": source.request_hash,
        }
        payload["retry_root_operation_id"] = str(source.operation_id)
        children = list(
            session.execute(
                select(
                    WorkerCacheManagementChild.guard_operation_id,
                    WorkerCacheManagementChild.generation,
                ).where(
                    WorkerCacheManagementChild.management_operation_id
                    == operation.target_operation_id
                )
            )
        )
        payload["retry_children"] = [
            {"guard_operation_id": str(child.guard_operation_id), "generation": child.generation}
            for child in children
        ]
    session.commit()
    actor_kind = str(operation.actor.get("kind", "administrator"))
    actor_id = operation.actor.get("user_id")
    actor = actor_kind if actor_id is None else f"{actor_kind}:{actor_id}"
    return CacheCommandClaim(
        operation_id=operation.operation_id,
        claim_epoch=operation.claim_epoch,
        kind=cast(Literal["preview", "clean", "protect", "retry"], operation.kind),
        request_hash=operation.request_hash,
        payload=payload,
        actor=actor,
    )


def _safe_result(value: object) -> None:
    encoded = _canonical(value)
    if len(encoded) > 65_536:
        raise domain_error(422, "cache_result_too_large", "Cache result is too large")
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                lowered = str(key).lower()
                if any(word in lowered for word in _SENSITIVE_KEYS):
                    raise domain_error(
                        422, "cache_result_sensitive", "Cache result contains unsupported fields"
                    )
                stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
        elif (
            not isinstance(item, (str, int, float, bool, type(None)))
            or (isinstance(item, str) and len(item) > 256)
            or isinstance(item, float)
            and not math.isfinite(item)
        ):
            raise domain_error(422, "cache_result_invalid", "Cache result is invalid")


def apply_command_result(
    session: Session,
    worker_id: int,
    operation_id: uuid.UUID,
    report: CacheCommandResult,
) -> WorkerCacheManagementOperation:
    _worker_locked(session, worker_id)
    operation = session.scalar(
        select(WorkerCacheManagementOperation)
        .where(WorkerCacheManagementOperation.operation_id == operation_id)
        .with_for_update()
    )
    if operation is None:
        raise domain_error(404, "cache_operation_not_found", "Cache operation not found")
    if operation.worker_id != worker_id:
        raise domain_error(
            409, "cache_operation_not_owned", "Cache operation belongs to another Worker"
        )
    if operation.request_hash != report.request_hash or operation.claim_epoch != report.claim_epoch:
        raise domain_error(409, "cache_operation_stale_claim", "Cache operation claim is stale")
    _safe_result(report.result)
    child_map: dict[str, object] = {}
    raw_children = report.result.get("child_operations", [])
    if operation.kind == "clean" or raw_children:
        if not isinstance(raw_children, list) or len(raw_children) > 200:
            raise domain_error(422, "cache_child_operation_invalid", "Child operations are invalid")
        for raw in raw_children:
            if not isinstance(raw, dict) or set(raw) != {
                "guard_operation_id",
                "generation",
                "adapter_id",
                "version_id",
                "phase",
            }:
                raise domain_error(
                    422, "cache_child_operation_invalid", "Child operation is invalid"
                )
            try:
                guard_id = uuid.UUID(str(raw["guard_operation_id"]))
            except (TypeError, ValueError) as error:
                raise domain_error(
                    422, "cache_child_operation_invalid", "Child operation is invalid"
                ) from error
            guard = session.get(WorkerCacheOperation, guard_id)
            if (
                guard is None
                or guard.worker_id != worker_id
                or guard.generation != raw["generation"]
                or guard.adapter_id != raw["adapter_id"]
                or guard.version_id != raw["version_id"]
            ):
                raise domain_error(
                    409, "cache_child_operation_conflict", "Child operation conflicts"
                )
            child_map[str(guard_id)] = {"generation": guard.generation}
            if session.get(WorkerCacheManagementChild, (operation.operation_id, guard_id)) is None:
                session.add(
                    WorkerCacheManagementChild(
                        management_operation_id=operation.operation_id,
                        guard_operation_id=guard_id,
                        generation=guard.generation,
                    )
                )
    if operation.status in {"completed", "failed"}:
        if (
            operation.status != report.status
            or operation.result != report.result
            or operation.error_code != report.error_code
        ):
            raise domain_error(
                409, "cache_operation_result_conflict", "Cache operation result conflicts"
            )
        session.rollback()
        return operation
    if operation.status != "running":
        raise domain_error(409, "cache_operation_not_running", "Cache operation is not running")
    operation.status = report.status
    operation.result = report.result
    operation.child_operations = child_map
    operation.error_code = report.error_code
    operation.finished_at = func.now() if report.status in {"completed", "failed"} else None
    session.commit()
    session.refresh(operation)
    return operation


def upload_snapshot(session: Session, worker_id: int, upload: CacheSnapshotUpload) -> None:
    worker = _worker_locked(session, worker_id)
    if not _governance_ready(worker):
        raise domain_error(
            409, "cache_worker_unsupported", "Worker does not support cache governance"
        )
    incoming_summary = upload.summary.model_dump(mode="json")
    incoming_summary["failed_guard_items"] = [
        item.model_dump(mode="json") for item in upload.failed_guard_items
    ]
    incoming_summary["failed_guard_cursor"] = upload.failed_guard_cursor
    incoming_summary["failed_guard_complete"] = upload.failed_guard_complete
    current = session.get(WorkerCacheSnapshot, worker_id)
    now = datetime.now(UTC)
    if upload.sampled_at < now - timedelta(seconds=SNAPSHOT_STALE_SECONDS):
        raise domain_error(409, "cache_snapshot_stale", "Snapshot observation is stale")
    if current is not None:
        if current.sample_id == upload.sample_id:
            current_items = list(
                session.scalars(
                    select(WorkerCacheSnapshotItem)
                    .where(WorkerCacheSnapshotItem.worker_id == worker_id)
                    .order_by(WorkerCacheSnapshotItem.cache_key)
                )
            )
            incoming_items = sorted(
                [item.model_dump(mode="json") for item in upload.items],
                key=lambda item: str(item["cache_key"]),
            )
            stored_items = [
                {
                    "cache_key": item.cache_key,
                    "kind": item.kind,
                    "adapter_id": item.adapter_id,
                    "version_id": item.version_id,
                    "identity": item.identity,
                    "digest": item.digest,
                    "bytes": item.bytes,
                    "pinned": item.pinned,
                    "rebuildability": item.rebuildability,
                    "reasons": item.reasons,
                }
                for item in current_items
            ]
            same = (
                current.sequence == upload.sequence
                and current.summary == incoming_summary
                and current.sampled_at == upload.sampled_at
                and current.state == upload.state
                and current.complete == upload.complete
                and current.cursor == upload.cursor
                and stored_items == incoming_items
            )
            if not same:
                raise domain_error(409, "cache_snapshot_conflict", "Snapshot ID was reused")
            session.rollback()
            return
        if upload.sequence <= current.sequence:
            raise domain_error(409, "cache_snapshot_stale", "Snapshot sequence is stale")
        if upload.sampled_at < current.sampled_at:
            raise domain_error(409, "cache_snapshot_stale", "Snapshot observation moved backwards")
    for failed in upload.failed_guard_items:
        operation = session.get(WorkerCacheOperation, failed.guard_operation_id)
        if (
            operation is None
            or operation.worker_id != worker_id
            or operation.adapter_id != failed.adapter_id
            or operation.version_id != failed.version_id
            or operation.generation != failed.generation
            or operation.operation_kind != failed.operation_kind
            or operation.phase != "acquired"
        ):
            raise domain_error(
                409,
                "cache_failed_guard_invalid",
                "Failed guard observation does not match Control authority",
            )
    values = upload.model_dump(
        mode="json",
        exclude={
            "items",
            "failed_guard_items",
            "failed_guard_cursor",
            "failed_guard_complete",
        },
    )
    values["sample_id"] = upload.sample_id
    values["sampled_at"] = upload.sampled_at
    values["summary"] = incoming_summary
    if current is None:
        current = WorkerCacheSnapshot(worker_id=worker_id, **values)
        session.add(current)
    else:
        for key, value in values.items():
            setattr(current, key, value)
        current.received_at = func.now()
    session.execute(
        delete(WorkerCacheSnapshotItem).where(WorkerCacheSnapshotItem.worker_id == worker_id)
    )
    for item in upload.items:
        session.add(
            WorkerCacheSnapshotItem(
                worker_id=worker_id,
                sample_id=upload.sample_id,
                sampled_at=upload.sampled_at,
                **item.model_dump(mode="json"),
            )
        )
    session.commit()


def cache_view(
    session: Session, worker_id: int, *, failed_cursor: int | None = None, failed_limit: int = 50
) -> CacheAdminView:
    worker = session.get(Worker, worker_id)
    if worker is None:
        raise domain_error(404, "worker_not_found", "Worker not found")
    if not _governance_ready(worker):
        return CacheAdminView(worker_id=worker_id, status="unsupported")
    snapshot = session.scalar(
        select(WorkerCacheSnapshot)
        .where(WorkerCacheSnapshot.worker_id == worker_id)
        .execution_options(populate_existing=True)
    )
    if snapshot is None:
        return CacheAdminView(
            worker_id=worker_id, status="offline" if worker.status == "offline" else "missing"
        )
    items: list[WorkerCacheSnapshotItem] = []
    for attempt in range(2):
        sample_id = snapshot.sample_id
        items = list(
            session.scalars(
                select(WorkerCacheSnapshotItem)
                .where(
                    WorkerCacheSnapshotItem.worker_id == worker_id,
                    WorkerCacheSnapshotItem.sample_id == sample_id,
                )
                .order_by(WorkerCacheSnapshotItem.adapter_id, WorkerCacheSnapshotItem.version_id)
                .limit(200)
            )
        )
        latest = session.scalar(
            select(WorkerCacheSnapshot)
            .where(WorkerCacheSnapshot.worker_id == worker_id)
            .execution_options(populate_existing=True)
        )
        if latest is not None and latest.sample_id == sample_id:
            snapshot = latest
            break
        if attempt:
            raise domain_error(409, "cache_snapshot_changed", "Cache snapshot changed during read")
        session.expire_all()
        if latest is None:
            raise domain_error(409, "cache_snapshot_changed", "Cache snapshot changed during read")
        snapshot = latest
    if worker.status == "offline":
        status = "offline"
    elif snapshot.received_at < datetime.now(UTC) - timedelta(
        seconds=SNAPSHOT_STALE_SECONDS
    ) or snapshot.sampled_at < datetime.now(UTC) - timedelta(seconds=SNAPSHOT_STALE_SECONDS):
        status = "stale"
    else:
        status = snapshot.state
    cleanup_query = select(WorkerCleanupRequest).where(
        WorkerCleanupRequest.worker_id == worker_id,
        WorkerCleanupRequest.status == "failed",
    )
    if failed_cursor is not None:
        cleanup_query = cleanup_query.where(WorkerCleanupRequest.id < failed_cursor)
    cleanups = list(
        session.scalars(
            cleanup_query.order_by(WorkerCleanupRequest.id.desc()).limit(failed_limit + 1)
        )
    )
    raw_failed_guards = snapshot.summary.get("failed_guard_items", [])
    failed_guards = (
        [FailedGuardItem.model_validate(item) for item in raw_failed_guards]
        if isinstance(raw_failed_guards, list)
        else []
    )
    public_summary = dict(snapshot.summary)
    public_summary.pop("failed_guard_items", None)
    failed_guard_cursor = public_summary.pop("failed_guard_cursor", None)
    failed_guard_complete = public_summary.pop("failed_guard_complete", True)
    return CacheAdminView.model_validate(
        {
            "worker_id": worker_id,
            "status": status,
            "sampled_at": snapshot.sampled_at,
            "received_at": snapshot.received_at,
            "sample_id": snapshot.sample_id,
            "sequence": snapshot.sequence,
            "complete": snapshot.complete,
            "cursor": snapshot.cursor,
            "summary": public_summary,
            "items": [
                CacheSnapshotItem.model_validate(item, from_attributes=True) for item in items
            ],
            "failed_guard_items": failed_guards,
            "failed_guard_cursor": failed_guard_cursor,
            "failed_guard_complete": failed_guard_complete,
            "failed_cleanup_items": [
                FailedCleanupItem(
                    cleanup_id=item.id,
                    adapter_id=item.adapter_id,
                    attempts=item.attempts,
                    error_code=item.error_code,
                )
                for item in cleanups[:failed_limit]
            ],
            "failed_cleanup_next_cursor": (
                cleanups[failed_limit - 1].id if len(cleanups) > failed_limit else None
            ),
        }
    )


def get_operation(
    session: Session, worker_id: int, operation_id: uuid.UUID
) -> WorkerCacheManagementOperation:
    operation = session.get(WorkerCacheManagementOperation, operation_id)
    if operation is None or operation.worker_id != worker_id:
        raise domain_error(404, "cache_operation_not_found", "Cache operation not found")
    return operation


def list_operations(
    session: Session, worker_id: int, *, cursor: uuid.UUID | None, limit: int
) -> CacheOperationPage:
    prune_terminal(session, worker_id)
    query = select(WorkerCacheManagementOperation).where(
        WorkerCacheManagementOperation.worker_id == worker_id
    )
    if cursor is not None:
        anchor = get_operation(session, worker_id, cursor)
        query = query.where(
            (WorkerCacheManagementOperation.created_at < anchor.created_at)
            | (
                (WorkerCacheManagementOperation.created_at == anchor.created_at)
                & (WorkerCacheManagementOperation.operation_id < anchor.operation_id)
            )
        )
    rows = list(
        session.scalars(
            query.order_by(
                WorkerCacheManagementOperation.created_at.desc(),
                WorkerCacheManagementOperation.operation_id.desc(),
            ).limit(limit + 1)
        )
    )
    next_cursor = str(rows[limit - 1].operation_id) if len(rows) > limit else None
    return CacheOperationPage(
        items=[CacheOperationResponse.model_validate(row) for row in rows[:limit]],
        next_cursor=next_cursor,
    )


def recover_running_operations(session: Session, worker_id: int) -> None:
    session.query(WorkerCacheManagementOperation).filter(
        WorkerCacheManagementOperation.worker_id == worker_id,
        WorkerCacheManagementOperation.status == "running",
        WorkerCacheManagementOperation.target_kind != "cleanup",
    ).update({"status": "pending"}, synchronize_session=False)


def finish_cleanup_retry(
    session: Session, cleanup: WorkerCleanupRequest, *, success: bool, error_code: str | None
) -> None:
    if cleanup.retry_operation_id is None:
        return
    operation = session.get(WorkerCacheManagementOperation, cleanup.retry_operation_id)
    if operation is None:
        raise domain_error(
            409, "cleanup_retry_operation_missing", "Cleanup retry operation is missing"
        )
    operation.status = "completed" if success else "failed"
    operation.result = {"cleanup_id": cleanup.id, "attempts": cleanup.attempts, "success": success}
    operation.error_code = error_code
    operation.finished_at = func.now()


def prune_terminal(session: Session, worker_id: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=TERMINAL_RETENTION_DAYS)
    protected = select(WorkerCleanupRequest.retry_operation_id).where(
        WorkerCleanupRequest.retry_operation_id.is_not(None),
        WorkerCleanupRequest.status != "completed",
    )
    retry_targets = select(WorkerCacheManagementOperation.target_operation_id).where(
        WorkerCacheManagementOperation.worker_id == worker_id,
        WorkerCacheManagementOperation.status.in_(("pending", "running")),
        WorkerCacheManagementOperation.target_operation_id.is_not(None),
    )
    guarded = (
        select(WorkerCacheManagementChild.management_operation_id)
        .join(
            WorkerCacheOperation,
            WorkerCacheOperation.operation_id == WorkerCacheManagementChild.guard_operation_id,
        )
        .where(WorkerCacheOperation.phase == "acquired")
    )
    overflow = list(
        session.scalars(
            select(WorkerCacheManagementOperation.operation_id)
            .where(
                WorkerCacheManagementOperation.worker_id == worker_id,
                WorkerCacheManagementOperation.status.in_(("completed", "failed")),
                WorkerCacheManagementOperation.operation_id.not_in(protected),
                WorkerCacheManagementOperation.operation_id.not_in(retry_targets),
                WorkerCacheManagementOperation.operation_id.not_in(guarded),
            )
            .order_by(WorkerCacheManagementOperation.finished_at.desc().nullslast())
            .offset(MAX_TERMINAL_PER_WORKER)
            .limit(100)
        )
    )
    expired = list(
        session.scalars(
            select(WorkerCacheManagementOperation.operation_id)
            .where(
                WorkerCacheManagementOperation.worker_id == worker_id,
                WorkerCacheManagementOperation.status.in_(("completed", "failed")),
                WorkerCacheManagementOperation.finished_at < cutoff,
                WorkerCacheManagementOperation.operation_id.not_in(protected),
                WorkerCacheManagementOperation.operation_id.not_in(retry_targets),
                WorkerCacheManagementOperation.operation_id.not_in(guarded),
            )
            .limit(100)
        )
    )
    ids = set(overflow) | set(expired)
    if ids:
        session.query(WorkerCleanupRequest).filter(
            WorkerCleanupRequest.worker_id == worker_id,
            WorkerCleanupRequest.status == "completed",
            WorkerCleanupRequest.retry_operation_id.in_(ids),
        ).update({"retry_operation_id": None}, synchronize_session=False)
        session.execute(
            delete(WorkerCacheManagementOperation).where(
                WorkerCacheManagementOperation.operation_id.in_(ids)
            )
        )
        session.commit()
    return len(ids)
