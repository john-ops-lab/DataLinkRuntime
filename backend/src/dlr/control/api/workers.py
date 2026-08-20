"""Worker-internal endpoints of the Control Node (Worker Token protected)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.execution import (
    ExecutionResponse,
    ExecutionResultReport,
    ProgressAck,
    ProgressReport,
)
from dlr.control.schemas.worker import WorkerRegister, WorkerResponse
from dlr.control.security import require_principal, require_worker_token
from dlr.control.services import execution as execution_service
from dlr.control.services import worker as worker_service
from dlr.control.services import worker_availability

router = APIRouter(dependencies=[Depends(require_worker_token)])
# M3: the read-only Worker list is an admin-facing observability API, so it
# lives on its own router with the unified Principal requirement.
admin_router = APIRouter(dependencies=[Depends(require_principal)])

DbSession = Annotated[Session, Depends(db.get_session)]


@router.post("/api/workers/register", response_model=WorkerResponse)
def register_worker(payload: WorkerRegister, session: DbSession) -> WorkerResponse:
    """Upsert by name and mark the Worker online."""
    return WorkerResponse.model_validate(worker_service.register_worker(session, payload))


@router.post("/api/workers/{worker_id}/heartbeat", status_code=204)
def heartbeat(worker_id: int, session: DbSession) -> Response:
    worker_service.heartbeat(session, worker_id)
    return Response(status_code=204)


@router.post("/api/workers/{worker_id}/offline", status_code=204)
def offline(worker_id: int, session: DbSession) -> Response:
    """Best-effort graceful offline on normal shutdown."""
    worker_service.mark_offline(session, worker_id)
    return Response(status_code=204)


@router.post("/api/workers/{worker_id}/tasks/claim")
def claim_task(worker_id: int, session: DbSession, wait_seconds: int = 20) -> Response:
    """Long-poll for one pending Execution; 204 when the deadline expires."""
    payload = worker_service.claim_task(session, worker_id, wait_seconds)
    if payload is None:
        return Response(status_code=204)
    return JSONResponse(content=payload.model_dump(mode="json"))


@router.post(
    "/api/workers/{worker_id}/executions/{execution_id}/result",
    response_model=ExecutionResponse,
)
def report_result(
    worker_id: int, execution_id: int, payload: ExecutionResultReport, session: DbSession
) -> ExecutionResponse:
    """Persist a terminal result; idempotent for terminal Executions."""
    return ExecutionResponse.model_validate(
        execution_service.apply_result(session, worker_id, execution_id, payload)
    )


@router.post(
    "/api/workers/{worker_id}/executions/{execution_id}/progress",
    response_model=ProgressAck,
)
def report_progress(
    worker_id: int, execution_id: int, payload: ProgressReport, session: DbSession
) -> ProgressAck:
    """Append best-effort stdout/stderr chunks while the Execution runs.

    The 200 acknowledgement carries the cancel flag (M3.2) so the owning
    Worker can kill the subprocess on its next upload. Once the Execution
    reached a terminal state the chunks are dropped but the flag is still
    answered; non-owning Workers still get 409.
    """
    cancel_requested = execution_service.apply_progress(session, worker_id, execution_id, payload)
    return ProgressAck(cancel_requested=cancel_requested)


@admin_router.get("/api/workers", response_model=list[WorkerResponse])
def list_workers(session: DbSession) -> list[WorkerResponse]:
    """Admin Worker list whose status is the derived effective status."""
    now = worker_availability.current_time(session)
    return [
        WorkerResponse.model_validate(worker).model_copy(
            update={"status": worker_availability.effective_status(worker, now=now)}
        )
        for worker in worker_service.list_workers(session)
    ]
