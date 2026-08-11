"""Execution management endpoints of the Control Node (admin-facing)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.execution import ExecutionCreate, ExecutionResponse
from dlr.control.security import require_admin_token
from dlr.control.services import execution as execution_service

router = APIRouter(dependencies=[Depends(require_admin_token)])

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
