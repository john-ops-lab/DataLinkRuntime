"""Bind supervised host recovery to existing immutable maintenance facts.

The container reference and evidence digest are trusted operator assertions,
not a remotely verified physical-stop proof. Worker-local checks are separate.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from sqlalchemy import BigInteger, literal_column, select, text
from sqlalchemy.orm import Session

from dlr.control.models import (
    AdapterExecutionSlot,
    AttemptResultIntentV4,
    Execution,
    ExecutionAttempt,
    Worker,
    WorkerLeaseCandidateV4,
    WorkerMaintenanceOperation,
    WorkerResourceReservation,
)
from dlr.control.security import Principal
from dlr.control.services import worker_availability
from dlr.control.services.attempt import TERMINAL_ATTEMPT_STATUSES
from dlr.control.services.execution_cancellation import (
    lock_execution_attempts,
    lock_execution_in_admission_order,
)
from dlr.control.services.worker_cleanup_v4 import V4HistoricalAttempt
from dlr.control.services.worker_identity_reservation import _actor
from dlr.control.services.worker_lease_candidates_v4 import _committed_xid_matches, _xid

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


class StoppedSettlement(BaseModel):
    """Explicit preservation of unknown business outcome, never a result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stopped_sha256: Sha
    result_intent_id: UUID
    body_mac: Sha
    attempt_status: Literal["worker_lost", "cancelled"]
    execution_status: Literal["dead_letter", "cancelled", "retry_wait"]
    acknowledgement: Literal["result_uncommitted_outcome_unknown"]

    @model_validator(mode="after")
    def allowed_terminal(self) -> StoppedSettlement:
        if self.result_intent_id.int == 0 or (
            self.attempt_status == "cancelled" and self.execution_status != "cancelled"
        ):
            raise ValueError("recovery terminal combination invalid")
        return self


class ForceStoppedSettlement(BaseModel):
    """An original supervised force-stop with no committed result intent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stopped_sha256: Sha
    force_stop_operation_id: UUID
    attempt_status: Literal["worker_lost", "cancelled"]
    execution_status: Literal["dead_letter", "cancelled", "retry_wait"]
    acknowledgement: Literal["force_stop_no_result_outcome_unknown"]

    @model_validator(mode="after")
    def allowed_terminal(self) -> ForceStoppedSettlement:
        if self.force_stop_operation_id.int == 0 or (
            self.attempt_status == "cancelled" and self.execution_status != "cancelled"
        ):
            raise ValueError("force-stop terminal combination invalid")
        return self


RecoveryMode = Literal[
    "committed_result", "settle_stopped_uncommitted", "settle_force_stopped_no_result"
]
Settlement = StoppedSettlement | ForceStoppedSettlement
_SETTLEMENT_MODE: dict[RecoveryMode, type[Settlement]] = {
    "settle_stopped_uncommitted": StoppedSettlement,
    "settle_force_stopped_no_result": ForceStoppedSettlement,
}


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID
    expected_version: StrictInt = Field(ge=1, lt=2**63 - 1)
    binding: RecoveryBinding
    mode: RecoveryMode = "committed_result"
    settlement: Settlement | None = None

    @model_validator(mode="after")
    def explicit_mode(self) -> RecoveryRequest:
        if type(self.settlement) is not _SETTLEMENT_MODE.get(self.mode, type(None)):
            raise ValueError("recovery mode invalid")
        return self


class RecoveryLookup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID
    binding: RecoveryBinding
    result_intent_id: UUID | None = None
    body_mac: Sha | None = None
    mode: RecoveryMode = "committed_result"
    disposition_id: UUID | None = None
    settlement: Settlement | None = None

    @model_validator(mode="after")
    def explicit_mode(self) -> RecoveryLookup:
        if (self.mode != "committed_result") != (self.disposition_id is not None):
            raise ValueError("recovery mode invalid")
        if self.disposition_id is not None and self.disposition_id.int == 0:
            raise ValueError("recovery disposition invalid")
        expects_result = self.mode != "settle_force_stopped_no_result"
        if (
            type(self.settlement) is not _SETTLEMENT_MODE.get(self.mode, type(None))
            or expects_result != (self.result_intent_id is not None)
            or expects_result != (self.body_mac is not None)
        ):
            raise ValueError("recovery settlement invalid")
        return self

    @field_validator("operation_id", "result_intent_id")
    @classmethod
    def nonzero_uuid(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("recovery reference invalid")
        return value


class RecoveryUnavailable(Exception):
    pass


def operation_response(row: WorkerMaintenanceOperation) -> dict[str, object]:
    response: dict[str, object] = {
        "request_id": str(row.request_id),
        "worker_id": row.worker_id,
        "maintenance_version": row.result_version,
        "binding": row.recovery_binding,
        "host_evidence": "operator_attestation",
    }
    if row.recovery_disposition is not None:
        forced = row.recovery_disposition["kind"] == "force_stopped_no_result"
        settlement_type = ForceStoppedSettlement if forced else StoppedSettlement
        response.update(
            mode="settle_force_stopped_no_result" if forced else "settle_stopped_uncommitted",
            disposition_id=str(row.recovery_disposition_id),
            settlement=settlement_type.model_validate(
                {key: row.recovery_disposition[key] for key in settlement_type.model_fields}
            ).model_dump(mode="json"),
        )
    return response


def _lock_business(
    session: Session, execution_id: int
) -> tuple[Execution, tuple[ExecutionAttempt, ...], AdapterExecutionSlot]:
    # Bound waits without turning a timeout into evidence of absence.
    session.execute(text("SET LOCAL lock_timeout = '3s'"))
    execution = lock_execution_in_admission_order(session, execution_id)
    if execution is None:
        raise RecoveryUnavailable
    attempts = lock_execution_attempts(session, execution_id)
    slot = session.get(
        AdapterExecutionSlot,
        (execution.adapter_id, 0),
        with_for_update=True,
        populate_existing=True,
    )
    if slot is None:
        raise RecoveryUnavailable
    from dlr.control.services.worker_reservation import lock_pool

    lock_pool(session, execution)
    return execution, attempts, slot


def _result_row(session: Session, attempt_id: int) -> AttemptResultIntentV4 | None:
    return session.scalar(
        select(AttemptResultIntentV4)
        .where(AttemptResultIntentV4.attempt_id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _uncommitted_result(
    row: AttemptResultIntentV4 | None,
    binding: RecoveryBinding,
    worker_id: int,
    intent_id: UUID,
    body_mac: str,
) -> None:
    ref = binding.attempt
    if (
        row is None
        or row.worker_id != worker_id
        or row.worker_identity_id != ref.worker_identity_id
        or row.instance_id != ref.original_instance_id
        or row.instance_generation != ref.original_instance_generation
        or row.fencing_token != ref.fencing_token
        or row.intent_id != intent_id
        or not hmac.compare_digest(row.body_mac, bytes.fromhex(body_mac))
        or any(
            value is not None
            for value in (
                row.finalization_id,
                row.finalization_xid,
                row.final_attempt_status,
                row.final_execution_status,
            )
        )
    ):
        raise RecoveryUnavailable


def _force_without_result(
    session: Session,
    *,
    worker_id: int,
    expected_version: int,
    binding: RecoveryBinding,
    settlement: ForceStoppedSettlement,
) -> None:
    original = session.get(
        WorkerMaintenanceOperation, settlement.force_stop_operation_id, populate_existing=True
    )
    if (
        original is None
        or original.action != "force_stop"
        or original.worker_id != worker_id
        or original.result_version != expected_version
        or original.recovery_binding != binding.attempt.model_dump(mode="json")
        or _result_row(session, binding.attempt.attempt_id) is not None
    ):
        raise RecoveryUnavailable


def _terminal_responsibility(
    execution: Execution,
    attempts: tuple[ExecutionAttempt, ...],
    slot: AdapterExecutionSlot,
    binding: RecoveryBinding,
    attempt_status: str,
    execution_status: str,
    *,
    completed: bool,
) -> ExecutionAttempt:
    ref = binding.attempt
    if not attempts:
        raise RecoveryUnavailable
    attempt = attempts[-1]
    if (
        attempt.id != ref.attempt_id
        or attempt.attempt_no != execution.attempt_count
        or any(a.id != attempt.id and a.cleanup_settled_at is None for a in attempts)
        or (attempt.status, execution.status)
        not in {
            ("worker_lost", "dead_letter"),
            ("worker_lost", "cancelled"),
            ("cancelled", "cancelled"),
        }
        or (attempt.status, execution.status) != (attempt_status, execution_status)
        or (execution.status == "dead_letter" and execution.cancel_requested)
        or slot.fencing_token != ref.fencing_token
        or (not completed and slot.active_attempt_id != attempt.id)
        or (completed and slot.active_attempt_id is not None)
    ):
        raise RecoveryUnavailable
    return attempt


def _disposition_attempt(
    session: Session,
    execution: Execution,
    attempts: tuple[ExecutionAttempt, ...],
    slot: AdapterExecutionSlot,
    binding: RecoveryBinding,
    settlement: Settlement,
    *,
    completed: bool,
) -> tuple[ExecutionAttempt, int]:
    if settlement.execution_status != "retry_wait":
        return (
            _terminal_responsibility(
                execution,
                attempts,
                slot,
                binding,
                settlement.attempt_status,
                settlement.execution_status,
                completed=completed,
            ),
            execution.dispatch_generation,
        )
    # After Pool transfer, the current Execution and Slot may belong to a
    # later Claim. The old Attempt's reservation is the independent authority.
    ref = binding.attempt
    attempt = next((item for item in attempts if item.id == ref.attempt_id), None)
    reservation = session.scalar(
        select(WorkerResourceReservation)
        .where(WorkerResourceReservation.attempt_id == ref.attempt_id)
        .execution_options(populate_existing=True)
    )
    if (
        not execution.requires_worker_reservation
        or execution.route_pool_id is None
        or attempt is None
        or attempt.status != "worker_lost"
        or attempt.recovery_not_before is None
        or reservation is None
        or reservation.execution_id != execution.id
        or reservation.worker_id != attempt.worker_id
        or reservation.worker_identity_id != ref.worker_identity_id
        or reservation.instance_id != ref.original_instance_id
        or reservation.instance_generation != ref.original_instance_generation
        or reservation.fencing_token != ref.fencing_token
        or reservation.status != ("released" if completed else "reclaiming")
        or (attempt.cleanup_settled_at is not None) != completed
        or (
            reservation.dispatch_generation >= execution.dispatch_generation
            and (
                reservation.dispatch_generation != execution.dispatch_generation
                or execution.status != "retry_wait"
            )
        )
        or (
            slot.active_attempt_id == attempt.id
            and (completed or slot.fencing_token != attempt.fencing_token)
        )
    ):
        raise RecoveryUnavailable
    return attempt, reservation.dispatch_generation


def request_recovery(
    session: Session, worker_id: int, payload: RecoveryRequest, principal: Principal
) -> dict[str, object]:
    """The caller holds a Control/Token fence and owns commit or rollback."""
    if type(worker_id) is not int or not 0 < worker_id < 2**63 or payload.request_id.int == 0:
        raise RecoveryUnavailable
    actor_kind, actor_id = _actor(principal)
    execution, attempts, slot = _lock_business(session, payload.binding.execution_id)
    worker = session.get(Worker, worker_id, with_for_update=True, populate_existing=True)
    if worker is None or worker.identity_lifecycle != "reserved" or worker.protocol_version != 4:
        raise RecoveryUnavailable
    binding = payload.binding.model_dump(mode="json")
    existing = session.get(
        WorkerMaintenanceOperation,
        payload.request_id,
        with_for_update=True,
        populate_existing=True,
    )
    if existing is not None:
        saved = existing.recovery_disposition
        if (
            existing.worker_id != worker_id
            or existing.action != "reconcile"
            or existing.expected_version != payload.expected_version
            or existing.actor_kind != actor_kind
            or existing.actor_id != actor_id
            or existing.recovery_binding != binding
            or (saved is None) != (payload.mode == "committed_result")
            or (
                payload.settlement is not None
                and (
                    saved is None
                    or any(
                        saved.get(key) != value
                        for key, value in payload.settlement.model_dump(mode="json").items()
                    )
                )
            )
        ):
            raise RecoveryUnavailable
        return operation_response(existing)
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
    candidate = session.get(
        WorkerLeaseCandidateV4,
        worker_id,
        with_for_update=True,
        populate_existing=True,
    )
    if (
        worker.instance_lease_until is None
        or worker.instance_lease_until > now
        or candidate is None
        or candidate.instance_id != ref.original_instance_id
        or candidate.instance_generation != ref.original_instance_generation
        or max(candidate.nominal_until, candidate.guard_until, candidate.proof_before) > now
    ):
        raise RecoveryUnavailable
    attempt = next((a for a in attempts if a.id == ref.attempt_id), None)
    if (
        attempt is None
        or attempt.worker_id != worker_id
        or attempt.worker_identity_id != worker.identity_id
        or attempt.worker_instance_id != ref.original_instance_id
        or attempt.worker_instance_generation != ref.original_instance_generation
        or attempt.fencing_token != ref.fencing_token
        or attempt.status not in TERMINAL_ATTEMPT_STATUSES
    ):
        raise RecoveryUnavailable
    operation = WorkerMaintenanceOperation(
        request_id=payload.request_id,
        worker_id=worker_id,
        expected_version=payload.expected_version,
        result_version=worker.maintenance_version + 1,
        action="reconcile",
        actor_kind=actor_kind,
        actor_id=actor_id,
        reason=None,
        recovery_binding=binding,
    )
    # The new audit is inserted once, with its final immutable snapshot.
    # Existing request IDs were locked above; result is the final lock suffix.
    if payload.settlement is not None:
        settlement = payload.settlement
        _, old_generation = _disposition_attempt(
            session,
            execution,
            attempts,
            slot,
            payload.binding,
            settlement,
            completed=False,
        )
        if attempt.cleanup_settled_at is not None:
            raise RecoveryUnavailable
        if isinstance(settlement, ForceStoppedSettlement):
            _force_without_result(
                session,
                worker_id=worker_id,
                expected_version=payload.expected_version,
                binding=payload.binding,
                settlement=settlement,
            )
            kind = "force_stopped_no_result"
        else:
            _uncommitted_result(
                _result_row(session, ref.attempt_id),
                payload.binding,
                worker_id,
                settlement.result_intent_id,
                settlement.body_mac,
            )
            kind = "platform_terminal"
        operation.recovery_disposition_id = uuid4()
        operation.recovery_disposition_xid = _xid(session)
        operation.recovery_disposition = {
            **settlement.model_dump(mode="json"),
            "kind": kind,
            "reason": settlement.acknowledgement,
            "dispatch_generation": old_generation,
            "attempt_no": attempt.attempt_no,
        }
    session.add(operation)
    worker.maintenance_version += 1
    worker.capacity_state = "reconciling"
    session.flush()
    return operation_response(operation)


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
    row = session.get(
        WorkerMaintenanceOperation, operation_id, with_for_update=True, populate_existing=True
    )
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

    operation = require_operation(
        session,
        worker_id=worker_id,
        operation_id=lookup.operation_id,
        binding=lookup.binding,
        instance_id=instance_id,
        generation=generation,
    )
    if (
        lookup.mode != "committed_result"
        or operation.recovery_disposition is not None
        or lookup.body_mac is None
    ):
        raise RecoveryUnavailable
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


def require_disposition(
    session: Session,
    lookup: RecoveryLookup,
    *,
    worker_id: int,
    instance_id: UUID,
    generation: int,
) -> tuple[WorkerMaintenanceOperation, AttemptResultIntentV4 | None]:
    """Only a later transaction may authorize the immutable terminal disposition."""
    operation = require_operation(
        session,
        worker_id=worker_id,
        operation_id=lookup.operation_id,
        binding=lookup.binding,
        instance_id=instance_id,
        generation=generation,
    )
    snapshot = operation.recovery_disposition
    if (
        lookup.mode not in _SETTLEMENT_MODE
        or snapshot is None
        or operation.recovery_disposition_id != lookup.disposition_id
        or operation.recovery_disposition_xid is None
    ):
        raise RecoveryUnavailable
    tuple_xid = session.scalar(
        select(
            literal_column("worker_maintenance_operations.xmin::text::bigint", type_=BigInteger())
        )
        .select_from(WorkerMaintenanceOperation)
        .where(WorkerMaintenanceOperation.request_id == operation.request_id)
    )
    if not _committed_xid_matches(_xid(session), operation.recovery_disposition_xid, tuple_xid):
        raise RecoveryUnavailable
    settlement = lookup.settlement
    if settlement is None or any(
        snapshot.get(key) != value for key, value in settlement.model_dump(mode="json").items()
    ):
        raise RecoveryUnavailable
    execution = session.get(Execution, lookup.binding.execution_id, populate_existing=True)
    if execution is None:
        raise RecoveryUnavailable
    attempts = tuple(
        session.scalars(
            select(ExecutionAttempt)
            .where(ExecutionAttempt.execution_id == execution.id)
            .order_by(ExecutionAttempt.id)
            .execution_options(populate_existing=True)
        )
    )
    slot = session.get(AdapterExecutionSlot, (execution.adapter_id, 0), populate_existing=True)
    if slot is None or not attempts:
        raise RecoveryUnavailable
    exact = next((item for item in attempts if item.id == lookup.binding.attempt.attempt_id), None)
    if exact is None:
        raise RecoveryUnavailable
    attempt, old_generation = _disposition_attempt(
        session,
        execution,
        attempts,
        slot,
        lookup.binding,
        settlement,
        completed=exact.cleanup_settled_at is not None,
    )
    if (
        attempt.attempt_no != snapshot["attempt_no"]
        or old_generation != snapshot["dispatch_generation"]
    ):
        raise RecoveryUnavailable
    if isinstance(settlement, ForceStoppedSettlement):
        if snapshot["kind"] != "force_stopped_no_result":
            raise RecoveryUnavailable
        _force_without_result(
            session,
            worker_id=worker_id,
            expected_version=operation.expected_version,
            binding=lookup.binding,
            settlement=settlement,
        )
        return operation, None
    result = _result_row(session, attempt.id)
    if (
        result is None
        or snapshot["kind"] != "platform_terminal"
        or settlement.result_intent_id != lookup.result_intent_id
        or settlement.body_mac != lookup.body_mac
    ):
        raise RecoveryUnavailable
    _uncommitted_result(
        result,
        lookup.binding,
        worker_id,
        lookup.result_intent_id,
        lookup.body_mac,
    )
    return operation, result


def require_recovery_fact(
    session: Session,
    lookup: RecoveryLookup,
    *,
    worker_id: int,
    instance_id: UUID,
    generation: int,
) -> tuple[WorkerMaintenanceOperation | None, AttemptResultIntentV4 | None]:
    if lookup.mode != "committed_result":
        return require_disposition(
            session,
            lookup,
            worker_id=worker_id,
            instance_id=instance_id,
            generation=generation,
        )
    return None, require_committed_result(
        session,
        lookup,
        worker_id=worker_id,
        instance_id=instance_id,
        generation=generation,
    )
