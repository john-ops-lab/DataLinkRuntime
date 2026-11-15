"""Bind supervised host recovery to existing immutable maintenance facts.

The container reference and evidence digest are trusted operator assertions,
not a remotely verified physical-stop proof. Worker-local checks are separate.
"""

from __future__ import annotations

import hmac
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.control.models import (
    AttemptResultIntentV4,
    ExecutionAttempt,
    Worker,
    WorkerLeaseCandidateV4,
    WorkerMaintenanceOperation,
)
from dlr.control.security import Principal
from dlr.control.services import worker_availability
from dlr.control.services.attempt import TERMINAL_ATTEMPT_STATUSES
from dlr.control.services.worker_cleanup_v4 import V4HistoricalAttempt
from dlr.control.services.worker_identity_reservation import _actor

Sha = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]


class RecoveryBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: StrictInt = Field(gt=0, lt=2**63)
    attempt: V4HistoricalAttempt
    old_container_id: Sha
    deployment_record_id: UUID
    evidence_sha256: Sha
    boot_id: UUID
    prepared_sha256: Sha

    @field_validator("deployment_record_id", "boot_id")
    @classmethod
    def nonzero_uuid(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("recovery reference invalid")
        return value


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    expected_version: StrictInt = Field(ge=1, lt=2**63 - 1)
    binding: RecoveryBinding


class RecoveryLookup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID
    binding: RecoveryBinding
    result_intent_id: UUID
    body_mac: Sha

    @field_validator("operation_id", "result_intent_id")
    @classmethod
    def nonzero_uuid(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("recovery reference invalid")
        return value


class RecoveryUnavailable(Exception):
    pass


def request_recovery(
    session: Session, worker_id: int, payload: RecoveryRequest, principal: Principal
) -> dict[str, object]:
    """The caller holds a Control/Token fence and owns commit or rollback."""
    if type(worker_id) is not int or not 0 < worker_id < 2**63 or payload.request_id.int == 0:
        raise RecoveryUnavailable
    actor_kind, actor_id = _actor(principal)
    worker = session.get(Worker, worker_id, with_for_update=True, populate_existing=True)
    if worker is None or worker.identity_lifecycle != "reserved" or worker.protocol_version != 4:
        raise RecoveryUnavailable
    binding = payload.binding.model_dump(mode="json")
    existing = session.get(WorkerMaintenanceOperation, payload.request_id)
    if existing is not None:
        if (
            existing.worker_id != worker_id
            or existing.action != "reconcile"
            or existing.expected_version != payload.expected_version
            or existing.actor_kind != actor_kind
            or existing.actor_id != actor_id
            or existing.recovery_binding != binding
        ):
            raise RecoveryUnavailable
        return {
            "request_id": str(existing.request_id),
            "worker_id": worker_id,
            "maintenance_version": existing.result_version,
            "binding": binding,
            "host_evidence": "operator_attestation",
        }
    ref = payload.binding.attempt
    if (
        worker.maintenance_version != payload.expected_version
        or worker.scheduling_state not in {"draining", "disabled"}
        or worker.identity_id != ref.worker_identity_id
        or worker.instance_id != ref.original_instance_id
        or worker.instance_generation != ref.original_instance_generation
    ):
        raise RecoveryUnavailable
    now = worker_availability.current_time(session)
    candidate = session.get(WorkerLeaseCandidateV4, worker_id, populate_existing=True)
    if (
        worker.instance_lease_until is None
        or worker.instance_lease_until > now
        or candidate is None
        or candidate.instance_id != ref.original_instance_id
        or candidate.instance_generation != ref.original_instance_generation
        or max(candidate.nominal_until, candidate.guard_until, candidate.proof_before) > now
    ):
        raise RecoveryUnavailable
    attempt = session.get(ExecutionAttempt, ref.attempt_id, populate_existing=True)
    if (
        attempt is None
        or attempt.execution_id != payload.binding.execution_id
        or attempt.worker_id != worker_id
        or attempt.worker_identity_id != worker.identity_id
        or attempt.worker_instance_id != ref.original_instance_id
        or attempt.worker_instance_generation != ref.original_instance_generation
        or attempt.fencing_token != ref.fencing_token
        or attempt.status not in TERMINAL_ATTEMPT_STATUSES
    ):
        raise RecoveryUnavailable
    worker.maintenance_version += 1
    worker.capacity_state = "reconciling"
    session.add(
        WorkerMaintenanceOperation(
            request_id=payload.request_id,
            worker_id=worker_id,
            expected_version=payload.expected_version,
            result_version=worker.maintenance_version,
            action="reconcile",
            actor_kind=actor_kind,
            actor_id=actor_id,
            reason=None,
            recovery_binding=binding,
        )
    )
    session.flush()
    return {
        "request_id": str(payload.request_id),
        "worker_id": worker_id,
        "maintenance_version": worker.maintenance_version,
        "binding": binding,
        "host_evidence": "operator_attestation",
    }


def require_operation(
    session: Session,
    *,
    worker_id: int,
    operation_id: UUID,
    binding: RecoveryBinding,
    instance_id: UUID,
    generation: int,
) -> WorkerMaintenanceOperation:
    """Authenticate current proof separately before this maintenance lookup."""
    worker = session.get(Worker, worker_id, populate_existing=True)
    row = session.get(WorkerMaintenanceOperation, operation_id)
    if (
        worker is None
        or row is None
        or row.worker_id != worker_id
        or row.action != "reconcile"
        or row.recovery_binding != binding.model_dump(mode="json")
        or row.result_version != worker.maintenance_version
        or worker.scheduling_state not in {"draining", "disabled"}
        or worker.capacity_state != "reconciling"
        or worker.instance_id != instance_id
        or worker.instance_generation != generation
        or binding.attempt.worker_identity_id != worker.identity_id
        or binding.attempt.original_instance_id == instance_id
        or binding.attempt.original_instance_generation >= generation
    ):
        raise RecoveryUnavailable
    return row


def require_committed_result(
    session: Session, lookup: RecoveryLookup, *, worker_id: int, instance_id: UUID, generation: int
) -> AttemptResultIntentV4:
    """Use only after the historical Attempt/current-proof locks are held."""
    from dlr.control.services.attempt_result_intent_v4 import _already_committed

    require_operation(
        session,
        worker_id=worker_id,
        operation_id=lookup.operation_id,
        binding=lookup.binding,
        instance_id=instance_id,
        generation=generation,
    )
    ref = lookup.binding.attempt
    row = session.scalar(
        select(AttemptResultIntentV4)
        .where(AttemptResultIntentV4.attempt_id == ref.attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    attempt = session.get(ExecutionAttempt, ref.attempt_id, populate_existing=True)
    if (
        row is None
        or attempt is None
        or attempt.execution_id != lookup.binding.execution_id
        or row.worker_id != worker_id
        or row.worker_identity_id != ref.worker_identity_id
        or row.instance_id != ref.original_instance_id
        or row.instance_generation != ref.original_instance_generation
        or row.fencing_token != ref.fencing_token
        or row.intent_id != lookup.result_intent_id
        or not hmac.compare_digest(row.body_mac, bytes.fromhex(lookup.body_mac))
        or row.finalization_id is None
        or row.finalization_id.int == 0
        or row.finalization_xid != row.writer_xid
        or row.final_attempt_status != attempt.status
        or attempt.status not in {"succeeded", "failed", "timed_out"}
        or row.final_execution_status is None
    ):
        raise RecoveryUnavailable
    _already_committed(session, row)
    return row
