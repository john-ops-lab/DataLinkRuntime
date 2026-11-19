"""Administrator activation of an enrolled, locally prepared fixed Worker."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from contextlib import suppress
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dlr.common.worker_languages_v4 import valid_capabilities
from dlr.control import db
from dlr.control.api.worker_instance_v4 import _HEADERS, _process
from dlr.control.models import ExecutionAttempt, Worker, WorkerMaintenanceOperation
from dlr.control.schemas.worker import isolation_capabilities_ready
from dlr.control.security import Principal, require_admin_principal
from dlr.control.services import (
    worker_availability,
    worker_maintenance_observation,
    worker_recovery_v4,
)
from dlr.control.services.attempt import ACTIVE_ATTEMPT_STATUSES
from dlr.control.services.worker_identity_reservation import _actor

router = APIRouter()
logger = logging.getLogger("dlr.control.worker_activation")
DbSession = Annotated[Session, Depends(db.get_session)]
Admin = Annotated[Principal, Depends(require_admin_principal)]


class ActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    expected_version: StrictInt = Field(ge=1, lt=2**63 - 1)


class ForceStopRequest(ActivationRequest):
    reason: StrictStr = Field(min_length=1, max_length=256)


class _Conflict(Exception):
    pass


@router.post("/api/workers/{worker_id}/v4/recovery-intent")
def recovery_intent(
    request: Request,
    worker_id: int,
    payload: worker_recovery_v4.RecoveryRequest,
    session: DbSession,
    principal: Admin,
) -> dict[str, object]:
    """Record supervised host checks; this endpoint cannot prove physical stop."""
    try:
        process = _process(request)
        fence = process.worker_fence(session)
        result = worker_recovery_v4.request_recovery(session, worker_id, payload, principal)
        fence.check()
        process.check_authority()
        session.commit()
        process.check_authority()
        return result
    except worker_recovery_v4.RecoveryUnavailable:
        with suppress(Exception):
            session.rollback()
        raise HTTPException(
            status_code=409, detail={"code": "worker_recovery_conflict"}, headers=_HEADERS
        ) from None
    except Exception as error:
        with suppress(Exception):
            session.rollback()
        logger.warning("worker recovery intent unavailable: type=%s", type(error).__name__)
        raise HTTPException(
            status_code=503, detail={"code": "worker_recovery_unknown"}, headers=_HEADERS
        ) from None


def _unsettled_attempts(session: Session, worker: Worker) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ExecutionAttempt)
            .where(
                or_(
                    ExecutionAttempt.worker_id == worker.id,
                    ExecutionAttempt.worker_identity_id == worker.identity_id,
                ),
                or_(
                    ExecutionAttempt.cleanup_settled_at.is_(None),
                    ExecutionAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
                    ExecutionAttempt.worker_id != worker.id,
                    ExecutionAttempt.worker_identity_id.is_distinct_from(worker.identity_id),
                ),
            )
        )
        or 0
    )


def _observe(
    read: Callable[[], dict[str, object]], *, not_found_code: str = "worker_maintenance_not_found"
) -> dict[str, object]:
    try:
        return read()
    except worker_maintenance_observation.ObservationNotFound:
        raise HTTPException(
            status_code=404, detail={"code": not_found_code}, headers=_HEADERS
        ) from None
    except Exception as error:
        logger.warning("worker maintenance observation unavailable: type=%s", type(error).__name__)
        raise HTTPException(
            status_code=503,
            detail={"code": "worker_maintenance_observation_unavailable"},
            headers=_HEADERS,
        ) from None


@router.get("/api/workers/{worker_id}/v4/status")
def maintenance_status(worker_id: int, response: Response, principal: Admin) -> dict[str, object]:
    response.headers.update(_HEADERS)
    return _observe(
        lambda: worker_maintenance_observation.read_status(db.SessionLocal, worker_id, principal),
        not_found_code="worker_not_found",
    )


@router.get("/api/workers/{worker_id}/v4/maintenance-operations/{request_id}")
def maintenance_operation(
    worker_id: int, request_id: UUID, response: Response, principal: Admin
) -> dict[str, object]:
    """Observe a committed action; absence does not prove an in-flight rollback."""
    response.headers.update(_HEADERS)
    return _observe(
        lambda: worker_maintenance_observation.read_operation(
            db.SessionLocal, worker_id, request_id, principal
        )
    )


@router.post("/api/workers/{worker_id}/v4/drain")
def drain(
    request: Request,
    worker_id: int,
    payload: ActivationRequest,
    session: DbSession,
    principal: Admin,
) -> dict[str, object]:
    """Stop new Claims; repeat the same request to confirm completed drain.

    Database settlement is not proof of an empty local journal, released
    budget or Broker ACK. Operators must observe those before stopping.
    """
    try:
        if not 0 < worker_id < 2**63 or payload.request_id.int == 0:
            raise _Conflict
        actor_kind, actor_id = _actor(principal)
        process = _process(request)
        fence = process.worker_fence(session)
        worker = session.get(Worker, worker_id, with_for_update=True, populate_existing=True)
        if (
            worker is None
            or worker.identity_lifecycle != "reserved"
            or worker.protocol_version != 4
        ):
            raise _Conflict
        existing = session.get(WorkerMaintenanceOperation, payload.request_id)
        if existing is not None:
            if (
                existing.worker_id != worker_id
                or existing.expected_version != payload.expected_version
                or existing.action != "drain"
                or existing.actor_kind != actor_kind
                or existing.actor_id != actor_id
                or worker.maintenance_version != existing.result_version
                or worker.scheduling_state not in {"draining", "disabled"}
            ):
                raise _Conflict
        else:
            if worker.maintenance_version != payload.expected_version:
                raise _Conflict
            recovered_drain = False
            if worker.scheduling_state == "draining":
                current_action = session.scalar(
                    select(WorkerMaintenanceOperation.action).where(
                        WorkerMaintenanceOperation.worker_id == worker.id,
                        WorkerMaintenanceOperation.result_version == worker.maintenance_version,
                    )
                )
                # B/C recovery advanced the version but did not change the
                # administrator's draining intent. Finish it with a NEW audit,
                # only after the existing recovery reported ready and settled.
                recovered_drain = (
                    current_action == "reconcile"
                    and worker.capacity_state == "ready"
                    and worker.identity_enrolled_at is not None
                    and _unsettled_attempts(session, worker) == 0
                    and not worker_availability.has_unreconciled_v4_attempt(
                        session, worker, now=worker_availability.current_time(session)
                    )
                )
            if worker.scheduling_state != "active" and not recovered_drain:
                raise _Conflict
            worker.scheduling_state = "disabled" if recovered_drain else "draining"
            worker.maintenance_version += 1
            session.add(
                WorkerMaintenanceOperation(
                    request_id=payload.request_id,
                    worker_id=worker_id,
                    expected_version=payload.expected_version,
                    result_version=worker.maintenance_version,
                    action="drain",
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    reason=None,
                )
            )
        unsettled = _unsettled_attempts(session, worker)
        if worker.scheduling_state == "draining" and not unsettled:
            worker.scheduling_state = "disabled"
        session.flush()
        result = {
            "worker_id": worker_id,
            "scheduling_state": worker.scheduling_state,
            "maintenance_version": worker.maintenance_version,
            "unsettled_attempts": unsettled,
        }
        fence.check()
        process.check_authority()
        session.commit()
        process.check_authority()
        return result
    except _Conflict:
        with suppress(Exception):
            session.rollback()
        raise HTTPException(
            status_code=409, detail={"code": "worker_maintenance_conflict"}, headers=_HEADERS
        ) from None
    except Exception as error:
        with suppress(Exception):
            session.rollback()
        logger.warning("worker drain unavailable: type=%s", type(error).__name__)
        raise HTTPException(
            status_code=503, detail={"code": "worker_maintenance_unknown"}, headers=_HEADERS
        ) from None


@router.post("/api/workers/{worker_id}/v4/force-stop")
def force_stop(
    request: Request,
    worker_id: int,
    payload: ForceStopRequest,
    session: DbSession,
    principal: Admin,
) -> dict[str, object]:
    """Record stop intent on a draining node while retaining its obligations."""
    try:
        reason = payload.reason.strip()
        if not 0 < worker_id < 2**63 or payload.request_id.int == 0 or not reason.isprintable():
            raise _Conflict
        actor_kind, actor_id = _actor(principal)
        process = _process(request)
        fence = process.worker_fence(session)
        worker = session.get(Worker, worker_id, with_for_update=True, populate_existing=True)
        if (
            worker is None
            or worker.identity_lifecycle != "reserved"
            or worker.protocol_version != 4
            or worker.identity_enrolled_at is None
        ):
            raise _Conflict
        existing = session.get(WorkerMaintenanceOperation, payload.request_id)
        if existing is None:
            if (
                worker.scheduling_state != "draining"
                or worker.maintenance_version != payload.expected_version
                or session.scalar(
                    select(WorkerMaintenanceOperation.action).where(
                        WorkerMaintenanceOperation.worker_id == worker.id,
                        WorkerMaintenanceOperation.result_version == worker.maintenance_version,
                    )
                )
                != "drain"
            ):
                raise _Conflict
            # Supported standalone Workers own one serial delivery. Claim and
            # result writers serialize on this same Worker row.
            active = list(
                session.scalars(
                    select(ExecutionAttempt)
                    .where(
                        ExecutionAttempt.worker_id == worker.id,
                        ExecutionAttempt.status.in_(ACTIVE_ATTEMPT_STATUSES),
                    )
                    .limit(2)
                )
            )
            if len(active) != 1:
                raise _Conflict
            attempt = active[0]
            if (
                attempt.worker_identity_id != worker.identity_id
                or attempt.worker_instance_id != worker.instance_id
                or attempt.worker_instance_generation != worker.instance_generation
                or worker.instance_id is None
                or worker.instance_generation is None
            ):
                raise _Conflict
            worker.scheduling_state = "disabled"
            worker.capacity_state = "reconciling"
            worker.maintenance_version += 1
            session.add(
                WorkerMaintenanceOperation(
                    request_id=payload.request_id,
                    worker_id=worker_id,
                    expected_version=payload.expected_version,
                    result_version=worker.maintenance_version,
                    action="force_stop",
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    reason=reason,
                    recovery_binding=worker_availability.attempt_reference(attempt),
                )
            )
        elif (
            existing.worker_id != worker_id
            or existing.action != "force_stop"
            or existing.expected_version != payload.expected_version
            or existing.actor_kind != actor_kind
            or existing.actor_id != actor_id
            or existing.reason != reason
            or worker.maintenance_version != existing.result_version
            or worker.scheduling_state != "disabled"
        ):
            raise _Conflict
        session.flush()
        result = {
            "worker_id": worker_id,
            "scheduling_state": worker.scheduling_state,
            "maintenance_version": worker.maintenance_version,
            "unsettled_attempts": _unsettled_attempts(session, worker),
        }
        fence.check()
        process.check_authority()
        session.commit()
        process.check_authority()
        return result
    except _Conflict:
        with suppress(Exception):
            session.rollback()
        raise HTTPException(
            status_code=409, detail={"code": "worker_maintenance_conflict"}, headers=_HEADERS
        ) from None
    except Exception as error:
        with suppress(Exception):
            session.rollback()
        logger.warning("worker force-stop unavailable: type=%s", type(error).__name__)
        raise HTTPException(
            status_code=503, detail={"code": "worker_maintenance_unknown"}, headers=_HEADERS
        ) from None


@router.post("/api/workers/{worker_id}/v4/activate")
def activate(
    request: Request,
    worker_id: int,
    payload: ActivationRequest,
    session: DbSession,
    principal: Admin,
) -> dict[str, object]:
    try:
        if type(worker_id) is not int or not 0 < worker_id < 2**63 or payload.request_id.int == 0:
            raise _Conflict
        actor_kind, actor_id = _actor(principal)
        process = _process(request)
        fence = process.worker_fence(session)
        worker = session.get(Worker, worker_id, with_for_update=True, populate_existing=True)
        if worker is None:
            raise _Conflict
        existing = session.get(WorkerMaintenanceOperation, payload.request_id)
        if existing is not None:
            if (
                existing.worker_id != worker_id
                or existing.expected_version != payload.expected_version
                or existing.action != "activate"
                or existing.actor_kind != actor_kind
                or existing.actor_id != actor_id
            ):
                raise _Conflict
            version = existing.result_version
            state = worker.scheduling_state
            fence.check()
            session.rollback()
        else:
            now = worker_availability.current_time(session)
            if (
                worker.identity_lifecycle != "reserved"
                or worker.protocol_version != 4
                or worker.identity_enrolled_at is None
                or worker.scheduling_state != "disabled"
                or worker.maintenance_version != payload.expected_version
                or worker.capacity_state != "ready"
                or worker.isolation_preflight_status != "passed"
                or not valid_capabilities(worker.capabilities)
                or not isolation_capabilities_ready(worker.isolation_capabilities)
                or not worker_availability.is_effectively_online(worker, now=now)
                or worker_availability.has_unreconciled_v4_attempt(session, worker, now=now)
            ):
                raise _Conflict
            version = worker.maintenance_version + 1
            worker.scheduling_state = "active"
            state = "active"
            worker.maintenance_version = version
            from dlr.control.services.worker_capacity import lock_capacity, signal_capacity_change

            capacity = lock_capacity(session, worker_id)
            if capacity is not None:
                signal_capacity_change(capacity, now=now)
            session.add(
                WorkerMaintenanceOperation(
                    request_id=payload.request_id,
                    worker_id=worker_id,
                    expected_version=payload.expected_version,
                    result_version=version,
                    action="activate",
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    reason=None,
                )
            )
            session.flush()
            fence.check()
            process.check_authority()
            session.commit()
        process.check_authority()
        return {"worker_id": worker_id, "scheduling_state": state, "maintenance_version": version}
    except HTTPException:
        with suppress(Exception):
            session.rollback()
        raise
    except _Conflict:
        with suppress(Exception):
            session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "worker_activation_unavailable",
                "message": "Worker activation unavailable",
            },
            headers=_HEADERS,
        ) from None
    except Exception as error:  # noqa: BLE001 - commit outcome may be unknown
        with suppress(Exception):
            session.rollback()
        constraint = getattr(
            getattr(getattr(error, "orig", None), "diag", None), "constraint_name", None
        )
        if (
            not isinstance(error, IntegrityError)
            or not isinstance(constraint, str)
            or not re.fullmatch(r"[A-Za-z0-9_]{1,63}", constraint)
        ):
            constraint = "none"
        logger.warning(
            "worker activation failed: type=%s constraint=%s", type(error).__name__, constraint
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "worker_activation_unknown",
                "message": "Worker activation unavailable",
            },
            headers=_HEADERS,
        ) from None
