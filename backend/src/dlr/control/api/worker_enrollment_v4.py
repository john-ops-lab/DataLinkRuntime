"""One-time enrollment for an administrator-reserved remote Worker."""

from contextlib import suppress
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.models import Worker
from dlr.control.models.worker_identity_reservation import WorkerIdentityReservation
from dlr.control.security import TOKEN_ENTRY_MODE, Principal, require_admin_principal, require_entry
from dlr.control.services import worker_credentials as credentials
from dlr.control.services import worker_enrollment_exchange_v4 as exchange_service
from dlr.control.services.control_process_v4 import ControlProcess
from dlr.control.services.worker_identity_reservation import _actor

router = APIRouter()
DbSession = Annotated[Session, Depends(db.get_session)]
AdminPrincipal = Annotated[Principal, Depends(require_admin_principal)]
_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class EnrollmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ttl_seconds: StrictInt = Field(default=600, ge=1, le=600)


class EnrollmentSecret(BaseModel):
    credential_id: str = Field(repr=False)
    credential_record_id: int
    expires_at: datetime | None


class RuntimeSecret(BaseModel):
    worker_id: int
    worker_identity_id: UUID
    credential_id: str = Field(repr=False)
    credential_record_id: int


def _process(request: Request) -> ControlProcess:
    process = getattr(request.app.state, "control_process_v4", None)
    if not isinstance(process, ControlProcess):
        raise RuntimeError("control authority unavailable")
    return process


def _rollback(session: Session) -> None:
    with suppress(Exception):
        session.rollback()


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"code": "worker_enrollment_unavailable", "message": "Enrollment unavailable"},
        headers=_HEADERS,
    )


def _invalid() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "worker_credential_invalid", "message": "Enrollment unavailable"},
        headers=_HEADERS,
    )


def _reserved_worker(session: Session, worker_id: int) -> Worker:
    worker = credentials._lock_worker(session, worker_id)
    reservation = session.scalar(
        select(WorkerIdentityReservation).where(WorkerIdentityReservation.worker_id == worker_id)
    )
    if (
        worker.identity_lifecycle != "reserved"
        or worker.protocol_version != 4
        or reservation is None
        or reservation.name != worker.name
    ):
        raise credentials.WorkerCredentialError("worker_credential_invalid")
    return worker


@router.post(
    "/api/workers/{worker_id}/credentials/enrollment",
    response_model=EnrollmentSecret,
    status_code=201,
)
def issue_enrollment(
    worker_id: int,
    payload: EnrollmentRequest,
    request: Request,
    response: Response,
    session: DbSession,
    principal: AdminPrincipal,
) -> EnrollmentSecret:
    try:
        actor_kind, actor_id = _actor(principal)
        process = _process(request)
        fence = process.worker_fence(session, issue_credentials=True)
        _reserved_worker(session, worker_id)
        issued = credentials.issue_enrollment(session, worker_id, ttl_seconds=payload.ttl_seconds)
        credentials.record_audit(
            session,
            worker_id=worker_id,
            credential_record_id=issued.credential_id,
            related_credential_record_id=None,
            event_kind="enrollment",
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        fence.check_candidate(issued.secret)
        process.check_authority()
        session.commit()
        # A late or uncertain commit must not release a speculative secret.
        process.check_authority()
    except credentials.WorkerCredentialError:
        _rollback(session)
        raise _invalid() from None
    except Exception:  # noqa: BLE001 - the commit outcome and errors may contain secrets
        _rollback(session)
        raise _unavailable() from None
    response.headers.update(_HEADERS)
    return EnrollmentSecret(
        credential_id=issued.secret,
        credential_record_id=issued.credential_id,
        expires_at=issued.expires_at,
    )


@router.post(
    "/api/workers/{worker_id}/credentials/recover",
    response_model=RuntimeSecret,
    status_code=201,
)
def recover_runtime(
    worker_id: int,
    request: Request,
    response: Response,
    session: DbSession,
    principal: AdminPrincipal,
) -> RuntimeSecret:
    """Revoke prior runtime credentials and issue a fresh one once."""
    try:
        actor_kind, actor_id = _actor(principal)
        process = _process(request)
        fence = process.worker_fence(session, issue_credentials=True)
        worker = _reserved_worker(session, worker_id)
        identity = worker.identity_id
        issued = credentials.recover_runtime_credential(session, worker_id)
        credentials.record_audit(
            session,
            worker_id=worker_id,
            credential_record_id=issued.credential_id,
            related_credential_record_id=None,
            event_kind="recover",
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        fence.check_candidate(issued.secret)
        process.check_authority()
        session.commit()
        process.check_authority()
    except credentials.WorkerCredentialError:
        _rollback(session)
        raise _invalid() from None
    except Exception:  # noqa: BLE001 - the commit outcome and errors may contain secrets
        _rollback(session)
        raise _unavailable() from None
    response.headers.update(_HEADERS)
    return RuntimeSecret(
        worker_id=worker_id,
        worker_identity_id=identity,
        credential_id=issued.secret,
        credential_record_id=issued.credential_id,
    )


@router.post(
    "/api/workers/{worker_id}/credentials/exchange",
    response_model=RuntimeSecret,
    status_code=201,
)
def exchange_enrollment(
    worker_id: str, request: Request, response: Response, session: DbSession
) -> RuntimeSecret:
    try:
        require_entry(request, TOKEN_ENTRY_MODE)
        headers = request.headers.getlist("authorization")
        authorization = headers[0] if len(headers) == 1 else ""
        if (
            not worker_id.isascii()
            or not worker_id.isdecimal()
            or worker_id.startswith("0")
            or len(worker_id) > 19
            or int(worker_id) > 2**63 - 1
            or not authorization.startswith("Bearer ")
        ):
            raise credentials.WorkerCredentialError("worker_credential_invalid")
        process = _process(request)
        fence = process.worker_fence(session, issue_credentials=True)
        issued = exchange_service.exchange(
            session, worker_id=int(worker_id), enrollment_secret=authorization[7:]
        )
        credentials.record_audit(
            session,
            worker_id=issued.worker_id,
            credential_record_id=issued.credential_record_id,
            related_credential_record_id=issued.enrollment_record_id,
            event_kind="exchange",
            actor_kind="worker",
            actor_id=None,
        )
        if issued.enrollment_expires_at <= credentials._now(session):
            raise credentials.WorkerCredentialError("worker_credential_invalid")
        fence.check_candidate(issued.secret)
        process.check_authority()
        session.commit()
        process.check_authority()
    except (credentials.WorkerCredentialError, HTTPException):
        _rollback(session)
        raise _invalid() from None
    except Exception:  # noqa: BLE001 - the commit outcome and errors may contain secrets
        _rollback(session)
        raise _unavailable() from None
    response.headers.update(_HEADERS)
    return RuntimeSecret(
        worker_id=issued.worker_id,
        worker_identity_id=issued.worker_identity_id,
        credential_id=issued.secret,
        credential_record_id=issued.credential_record_id,
    )
