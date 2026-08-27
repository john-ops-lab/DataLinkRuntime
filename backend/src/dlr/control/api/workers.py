"""Worker-internal endpoints of the Control Node (Worker Token protected)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.execution import (
    ExecutionResponse,
    ExecutionResultReport,
    ProgressAck,
    ProgressReport,
    WorkspaceCleanupReceipt,
)
from dlr.control.schemas.worker import CleanupResult, WorkerRegister, WorkerResponse
from dlr.control.security import require_business_principal, require_worker_token
from dlr.control.services import execution as execution_service
from dlr.control.services import worker as worker_service
from dlr.control.services import worker_availability
from dlr.control.services.adapter import domain_error
from dlr.control.services.worker_protocol import (
    CLAIM_TOKEN_HEADER,
    CLEANUP_TOKEN_HEADER,
)

router = APIRouter(dependencies=[Depends(require_worker_token)])
# M5.9 Wave D: Worker metadata is required by the Adapter runtime editor for
# ordinary account users. The list is read-only; registration and task
# execution remain on the Worker-token router above.
admin_router = APIRouter(dependencies=[Depends(require_business_principal)])

DbSession = Annotated[Session, Depends(db.get_session)]
ClaimHeader = Annotated[str | None, Header(alias=CLAIM_TOKEN_HEADER)]
CleanupHeader = Annotated[str | None, Header(alias=CLEANUP_TOKEN_HEADER)]


def _reject_query_tokens(request: Request, *, error_code: str) -> None:
    """Keep credentials out of URLs, including on not-yet-ready routes."""
    if "claim_token" in request.query_params or "cleanup_token" in request.query_params:
        raise domain_error(422, error_code, "Tokens must be sent in their designated Header")


def _reject_swapped_cleanup_header(cleanup_token: str | None) -> None:
    if cleanup_token is not None:
        raise domain_error(
            422,
            "execution_claim_token_invalid",
            "The Cleanup Token cannot authorize this operation",
        )


def _task_payload_content(payload: BaseModel) -> dict[str, object]:
    """Keep legacy v1 payload shape free of token fields without dropping nulls."""
    # Both task payload variants currently expose model_dump; keeping this
    # helper local avoids an ``exclude_none`` pass that would remove the
    # existing ``index_url: null`` compatibility field.
    body = payload.model_dump(mode="json")
    if body.get("claim_token") is None:
        body.pop("claim_token", None)
    if body.get("cleanup_token") is None:
        body.pop("cleanup_token", None)
    return body


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
    return JSONResponse(content=_task_payload_content(payload))


@router.post(
    "/api/workers/{worker_id}/executions/{execution_id}/result",
    response_model=ExecutionResponse,
)
def report_result(
    request: Request,
    worker_id: int,
    execution_id: int,
    payload: ExecutionResultReport,
    session: DbSession,
    claim_token: ClaimHeader = None,
    cleanup_token: CleanupHeader = None,
) -> ExecutionResponse:
    """Persist a terminal result; idempotent for terminal Executions."""
    _reject_query_tokens(request, error_code="execution_claim_token_invalid")
    _reject_swapped_cleanup_header(cleanup_token)
    return ExecutionResponse.model_validate(
        execution_service.apply_result(
            session,
            worker_id,
            execution_id,
            payload,
            claim_token=claim_token,
        )
    )


@router.post(
    "/api/workers/{worker_id}/executions/{execution_id}/progress",
    response_model=ProgressAck,
)
def report_progress(
    request: Request,
    worker_id: int,
    execution_id: int,
    payload: ProgressReport,
    session: DbSession,
    claim_token: ClaimHeader = None,
    cleanup_token: CleanupHeader = None,
) -> ProgressAck:
    """Append best-effort stdout/stderr chunks while the Execution runs.

    The 200 acknowledgement carries the cancel flag (M3.2) so the owning
    Worker can kill the subprocess on its next upload. Once the Execution
    reached a terminal state the chunks are dropped but the flag is still
    answered; non-owning Workers still get 409.
    """
    _reject_query_tokens(request, error_code="execution_claim_token_invalid")
    _reject_swapped_cleanup_header(cleanup_token)
    cancel_requested = execution_service.apply_progress(
        session,
        worker_id,
        execution_id,
        payload,
        claim_token=claim_token,
    )
    return ProgressAck(cancel_requested=cancel_requested)


@router.get(
    "/api/workers/{worker_id}/executions/{execution_id}/input-artifacts/{artifact_id}/content",
    status_code=501,
)
def download_input_artifact(
    request: Request,
    worker_id: int,
    execution_id: int,
    artifact_id: int,
    session: DbSession,
    claim_token: ClaimHeader = None,
    cleanup_token: CleanupHeader = None,
) -> Response:
    """Reserve the C0 download contract without starting the C1 stream path."""
    _reject_query_tokens(request, error_code="execution_claim_token_invalid")
    _reject_swapped_cleanup_header(cleanup_token)
    worker_service.validate_claim_for_route(session, worker_id, execution_id, claim_token)
    # ``artifact_id`` is deliberately accepted only as a route placeholder;
    # checking the Lease and streaming bytes belong to task 11.1.
    _ = artifact_id
    raise domain_error(
        501,
        "worker_input_download_not_ready",
        "Worker input download is not available in this protocol wave",
    )


@router.post(
    "/api/workers/{worker_id}/executions/{execution_id}/cleanup-receipt",
    status_code=501,
)
def report_cleanup_receipt(
    request: Request,
    worker_id: int,
    execution_id: int,
    payload: WorkspaceCleanupReceipt,
    session: DbSession,
    claim_token: ClaimHeader = None,
    cleanup_token: CleanupHeader = None,
) -> Response:
    """Reserve the independent Cleanup Token receipt route for C1/C3."""
    _reject_query_tokens(request, error_code="execution_cleanup_token_invalid")
    if claim_token is not None:
        raise domain_error(
            422,
            "execution_cleanup_token_invalid",
            "The Claim Token cannot authorize cleanup",
        )
    worker_service.validate_cleanup_for_route(session, worker_id, execution_id, cleanup_token)
    _ = payload
    raise domain_error(
        501,
        "worker_cleanup_receipt_not_ready",
        "Worker cleanup receipts are not available in this protocol wave",
    )


@router.post("/api/workers/{worker_id}/cleanups/{cleanup_id}/result", status_code=204)
def report_cleanup(
    worker_id: int, cleanup_id: int, payload: CleanupResult, session: DbSession
) -> Response:
    """Acknowledge adapter-private filesystem cleanup without raw errors."""
    worker_service.apply_cleanup_result(session, worker_id, cleanup_id, payload)
    return Response(status_code=204)


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
