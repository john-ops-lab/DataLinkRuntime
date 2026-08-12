"""Python package source management endpoints of the Control Node."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.package_source import (
    PackageSourceCreate,
    PackageSourceResponse,
    PackageSourceUpdate,
    ReachabilityResponse,
)
from dlr.control.security import require_admin_token
from dlr.control.services import package_source as package_source_service

router = APIRouter(dependencies=[Depends(require_admin_token)])

DbSession = Annotated[Session, Depends(db.get_session)]


@router.get("/api/package-sources", response_model=list[PackageSourceResponse])
def list_package_sources(session: DbSession) -> list[PackageSourceResponse]:
    sources = package_source_service.list_package_sources(session)
    return [package_source_service.package_source_response(session, source) for source in sources]


@router.post("/api/package-sources", status_code=201, response_model=PackageSourceResponse)
def create_package_source(
    payload: PackageSourceCreate, session: DbSession
) -> PackageSourceResponse:
    return package_source_service.package_source_response(
        session, package_source_service.create_package_source(session, payload)
    )


@router.get("/api/package-sources/{package_source_id}", response_model=PackageSourceResponse)
def get_package_source(package_source_id: int, session: DbSession) -> PackageSourceResponse:
    return package_source_service.package_source_response(
        session, package_source_service.get_package_source(session, package_source_id)
    )


@router.patch("/api/package-sources/{package_source_id}", response_model=PackageSourceResponse)
def update_package_source(
    package_source_id: int, payload: PackageSourceUpdate, session: DbSession
) -> PackageSourceResponse:
    return package_source_service.package_source_response(
        session, package_source_service.update_package_source(session, package_source_id, payload)
    )


@router.delete("/api/package-sources/{package_source_id}", status_code=204)
def delete_package_source(package_source_id: int, session: DbSession) -> Response:
    package_source_service.delete_package_source(session, package_source_id)
    return Response(status_code=204)


@router.post("/api/package-sources/{package_source_id}/test", response_model=ReachabilityResponse)
def test_package_source(package_source_id: int, session: DbSession) -> ReachabilityResponse:
    """Probe the saved source's effective index URL (basic auth embedded)."""
    source = package_source_service.get_package_source(session, package_source_id)
    ok, status_code, error = package_source_service.probe_index_url(
        package_source_service.resolve_source_url(session, source)
    )
    return ReachabilityResponse(ok=ok, status_code=status_code, error=error)
