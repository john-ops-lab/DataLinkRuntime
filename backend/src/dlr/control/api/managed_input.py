"""Administrator Managed Input policy endpoints (Issue #127 B0)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.managed_input import (
    ManagedInputSettingsResponse,
    ManagedInputSettingsUpdate,
)
from dlr.control.security import require_admin_principal
from dlr.control.services import managed_input as managed_input_service

router = APIRouter(dependencies=[Depends(require_admin_principal)])
DbSession = Annotated[Session, Depends(db.get_session)]


@router.get(
    "/api/system/managed-input-settings",
    response_model=ManagedInputSettingsResponse,
)
def get_managed_input_settings(session: DbSession) -> ManagedInputSettingsResponse:
    """Return policy, capacity usage, and observable quota state."""
    return managed_input_service.settings_response(session)


@router.put(
    "/api/system/managed-input-settings",
    response_model=ManagedInputSettingsResponse,
)
def put_managed_input_settings(
    payload: ManagedInputSettingsUpdate,
    session: DbSession,
) -> ManagedInputSettingsResponse:
    """Replace the database policy without changing stored artifacts."""
    return managed_input_service.update_settings(session, payload)
