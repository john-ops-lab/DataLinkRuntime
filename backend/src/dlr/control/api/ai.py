"""Admin-only M4 AI model setting, discovery, test and assist endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.ai import (
    AiAssistRequest,
    AiAssistResponse,
    AiAttachmentCapabilitiesResponse,
    AiConnectionTestResponse,
    AiModelsResponse,
    AiProviderDraft,
    AiSettingDraft,
    AiSettingResponse,
)
from dlr.control.security import (
    Principal,
    require_admin_principal,
    require_business_principal,
    require_principal,
)
from dlr.control.services import adapter_access
from dlr.control.services import ai as ai_service

router = APIRouter(dependencies=[Depends(require_admin_principal)])
adapter_router = APIRouter(dependencies=[Depends(require_business_principal)])

DbSession = Annotated[Session, Depends(db.get_session)]
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


@router.get("/api/ai/settings", response_model=AiSettingResponse | None)
def get_ai_setting(session: DbSession) -> AiSettingResponse | None:
    setting = ai_service.get_setting(session)
    return None if setting is None else ai_service.setting_response(session, setting)


@router.put("/api/ai/settings", response_model=AiSettingResponse)
def put_ai_setting(payload: AiSettingDraft, session: DbSession) -> AiSettingResponse:
    setting = ai_service.save_setting(session, payload)
    return ai_service.setting_response(session, setting)


@router.post("/api/ai/settings/test", response_model=AiConnectionTestResponse)
def test_ai_setting(payload: AiSettingDraft, session: DbSession) -> AiConnectionTestResponse:
    """Make one real, minimal model request without saving the draft."""
    return ai_service.test_connection(session, payload)


@router.post("/api/ai/models/refresh", response_model=AiModelsResponse)
def refresh_ai_models(payload: AiProviderDraft, session: DbSession) -> AiModelsResponse:
    """Discover model IDs; failure never removes the manually editable field."""
    return ai_service.refresh_models(session, payload)


@router.get("/api/ai/attachment-capabilities", response_model=AiAttachmentCapabilitiesResponse)
def get_ai_attachment_capabilities() -> AiAttachmentCapabilitiesResponse:
    """M5.7 Wave B2: stable attachment limits, accepted MIME types and the
    per-Provider native-attachment capability table for the Wave B3 UI."""
    return ai_service.attachment_capabilities()


@adapter_router.post("/api/adapters/{adapter_id}/ai/assist", response_model=AiAssistResponse)
def assist_adapter(
    adapter_id: int,
    payload: AiAssistRequest,
    principal: CurrentPrincipal,
    session: DbSession,
) -> AiAssistResponse:
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    return ai_service.assist(session, adapter_id, payload)
