"""Scoped cache management for a current, signed Worker instance."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.api.worker_cleanup_v4 import _read_json
from dlr.control.api.worker_instance_v4 import _HEADERS, _process, _proved_identity, _rollback
from dlr.control.models import (
    Worker,
    WorkerCacheManagementChild,
    WorkerCacheManagementOperation,
    WorkerCacheOperation,
    WorkerCacheSnapshot,
    WorkerCacheSnapshotItem,
    WorkerCleanupRequest,
)
from dlr.control.models.cache_governance import WorkerCacheOwner
from dlr.control.schemas.cache_admin import CacheCommandResult, CacheSnapshotUpload
from dlr.control.schemas.worker import (
    CacheGuardAcquire,
    CacheGuardOperationResponse,
    CacheGuardPage,
    CacheGuardResponse,
    CacheGuardResult,
    CacheKeyReferenceBatch,
    CacheKeyReferenceBatchResponse,
    CacheKeyReferenceResult,
)
from dlr.control.services import cache_admin, cache_governance, worker_credentials
from dlr.control.services.worker_lease_candidates_v4 import validate_current_proof

router = APIRouter()
DbSession = Annotated[Session, Depends(db.get_session)]
Action = Literal[
    "snapshot", "references", "guards", "acquire", "check", "release", "claim", "result"
]


class _Page(BaseModel):
    model_config = ConfigDict(extra="forbid")
    after_version_id: StrictInt | None = Field(default=None, gt=0, lt=2**63)
    limit: StrictInt = Field(default=8, gt=0, le=8)


class _CommandGuard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: UUID | None = None
    request: CacheGuardAcquire


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError
    key = UUID(value)
    if not key.int or str(key) != value:
        raise ValueError
    return key


@router.post("/api/workers/{worker_id}/v4/cache/{action}")
async def exchange(
    request: Request, worker_id: int, action: Action, session: DbSession
) -> dict[str, object]:
    credential, instance, generation, proof = _proved_identity(request, worker_id)
    body = await _read_json(request)

    def transact() -> dict[str, object]:
        process = _process(request)
        fence = process.worker_fence(session)
        # Guard acquisition takes Adapter and guard locks before Worker,
        # matching Claim's order. This prescreen grants no data authority.
        worker_credentials.prescreen_runtime_credential(session, worker_id, credential)
        preliminary = session.get(Worker, worker_id)
        if preliminary is None:
            raise ValueError
        scope = (preliminary.identity_id, instance, generation)

        def verify() -> None:
            candidate = validate_current_proof(
                session,
                worker_id=worker_id,
                credential=credential,
                instance_id=instance,
                generation=generation,
                token=proof,
                authority=process.worker_proof_authority(session),
            )
            worker = session.get(Worker, worker_id, populate_existing=True)
            if (
                worker is None
                or candidate.worker_identity_id != scope[0]
                or not cache_admin._governance_ready(worker)
            ):
                raise ValueError

        def owned(row: WorkerCacheOwner) -> bool:
            return (row.v4_identity_id, row.v4_instance_id, row.v4_generation) == scope

        def bind(row: WorkerCacheOwner, *, new: bool = False) -> None:
            if new:
                row.v4_identity_id, row.v4_instance_id, row.v4_generation = scope
            if not owned(row):
                raise ValueError
            session.flush()
            verify()
            fence.check()
            process.check_authority()

        def readback() -> None:
            verify()
            process.worker_fence(session).check()
            process.check_authority()

        result: object
        if action == "snapshot":
            verify()
            upload = CacheSnapshotUpload.model_validate(body)
            if len(upload.items) > 8 or len(upload.failed_guard_items) > 8:
                raise ValueError
            previous = session.get(WorkerCacheSnapshot, worker_id)
            fresh = previous is None or previous.sample_id != upload.sample_id
            cache_admin.upload_snapshot(
                session, worker_id, upload, _before_commit=lambda row: bind(row, new=fresh)
            )
            readback()
            snapshot = session.get(WorkerCacheSnapshot, worker_id, populate_existing=True)
            if snapshot is None or not owned(snapshot) or snapshot.sample_id != upload.sample_id:
                raise ValueError
            result = {"sample_id": str(snapshot.sample_id)}
        elif action == "claim":
            if body:
                raise ValueError
            verify()
            command = cache_admin.claim_command(
                session,
                worker_id,
                _before_commit=lambda row: bind(
                    row, new=row.v4_identity_id is None and row.claim_epoch == 1
                ),
            )
            readback()
            if command is not None:
                saved = session.get(WorkerCacheManagementOperation, command.operation_id)
                if (
                    saved is None
                    or not owned(saved)
                    or (saved.status, saved.claim_epoch, saved.request_hash)
                    != ("running", command.claim_epoch, command.request_hash)
                ):
                    raise ValueError
            result = command.model_dump(mode="json") if command is not None else None
        elif action == "result":
            verify()
            if set(body) != {"operation_id", "result"}:
                raise ValueError
            operation_id = _uuid(body["operation_id"])
            report = CacheCommandResult.model_validate(body["result"])
            current = session.get(WorkerCacheManagementOperation, operation_id)
            if current is None or current.worker_id != worker_id or not owned(current):
                raise ValueError
            children = {
                key: guard
                for key, guard in session.execute(
                    select(WorkerCacheManagementChild.guard_operation_id, WorkerCacheOperation)
                    .join(
                        WorkerCacheOperation,
                        WorkerCacheOperation.operation_id
                        == WorkerCacheManagementChild.guard_operation_id,
                    )
                    .where(WorkerCacheManagementChild.management_operation_id == operation_id)
                    .limit(9)
                )
            }
            announced = report.result.get("child_operations", [])
            if (
                not isinstance(announced, list)
                or len(children) > 8
                or any(
                    not isinstance(child, dict)
                    or _uuid(child.get("guard_operation_id")) not in children
                    for child in announced
                )
                or (
                    report.status == "completed"
                    and any(guard.phase == "acquired" for guard in children.values())
                )
            ):
                raise ValueError
            cache_admin.apply_command_result(
                session, worker_id, operation_id, report, _before_commit=bind
            )
            readback()
            saved = session.get(
                WorkerCacheManagementOperation, operation_id, populate_existing=True
            )
            if (
                saved is None
                or not owned(saved)
                or (saved.status, saved.result, saved.error_code)
                != (report.status, report.result, report.error_code)
            ):
                raise ValueError
            result = {"operation_id": str(operation_id), "status": report.status}
        elif action == "acquire":
            command_guard = _CommandGuard.model_validate(body)
            payload, command_id = command_guard.request, command_guard.command_id
            observed = payload.observed_identity
            cleanup_context = payload.cleanup_context
            if (
                observed is None
                or payload.replacement_context is not None
                or (cleanup_context is None) != (command_id is not None)
                or not payload.operation_id.int
                or not 0 < payload.adapter_id < 2**63
                or not 0 < payload.version_id < 2**63
            ):
                raise ValueError

            def bind_guard(operation: WorkerCacheOperation) -> None:
                fresh = operation in session.new
                verify()
                selection = {"adapter_id": operation.adapter_id, "version_id": operation.version_id}
                if cleanup_context is not None:
                    ticket = session.get(WorkerCleanupRequest, cleanup_context.cleanup_id)
                    if (
                        ticket is None
                        or ticket.worker_id != worker_id
                        or (ticket.v4_identity_id, ticket.v4_instance_id, ticket.v4_generation)
                        != scope
                        or ticket.status != "running"
                        or ticket.adapter_id != operation.adapter_id
                        or ticket.attempts != cleanup_context.claim_attempt
                    ):
                        raise ValueError
                else:
                    command_row = session.get(WorkerCacheManagementOperation, command_id)
                    selected_keys = command_row.request_payload.get("keys") if command_row else None
                    if (
                        command_row is None
                        or not owned(command_row)
                        or command_row.worker_id != worker_id
                        or command_row.kind != "clean"
                        or command_row.status != "running"
                        or not isinstance(selected_keys, list)
                        or selection not in selected_keys
                    ):
                        raise ValueError
                sample = session.get(WorkerCacheSnapshot, worker_id)
                item = session.get(
                    WorkerCacheSnapshotItem,
                    (worker_id, f"{payload.adapter_id}-{payload.version_id}"),
                )
                if (
                    sample is None
                    or not owned(sample)
                    or item is None
                    or item.sample_id != sample.sample_id
                    or item.digest != observed.digest
                    or item.pinned
                    or item.identity is None
                    or item.identity
                    != {
                        **selection,
                        "language": observed.language,
                        "source_sha256": observed.source_sha256,
                    }
                ):
                    raise ValueError
                if command_id is not None:
                    child = session.get(
                        WorkerCacheManagementChild, (command_id, operation.operation_id)
                    )
                    if fresh:
                        if child is not None:
                            raise ValueError
                        session.add(
                            WorkerCacheManagementChild(
                                management_operation_id=command_id,
                                guard_operation_id=operation.operation_id,
                                generation=operation.generation,
                            )
                        )
                    elif child is None or child.generation != operation.generation:
                        raise ValueError
                bind(operation, new=fresh)

            operation = cache_governance.acquire_guard(
                session,
                worker_id=worker_id,
                adapter_id=payload.adapter_id,
                version_id=payload.version_id,
                operation_id=payload.operation_id,
                observed_identity=observed.model_dump(),
                cleanup_context=cleanup_context.model_dump() if cleanup_context else None,
                _before_commit=bind_guard,
            )
            readback()
            observed_operation = session.get(
                WorkerCacheOperation, operation.operation_id, populate_existing=True
            )
            if (
                observed_operation is None
                or not owned(observed_operation)
                or observed_operation.phase != "acquired"
            ):
                raise ValueError
            result = CacheGuardOperationResponse.model_validate(observed_operation).model_dump(
                mode="json"
            )
        elif action in {"check", "release"}:
            if set(body) != ({"operation_id"} if action == "check" else {"operation_id", "result"}):
                raise ValueError
            operation_id = _uuid(body["operation_id"])
            operation = cache_governance.check_guard(
                session, worker_id=worker_id, operation_id=operation_id
            )
            if not owned(operation):
                raise ValueError
            if action == "release":
                report_guard = CacheGuardResult.model_validate(body["result"])
                operation = cache_governance.finish_guard(
                    session,
                    worker_id=worker_id,
                    operation_id=operation_id,
                    generation=report_guard.generation,
                    outcome=report_guard.outcome,
                    _before_commit=bind,
                )
            readback()
            observed_operation = session.get(
                WorkerCacheOperation, operation_id, populate_existing=True
            )
            if observed_operation is None or not owned(observed_operation):
                raise ValueError
            result = CacheGuardOperationResponse.model_validate(observed_operation).model_dump(
                mode="json"
            )
        elif action == "guards":
            verify()
            page = _Page.model_validate(body)
            rows, after = cache_governance.list_active_guards(
                session,
                worker_id=worker_id,
                after_version_id=page.after_version_id,
                limit=page.limit,
            )
            result = CacheGuardPage(
                items=[CacheGuardResponse.model_validate(row) for row in rows],
                next_after_version_id=after,
            ).model_dump(mode="json")
            readback()
        else:
            verify()
            keys = CacheKeyReferenceBatch.model_validate(body)
            if len(keys.items) > 8:
                raise ValueError
            at, complete, items = cache_governance.cache_key_reference_facts(
                session,
                worker_id=worker_id,
                items=[(item.adapter_id, item.version_id) for item in keys.items],
            )
            result = CacheKeyReferenceBatchResponse(
                worker_id=worker_id,
                sampled_at=at,
                complete=complete,
                items=[CacheKeyReferenceResult.model_validate(item) for item in items],
            ).model_dump(mode="json")
            readback()
        session.rollback()
        process.check_authority()
        return {"result": result}

    try:
        return await run_in_threadpool(transact)
    except Exception:
        _rollback(session)
        raise HTTPException(
            status_code=503, detail={"code": "worker_cache_unavailable"}, headers=_HEADERS
        ) from None
