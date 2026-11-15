"""Administrator API for fresh, disabled Worker identities.

This endpoint neither adopts legacy identities nor issues credentials. Replay
is keyed by the administrator's request ID and succeeds only for the same
normalized name and principal.
"""

from contextlib import suppress
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.models import Worker
from dlr.control.security import Principal, require_admin_principal
from dlr.control.services import worker_identity_reservation

router = APIRouter()
DbSession = Annotated[Session, Depends(db.get_session)]
AdminPrincipal = Annotated[Principal, Depends(require_admin_principal)]
_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class FreshIdentityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr = Field(max_length=256)
    request_id: UUID


class FreshIdentityResponse(BaseModel):
    request_id: UUID
    worker_id: int
    worker_identity_id: UUID
    name: str


def _rollback(session: Session) -> None:
    with suppress(Exception):
        session.rollback()


@router.post("/api/workers/v4/identities", response_model=FreshIdentityResponse, status_code=201)
def reserve_identity(
    payload: FreshIdentityRequest,
    response: Response,
    session: DbSession,
    principal: AdminPrincipal,
) -> FreshIdentityResponse:
    try:
        reservation = worker_identity_reservation.reserve_identity(
            session, name=payload.name, request_id=payload.request_id, principal=principal
        )
        worker = session.get(Worker, reservation.worker_id)
        if (
            worker is None
            or worker.identity_lifecycle != "reserved"
            or worker.name != reservation.name
        ):
            raise RuntimeError("reserved identity unavailable")
        result = FreshIdentityResponse(
            request_id=reservation.request_id,
            worker_id=worker.id,
            worker_identity_id=worker.identity_id,
            name=reservation.name,
        )
        session.commit()
    except HTTPException as error:
        _rollback(session)
        raise HTTPException(
            status_code=error.status_code, detail=error.detail, headers=_HEADERS
        ) from None
    except Exception:  # noqa: BLE001 - do not disclose database/exception details
        _rollback(session)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "worker_identity_result_unknown",
                "message": "Worker identity operation unavailable",
            },
            headers=_HEADERS,
        ) from None
    response.headers.update(_HEADERS)
    return result
