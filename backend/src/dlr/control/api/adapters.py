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
    ProductionStopRequest,
    PublishGateResponse,
    VersionCreate,
    VersionDetail,
    VersionSummary,
)
from dlr.control.security import require_admin_token
from dlr.control.services import adapter as adapter_service

router = APIRouter(dependencies=[Depends(require_admin_token)])

DbSession = Annotated[Session, Depends(db.get_session)]

# Stop without a body means "wait".
_DEFAULT_STOP_REQUEST = ProductionStopRequest()


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


@router.post(
    "/api/adapters/{adapter_id}/versions/{version_id}/publish",
    response_model=AdapterResponse,
)
def publish_version(adapter_id: int, version_id: int, session: DbSession) -> AdapterResponse:
    """Publish one version; the publish gate is enforced server-side (M3.2)."""
    return adapter_service.adapter_response(
        session, adapter_service.publish_version(session, adapter_id, version_id)
    )


@router.get(
    "/api/adapters/{adapter_id}/versions/{version_id}/publish-gate",
    response_model=PublishGateResponse,
)
def get_publish_gate(adapter_id: int, version_id: int, session: DbSession) -> PublishGateResponse:
    """Read-only publish gate evaluation for the Publish confirmation dialog."""
    return adapter_service.publish_gate(session, adapter_id, version_id)


@router.post(
    "/api/adapters/{adapter_id}/production/start",
    response_model=AdapterResponse,
)
def start_production(adapter_id: int, session: DbSession) -> AdapterResponse:
    """Open the production entry and lock the production version (M5.1).

    Synchronous state change: the row lock, gates and commit all complete
    before the final AdapterResponse is returned, so the answer is 200.
    """
    return adapter_service.adapter_response(
        session, adapter_service.start_production(session, adapter_id)
    )


@router.post("/api/adapters/{adapter_id}/production/stop", response_model=AdapterResponse)
def stop_production(
    adapter_id: int,
    session: DbSession,
    payload: ProductionStopRequest = _DEFAULT_STOP_REQUEST,
) -> AdapterResponse:
    """Close the production entry; ``terminate`` also cancels the active run."""
    return adapter_service.adapter_response(
        session, adapter_service.stop_production(session, adapter_id, payload.mode)
    )


@router.post("/api/adapters/{adapter_id}/unpublish", response_model=AdapterResponse)
def unpublish_adapter(adapter_id: int, session: DbSession) -> AdapterResponse:
    """Clear the published pointer; requires production to be stopped."""
    return adapter_service.adapter_response(
        session, adapter_service.unpublish_adapter(session, adapter_id)
    )


@router.post("/api/adapters/{adapter_id}/archive", response_model=AdapterResponse)
def archive_adapter(adapter_id: int, session: DbSession) -> AdapterResponse:
    """Archive the Adapter (read-only afterwards); requires production stopped."""
    return adapter_service.adapter_response(
        session, adapter_service.archive_adapter(session, adapter_id)
    )


@router.post("/api/adapters/{adapter_id}/restore", response_model=AdapterResponse)
def restore_adapter(adapter_id: int, session: DbSession) -> AdapterResponse:
    """Restore an archived Adapter."""
    return adapter_service.adapter_response(
        session, adapter_service.restore_adapter(session, adapter_id)
    )


@router.post("/api/adapters/{adapter_id}/clone", status_code=201, response_model=AdapterResponse)
def clone_adapter(adapter_id: int, payload: CloneRequest, session: DbSession) -> AdapterResponse:
    """Copy the Adapter: working copy becomes v1, unpublished and not running."""
    return adapter_service.adapter_response(
        session, adapter_service.clone_adapter(session, adapter_id, payload)
    )
