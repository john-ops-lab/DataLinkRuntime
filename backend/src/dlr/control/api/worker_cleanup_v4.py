"""Bounded, proof-fenced cleanup settlement for one fixed Worker."""

from __future__ import annotations

import json
import re
from contextlib import suppress
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from dlr.control import db
from dlr.control.api.worker_attempt_authority_v4 import _invalid_constant, _unique_pairs
from dlr.control.api.worker_instance_v4 import _HEADERS, _one_header, _process, _proved_identity
from dlr.control.services import worker_cleanup_intent_v4 as service
from dlr.control.services.attempt_result_finalize_v4 import ResultControlFence
from dlr.control.services.worker_cleanup_v4 import V4CleanupReceipt
from dlr.control.services.worker_lease_candidates_v4 import ProofAuthority

router = APIRouter()
_MAX_BODY = 4096


def _error(status: int) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": "worker_cleanup_unavailable", "message": "Worker cleanup unavailable"},
        headers=_HEADERS,
    )


def _uuid_header(request: Request, name: str) -> UUID:
    try:
        raw = _one_header(request, name)
        value = UUID(raw)
        if value.int == 0 or str(value) != raw:
            raise ValueError
        return value
    except ValueError:
        raise _error(422) from None


async def _read_json(request: Request) -> dict[str, object]:
    lengths = request.headers.getlist("content-length")
    types = request.headers.getlist("content-type")
    if (
        len(lengths) != 1
        or re.fullmatch(r"[1-9][0-9]{0,3}", lengths[0]) is None
        or int(lengths[0]) > _MAX_BODY
        or len(types) != 1
        or types[0].split(";", 1)[0].strip().lower() != "application/json"
        or request.headers.getlist("transfer-encoding")
        or request.url.query
    ):
        raise _error(422)
    try:
        body = bytearray()
        async for chunk in request.stream():
            if len(chunk) > int(lengths[0]) - len(body):
                raise ValueError
            body.extend(chunk)
        if len(body) != int(lengths[0]):
            raise ValueError
        value = json.loads(body, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError, ValidationError):
        raise _error(422) from None


async def _receipt(request: Request) -> V4CleanupReceipt:
    try:
        return V4CleanupReceipt.model_validate(await _read_json(request))
    except ValidationError:
        raise _error(422) from None


async def _exchange(
    request: Request, worker_id: int, execution_id: int, operation: str
) -> JSONResponse:
    credential, instance, generation, instance_proof = _proved_identity(request, worker_id)
    if type(execution_id) is not int or not 0 < execution_id < 2**63:
        raise _error(422)
    cleanup_token = _one_header(request, "x-dlr-cleanup-token")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,512}", cleanup_token) is None:
        raise _error(422)
    intent_id = _uuid_header(request, "x-dlr-cleanup-intent")
    nonce = _uuid_header(request, "x-dlr-cleanup-observation-nonce")
    receipt = await _receipt(request)
    proof: str | None = None
    receipt_nonce: UUID | None = None
    if operation == "finalize":
        proof = _one_header(request, "x-dlr-cleanup-proof")
        receipt_nonce = _uuid_header(request, "x-dlr-cleanup-receipt-nonce")
        if nonce == receipt_nonce:
            raise _error(422)
    elif (
        operation != "stage"
        or request.headers.getlist("x-dlr-cleanup-proof")
        or request.headers.getlist("x-dlr-cleanup-receipt-nonce")
    ):
        raise _error(422)

    def execute() -> dict[str, object]:
        process = _process(request)
        process.check_authority()
        deployment = process._deployment
        if type(deployment.worker_proof_signing_key) is not bytes:
            raise ValueError("cleanup authority unavailable")
        authority = ProofAuthority(
            deployment.authority.cluster_id,
            deployment.authority_epoch,
            deployment.worker_proof_signing_key,
        )
        control = ResultControlFence(
            deployment, process._instance, process._token, process.check_authority
        )
        context = service.CleanupIntentContext(
            worker_id,
            execution_id,
            credential,
            instance,
            generation,
            instance_proof,
            receipt,
            cleanup_token,
            authority,
        )
        if operation == "stage":
            service.stage_fenced_cleanup_intent(
                db.SessionLocal, context, intent_id=intent_id, control=control
            )
            token = service.observe_cleanup_intent(
                db.SessionLocal,
                context,
                intent_id=intent_id,
                observation_nonce=nonce,
                control=control,
            )
            process.check_authority()
            return {"cleanup_proof": token}
        assert proof is not None and receipt_nonce is not None
        # The commit response is intentionally inconclusive. A retry may find
        # it already committed; only the separate observer can issue a receipt.
        with suppress(service.CleanupReceiptUnknown):
            service.finalize_cleanup_intent(
                db.SessionLocal,
                context,
                intent_id=intent_id,
                intent_nonce=nonce,
                intent_token=proof,
                control=control,
            )
        token = service.observe_committed_cleanup(
            db.SessionLocal,
            context,
            intent_id=intent_id,
            intent_nonce=nonce,
            intent_token=proof,
            receipt_nonce=receipt_nonce,
            control=control,
        )
        process.check_authority()
        return {"cleanup_receipt": token}

    try:
        return JSONResponse(await run_in_threadpool(execute), headers=_HEADERS)
    except Exception:  # noqa: BLE001 - private request or commit outcome may be unknown
        raise _error(503) from None


@router.post("/api/workers/{worker_id}/v4/executions/{execution_id}/cleanup/stage")
async def stage(request: Request, worker_id: int, execution_id: int) -> JSONResponse:
    return await _exchange(request, worker_id, execution_id, "stage")


@router.post("/api/workers/{worker_id}/v4/executions/{execution_id}/cleanup/finalize")
async def finalize(request: Request, worker_id: int, execution_id: int) -> JSONResponse:
    return await _exchange(request, worker_id, execution_id, "finalize")
