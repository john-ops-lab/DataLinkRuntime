"""Authenticated Template Gallery catalog and instantiate endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.adapter import AdapterResponse
from dlr.control.schemas.template import (
    TemplateInstantiateRequest,
    TemplateScenarioDetail,
    TemplateScenarioListResponse,
    TemplateThemeResponse,
    TemplateVariantResponse,
)
from dlr.control.security import Principal, require_business_principal, require_principal
from dlr.control.services import adapter as adapter_service
from dlr.control.services import adapter_access
from dlr.control.services import template as template_service

router = APIRouter(dependencies=[Depends(require_business_principal)])

DbSession = Annotated[Session, Depends(db.get_session)]
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


@router.get("/api/templates/themes", response_model=list[TemplateThemeResponse])
def list_template_themes() -> list[TemplateThemeResponse]:
    return template_service.list_template_themes()


@router.get("/api/templates/scenarios", response_model=TemplateScenarioListResponse)
def list_template_scenarios(
    theme: str,
    q: Annotated[str | None, Query(max_length=128)] = None,
    vendor: str | None = None,
    adapter_type: str | None = None,
    protocol: str | None = None,
    language: str | None = None,
    maturity: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=48)] = 12,
) -> TemplateScenarioListResponse:
    return template_service.list_template_scenarios(
        theme=theme,
        q=q,
        vendor=vendor,
        adapter_type=adapter_type,
        protocol=protocol,
        language=language,
        maturity=maturity,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/api/templates/scenarios/{scenario_slug}",
    response_model=TemplateScenarioDetail,
)
def get_template_scenario(scenario_slug: str) -> TemplateScenarioDetail:
    return template_service.get_template_scenario(scenario_slug)


@router.get(
    "/api/templates/scenarios/{scenario_slug}/variants/{language}",
    response_model=TemplateVariantResponse,
)
def get_template_variant(scenario_slug: str, language: str) -> TemplateVariantResponse:
    return template_service.get_template_variant(scenario_slug, language)


@router.post(
    "/api/templates/scenarios/{scenario_slug}/variants/{language}/instantiate",
    status_code=201,
    response_model=AdapterResponse,
)
def instantiate_template_variant(
    scenario_slug: str,
    language: str,
    payload: TemplateInstantiateRequest,
    response: Response,
    principal: CurrentPrincipal,
    session: DbSession,
) -> AdapterResponse:
    adapter = template_service.instantiate_template_adapter(
        session,
        scenario_slug=scenario_slug,
        language=language,
        payload=payload,
        owner_user_id=template_service.owner_user_id_for_template_principal(
            principal_kind=principal.kind,
            user_id=principal.user_id,
        ),
    )
    result = adapter_service.adapter_response(session, adapter)
    level, owner_username = adapter_access.response_metadata(session, adapter, principal)
    response.headers["Location"] = f"/api/adapters/{adapter.id}"
    return result.model_copy(update={"access_level": level, "owner_username": owner_username})
