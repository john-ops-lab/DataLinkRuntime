"""Worker-internal endpoints of the Control Node (Worker Token protected)."""

from collections.abc import Iterator
from typing import Annotated, Any, BinaryIO

from fastapi import APIRouter, Body, Depends, Header, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.execution import (
    ExecutionResponse,
    WorkspaceCleanupReceipt,
)
from dlr.control.schemas.reliable_runtime import (
    AttemptPrepareFailedBody,
    AttemptProgressBody,
    AttemptRenewBody,
    AttemptResultBody,
    AttemptStartBody,
    ClaimDecision,
)
from dlr.control.schemas.worker import (
    CleanupResult,
    WorkerHeartbeat,
    WorkerRegister,
    WorkerResponse,
)
from dlr.control.security import require_business_principal, require_worker_token
from dlr.control.services import attempt as attempt_service
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


@router.post("/api/workers/register", response_model=WorkerResponse)
def register_worker(payload: WorkerRegister, session: DbSession) -> WorkerResponse:
    """Upsert by name and mark the Worker online."""
    return WorkerResponse.model_validate(worker_service.register_worker(session, payload))


@router.post("/api/workers/{worker_id}/heartbeat", status_code=204)
def heartbeat(
    worker_id: int,
    session: DbSession,
    payload: WorkerHeartbeat | None = None,
) -> Response:
    worker_service.heartbeat(
        session,
        worker_id,
        payload.isolation_capabilities if payload is not None else None,
    )
    return Response(status_code=204)


@router.post("/api/workers/{worker_id}/offline", status_code=204)
def offline(worker_id: int, session: DbSession) -> Response:
    """Best-effort graceful offline on normal shutdown."""
    worker_service.mark_offline(session, worker_id)
    return Response(status_code=204)


@router.get(
    "/api/workers/{worker_id}/executions/{execution_id}/input-artifacts/{artifact_id}/content"
)
def download_input_artifact(
    request: Request,
    worker_id: int,
    execution_id: int,
    artifact_id: int,
    session: DbSession,
    claim_token: ClaimHeader = None,
    cleanup_token: CleanupHeader = None,
) -> StreamingResponse:
    """Stream one active-Lease Artifact after full metadata verification."""
    _reject_query_tokens(request, error_code="execution_claim_token_invalid")
    _reject_swapped_cleanup_header(cleanup_token)
    download = worker_service.open_input_artifact_for_download(
        session,
        worker_id,
        execution_id,
        artifact_id,
        claim_token,
    )

    def chunks(stream: BinaryIO) -> Iterator[bytes]:
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            stream.close()

    return StreamingResponse(
        chunks(download.stream),
        media_type=download.content_type,
        headers={"Content-Length": str(download.size_bytes)},
    )


@router.post(
    "/api/workers/executions/{execution_id}/workspace-cleanup",
    response_model=ExecutionResponse,
)
def report_cleanup_receipt(
    request: Request,
    execution_id: int,
    payload: WorkspaceCleanupReceipt,
    session: DbSession,
    claim_token: ClaimHeader = None,
    cleanup_token: CleanupHeader = None,
) -> ExecutionResponse:
    """Confirm local Workspace cleanup without changing business state."""
    _reject_query_tokens(request, error_code="execution_cleanup_token_invalid")
    if claim_token is not None:
        raise domain_error(
            422,
            "execution_cleanup_token_invalid",
            "The Claim Token cannot authorize cleanup",
        )
    _ = payload
    return ExecutionResponse.model_validate(
        worker_service.apply_cleanup_receipt(
            session,
            execution_id,
            cleanup_token,
        )
    )


@router.post("/api/workers/{worker_id}/cleanups/claim")
def claim_cleanup(worker_id: int, session: DbSession) -> Response:
    payload = worker_service.claim_cleanup(session, worker_id)
    if payload is None:
        return Response(status_code=204)
    return JSONResponse(content=payload.model_dump(mode="json"))


@router.post("/api/workers/{worker_id}/cleanups/{cleanup_id}/result", status_code=204)
def report_cleanup(
    worker_id: int, cleanup_id: int, payload: CleanupResult, session: DbSession
) -> Response:
    """Acknowledge adapter-private filesystem cleanup without raw errors."""
    worker_service.apply_cleanup_result(session, worker_id, cleanup_id, payload)
    return Response(status_code=204)


@router.post(
    "/api/workers/{worker_id}/v3/claim",
    response_model=ClaimDecision,
)
def claim_v3(
    request: Request,
    worker_id: int,
    payload: Annotated[Any, Body(...)],
    session: DbSession,
) -> ClaimDecision:
    """Claim one RabbitMQ delivery through the closed v3 decision contract."""
    _reject_query_tokens(request, error_code="worker_protocol_payload_invalid")
    return attempt_service.claim_dispatch(session, worker_id, payload)


@router.post(
    "/api/workers/{worker_id}/attempts/{attempt_id}/start",
    response_model=ClaimDecision,
)
def start_attempt(
    request: Request,
    worker_id: int,
    attempt_id: int,
    payload: AttemptStartBody,
    session: DbSession,
) -> ClaimDecision:
    _reject_query_tokens(request, error_code="attempt_token_invalid")
    return attempt_service.start_attempt(session, worker_id, attempt_id, payload)


@router.post(
    "/api/workers/{worker_id}/attempts/{attempt_id}/renew",
    response_model=ClaimDecision,
)
def renew_attempt(
    request: Request,
    worker_id: int,
    attempt_id: int,
    payload: AttemptRenewBody,
    session: DbSession,
) -> ClaimDecision:
    _reject_query_tokens(request, error_code="attempt_token_invalid")
    return attempt_service.renew_attempt(session, worker_id, attempt_id, payload)


@router.post(
    "/api/workers/{worker_id}/attempts/{attempt_id}/progress",
    response_model=ClaimDecision,
)
def progress_attempt(
    request: Request,
    worker_id: int,
    attempt_id: int,
    payload: AttemptProgressBody,
    session: DbSession,
) -> ClaimDecision:
    _reject_query_tokens(request, error_code="attempt_token_invalid")
    return attempt_service.progress_attempt(session, worker_id, attempt_id, payload)


@router.post(
    "/api/workers/{worker_id}/attempts/{attempt_id}/result",
    response_model=ClaimDecision,
)
def result_attempt(
    request: Request,
    worker_id: int,
    attempt_id: int,
    payload: AttemptResultBody,
    session: DbSession,
) -> ClaimDecision:
    _reject_query_tokens(request, error_code="attempt_token_invalid")
    return attempt_service.finish_attempt(session, worker_id, attempt_id, payload)


@router.post(
    "/api/workers/{worker_id}/attempts/{attempt_id}/prepare-failed",
    response_model=ClaimDecision,
)
def prepare_failed_attempt(
    request: Request,
    worker_id: int,
    attempt_id: int,
    payload: AttemptPrepareFailedBody,
    session: DbSession,
) -> ClaimDecision:
    _reject_query_tokens(request, error_code="attempt_token_invalid")
    return attempt_service.prepare_failed(session, worker_id, attempt_id, payload)


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
