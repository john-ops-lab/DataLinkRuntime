"""V4 Attempt authentication after the established business lock prefix.

This module is not a router or Claim implementation. The caller must lock the
Adapter/Admission, Execution, all relevant Attempts and Slot 0 in the current
transaction before invoking this function, and must not mutate business facts
before authorization. The existing `_lock_attempt_context` uses that prefix.
The helper then locks Worker and its credentials, in that order, until the
caller commits or rolls back. A `terminal` result is an idempotent read-only
path: callers must not reapply terminal business state, release resources, or
extend leases. This helper does not provide deferred workspace-cleanup access.
"""

from datetime import UTC
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session, object_session

from dlr.control.models import AdapterExecutionSlot, Execution, ExecutionAttempt
from dlr.control.schemas.reliable_runtime import AttemptActionBody
from dlr.control.services import worker_availability, worker_credentials
from dlr.control.services.adapter import domain_error


def validate_v4_attempt_locked(
    session: Session,
    *,
    execution: Execution,
    attempt: ExecutionAttempt,
    slot: AdapterExecutionSlot,
    worker_id: int,
    credential: str,
    instance_id: UUID,
    instance_generation: int,
    action: AttemptActionBody,
) -> Literal["active", "terminal"]:
    """Authenticate the live instance and the exact Attempt under held locks.

    Session membership is checked, but cannot prove the row locks exist: the
    caller owns that precondition and must use the established lock helper.
    This function neither commits nor creates Worker/Attempt state.
    """
    if any(object_session(row) is not session for row in (execution, attempt, slot)):
        raise ValueError("Attempt context must belong to the current Session")
    if (
        type(worker_id) is not int
        or worker_id <= 0
        or not isinstance(instance_id, UUID)
        or type(instance_generation) is not int
        or instance_generation <= 0
    ):
        raise domain_error(409, "worker_instance_invalid", "Worker instance is invalid")
    try:
        worker = worker_credentials.authenticate_runtime_credential(session, worker_id, credential)
    except worker_credentials.WorkerCredentialError:
        raise domain_error(
            401, "worker_credential_invalid", "Worker credential is invalid"
        ) from None
    if type(worker.protocol_version) is not int or worker.protocol_version != 4:
        raise domain_error(409, "worker_protocol_incompatible", "Worker protocol is incompatible")

    # Sample after waiting for both Worker and credential row locks. The DB
    # clock, not a Worker wall clock or transaction-start time, owns expiry.
    now = worker_availability.current_time(session)
    try:
        worker_credentials._confirm_runtime_credential_at_locked(
            session, worker_id, credential, now
        )
    except worker_credentials.WorkerCredentialError:
        raise domain_error(
            401, "worker_credential_invalid", "Worker credential is invalid"
        ) from None
    if (
        worker.instance_id != instance_id
        or worker.instance_generation != instance_generation
        or worker.instance_lease_until is None
        or worker.instance_lease_until.astimezone(UTC) <= now.astimezone(UTC)
    ):
        raise domain_error(409, "worker_instance_expired", "Worker instance is no longer valid")
    if (
        attempt.worker_id != worker_id
        or attempt.worker_identity_id != worker.identity_id
        or attempt.worker_instance_id != instance_id
        or attempt.worker_instance_generation != instance_generation
        or attempt.execution_id != execution.id
        or attempt.adapter_id != execution.adapter_id
        or slot.adapter_id != execution.adapter_id
        or slot.slot_no != 0
    ):
        raise domain_error(409, "worker_attempt_identity_mismatch", "Attempt identity is invalid")

    # Reuse the established exact Token/fence/error contract; delayed import
    # avoids a circular dependency when the eventual caller lives in attempt.py.
    from dlr.control.services.attempt import (
        ACTIVE_ATTEMPT_STATUSES,
        TERMINAL_ATTEMPT_STATUSES,
        _validate_action,
    )

    _validate_action(execution, attempt, action, now)
    if attempt.status in TERMINAL_ATTEMPT_STATUSES:
        return "terminal"
    if (
        attempt.status not in ACTIVE_ATTEMPT_STATUSES
        or execution.status != "running"
        or execution.attempt_count != attempt.attempt_no
        or slot.active_attempt_id != attempt.id
        or slot.fencing_token != attempt.fencing_token
    ):
        raise domain_error(409, "attempt_stale_fence", "Attempt ownership is stale")
    return "active"
