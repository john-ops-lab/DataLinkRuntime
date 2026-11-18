"""One-time enrollment for an administrator-reserved remote Worker."""

from contextlib import suppress
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.common.schema_gate import try_control_gate
from dlr.control import db
from dlr.control.models import Worker, WorkerCredential
from dlr.control.models.worker_identity_reservation import WorkerIdentityReservation
from dlr.control.security import TOKEN_ENTRY_MODE, Principal, require_admin_principal, require_entry
from dlr.control.services import worker_credential_operations as operations
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


def _credential_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"code": "worker_credential_operation_unavailable"},
        headers=_HEADERS,
    )


def _credential_conflict() -> HTTPException:
    return HTTPException(
        status_code=409, detail={"code": "worker_credential_operation_conflict"}, headers=_HEADERS
    )


def _credential_read(session: Session) -> None:
    session.execute(text("SET TRANSACTION READ ONLY"))
    try_control_gate(session)
    session.execute(text("SET LOCAL statement_timeout = '3000ms'"))
    session.execute(text("SET LOCAL lock_timeout = '1000ms'"))
    # Independent observations hold the normal schema gate, without row locks or writes.


@router.get("/api/workers/{worker_id}/credentials/runtime")
def runtime_metadata(
    worker_id: int, request: Request, response: Response, principal: AdminPrincipal
) -> dict[str, object]:
    try:
        process = _process(request)
        process.check_authority()
        with db.SessionLocal() as session:
            _credential_read(session)
            row = operations.worker(session, worker_id, lock=False)
            latest = operations.latest(session, worker_id)
            now = credentials._now(session)
            current = list(
                session.scalars(
                    select(WorkerCredential)
                    .where(
                        WorkerCredential.worker_id == worker_id,
                        WorkerCredential.kind == "runtime",
                        WorkerCredential.revoked_at.is_(None),
                        or_(
                            WorkerCredential.expires_at.is_(None), WorkerCredential.expires_at > now
                        ),
                    )
                    .order_by(WorkerCredential.id)
                    .limit(3)
                )
            )
            if len(current) > 2:
                raise operations.OperationConflict
            result = {
                "worker_id": row.id,
                "worker_identity_id": str(row.identity_id),
                "latest_credential_record_id": latest.id,
                "credentials": [
                    {"credential_record_id": c.id, "expires_at": c.expires_at} for c in current
                ],
            }
        process.check_authority()
    except operations.OperationConflict:
        raise _credential_conflict() from None
    except Exception:
        raise _credential_unavailable() from None
    response.headers.update(_HEADERS)
    return result


@router.get("/api/workers/{worker_id}/credentials/runtime/operations/{request_id}")
def runtime_operation(
    worker_id: int,
    request_id: UUID,
    request: Request,
    response: Response,
    principal: AdminPrincipal,
) -> dict[str, object]:
    if request_id.int == 0:
        raise HTTPException(
            status_code=422, detail={"code": "worker_credential_request_invalid"}, headers=_HEADERS
        )
    try:
        process = _process(request)
        process.check_authority()
        with db.SessionLocal() as session:
            _credential_read(session)
            result = operations.read_receipt(session, worker_id, request_id, principal)
        process.check_authority()
    except operations.OperationConflict:
        raise _credential_conflict() from None
    except Exception:
        raise _credential_unavailable() from None
    response.headers.update(_HEADERS)
    return result


def _runtime_mutation(
    worker_id: int,
    payload: operations.RevokeRequest,
    request: Request,
    response: Response,
    session: Session,
    principal: AdminPrincipal,
    *,
    action: Literal["rotate", "revoke"],
) -> RuntimeSecret | dict[str, object]:
    committed = False
    try:
        process = _process(request)
        fence = process.worker_fence(session, issue_credentials=True)
        _receipt, issued = operations.execute(session, worker_id, payload, principal, action=action)
        if issued is not None:
            fence.check_candidate(issued.secret)
        else:
            fence.check()
        process.check_authority()
        session.commit()
        committed = True
        session.close()
        process.check_authority()
        with db.SessionLocal() as observer:
            # A separate, non-mutating transaction checks the actual current
            # Control/database fence; a cached local lease alone cannot issue.
            observed_fence = process.worker_fence(observer)
            observed = operations.read_receipt(observer, worker_id, payload.request_id, principal)
            if observed["status"] != "committed":
                raise operations.OperationConflict
            if issued is not None:
                metadata = operations.ReceiptMetadata.model_validate(observed["metadata"])
                if (
                    not observed["currently_usable"]
                    or metadata.credential_record_id != issued.credential_id
                    or metadata.worker_identity_id != payload.worker_identity_id
                ):
                    raise operations.OperationConflict
                credentials.authenticate_runtime_credential(observer, worker_id, issued.secret)
            observed_fence.check()
        process.check_authority()
    except (operations.OperationConflict, IntegrityError):
        _rollback(session)
        raise (_credential_unavailable() if committed else _credential_conflict()) from None
    except Exception:
        _rollback(session)
        raise _credential_unavailable() from None
    response.headers.update(_HEADERS)
    response.headers["Location"] = (
        f"/api/workers/{worker_id}/credentials/runtime/operations/{payload.request_id}"
    )
    if issued is None:
        response.status_code = 200
        return observed
    response.status_code = 201
    return RuntimeSecret(
        worker_id=worker_id,
        worker_identity_id=payload.worker_identity_id,
        credential_id=issued.secret,
        credential_record_id=issued.credential_id,
    )


@router.post("/api/workers/{worker_id}/credentials/runtime/rotate", response_model=None)
def rotate_runtime(
    worker_id: int,
    payload: operations.RotateRequest,
    request: Request,
    response: Response,
    session: DbSession,
    principal: AdminPrincipal,
) -> RuntimeSecret | dict[str, object]:
    return _runtime_mutation(
        worker_id, payload, request, response, session, principal, action="rotate"
    )


@router.post("/api/workers/{worker_id}/credentials/runtime/revoke", response_model=None)
def revoke_runtime(
    worker_id: int,
    payload: operations.RevokeRequest,
    request: Request,
    response: Response,
    session: DbSession,
    principal: AdminPrincipal,
) -> RuntimeSecret | dict[str, object]:
    return _runtime_mutation(
        worker_id, payload, request, response, session, principal, action="revoke"
    )
