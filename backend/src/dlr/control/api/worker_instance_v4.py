"""Remote Worker registration and proof-bearing instance renewal."""

import json
from contextlib import suppress
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.models import Worker
from dlr.control.schemas.worker import REQUIRED_ISOLATION_CAPABILITIES
from dlr.control.security import TOKEN_ENTRY_MODE, _bearer_token, require_entry
from dlr.control.services import worker_availability
from dlr.control.services import worker_lease_candidates_v4 as candidates
from dlr.control.services.control_process_v4 import ControlProcess
from dlr.control.services.worker_token_fence_v4 import TokenFenceUnavailable

router = APIRouter()
DbSession = Annotated[Session, Depends(db.get_session)]
_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class _ReadyReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    isolation_capabilities: dict[str, StrictBool]


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate field")
        value[key] = item
    return value


def _constant(_value: str) -> None:
    raise ValueError("invalid number")


def _error(status: int) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": "worker_lease_unavailable", "message": "Worker lease unavailable"},
        headers=_HEADERS,
    )


def _one_header(request: Request, name: str) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1:
        raise _error(401)
    return values[0]


def _identity(request: Request, worker_id: int) -> tuple[str, UUID]:
    require_entry(request, TOKEN_ENTRY_MODE)
    if type(worker_id) is not int or not 0 < worker_id < 2**63:
        raise _error(401)
    credential = _bearer_token(_one_header(request, "authorization"))
    raw_instance = _one_header(request, "x-dlr-worker-instance")
    try:
        instance = UUID(raw_instance)
        if credential is None or str(instance) != raw_instance or instance.int == 0:
            raise ValueError
    except ValueError:
        raise _error(401) from None
    return credential, instance


def _process(request: Request) -> ControlProcess:
    process = getattr(request.app.state, "control_process_v4", None)
    if not isinstance(process, ControlProcess):
        raise TokenFenceUnavailable("control authority unavailable")
    return process


def _proved_identity(request: Request, worker_id: int) -> tuple[str, UUID, int, str]:
    credential, instance = _identity(request, worker_id)
    generation_text = _one_header(request, "x-dlr-worker-generation")
    proof = _one_header(request, "x-dlr-worker-lease-proof")
    if (
        not generation_text.isascii()
        or not generation_text.isdecimal()
        or generation_text.startswith("0")
        or len(generation_text) > 19
        or int(generation_text) >= 2**63
    ):
        raise _error(401)
    return credential, instance, int(generation_text), proof


def _rollback(session: Session) -> None:
    with suppress(Exception):
        session.rollback()


@router.post("/api/workers/{worker_id}/v4/proof-register")
def register(request: Request, worker_id: int, session: DbSession) -> JSONResponse:
    credential, instance = _identity(request, worker_id)
    try:
        process = _process(request)
        fence = process.worker_fence(session)
        process.worker_proof_authority(session)
        candidate = candidates.create_initial_candidate(
            session, worker_id=worker_id, credential=credential, instance_id=instance
        )
        worker = session.get(Worker, worker_id)
        if worker is None:
            raise candidates.LeaseCandidateError("lease candidate unavailable")
        identity = worker.identity_id
        fence.check()
        process.check_authority()
        session.commit()
        process.check_authority()
        proof = process.observe_worker_candidate(candidate, credential)
    except candidates.LeaseCandidateError:
        _rollback(session)
        raise _error(409) from None
    except Exception:  # noqa: BLE001 - do not expose secrets or uncertain commit details
        _rollback(session)
        raise _error(503) from None
    return JSONResponse(
        {
            "worker_identity_id": str(identity),
            "instance_id": str(instance),
            "instance_generation": candidate.generation,
            "instance_proof": proof,
        },
        headers=_HEADERS,
    )


@router.post("/api/workers/{worker_id}/v4/proof-heartbeat")
def heartbeat(request: Request, worker_id: int, session: DbSession) -> JSONResponse:
    credential, instance, generation, proof = _proved_identity(request, worker_id)
    try:
        process = _process(request)
        fence = process.worker_fence(session)
        authority = process.worker_proof_authority(session)
        candidate = candidates.stage_renewal_candidate(
            session,
            worker_id=worker_id,
            credential=credential,
            instance_id=instance,
            generation=generation,
            token=proof,
            authority=authority,
        )
        fence.check()
        process.check_authority()
        session.commit()
        process.check_authority()
        renewed_proof = process.observe_worker_candidate(candidate, credential)
    except candidates.LeaseCandidateError:
        _rollback(session)
        raise _error(409) from None
    except Exception:  # noqa: BLE001 - do not expose secrets or uncertain commit details
        _rollback(session)
        raise _error(503) from None
    return JSONResponse({"instance_proof": renewed_proof}, headers=_HEADERS)


@router.post("/api/workers/{worker_id}/v4/ready")
async def ready(request: Request, worker_id: int, session: DbSession) -> JSONResponse:
    """Observe a trusted fixed Worker's local preflight and empty startup state.

    Administrator scheduling intent remains independent of this report.
    """
    credential, instance, generation, proof = _proved_identity(request, worker_id)
    raw = bytearray()
    try:
        if request.url.query:
            raise ValueError
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 4096:
                raise ValueError
            raw.extend(chunk)
        report = _ReadyReport.model_validate(
            json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
        )
        flags = report.isolation_capabilities
        if set(flags) != REQUIRED_ISOLATION_CAPABILITIES or not all(flags.values()):
            raise ValueError
    except (ValueError, UnicodeError, RecursionError, ValidationError):
        raise _error(422) from None

    def commit_and_observe() -> None:
        process = _process(request)

        def verify() -> Worker:
            authority = process.worker_proof_authority(session)
            candidates.validate_current_proof(
                session,
                worker_id=worker_id,
                credential=credential,
                instance_id=instance,
                generation=generation,
                token=proof,
                authority=authority,
            )
            worker = session.get(Worker, worker_id, populate_existing=True)
            now = worker_availability.current_time(session)
            if (
                worker is None
                or worker.identity_lifecycle != "reserved"
                or worker.instance_id != instance
                or worker.instance_generation != generation
                or worker_availability.has_unreconciled_v4_attempt(session, worker, now=now)
            ):
                raise ValueError("Worker readiness unavailable")
            return worker

        fence = process.worker_fence(session)
        worker = verify()
        worker.capabilities = ["python"]
        worker.isolation_capabilities = dict(flags)
        worker.isolation_preflight_status = "passed"
        worker.isolation_preflight_at = worker_availability.current_time(session)
        worker.capacity_state = "ready"
        session.flush()
        verify()
        fence.check()
        process.check_authority()
        session.commit()
        process.check_authority()
        # A successful COMMIT response alone does not establish that it was
        # visible while the original instance proof remained valid.
        fence = process.worker_fence(session)
        worker = verify()
        if (
            worker.capacity_state != "ready"
            or worker.capabilities != ["python"]
            or worker.isolation_capabilities != flags
            or worker.isolation_preflight_status != "passed"
        ):
            raise ValueError("Worker readiness unavailable")
        fence.check()
        process.check_authority()
        session.rollback()
        process.check_authority()

    try:
        await run_in_threadpool(commit_and_observe)
    except Exception:  # noqa: BLE001 - a failed commit or observation remains unknown
        _rollback(session)
        raise _error(503) from None
    return JSONResponse(
        {"capacity_state": "ready", "instance_generation": generation}, headers=_HEADERS
    )


@router.post("/api/workers/{worker_id}/v4/proof-offline", status_code=204)
def offline(request: Request, worker_id: int, session: DbSession) -> Response:
    credential, instance, generation, proof = _proved_identity(request, worker_id)
    try:
        process = _process(request)
        fence = process.worker_fence(session)
        authority = process.worker_proof_authority(session)
        candidates.retire_instance(
            session,
            worker_id=worker_id,
            credential=credential,
            instance_id=instance,
            generation=generation,
            token=proof,
            authority=authority,
        )
        fence.check()
        process.check_authority()
        session.commit()
        process.check_authority()
    except candidates.LeaseCandidateError:
        _rollback(session)
        raise _error(409) from None
    except Exception:  # noqa: BLE001 - a failed commit may have taken effect
        _rollback(session)
        raise _error(503) from None
    return Response(status_code=204, headers=_HEADERS)
