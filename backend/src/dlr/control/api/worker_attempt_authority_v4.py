"""Proof-bound start, progress and renewal for a fixed v4 Worker Attempt.

These endpoints do not admit a Claim or prove the Worker has stopped physical
work. A Worker may launch only after a timely start observation and must stop
when either local proof expires.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.api.worker_instance_v4 import (
    _HEADERS,
    _one_header,
    _process,
    _proved_identity,
    _rollback,
)
from dlr.control.models import Execution, ExecutionAttempt
from dlr.control.schemas.reliable_runtime import AttemptActionBody, AttemptProgressBody
from dlr.control.services import attempt as attempt_service
from dlr.control.services import attempt_lease_candidates_v4 as candidates
from dlr.control.services import worker_availability
from dlr.control.services.claim_coordinator_v4 import ClaimCoordinator
from dlr.control.services.control_process_v4 import ControlProcess

router = APIRouter()
DbSession = Annotated[Session, Depends(db.get_session)]
_MAX_ACTION_BYTES = 4096


def _anticipate_result(request: Request, process: ControlProcess) -> None:
    coordinator = getattr(request.app.state, "claim_coordinator_v4", None)
    if isinstance(coordinator, ClaimCoordinator):
        coordinator.anticipate_result(process)


def _error(status: int) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": "worker_attempt_unavailable", "message": "Worker attempt unavailable"},
        headers=_HEADERS,
    )


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError("invalid constant")


async def _action[T: AttemptActionBody](request: Request, attempt_id: int, model: type[T]) -> T:
    if (
        type(attempt_id) is not int
        or not 0 < attempt_id < 2**63
        or any(
            key in request.query_params
            for key in ("claim_token", "cleanup_token", "attempt_proof", "instance_proof")
        )
    ):
        raise _error(422)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _MAX_ACTION_BYTES:
            raise _error(422)
        raw.extend(chunk)
    try:
        value: Any = json.loads(
            raw, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant
        )
        action = model.model_validate(value)
        if action.attempt_id != attempt_id:
            raise ValueError("attempt mismatch")
        return action
    except (ValueError, UnicodeError, RecursionError, ValidationError):
        # Neither validation details nor request bodies may disclose tokens.
        raise _error(422) from None


def _start(
    request: Request, session: Session, worker_id: int, attempt_id: int, action: AttemptActionBody
) -> JSONResponse:
    credential, instance, generation, instance_proof = _proved_identity(request, worker_id)
    attempt_proof = _one_header(request, "x-dlr-worker-attempt-proof")
    try:
        process = _process(request)

        def verify() -> tuple[Execution, ExecutionAttempt]:
            authority = process.worker_proof_authority(session)
            candidates.validate_current_attempt_proof(
                session,
                worker_id=worker_id,
                attempt_id=attempt_id,
                credential=credential,
                instance_id=instance,
                generation=generation,
                instance_proof=instance_proof,
                attempt_proof=attempt_proof,
                action=action,
                authority=authority,
            )
            attempt = session.get(ExecutionAttempt, attempt_id)
            execution = session.get(Execution, attempt.execution_id) if attempt else None
            if attempt is None or execution is None:
                raise candidates.AttemptLeaseCandidateError("Attempt unavailable")
            return execution, attempt

        fence = process.worker_fence(session)
        execution, attempt = verify()
        if execution.cancel_requested:
            session.rollback()
            return JSONResponse(
                {
                    "decision": "ACK_NOOP",
                    "reason": "cancel_requested",
                    "attempt_id": attempt_id,
                    "cancel_requested": True,
                },
                headers=_HEADERS,
            )
        if attempt.status == "claimed":
            attempt.status = "running"
            attempt.started_at = worker_availability.current_time(session)
            session.flush()
            execution, attempt = verify()
            if execution.cancel_requested or attempt.status != "running":
                raise candidates.AttemptLeaseCandidateError("Attempt unavailable")
            fence.check()
            process.check_authority()
            session.commit()
            process.check_authority()
        elif attempt.status != "running":
            raise candidates.AttemptLeaseCandidateError("Attempt unavailable")
        else:
            session.rollback()

        # A timely commit check is not evidence that COMMIT finished in time.
        # Observe the running state in a new fenced transaction before reply.
        fence = process.worker_fence(session)
        execution, attempt = verify()
        if execution.cancel_requested or attempt.status != "running":
            raise candidates.AttemptLeaseCandidateError("Attempt unavailable")
        fence.check()
        process.check_authority()
        session.rollback()
        process.check_authority()
        _anticipate_result(request, process)
        return JSONResponse(
            {
                "decision": "ACK_NOOP",
                "reason": "started",
                "attempt_id": attempt_id,
                "cancel_requested": False,
            },
            headers=_HEADERS,
        )
    except Exception:  # noqa: BLE001 - commit or observation may be unknown
        _rollback(session)
        raise _error(503) from None


def _renew(
    request: Request, session: Session, worker_id: int, attempt_id: int, action: AttemptActionBody
) -> JSONResponse:
    credential, instance, generation, instance_proof = _proved_identity(request, worker_id)
    attempt_proof = _one_header(request, "x-dlr-worker-attempt-proof")
    try:
        process = _process(request)
        fence = process.worker_fence(session)
        authority = process.worker_proof_authority(session)
        try:
            candidate = candidates.stage_attempt_renewal(
                session,
                worker_id=worker_id,
                attempt_id=attempt_id,
                credential=credential,
                instance_id=instance,
                generation=generation,
                instance_proof=instance_proof,
                attempt_proof=attempt_proof,
                action=action,
                authority=authority,
            )
        except candidates.AttemptForceStopRequested as error:
            fence.check()
            process.check_authority()
            session.rollback()
            return JSONResponse(
                {"stop_requested": True, "operation_id": str(error.operation_id)}, headers=_HEADERS
            )
        fence.check()
        process.check_authority()
        session.commit()
        process.check_authority()
        proof = process.observe_attempt_candidate(candidate, credential, instance_proof, action)
        _anticipate_result(request, process)
        return JSONResponse({"attempt_proof": proof}, headers=_HEADERS)
    except Exception:  # noqa: BLE001 - commit or observation may be unknown
        _rollback(session)
        raise _error(503) from None


def _progress(
    request: Request, session: Session, worker_id: int, attempt_id: int, action: AttemptProgressBody
) -> JSONResponse:
    credential, instance, generation, instance_proof = _proved_identity(request, worker_id)
    attempt_proof = _one_header(request, "x-dlr-worker-attempt-proof")
    try:
        process = _process(request)
        fence = process.worker_fence(session)
        authority = process.worker_proof_authority(session)

        def verify() -> None:
            candidates.validate_current_attempt_proof(
                session,
                worker_id=worker_id,
                attempt_id=attempt_id,
                credential=credential,
                instance_id=instance,
                generation=generation,
                instance_proof=instance_proof,
                attempt_proof=attempt_proof,
                action=action,
                authority=authority,
            )

        verify()
        attempt = session.get(ExecutionAttempt, attempt_id)
        execution = session.get(Execution, attempt.execution_id) if attempt else None
        if attempt is None or execution is None or attempt.status != "running":
            raise candidates.AttemptLeaseCandidateError("Attempt unavailable")
        cancelled = execution.cancel_requested
        if cancelled:
            session.rollback()
        else:
            # Credential-bound work publishes text only via the existing
            # sealed, redacted result path. A progress call may report status.
            if execution.credential_bindings_snapshot and (
                action.stdout_chunk or action.stderr_chunk
            ):
                raise candidates.AttemptLeaseCandidateError("Attempt unavailable")
            attempt_service.record_progress_locked(execution, attempt, action)
            session.flush()
            verify()
            fence.check()
            process.check_authority()
            session.commit()
            process.check_authority()
        return JSONResponse(
            {
                "decision": "ACK_NOOP",
                "reason": "cancel_requested" if cancelled else "progressed",
                "attempt_id": attempt_id,
                "cancel_requested": cancelled,
            },
            headers=_HEADERS,
        )
    except Exception:  # noqa: BLE001 - commit may be unknown
        _rollback(session)
        raise _error(503) from None


@router.post("/api/workers/{worker_id}/v4/attempts/{attempt_id}/start")
async def start(
    request: Request, worker_id: int, attempt_id: int, session: DbSession
) -> JSONResponse:
    action = await _action(request, attempt_id, AttemptActionBody)
    return await run_in_threadpool(_start, request, session, worker_id, attempt_id, action)


@router.post("/api/workers/{worker_id}/v4/attempts/{attempt_id}/renew")
async def renew(
    request: Request, worker_id: int, attempt_id: int, session: DbSession
) -> JSONResponse:
    action = await _action(request, attempt_id, AttemptActionBody)
    return await run_in_threadpool(_renew, request, session, worker_id, attempt_id, action)


@router.post("/api/workers/{worker_id}/v4/attempts/{attempt_id}/progress")
async def progress(
    request: Request, worker_id: int, attempt_id: int, session: DbSession
) -> JSONResponse:
    action = await _action(request, attempt_id, AttemptProgressBody)
    return await run_in_threadpool(_progress, request, session, worker_id, attempt_id, action)
