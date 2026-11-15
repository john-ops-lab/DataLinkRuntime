"""Isolated v4 cleanup receipt authority; not mounted in the application.

A receipt is a trusted Worker's attestation, not remote proof of physical
cleanup. The Worker recovery path must establish its local evidence first.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy.orm import Session

from dlr.control.models import Execution, ExecutionAttempt, Worker
from dlr.control.models.worker_instance_history import WorkerInstanceHistory
from dlr.control.services import worker_availability, worker_credentials
from dlr.control.services.adapter import domain_error
from dlr.control.services.attempt import TERMINAL_ATTEMPT_STATUSES, V4AttemptAuthority
from dlr.control.services.execution_cancellation import lock_execution_attempts
from dlr.control.services.worker_protocol import token_matches


class V4CleanupReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed"]
    attempt_id: StrictInt = Field(gt=0, le=2**63 - 1)
    fencing_token: StrictInt = Field(gt=0, le=2**63 - 1)
    worker_identity_id: UUID
    original_instance_id: UUID
    original_instance_generation: StrictInt = Field(gt=0, le=2**63 - 1)


class V4HistoricalAttempt(BaseModel):
    """An exact old Attempt reference; it does not assert physical cleanup."""

    model_config = ConfigDict(extra="forbid")

    attempt_id: StrictInt = Field(gt=0, le=2**63 - 1)
    fencing_token: StrictInt = Field(gt=0, le=2**63 - 1)
    worker_identity_id: UUID
    original_instance_id: UUID
    original_instance_generation: StrictInt = Field(gt=0, le=2**63 - 1)


def _authenticate_current(
    session: Session, worker_id: int, authority: V4AttemptAuthority
) -> Worker:
    try:
        worker = worker_credentials.authenticate_runtime_credential(
            session, worker_id, authority.credential
        )
    except worker_credentials.WorkerCredentialError:
        raise domain_error(
            401, "worker_credential_invalid", "Worker credential is invalid"
        ) from None
    now = worker_availability.current_time(session)
    try:
        worker_credentials._confirm_runtime_credential_at_locked(
            session, worker_id, authority.credential, now
        )
    except worker_credentials.WorkerCredentialError:
        raise domain_error(
            401, "worker_credential_invalid", "Worker credential is invalid"
        ) from None
    if (
        worker.protocol_version != 4
        or worker.instance_id != authority.instance_id
        or worker.instance_generation != authority.instance_generation
        or worker.instance_lease_until is None
        or worker.instance_lease_until <= now
    ):
        raise domain_error(409, "worker_instance_expired", "Worker instance is no longer valid")
    return worker


def _lock_historical_attempt(
    session: Session,
    execution_id: int,
    worker_id: int,
    authority: V4AttemptAuthority,
    receipt: V4CleanupReceipt | V4HistoricalAttempt,
    cleanup_token: str,
) -> tuple[Execution, ExecutionAttempt]:
    """Lock and authenticate a terminal historical Attempt without changing it."""
    execution = session.get(Execution, execution_id, with_for_update=True, populate_existing=True)
    if execution is None:
        raise domain_error(422, "execution_cleanup_token_invalid", "Cleanup authorization invalid")
    attempts = lock_execution_attempts(session, execution_id)
    attempt = next((value for value in attempts if value.id == receipt.attempt_id), None)
    if attempt is None:
        raise domain_error(422, "execution_cleanup_token_invalid", "Cleanup authorization invalid")
    worker = _authenticate_current(session, worker_id, authority)
    current_history = session.get(WorkerInstanceHistory, (worker_id, authority.instance_id))
    original_history = session.get(WorkerInstanceHistory, (worker_id, receipt.original_instance_id))
    if (
        current_history is None
        or current_history.generation != authority.instance_generation
        or original_history is None
        or original_history.generation != receipt.original_instance_generation
        or receipt.original_instance_generation > authority.instance_generation
        or worker.identity_id != receipt.worker_identity_id
        or attempt.worker_id != worker_id
        or attempt.worker_identity_id != receipt.worker_identity_id
        or attempt.worker_instance_id != receipt.original_instance_id
        or attempt.worker_instance_generation != receipt.original_instance_generation
        or attempt.fencing_token != receipt.fencing_token
        or not token_matches(cleanup_token, attempt.cleanup_token_hash)
        or token_matches(cleanup_token, attempt.claim_token_hash)
    ):
        raise domain_error(422, "execution_cleanup_token_invalid", "Cleanup authorization invalid")
    if attempt.status not in TERMINAL_ATTEMPT_STATUSES:
        raise domain_error(422, "workspace_cleanup_transition_invalid", "Attempt is not terminal")
    # History reads and validation can wait; confirm current authorization.
    _authenticate_current(session, worker_id, authority)
    return execution, attempt


def _lock_receipt(
    session: Session,
    execution_id: int,
    worker_id: int,
    authority: V4AttemptAuthority,
    receipt: V4CleanupReceipt,
    cleanup_token: str,
) -> tuple[Execution, ExecutionAttempt]:
    """Lock and authenticate the exact historical cleanup responsibility."""
    execution, attempt = _lock_historical_attempt(
        session, execution_id, worker_id, authority, receipt, cleanup_token
    )
    summary = dict(attempt.cleanup_summary or {})
    old_status = summary.get("workspace_cleanup_status")
    if old_status not in {"deferred", "completed"}:
        raise domain_error(422, "workspace_cleanup_transition_invalid", "Cleanup state is invalid")
    if attempt.attempt_no == execution.attempt_count and execution.workspace_cleanup_status not in {
        "deferred",
        "completed",
    }:
        raise domain_error(422, "workspace_cleanup_transition_invalid", "Cleanup state is invalid")
    # Both the credential and instance lease must still be current immediately
    # before the receipt takes effect.
    _authenticate_current(session, worker_id, authority)
    return execution, attempt


def _mark_cleanup_completed(execution: Execution, attempt: ExecutionAttempt) -> bool:
    """Update only the exact old cleanup fact; caller holds locks and owns commit."""
    summary = dict(attempt.cleanup_summary or {})
    old_status = summary.get("workspace_cleanup_status")
    if old_status == "completed":
        if (
            attempt.attempt_no == execution.attempt_count
            and execution.workspace_cleanup_status != "completed"
        ):
            raise domain_error(
                422, "workspace_cleanup_transition_invalid", "Cleanup state is invalid"
            )
        return False
    summary["workspace_cleanup_status"] = "completed"
    summary.pop("workspace_cleanup_error_code", None)
    attempt.cleanup_summary = summary
    if attempt.attempt_no == execution.attempt_count:
        execution.workspace_cleanup_status = "completed"
        execution.workspace_cleanup_error_code = None
    return True


def apply_receipt(
    session: Session,
    execution_id: int,
    worker_id: int,
    authority: V4AttemptAuthority,
    receipt: V4CleanupReceipt,
    cleanup_token: str,
) -> None:
    """Legacy isolated experiment; it is not signed-proof or commit authority."""
    execution, attempt = _lock_receipt(
        session, execution_id, worker_id, authority, receipt, cleanup_token
    )
    if not _mark_cleanup_completed(execution, attempt):
        session.rollback()
        return
    session.commit()
