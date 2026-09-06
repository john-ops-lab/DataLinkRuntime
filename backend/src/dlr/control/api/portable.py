"""Authenticated preview-first portability endpoints."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.adapter import AdapterResponse
from dlr.control.schemas.portable import (
    AdapterExportOptions,
    PortableImportRequest,
    PortablePackage,
    TemplateWriteRequest,
)
from dlr.control.schemas.template import TemplateScenarioDetail
from dlr.control.security import Principal, require_business_principal, require_principal
from dlr.control.services import adapter, adapter_access, portable, user_templates
from dlr.control.services.adapter import domain_error
from dlr.control.services.portable_zip import MAX_ZIP_BYTES, decode_package, encode_package


class BoundedPortableRoute(APIRoute):
    """Bound JSON as well as ZIP bodies before FastAPI materializes input models."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            limit = (
                MAX_ZIP_BYTES
                if request.url.path.endswith("/portable/preview")
                else 48 * 1024 * 1024
            )
            body = bytearray()
            try:
                async with asyncio.timeout(30):
                    async for chunk in request.stream():
                        if len(body) + len(chunk) > limit:
                            raise domain_error(
                                413, "portable_package_too_large", "DLR package is too large"
                            )
                        body.extend(chunk)
            except TimeoutError:
                raise domain_error(
                    408, "portable_upload_timeout", "Package upload timed out"
                ) from None
            request._body = bytes(body)
            return await original(request)

        return bounded


router = APIRouter(
    dependencies=[Depends(require_business_principal)], route_class=BoundedPortableRoute
)
DbSession = Annotated[Session, Depends(db.get_session)]
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


@router.post("/api/portable/preview", response_model=PortablePackage)
async def preview_package(request: Request, response: Response) -> PortablePackage:
    response.headers["Cache-Control"] = "no-store"
    content = bytearray()
    try:
        async with asyncio.timeout(30):
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content) > MAX_ZIP_BYTES:
                    raise domain_error(
                        413, "portable_package_too_large", "DLR package is too large"
                    )
    except TimeoutError:
        raise domain_error(408, "portable_upload_timeout", "Package upload timed out") from None
    return await asyncio.to_thread(decode_package, bytes(content))


@router.post("/api/portable/export")
def export_package(package: PortablePackage) -> Response:
    return Response(
        encode_package(package),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="export.{package.object_type}.dlr.zip"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/api/adapters/{adapter_id}/portable-preview", response_model=PortablePackage)
def preview_adapter(
    adapter_id: int,
    options: AdapterExportOptions,
    principal: CurrentPrincipal,
    session: DbSession,
    response: Response,
) -> PortablePackage:
    response.headers["Cache-Control"] = "no-store"
    return portable.export_adapter(session, adapter_id, principal, options)


@router.post("/api/portable/adapters", response_model=AdapterResponse, status_code=201)
def import_adapter(
    payload: PortableImportRequest,
    principal: CurrentPrincipal,
    session: DbSession,
    response: Response,
) -> AdapterResponse:
    result = portable.import_adapter(session, payload, principal)
    level, owner = adapter_access.response_metadata(session, result, principal)
    response.headers["Location"] = f"/api/adapters/{result.id}"
    return adapter.adapter_response(session, result).model_copy(
        update={"access_level": level, "owner_username": owner}
    )


@router.get("/api/templates/scenarios/{slug}/portable", response_model=PortablePackage)
def template_package(slug: str, session: DbSession) -> PortablePackage:
    return user_templates.export_template(session, slug)


@router.post("/api/portable/templates", response_model=TemplateScenarioDetail, status_code=201)
def import_template(
    payload: TemplateWriteRequest, principal: CurrentPrincipal, session: DbSession
) -> TemplateScenarioDetail:
    row = user_templates.write_template(session, payload, principal, source="imported")
    return user_templates.detail(session, row.slug, principal)


@router.post(
    "/api/adapters/{adapter_id}/templates", response_model=TemplateScenarioDetail, status_code=201
)
def save_template(
    adapter_id: int, payload: TemplateWriteRequest, principal: CurrentPrincipal, session: DbSession
) -> TemplateScenarioDetail:
    source = adapter_access.require_adapter_access(session, adapter_id, principal, "owner").adapter
    if source.latest_version_id is None:
        raise domain_error(
            409, "portable_saved_version_required", "Save the adapter before exporting"
        )
    if (
        len(payload.package.variants) != 1
        or payload.package.variants[0].language != source.language
        or payload.package.adapter_type != source.adapter_type
    ):
        raise domain_error(
            422, "portable_object_type", "Preserve the source adapter language and type"
        )
    row = user_templates.write_template(session, payload, principal, source="saved")
    return user_templates.detail(session, row.slug, principal)


@router.put("/api/templates/scenarios/{slug}", response_model=TemplateScenarioDetail)
def update_template(
    slug: str, payload: TemplateWriteRequest, principal: CurrentPrincipal, session: DbSession
) -> TemplateScenarioDetail:
    row = user_templates.write_template(session, payload, principal, source="saved", slug=slug)
    return user_templates.detail(session, row.slug, principal)


@router.delete("/api/templates/scenarios/{slug}", status_code=204)
def delete_template(
    slug: str,
    principal: CurrentPrincipal,
    session: DbSession,
    expected_version: Annotated[str, Query(max_length=64)],
) -> Response:
    user_templates.delete_template(session, slug, principal, expected_version)
    return Response(status_code=204)
