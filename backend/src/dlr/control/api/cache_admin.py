"""Administrator cache operations; Worker execution remains outbound-only."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.cache_admin import (
    CacheAdminView,
    CacheOperationCreate,
    CacheOperationPage,
    CacheOperationResponse,
)
from dlr.control.security import Principal, require_admin_principal
from dlr.control.services import cache_admin as service

router = APIRouter(dependencies=[Depends(require_admin_principal)])
DbSession = Annotated[Session, Depends(db.get_session)]
AdminPrincipal = Annotated[Principal, Depends(require_admin_principal)]


@router.get("/api/workers/{worker_id}/cache", response_model=CacheAdminView)
def get_cache(
    worker_id: int,
    session: DbSession,
    failed_cursor: Annotated[int | None, Query(gt=0)] = None,
    failed_limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> CacheAdminView:
    return service.cache_view(
        session, worker_id, failed_cursor=failed_cursor, failed_limit=failed_limit
    )


@router.post(
    "/api/workers/{worker_id}/cache/operations",
    response_model=CacheOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_cache_operation(
    worker_id: int,
    payload: CacheOperationCreate,
    session: DbSession,
    principal: AdminPrincipal,
) -> CacheOperationResponse:
    return CacheOperationResponse.model_validate(
        service.create_operation(session, worker_id, payload, principal)
    )


@router.get(
    "/api/workers/{worker_id}/cache/operations",
    response_model=CacheOperationPage,
)
def list_cache_operations(
    worker_id: int,
    session: DbSession,
    cursor: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> CacheOperationPage:
    return service.list_operations(session, worker_id, cursor=cursor, limit=limit)


@router.get(
    "/api/workers/{worker_id}/cache/operations/{operation_id}",
    response_model=CacheOperationResponse,
)
def get_cache_operation(
    worker_id: int, operation_id: uuid.UUID, session: DbSession
) -> CacheOperationResponse:
    return CacheOperationResponse.model_validate(
        service.get_operation(session, worker_id, operation_id)
    )
