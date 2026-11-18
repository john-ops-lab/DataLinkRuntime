"""Administrator API for reserved and explicitly adopted Worker identities."""

from contextlib import suppress
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.api.worker_instance_v4 import _process
from dlr.control.models import Worker, WorkerIdentityReservation
from dlr.control.security import Principal, require_admin_principal
from dlr.control.services import worker_identity_reservation, worker_legacy_window

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


class LegacyIdentityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    worker_identity_id: UUID
    legacy_stop_evidence_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")


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


@router.post("/api/workers/{worker_id}/v4/identity/adopt", response_model=FreshIdentityResponse)
def adopt_legacy_identity(
    request: Request,
    worker_id: int,
    payload: LegacyIdentityRequest,
    response: Response,
    session: DbSession,
    principal: AdminPrincipal,
) -> FreshIdentityResponse:
    """Bind a stopped, settled legacy node at its existing ID and queue."""
    try:
        if not 0 < worker_id < 2**63 or payload.request_id.int == 0:
            raise worker_legacy_window.LegacyWindowUnavailable
        actor_kind, actor_id = worker_identity_reservation._actor(principal)
        process = _process(request)
        fence = process.worker_fence(session)
        worker_legacy_window.lock_cutover(session)
        worker = session.get(Worker, worker_id, with_for_update=True, populate_existing=True)
        if worker is None or worker.identity_id != payload.worker_identity_id:
            raise worker_legacy_window.LegacyWindowUnavailable
        existing = session.get(WorkerIdentityReservation, payload.request_id)
        if existing is None:
            if (
                not worker_legacy_window.is_legacy_worker(worker)
                or worker.status != "offline"
                or worker_legacy_window.has_unsettled_worker_work(session, worker.id)
            ):
                raise worker_legacy_window.LegacyWindowUnavailable
            worker.protocol_version = 4
            worker.identity_lifecycle = "reserved"
            worker.scheduling_state = "disabled"
            worker.capacity_state = "unknown"
            worker.capabilities = []
            worker.isolation_capabilities = {}
            worker.isolation_preflight_status = "unknown"
            worker.isolation_preflight_at = None
            worker.rabbitmq_execution_v3 = False
            session.add(
                WorkerIdentityReservation(
                    request_id=payload.request_id,
                    worker_id=worker.id,
                    name=worker.name,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    legacy_stop_evidence_sha256=payload.legacy_stop_evidence_sha256,
                )
            )
        elif (
            existing.worker_id != worker.id
            or existing.name != worker.name
            or existing.actor_kind != actor_kind
            or existing.actor_id != actor_id
            or existing.legacy_stop_evidence_sha256 != payload.legacy_stop_evidence_sha256
            or worker.identity_lifecycle != "reserved"
        ):
            raise worker_legacy_window.LegacyWindowUnavailable
        result = FreshIdentityResponse(
            request_id=payload.request_id,
            worker_id=worker.id,
            worker_identity_id=worker.identity_id,
            name=worker.name,
        )
        session.flush()
        fence.check()
        process.check_authority()
        session.commit()
        process.check_authority()
    except (worker_legacy_window.LegacyWindowUnavailable, IntegrityError):
        _rollback(session)
        raise HTTPException(
            status_code=409, detail={"code": "worker_legacy_identity_conflict"}, headers=_HEADERS
        ) from None
    except Exception:
        _rollback(session)
        raise HTTPException(
            status_code=503, detail={"code": "worker_identity_result_unknown"}, headers=_HEADERS
        ) from None
    response.headers.update(_HEADERS)
    return result
