"""Bounded historical observation/cleanup on the existing Worker authority."""

from __future__ import annotations

import re
from contextlib import suppress
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from dlr.control import db
from dlr.control.api.worker_cleanup_v4 import _read_json, _uuid_header
from dlr.control.api.worker_instance_v4 import (
    _HEADERS,
    _identity,
    _one_header,
    _process,
    _proved_identity,
)
from dlr.control.models import WorkerMaintenanceOperation
from dlr.control.services import recovery_observation_v4, worker_availability, worker_credentials
from dlr.control.services import worker_cleanup_intent_v4 as cleanup
from dlr.control.services.attempt_result_finalize_v4 import ResultControlFence
from dlr.control.services.worker_cleanup_v4 import V4CleanupReceipt
from dlr.control.services.worker_lease_candidates_v4 import ProofAuthority
from dlr.control.services.worker_recovery_v4 import RecoveryBinding, RecoveryLookup, Sha

router = APIRouter()


class _IntentRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    prepared_sha256: Sha


@router.post("/api/workers/{worker_id}/v4/recovery-intent/read")
async def read_intent(request: Request, worker_id: int) -> JSONResponse:
    """Pre-registration metadata read, authenticated like initial registration.

    It creates no instance and grants no historical cleanup or business rights.
    A new valid instance proof is mandatory for all subsequent recovery work.
    """
    credential, proposed_instance = _identity(request, worker_id)
    try:
        payload = _IntentRead.model_validate(await _read_json(request))
        if payload.operation_id.int == 0:
            raise ValueError
    except (ValueError, ValidationError):
        raise _error(422) from None

    def execute() -> dict[str, object]:
        process = _process(request)
        with db.SessionLocal() as session:
            fence = process.worker_fence(session)
            worker = worker_credentials.authenticate_runtime_credential(
                session, worker_id, credential
            )
            worker_credentials._confirm_runtime_credential_at_locked(
                session, worker_id, credential, worker_availability.current_time(session)
            )
            operation = session.get(WorkerMaintenanceOperation, payload.operation_id)
            if (
                worker.protocol_version != 4
                or worker.identity_lifecycle != "reserved"
                or worker.scheduling_state not in {"draining", "disabled"}
                or worker.capacity_state != "reconciling"
                or operation is None
                or operation.worker_id != worker_id
                or operation.action != "reconcile"
                or operation.result_version != worker.maintenance_version
            ):
                raise ValueError
            binding = RecoveryBinding.model_validate(operation.recovery_binding)
            if (
                binding.prepared_sha256 != payload.prepared_sha256
                or binding.attempt.worker_identity_id != worker.identity_id
                or binding.attempt.original_instance_id == proposed_instance
            ):
                raise ValueError
            result = {
                "request_id": str(operation.request_id),
                "maintenance_version": operation.result_version,
                "binding": binding.model_dump(mode="json"),
                "host_evidence": "operator_attestation",
            }
            fence.check()
            process.check_authority()
            session.rollback()
            return result

    try:
        return JSONResponse(await run_in_threadpool(execute), headers=_HEADERS)
    except Exception:
        raise _error(503) from None


def _error(status: int) -> HTTPException:
    return HTTPException(
        status_code=status, detail={"code": "worker_recovery_unavailable"}, headers=_HEADERS
    )


async def _exchange(
    request: Request, worker_id: int, execution_id: int, operation: str
) -> JSONResponse:
    credential, instance, generation, instance_proof = _proved_identity(request, worker_id)
    cleanup_token = _one_header(request, "x-dlr-cleanup-token")
    if not 0 < execution_id < 2**63 or re.fullmatch(r"[A-Za-z0-9_-]{1,512}", cleanup_token) is None:
        raise _error(422)
    try:
        lookup = RecoveryLookup.model_validate(await _read_json(request))
        if lookup.binding.execution_id != execution_id:
            raise ValueError
    except (ValueError, ValidationError):
        raise _error(422) from None
    if operation == "observe":
        nonce = _uuid_header(request, "x-dlr-result-recovery-nonce")
        if any(
            request.headers.getlist(name)
            for name in (
                "x-dlr-cleanup-intent",
                "x-dlr-cleanup-observation-nonce",
                "x-dlr-cleanup-proof",
                "x-dlr-cleanup-receipt-nonce",
            )
        ):
            raise _error(422)
        intent_id = proof = receipt_nonce = None
    else:
        if request.headers.getlist("x-dlr-result-recovery-nonce"):
            raise _error(422)
        intent_id = _uuid_header(request, "x-dlr-cleanup-intent")
        nonce = _uuid_header(request, "x-dlr-cleanup-observation-nonce")
        proof = receipt_nonce = None
        if operation == "finalize":
            proof = _one_header(request, "x-dlr-cleanup-proof")
            receipt_nonce = _uuid_header(request, "x-dlr-cleanup-receipt-nonce")
            if receipt_nonce == nonce:
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
            raise ValueError
        authority = ProofAuthority(
            deployment.authority.cluster_id,
            deployment.authority_epoch,
            deployment.worker_proof_signing_key,
        )
        control = ResultControlFence(
            deployment, process._instance, process._token, process.check_authority
        )
        receipt = V4CleanupReceipt(status="completed", **lookup.binding.attempt.model_dump())
        context = cleanup.CleanupIntentContext(
            worker_id,
            execution_id,
            credential,
            instance,
            generation,
            instance_proof,
            receipt,
            cleanup_token,
            authority,
            lookup,
        )
        if operation == "observe":
            token = recovery_observation_v4.observe(
                db.SessionLocal, context, nonce=nonce, control=control
            )
            process.check_authority()
            return {"recovery_proof": token}
        assert intent_id is not None
        if operation == "stage":
            cleanup.stage_fenced_cleanup_intent(
                db.SessionLocal, context, intent_id=intent_id, control=control
            )
            token = cleanup.observe_cleanup_intent(
                db.SessionLocal,
                context,
                intent_id=intent_id,
                observation_nonce=nonce,
                control=control,
            )
            process.check_authority()
            return {"cleanup_proof": token}
        assert proof is not None and receipt_nonce is not None
        with suppress(cleanup.CleanupReceiptUnknown):
            cleanup.finalize_cleanup_intent(
                db.SessionLocal,
                context,
                intent_id=intent_id,
                intent_nonce=nonce,
                intent_token=proof,
                control=control,
            )
        token = cleanup.observe_committed_cleanup(
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
    except Exception:
        raise _error(503) from None


@router.post("/api/workers/{worker_id}/v4/executions/{execution_id}/recovery/observe")
async def observe(request: Request, worker_id: int, execution_id: int) -> JSONResponse:
    return await _exchange(request, worker_id, execution_id, "observe")


@router.post("/api/workers/{worker_id}/v4/executions/{execution_id}/recovery/cleanup/stage")
async def stage(request: Request, worker_id: int, execution_id: int) -> JSONResponse:
    return await _exchange(request, worker_id, execution_id, "stage")


@router.post("/api/workers/{worker_id}/v4/executions/{execution_id}/recovery/cleanup/finalize")
async def finalize(request: Request, worker_id: int, execution_id: int) -> JSONResponse:
    return await _exchange(request, worker_id, execution_id, "finalize")
