"""Adapter-level InputConfig endpoints (Issue #127 A0)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.input_config import (
    AdapterInputConfigResponse,
    AdapterInputConfigUpsert,
)
from dlr.control.security import Principal, require_business_principal, require_principal
from dlr.control.services import adapter_access
from dlr.control.services import input_config as input_config_service

router = APIRouter(dependencies=[Depends(require_business_principal)])

DbSession = Annotated[Session, Depends(db.get_session)]
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


@router.get(
    "/api/adapters/{adapter_id}/input-config",
    response_model=AdapterInputConfigResponse,
)
def get_input_config(
    adapter_id: int, principal: CurrentPrincipal, session: DbSession
) -> AdapterInputConfigResponse:
    """Return only safe current input metadata and the computed run gate."""
    adapter_access.require_adapter_access(session, adapter_id, principal, "read")
    return input_config_service.input_config_response(
        input_config_service.get_input_config(session, adapter_id), session=session
    )


@router.put(
    "/api/adapters/{adapter_id}/input-config",
    response_model=AdapterInputConfigResponse,
)
def put_input_config(
    adapter_id: int,
    payload: AdapterInputConfigUpsert,
    principal: CurrentPrincipal,
    session: DbSession,
) -> AdapterInputConfigResponse:
    """Update the one current input object with optimistic revision control."""
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    config = input_config_service.upsert_input_config(session, adapter_id, payload)
    return input_config_service.input_config_response(config, session=session)
