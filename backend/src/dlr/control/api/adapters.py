"""Adapter and AdapterVersion management endpoints of the Control Node."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.models import Adapter
from dlr.control.schemas.adapter import (
    AdapterCreate,
    AdapterPermissionCandidate,
    AdapterPermissionResponse,
    AdapterPermissionUpsert,
    AdapterResponse,
    AdapterUpdate,
    CloneRequest,
    VersionCreate,
    VersionDetail,
    VersionSummary,
)
from dlr.control.security import Principal, require_business_principal, require_principal
from dlr.control.services import adapter as adapter_service
from dlr.control.services import adapter_access

router = APIRouter(dependencies=[Depends(require_business_principal)])

DbSession = Annotated[Session, Depends(db.get_session)]
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


def _decorate_response(
    session: Session,
    adapter: Adapter,
    principal: Principal,
    response: AdapterResponse,
) -> AdapterResponse:
    """Attach server-resolved relationship metadata for UI display/gating."""
    level, owner_username = adapter_access.response_metadata(session, adapter, principal)
    return response.model_copy(update={"access_level": level, "owner_username": owner_username})


def _adapter_response(session: Session, adapter: Adapter, principal: Principal) -> AdapterResponse:
    return _decorate_response(
        session,
        adapter,
        principal,
        adapter_service.adapter_response(session, adapter),
    )


@router.get("/api/adapters", response_model=list[AdapterResponse])
def list_adapters(principal: CurrentPrincipal, session: DbSession) -> list[AdapterResponse]:
    adapters = adapter_access.list_visible_adapters(session, principal)
    responses = adapter_service.adapter_responses(session, adapters)
    return [
        _decorate_response(session, adapter, principal, response)
        for adapter, response in zip(adapters, responses, strict=True)
    ]


@router.post("/api/adapters", status_code=201, response_model=AdapterResponse)
def create_adapter(
    payload: AdapterCreate, principal: CurrentPrincipal, session: DbSession
) -> AdapterResponse:
    adapter = adapter_service.create_adapter(
        session,
        payload,
        owner_user_id=adapter_access.owner_user_id_for_create(principal),
    )
    return _adapter_response(session, adapter, principal)


@router.get("/api/adapters/{adapter_id}", response_model=AdapterResponse)
def get_adapter(
    adapter_id: int, principal: CurrentPrincipal, session: DbSession
) -> AdapterResponse:
    adapter_access.require_adapter_access(session, adapter_id, principal, "read")
    return _adapter_response(session, adapter_service.get_adapter(session, adapter_id), principal)


@router.patch("/api/adapters/{adapter_id}", response_model=AdapterResponse)
def update_adapter(
    adapter_id: int, payload: AdapterUpdate, principal: CurrentPrincipal, session: DbSession
) -> AdapterResponse:
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    return _adapter_response(
        session,
        adapter_service.update_adapter(session, adapter_id, payload),
        principal,
    )


@router.delete("/api/adapters/{adapter_id}", response_model=None)
def delete_adapter(
    adapter_id: int,
    principal: CurrentPrincipal,
    session: DbSession,
    stop: bool = False,
) -> Response | JSONResponse:
    adapter_access.require_adapter_access(session, adapter_id, principal, "delete")
    result = adapter_service.delete_adapter(session, adapter_id, stop=stop)
    if result.waiting_for_worker:
        return JSONResponse(
            status_code=202,
            content={
                "detail": {
                    "code": "adapter_delete_waiting_for_worker",
                    "message": "Cancellation requested; wait for the Worker to report cancelled",
                    "params": {"active_execution_id": result.active_execution_id},
                }
            },
        )
    return Response(status_code=204)


@router.get("/api/adapters/{adapter_id}/versions", response_model=list[VersionSummary])
def list_versions(
    adapter_id: int, principal: CurrentPrincipal, session: DbSession
) -> list[VersionSummary]:
    adapter_access.require_adapter_access(session, adapter_id, principal, "read")
    versions = adapter_service.list_versions(session, adapter_id)
    return [VersionSummary.model_validate(version) for version in versions]


@router.post("/api/adapters/{adapter_id}/versions", status_code=201, response_model=VersionDetail)
def save_version(
    adapter_id: int,
    payload: VersionCreate,
    principal: CurrentPrincipal,
    session: DbSession,
) -> VersionDetail:
    """Save new version: creates a new immutable AdapterVersion (latest)."""
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    return VersionDetail.model_validate(adapter_service.save_version(session, adapter_id, payload))


@router.get("/api/adapters/{adapter_id}/versions/{version_id}", response_model=VersionDetail)
def get_version(
    adapter_id: int, version_id: int, principal: CurrentPrincipal, session: DbSession
) -> VersionDetail:
    adapter_access.require_adapter_access(session, adapter_id, principal, "read")
    return VersionDetail.model_validate(
        adapter_service.get_version(session, adapter_id, version_id)
    )


@router.post("/api/adapters/{adapter_id}/clone", status_code=201, response_model=AdapterResponse)
def clone_adapter(
    adapter_id: int,
    payload: CloneRequest,
    principal: CurrentPrincipal,
    session: DbSession,
) -> AdapterResponse:
    """Copy common Adapter facts into a stopped clone with its own Revision 1."""
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    cloned = adapter_service.clone_adapter(
        session,
        adapter_id,
        payload,
        owner_user_id=adapter_access.owner_user_id_for_create(principal),
    )
    return _adapter_response(session, cloned, principal)


@router.get(
    "/api/adapters/{adapter_id}/permissions",
    response_model=list[AdapterPermissionResponse],
)
def list_adapter_permissions(
    adapter_id: int, principal: CurrentPrincipal, session: DbSession
) -> list[AdapterPermissionResponse]:
    """List ACL metadata; only the owner and administrators may inspect it."""
    return adapter_access.list_permissions(session, adapter_id, principal)


@router.get(
    "/api/adapters/{adapter_id}/permission-candidates",
    response_model=list[AdapterPermissionCandidate],
)
def list_permission_candidates(
    adapter_id: int, principal: CurrentPrincipal, session: DbSession
) -> list[AdapterPermissionCandidate]:
    """Return minimal account rows for the owner/admin sharing picker."""
    return adapter_access.list_permission_candidates(session, adapter_id, principal)


@router.put(
    "/api/adapters/{adapter_id}/permissions/{user_id}",
    response_model=AdapterPermissionResponse,
)
def set_adapter_permission(
    adapter_id: int,
    user_id: int,
    payload: AdapterPermissionUpsert,
    principal: CurrentPrincipal,
    session: DbSession,
) -> AdapterPermissionResponse:
    """Create or replace one read/edit grant."""
    return adapter_access.set_permission(
        session, adapter_id, user_id, payload.permission, principal
    )


@router.delete("/api/adapters/{adapter_id}/permissions/{user_id}", status_code=204)
def revoke_adapter_permission(
    adapter_id: int, user_id: int, principal: CurrentPrincipal, session: DbSession
) -> Response:
    """Revoke one grant; repeating a revoke is idempotent."""
    adapter_access.revoke_permission(session, adapter_id, user_id, principal)
    return Response(status_code=204)
