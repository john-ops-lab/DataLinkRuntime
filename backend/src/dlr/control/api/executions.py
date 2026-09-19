"""Execution management endpoints of the Control Node (admin-facing)."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.execution import (
    ExecutionCreate,
    ExecutionHistoryPage,
    ExecutionResponse,
)
from dlr.control.schemas.reliable_runtime import (
    HoldPurgeBody,
    IncidentDispositionBody,
    IncidentDispositionPage,
    IncidentDispositionResponse,
    ReliableExecutionDetail,
    ReplayResponse,
)
from dlr.control.security import (
    Principal,
    require_admin_principal,
    require_business_principal,
    require_principal,
)
from dlr.control.services import adapter_access, incident_disposition
from dlr.control.services import attempt as attempt_service
from dlr.control.services import execution as execution_service

router = APIRouter(dependencies=[Depends(require_business_principal)])

DbSession = Annotated[Session, Depends(db.get_session)]
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]
DispositionIdempotencyHeader = Annotated[uuid.UUID, Header(alias="Idempotency-Key")]


@router.post(
    "/api/adapters/{adapter_id}/executions",
    status_code=202,
    response_model=ExecutionResponse,
)
def create_execution(
    adapter_id: int,
    payload: ExecutionCreate,
    principal: CurrentPrincipal,
    session: DbSession,
    idempotency_key: IdempotencyHeader = None,
) -> ExecutionResponse:
    """Create a Manual Execution pinned to one immutable version."""
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    # Use FastAPI's already validated model rather than Starlette's private
    # body cache or a second JSON parse.  ``exclude_unset`` preserves the
    # distinction between an omitted ``input`` field and an explicit JSON
    # null, while the service's JCS layer removes insignificant whitespace and
    # key-order differences.
    idempotency_body = payload.model_dump(mode="json", exclude_unset=True)
    return ExecutionResponse.model_validate(
        execution_service.create_execution(
            session,
            adapter_id,
            payload,
            idempotency_key=idempotency_key,
            idempotency_body=idempotency_body,
        )
    )


@router.get("/api/executions/{execution_id}", response_model=ExecutionResponse)
def get_execution(
    execution_id: int, principal: CurrentPrincipal, session: DbSession
) -> ExecutionResponse:
    adapter_access.require_execution_access(session, execution_id, principal, "read")
    return ExecutionResponse.model_validate(execution_service.get_execution(session, execution_id))


@router.get(
    "/api/executions/{execution_id}/reliable-detail",
    response_model=ReliableExecutionDetail,
)
def get_reliable_detail(
    execution_id: int, principal: CurrentPrincipal, session: DbSession
) -> ReliableExecutionDetail:
    execution = adapter_access.require_execution_access(session, execution_id, principal, "read")
    access = adapter_access.require_adapter_access(session, execution.adapter_id, principal, "read")
    return attempt_service.execution_detail(
        session,
        execution_id,
        can_edit=access.level in {"admin", "owner", "edit"},
    )


@router.post(
    "/api/executions/{execution_id}/incidents/{incident_id}/dispositions",
    response_model=IncidentDispositionResponse,
)
def dispose_infrastructure_incident(
    execution_id: int,
    incident_id: int,
    payload: IncidentDispositionBody,
    response: Response,
    principal: CurrentPrincipal,
    session: DbSession,
    idempotency_key: DispositionIdempotencyHeader,
) -> IncidentDispositionResponse:
    result = incident_disposition.dispose_incident(
        session,
        execution_id,
        incident_id,
        payload.action,
        payload.expected_generation,
        idempotency_key,
        payload.reason_code,
        principal,
    )
    response.status_code = result.status_code
    if result.response.retry_after_seconds is not None:
        response.headers["Retry-After"] = str(result.response.retry_after_seconds)
    return result.response


@router.get(
    "/api/executions/{execution_id}/incidents/{incident_id}/dispositions",
    response_model=IncidentDispositionPage,
)
def list_infrastructure_incident_dispositions(
    execution_id: int,
    incident_id: int,
    principal: CurrentPrincipal,
    session: DbSession,
    before_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> IncidentDispositionPage:
    adapter_access.require_execution_access(session, execution_id, principal, "read")
    return incident_disposition.list_dispositions(
        session,
        execution_id=execution_id,
        incident_id=incident_id,
        before_id=before_id,
        limit=limit,
    )


@router.post("/api/executions/{execution_id}/replay", response_model=ReplayResponse)
def replay_execution(
    execution_id: int, principal: CurrentPrincipal, session: DbSession
) -> ReplayResponse:
    adapter_access.require_execution_access(session, execution_id, principal, "edit")
    return attempt_service.replay_execution(session, execution_id)


@router.post(
    "/api/executions/{execution_id}/artifact-holds/purge",
    response_model=dict[str, int],
)
def purge_execution_holds(
    execution_id: int,
    payload: HoldPurgeBody,
    principal: Annotated[Principal, Depends(require_admin_principal)],
    session: DbSession,
) -> dict[str, int]:
    adapter_access.require_execution_access(session, execution_id, principal, "edit")
    actor = principal.username or principal.kind
    return {
        "purged": attempt_service.purge_holds(
            session, execution_id, actor=actor, reason=payload.reason
        )
    }


@router.post("/api/executions/{execution_id}/cancel", response_model=ExecutionResponse)
def cancel_execution(
    execution_id: int, principal: CurrentPrincipal, session: DbSession
) -> ExecutionResponse:
    """Request cancellation (M3.2).

    Idempotent: pending becomes cancelled immediately, running gets the
    cancel flag the owning Worker picks up on its next progress upload,
    and terminal Executions are returned unchanged.
    """
    adapter_access.require_execution_access(session, execution_id, principal, "edit")
    return ExecutionResponse.model_validate(
        execution_service.cancel_execution(session, execution_id)
    )


@router.get(
    "/api/adapters/{adapter_id}/executions",
    response_model=ExecutionHistoryPage,
)
def list_executions(
    adapter_id: int,
    principal: CurrentPrincipal,
    session: DbSession,
    limit: int = execution_service.DEFAULT_HISTORY_LIMIT,
    before_id: int | None = None,
    trigger: Literal["manual", "schedule", "webhook"] | None = None,
) -> ExecutionHistoryPage:
    """Cursor-paged execution history of one Adapter, newest first.

    Summaries only: the input/output/stdout/stderr big fields are never
    included; ``GET /api/executions/{id}`` loads a full detail.
    """
    adapter_access.require_adapter_access(session, adapter_id, principal, "read")
    items, next_before_id = execution_service.list_adapter_executions(
        session,
        adapter_id,
        limit=limit,
        before_id=before_id,
        trigger=trigger,
    )
    return ExecutionHistoryPage(items=items, next_before_id=next_before_id)
