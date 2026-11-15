"""Bounded result transport for a fixed v4 Worker."""

from __future__ import annotations

import asyncio
import re
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from dlr.common.result_intent_proof_wire_v4 import verify_result_intent_proof_signature
from dlr.common.result_receipt_proof_v4 import verify_result_receipt
from dlr.control.api.worker_claim_v4 import _coordinator
from dlr.control.api.worker_instance_v4 import _HEADERS, _one_header, _process, _proved_identity
from dlr.control.services.claim_child_v4 import _clock
from dlr.control.services.worker_lease_proof_v4 import public_key_bytes

router = APIRouter()


def _error(status: int) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": "worker_result_unavailable", "message": "Worker result unavailable"},
        headers=_HEADERS,
    )


def _uuid_header(request: Request, name: str) -> UUID:
    try:
        value = _one_header(request, name)
        parsed = UUID(value)
        if parsed.int == 0 or str(parsed) != value:
            raise ValueError
        return parsed
    except ValueError:
        raise _error(422) from None


async def _exchange(
    request: Request, worker_id: int, attempt_id: int, operation: str
) -> JSONResponse:
    credential, instance, generation, instance_proof = _proved_identity(request, worker_id)
    attempt_proof = _one_header(request, "x-dlr-worker-attempt-proof")
    nonce = _uuid_header(request, "x-dlr-result-observation-nonce")
    lengths = request.headers.getlist("content-length")
    content_types = request.headers.getlist("content-type")
    if (
        type(attempt_id) is not int
        or not 0 < attempt_id < 2**63
        or operation not in {"stage", "finalize"}
        or request.url.query
        or len(lengths) != 1
        or re.fullmatch(r"[1-9][0-9]{0,18}", lengths[0]) is None
        or len(content_types) != 1
        or content_types[0].split(";", 1)[0].strip().lower() != "application/json"
        or request.headers.getlist("transfer-encoding")
    ):
        raise _error(422)
    metadata: dict[str, object] = {
        "operation": operation,
        "worker_id": worker_id,
        "attempt_id": attempt_id,
        "credential": credential,
        "instance_id": str(instance),
        "generation": generation,
        "instance_proof": instance_proof,
        "attempt_proof": attempt_proof,
        "observation_nonce": str(nonce),
    }
    if operation == "stage":
        if any(
            request.headers.getlist(name)
            for name in ("x-dlr-result-intent", "x-dlr-result-proof", "x-dlr-result-receipt-nonce")
        ):
            raise _error(422)
    else:
        intent = _uuid_header(request, "x-dlr-result-intent")
        receipt_nonce = _uuid_header(request, "x-dlr-result-receipt-nonce")
        if receipt_nonce == nonce:
            raise _error(422)
        metadata.update(
            intent_id=str(intent),
            receipt_nonce=str(receipt_nonce),
            result_proof=_one_header(request, "x-dlr-result-proof"),
        )
    coordinator = _coordinator(request)
    process = _process(request)
    entry = None
    stage_confirmed = False
    try:
        entry = await run_in_threadpool(
            coordinator.begin_result, process, metadata=metadata, body_bytes=int(lengths[0])
        )
        async with asyncio.timeout(max(0.0, entry.deadline - _clock())):
            async for chunk in request.stream():
                for offset in range(0, len(chunk), 65_536):
                    await run_in_threadpool(
                        coordinator.send_result, entry, chunk[offset : offset + 65_536]
                    )
            response = await run_in_threadpool(coordinator.finish_result, entry)
        signing_key = process._deployment.worker_proof_signing_key
        if type(signing_key) is not bytes:
            raise ValueError("result authority unavailable")
        public_key = public_key_bytes(signing_key)
        if operation == "stage":
            if set(response) != {"intent_id", "result_proof"}:
                raise ValueError("result unavailable")
            returned_intent = UUID(str(response["intent_id"]))
            intent_token = response["result_proof"]
        else:
            returned_intent = intent
            intent_token = metadata["result_proof"]
        proof = verify_result_intent_proof_signature(intent_token, public_key)
        if (
            proof is None
            or proof.intent_id != returned_intent
            or proof.observation_nonce != nonce
            or proof.worker_id != worker_id
            or proof.attempt_id != attempt_id
            or proof.instance_id != instance
            or proof.generation != generation
            or proof.cluster_id != process._deployment.authority.cluster_id
            or proof.authority_epoch != process._deployment.authority_epoch
        ):
            raise ValueError("result unavailable")
        if operation == "finalize" and (
            set(response) != {"result_receipt"}
            or verify_result_receipt(
                response["result_receipt"],
                public_key=public_key,
                intent_token=intent_token,
                observation_nonce=receipt_nonce,
            )
            is None
        ):
            raise ValueError("result unavailable")
        process.check_authority()
        result = JSONResponse(response, headers=_HEADERS)
        stage_confirmed = operation == "stage"
        return result
    except Exception:  # noqa: BLE001 - body and transaction outcome may be private or unknown
        raise _error(503) from None
    finally:
        if entry is not None and not stage_confirmed:
            await run_in_threadpool(coordinator.end_result, entry)


@router.post("/api/workers/{worker_id}/v4/attempts/{attempt_id}/result/stage")
async def stage(request: Request, worker_id: int, attempt_id: int) -> JSONResponse:
    return await _exchange(request, worker_id, attempt_id, "stage")


@router.post("/api/workers/{worker_id}/v4/attempts/{attempt_id}/result/finalize")
async def finalize(request: Request, worker_id: int, attempt_id: int) -> JSONResponse:
    return await _exchange(request, worker_id, attempt_id, "finalize")
