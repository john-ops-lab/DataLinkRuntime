"""Adapter and AdapterVersion management endpoints of the Control Node."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.adapter import (
    AdapterCreate,
    AdapterResponse,
    AdapterUpdate,
    CloneRequest,
    VersionCreate,
    VersionDetail,
    VersionSummary,
)
from dlr.control.security import require_admin_token
from dlr.control.services import adapter as adapter_service

router = APIRouter(dependencies=[Depends(require_admin_token)])

DbSession = Annotated[Session, Depends(db.get_session)]


@router.get("/api/adapters", response_model=list[AdapterResponse])
def list_adapters(session: DbSession) -> list[AdapterResponse]:
    adapters = adapter_service.list_adapters(session)
    return adapter_service.adapter_responses(session, adapters)


@router.post("/api/adapters", status_code=201, response_model=AdapterResponse)
def create_adapter(payload: AdapterCreate, session: DbSession) -> AdapterResponse:
    return adapter_service.adapter_response(
        session, adapter_service.create_adapter(session, payload)
    )


@router.get("/api/adapters/{adapter_id}", response_model=AdapterResponse)
def get_adapter(adapter_id: int, session: DbSession) -> AdapterResponse:
    return adapter_service.adapter_response(
        session, adapter_service.get_adapter(session, adapter_id)
    )


@router.patch("/api/adapters/{adapter_id}", response_model=AdapterResponse)
def update_adapter(adapter_id: int, payload: AdapterUpdate, session: DbSession) -> AdapterResponse:
    return adapter_service.adapter_response(
        session, adapter_service.update_adapter(session, adapter_id, payload)
    )


@router.delete("/api/adapters/{adapter_id}", status_code=204)
def delete_adapter(adapter_id: int, session: DbSession) -> Response:
    adapter_service.delete_adapter(session, adapter_id)
    return Response(status_code=204)


@router.get("/api/adapters/{adapter_id}/versions", response_model=list[VersionSummary])
def list_versions(adapter_id: int, session: DbSession) -> list[VersionSummary]:
    versions = adapter_service.list_versions(session, adapter_id)
    return [VersionSummary.model_validate(version) for version in versions]


@router.post("/api/adapters/{adapter_id}/versions", status_code=201, response_model=VersionDetail)
def save_version(adapter_id: int, payload: VersionCreate, session: DbSession) -> VersionDetail:
    """Save new version: creates a new immutable AdapterVersion (latest)."""
    return VersionDetail.model_validate(adapter_service.save_version(session, adapter_id, payload))


@router.get("/api/adapters/{adapter_id}/versions/{version_id}", response_model=VersionDetail)
def get_version(adapter_id: int, version_id: int, session: DbSession) -> VersionDetail:
    return VersionDetail.model_validate(
        adapter_service.get_version(session, adapter_id, version_id)
    )


@router.post("/api/adapters/{adapter_id}/clone", status_code=201, response_model=AdapterResponse)
def clone_adapter(adapter_id: int, payload: CloneRequest, session: DbSession) -> AdapterResponse:
    """Copy common Adapter facts into a stopped clone with its own Revision 1."""
    return adapter_service.adapter_response(
        session, adapter_service.clone_adapter(session, adapter_id, payload)
    )
