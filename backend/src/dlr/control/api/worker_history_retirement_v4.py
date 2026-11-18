"""Bounded, reentrant confirmation that a current Worker retired local journals."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.api.worker_cleanup_v4 import _read_json
from dlr.control.api.worker_instance_v4 import _HEADERS, _process, _proved_identity, _rollback
from dlr.control.models import ExecutionAttempt, WorkerCleanupIntentV4
from dlr.control.services import worker_availability
from dlr.control.services.attempt import TERMINAL_ATTEMPT_STATUSES
from dlr.control.services.worker_lease_candidates_v4 import validate_current_proof

router = APIRouter()
DbSession = Annotated[Session, Depends(db.get_session)]
_MAX_RETIREMENTS = 8


class _Retirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: StrictInt = Field(gt=0, lt=2**63)
    execution_id: StrictInt = Field(gt=0, lt=2**63)
    fencing_token: StrictInt = Field(gt=0, lt=2**63)
    completion_id: UUID


class _RetirementBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[_Retirement] = Field(min_length=1, max_length=_MAX_RETIREMENTS)


def _error(status: int) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": "worker_history_retirement_unavailable"},
        headers=_HEADERS,
    )


@router.post("/api/workers/{worker_id}/v4/history-retirement/{action}")
async def retire_history(
    request: Request, worker_id: int, action: Literal["observe", "confirm"], session: DbSession
) -> JSONResponse:
    credential, instance, generation, proof = _proved_identity(request, worker_id)
    try:
        body = await _read_json(request)
        wanted = (
            sorted(_RetirementBatch.model_validate(body).items, key=lambda row: row.attempt_id)
            if action == "confirm"
            else []
        )
        ids = [identity.attempt_id for identity in wanted]
        if (
            (action == "observe" and body)
            or len(set(ids)) != len(ids)
            or any(not identity.completion_id.int for identity in wanted)
        ):
            raise ValueError
    except ValueError:
        raise _error(422) from None

    def transact() -> list[_Retirement]:
        process = _process(request)

        def verify() -> UUID:
            lease = validate_current_proof(
                session,
                worker_id=worker_id,
                credential=credential,
                instance_id=instance,
                generation=generation,
                token=proof,
                authority=process.worker_proof_authority(session),
            )
            return lease.worker_identity_id

        fence = process.worker_fence(session)
        identity = verify()
        attempt = ExecutionAttempt
        cleanup = WorkerCleanupIntentV4
        query = (
            select(attempt, cleanup.completion_id)
            .join(cleanup, cleanup.attempt_id == attempt.id)
            .where(
                attempt.worker_id == worker_id,
                attempt.worker_identity_id == identity,
                attempt.status.in_(TERMINAL_ATTEMPT_STATUSES),
                attempt.cleanup_settled_at.is_not(None),
                cleanup.instance_id == instance,
                cleanup.instance_generation == generation,
                cleanup.completion_id.is_not(None),
                cleanup.completion_xid == cleanup.writer_xid,
            )
            .order_by(attempt.id)
        )

        def item(row: ExecutionAttempt, completion_id: UUID) -> _Retirement:
            return _Retirement(
                attempt_id=row.id,
                execution_id=row.execution_id,
                fencing_token=row.fencing_token,
                completion_id=completion_id,
            )

        if action == "observe":
            pending = query.where(attempt.journal_retired_at.is_(None)).limit(_MAX_RETIREMENTS)
            result = [item(row, completion) for row, completion in session.execute(pending)]
            fence.check()
            session.rollback()
            process.check_authority()
            return result

        selected = query.where(attempt.id.in_(ids))
        rows = list(
            session.execute(
                selected.with_for_update(of=attempt).execution_options(populate_existing=True)
            )
        )
        if [item(row, completion) for row, completion in rows] != wanted:
            raise ValueError
        now = worker_availability.current_time(session)
        for row, _ in rows:
            if row.journal_retired_at is None:
                row.journal_retired_at = now
        session.flush()
        verify()
        fence.check()
        process.check_authority()
        session.commit()
        process.check_authority()
        fence = process.worker_fence(session)
        if verify() != identity:
            raise ValueError
        committed = selected.where(attempt.journal_retired_at.is_not(None))
        result = [item(row, completion) for row, completion in session.execute(committed)]
        if result != wanted:
            raise ValueError
        fence.check()
        session.rollback()
        process.check_authority()
        return result

    try:
        result = await run_in_threadpool(transact)
    except Exception:
        _rollback(session)
        raise _error(503) from None
    return JSONResponse(
        {"items": [row.model_dump(mode="json") for row in result]}, headers=_HEADERS
    )
