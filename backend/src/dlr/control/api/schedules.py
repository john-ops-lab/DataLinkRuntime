"""Adapter Schedule configuration endpoints of the Control Node (M5.2).

Singleton semantics: one Adapter has at most one Schedule. GET answers a
stable 404 ``schedule_not_configured`` before configuration; PUT creates or
fully replaces the Schedule.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.schedule import ScheduleResponse, ScheduleUpsert
from dlr.control.security import Principal, require_business_principal, require_principal
from dlr.control.services import adapter_access
from dlr.control.services import schedule as schedule_service

router = APIRouter(dependencies=[Depends(require_business_principal)])

DbSession = Annotated[Session, Depends(db.get_session)]
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


@router.get("/api/adapters/{adapter_id}/schedule", response_model=ScheduleResponse)
def get_schedule(
    adapter_id: int, principal: CurrentPrincipal, session: DbSession
) -> ScheduleResponse:
    """Return the Adapter's Schedule; 404 ``schedule_not_configured`` if absent."""
    adapter_access.require_adapter_access(session, adapter_id, principal, "read")
    return ScheduleResponse.model_validate(schedule_service.get_schedule(session, adapter_id))


@router.put("/api/adapters/{adapter_id}/schedule", response_model=ScheduleResponse)
def put_schedule(
    adapter_id: int,
    payload: ScheduleUpsert,
    principal: CurrentPrincipal,
    session: DbSession,
) -> ScheduleResponse:
    """Create or fully replace the Adapter's Schedule.

    Validation (cron, timezone, input size) happens before anything is
    persisted; the saved cursor is re-based to the next future planned
    point (or null while disabled).
    """
    adapter_access.require_adapter_access(session, adapter_id, principal, "edit")
    return ScheduleResponse.model_validate(
        schedule_service.upsert_schedule(session, adapter_id, payload)
    )
