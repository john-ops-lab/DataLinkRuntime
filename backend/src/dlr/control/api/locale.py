"""Public read and administrator update endpoints for the system locale."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.locale import SystemLocaleResponse, SystemLocaleUpdate
from dlr.control.security import require_principal
from dlr.control.services import locale as locale_service

public_router = APIRouter()
router = APIRouter(dependencies=[Depends(require_principal)])

DbSession = Annotated[Session, Depends(db.get_session)]


@public_router.get("/api/locale", response_model=SystemLocaleResponse)
def get_public_locale(session: DbSession) -> SystemLocaleResponse:
    """Return only the deployment locale needed before administrator login."""
    return locale_service.system_locale_response(session)


@router.put("/api/locale", response_model=SystemLocaleResponse)
def put_locale(payload: SystemLocaleUpdate, session: DbSession) -> SystemLocaleResponse:
    """Allow an authenticated administrator to replace the deployment locale."""
    return locale_service.update_system_locale(session, payload.locale)
