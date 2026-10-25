"""Business-authenticated bounded batch preparation, mutation and observation."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.adapter_batch import BatchCreate, BatchResponse, BatchRetry
from dlr.control.security import Principal, require_business_principal, require_principal
from dlr.control.services import adapter_batch

router = APIRouter(dependencies=[Depends(require_business_principal)])
DbSession = Annotated[Session, Depends(db.get_session)]
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


@router.post("/api/adapter-batches", response_model=BatchResponse)
def prepare_batch(
    payload: BatchCreate, principal: CurrentPrincipal, session: DbSession
) -> BatchResponse:
    return adapter_batch.prepare(session, payload, principal)


@router.get("/api/adapter-batches/{batch_id}", response_model=BatchResponse)
def get_batch(
    batch_id: uuid.UUID, principal: CurrentPrincipal, session: DbSession
) -> BatchResponse:
    return adapter_batch.get_batch(session, batch_id, principal)


@router.post("/api/adapter-batches/{batch_id}/execute", response_model=BatchResponse)
def execute_batch(
    batch_id: uuid.UUID, principal: CurrentPrincipal, session: DbSession
) -> BatchResponse:
    return adapter_batch.execute(session, batch_id, principal)


@router.post("/api/adapter-batches/{batch_id}/retry", response_model=BatchResponse)
def retry_batch(
    batch_id: uuid.UUID,
    payload: BatchRetry,
    principal: CurrentPrincipal,
    session: DbSession,
) -> BatchResponse:
    return adapter_batch.execute(session, batch_id, principal, retry_ids=payload.adapter_ids)
