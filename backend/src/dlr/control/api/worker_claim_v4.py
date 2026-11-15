"""Two-phase fixed Worker Claim with bounded Control-side preparation."""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from dlr.control.api.worker_instance_v4 import _HEADERS, _one_header, _process, _proved_identity
from dlr.control.services.claim_coordinator_v4 import ClaimCoordinator
from dlr.control.services.dispatch import deserialize_dispatch_message

router = APIRouter()
_MAX_DISPATCH_BYTES = 16_384


def _error(status: int) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": "worker_claim_unavailable", "message": "Worker Claim unavailable"},
        headers=_HEADERS,
    )


def _coordinator(request: Request) -> ClaimCoordinator:
    value = getattr(request.app.state, "claim_coordinator_v4", None)
    if not isinstance(value, ClaimCoordinator):
        raise _error(503)
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate field")
        value[key] = item
    return value


def _invalid_constant(_value: str) -> None:
    raise ValueError("invalid constant")


@router.post("/api/workers/{worker_id}/v4/claim/prepare")
async def prepare(request: Request, worker_id: int) -> StreamingResponse:
    credential, instance, generation, instance_proof = _proved_identity(request, worker_id)
    raw_budget = _one_header(request, "x-dlr-worker-claim-wire-bytes")
    if (
        re.fullmatch(r"[1-9][0-9]{0,18}", raw_budget) is None
        or not 4096 <= int(raw_budget) < 2**63
        or any(key in request.query_params for key in ("claim_token", "proposal", "instance_proof"))
    ):
        raise _error(422)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _MAX_DISPATCH_BYTES:
            raise _error(422)
        raw.extend(chunk)
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        message = deserialize_dispatch_message(value)
        if message.target_worker_id != worker_id:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _error(422) from None
    coordinator = _coordinator(request)
    try:
        entry = await run_in_threadpool(
            coordinator.prepare,
            _process(request),
            worker_id=worker_id,
            instance_id=instance,
            generation=generation,
            credential=credential,
            instance_proof=instance_proof,
            message=message,
            wire_budget=int(raw_budget),
        )
    except Exception:  # noqa: BLE001 - child and transaction status may be unknown
        raise _error(503) from None
    headers = {**_HEADERS, "Content-Length": str(entry.size)}
    if entry.decision == "EXECUTE":
        headers["X-DLR-Claim-Proposal"] = entry.token
    return StreamingResponse(
        coordinator.stream(entry), media_type="application/json", headers=headers
    )


@router.post("/api/workers/{worker_id}/v4/claim/finalize")
async def finalize(request: Request, worker_id: int) -> JSONResponse:
    credential, instance, generation, instance_proof = _proved_identity(request, worker_id)
    token = _one_header(request, "x-dlr-claim-proposal")
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None or any(
        key in request.query_params for key in ("claim_token", "proposal", "instance_proof")
    ):
        raise _error(422)
    async for chunk in request.stream():
        if chunk:
            raise _error(422)
    try:
        proof = await run_in_threadpool(
            _coordinator(request).finalize,
            token=token,
            worker_id=worker_id,
            instance_id=instance,
            generation=generation,
            credential=credential,
            instance_proof=instance_proof,
        )
    except Exception:  # noqa: BLE001 - final commit may already have completed
        raise _error(503) from None
    return JSONResponse({"attempt_proof": proof}, headers=_HEADERS)
