"""Settle a deleted Adapter's dedicated, ephemeral v4 Worker scope."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.api.worker_cleanup_v4 import _read_json
from dlr.control.api.worker_instance_v4 import _HEADERS, _process, _proved_identity, _rollback
from dlr.control.models import (
    Adapter,
    Worker,
    WorkerCacheOperation,
    WorkerCleanupRequest,
    WorkerIdentityReservation,
)
from dlr.control.services import worker_availability
from dlr.control.services.worker_lease_candidates_v4 import validate_current_proof

router = APIRouter()
DbSession = Annotated[Session, Depends(db.get_session)]


class _Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cleanup_id: StrictInt = Field(gt=0, lt=2**63)
    adapter_id: StrictInt = Field(gt=0, lt=2**63)
    claim_attempt: StrictInt = Field(gt=0, lt=2**31)


@router.post("/api/workers/{worker_id}/v4/adapter-cleanup/claim")
async def claim(request: Request, worker_id: int, session: DbSession) -> dict[str, object]:
    return await _exchange(request, worker_id, session, complete=False)


@router.post("/api/workers/{worker_id}/v4/adapter-cleanup/complete")
async def complete(request: Request, worker_id: int, session: DbSession) -> dict[str, object]:
    return await _exchange(request, worker_id, session, complete=True)


def _error(status: int) -> HTTPException:
    return HTTPException(
        status_code=status, detail={"code": "worker_adapter_cleanup_unavailable"}, headers=_HEADERS
    )


async def _exchange(
    request: Request, worker_id: int, session: Session, *, complete: bool
) -> dict[str, object]:
    if request.url.scheme != "https":
        raise _error(401)
    credential, instance, generation, proof = _proved_identity(request, worker_id)
    try:
        body = await _read_json(request)
        receipt = _Receipt.model_validate(body) if complete else None
        if not complete and body:
            raise ValueError
    except ValueError:
        raise _error(422) from None

    def transact() -> dict[str, object]:
        process = _process(request)

        def verify() -> Worker:
            candidate = validate_current_proof(
                session,
                worker_id=worker_id,
                credential=credential,
                instance_id=instance,
                generation=generation,
                token=proof,
                authority=process.worker_proof_authority(session),
            )
            worker = session.get(Worker, worker_id)
            reservation = session.scalar(
                select(WorkerIdentityReservation).where(
                    WorkerIdentityReservation.worker_id == worker_id
                )
            )
            if (
                worker is None
                or worker.identity_id != candidate.worker_identity_id
                or reservation is None
                or reservation.legacy_stop_evidence_sha256 is not None
                or not worker_availability.v4_adapter_cleanup_ready(worker)
            ):
                raise ValueError
            return worker

        fence = process.worker_fence(session)
        worker = verify()
        query = select(WorkerCleanupRequest).where(
            WorkerCleanupRequest.worker_id == worker_id,
            WorkerCleanupRequest.v4_identity_id == worker.identity_id,
            WorkerCleanupRequest.retry_operation_id.is_(None),
        )
        if receipt is None:
            query = (
                query.where(WorkerCleanupRequest.status.in_(("pending", "running")))
                .order_by(
                    case((WorkerCleanupRequest.status == "running", 0), else_=1),
                    WorkerCleanupRequest.id,
                )
                .limit(1)
            )
        else:
            query = query.where(
                WorkerCleanupRequest.id == receipt.cleanup_id,
                WorkerCleanupRequest.adapter_id == receipt.adapter_id,
                WorkerCleanupRequest.attempts == receipt.claim_attempt,
            )
        row = session.scalar(query.with_for_update().execution_options(populate_existing=True))
        if row is None and receipt is None:
            verify()
            fence.check()
            session.rollback()
            process.check_authority()
            return {"claim": None}
        if row is None:
            raise ValueError
        adapter = session.get(Adapter, row.adapter_id)
        if adapter is not None and adapter.archived_at is None:
            raise ValueError
        changed = False
        if row.status == "pending" and receipt is None:
            if row.attempts != 0 or row.v4_instance_id is not None or row.v4_generation is not None:
                raise ValueError
            row.v4_instance_id, row.v4_generation = instance, generation
            row.attempts = 1
            row.status = "running"
            changed = True
        if (row.v4_instance_id, row.v4_generation) != (instance, generation):
            raise ValueError
        if receipt is not None and row.status == "running":
            if (
                session.scalar(
                    select(WorkerCacheOperation.operation_id)
                    .where(
                        WorkerCacheOperation.worker_id == worker_id,
                        WorkerCacheOperation.cleanup_id == row.id,
                        WorkerCacheOperation.phase == "acquired",
                    )
                    .limit(1)
                )
                is not None
            ):
                raise ValueError
            row.status = "completed"
            row.completed_at = worker_availability.current_time(session)
            changed = True
        expected_status = "running" if receipt is None else "completed"
        if row.status != expected_status:
            raise ValueError
        exact = _Receipt(cleanup_id=row.id, adapter_id=row.adapter_id, claim_attempt=row.attempts)
        if changed:
            session.flush()
            verify()
            fence.check()
            process.check_authority()
            session.commit()
            process.check_authority()
            fence = process.worker_fence(session)
            worker = verify()
            row = session.get(WorkerCleanupRequest, exact.cleanup_id, populate_existing=True)
            if row is None or (
                row.worker_id,
                row.v4_identity_id,
                row.v4_instance_id,
                row.v4_generation,
                row.adapter_id,
                row.attempts,
                row.status,
            ) != (
                worker_id,
                worker.identity_id,
                instance,
                generation,
                exact.adapter_id,
                exact.claim_attempt,
                expected_status,
            ):
                raise ValueError
        fence.check()
        session.rollback()
        process.check_authority()
        return {"claim" if receipt is None else "completed": exact.model_dump()}

    try:
        return await run_in_threadpool(transact)
    except Exception:
        _rollback(session)
        raise _error(503) from None
