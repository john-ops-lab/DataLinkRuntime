"""Execution management endpoints of the Control Node (admin-facing)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.execution import (
    ExecutionCreate,
    ExecutionHistoryPage,
    ExecutionResponse,
)
from dlr.control.security import require_business_principal
from dlr.control.services import execution as execution_service

router = APIRouter(dependencies=[Depends(require_business_principal)])

DbSession = Annotated[Session, Depends(db.get_session)]


@router.post(
    "/api/adapters/{adapter_id}/executions",
    status_code=202,
    response_model=ExecutionResponse,
)
def create_execution(
    adapter_id: int, payload: ExecutionCreate, session: DbSession
) -> ExecutionResponse:
    """Create a Manual Execution pinned to one immutable version."""
    return ExecutionResponse.model_validate(
        execution_service.create_execution(session, adapter_id, payload)
    )


@router.get("/api/executions/{execution_id}", response_model=ExecutionResponse)
def get_execution(execution_id: int, session: DbSession) -> ExecutionResponse:
    return ExecutionResponse.model_validate(execution_service.get_execution(session, execution_id))


@router.post("/api/executions/{execution_id}/cancel", response_model=ExecutionResponse)
def cancel_execution(execution_id: int, session: DbSession) -> ExecutionResponse:
    """Request cancellation (M3.2).

    Idempotent: pending becomes cancelled immediately, running gets the
    cancel flag the owning Worker picks up on its next progress upload,
    and terminal Executions are returned unchanged.
    """
    return ExecutionResponse.model_validate(
        execution_service.cancel_execution(session, execution_id)
    )


@router.get(
    "/api/adapters/{adapter_id}/executions",
    response_model=ExecutionHistoryPage,
)
def list_executions(
    adapter_id: int,
    session: DbSession,
    limit: int = execution_service.DEFAULT_HISTORY_LIMIT,
    before_id: int | None = None,
    trigger: Literal["manual", "schedule", "webhook"] | None = None,
) -> ExecutionHistoryPage:
    """Cursor-paged execution history of one Adapter, newest first.

    Summaries only: the input/output/stdout/stderr big fields are never
    included; ``GET /api/executions/{id}`` loads a full detail.
    """
    items, next_before_id = execution_service.list_adapter_executions(
        session,
        adapter_id,
        limit=limit,
        before_id=before_id,
        trigger=trigger,
    )
    return ExecutionHistoryPage(items=items, next_before_id=next_before_id)
