"""Read committed historical result/cleanup without reviving old authority."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.recovery_observation_v4 import RecoveryClaims, sign_observation
from dlr.control.models import (
    AdapterExecutionSlot,
    Execution,
    ExecutionAttempt,
    WorkerCleanupIntentV4,
)
from dlr.control.services import worker_cleanup_intent_v4 as cleanup
from dlr.control.services import worker_recovery_v4
from dlr.control.services.attempt_result_finalize_v4 import ResultControlFence, _control_authority
from dlr.control.services.worker_lease_proof_v4 import _timestamp


def observe(
    factory: sessionmaker[Session],
    context: cleanup.CleanupIntentContext,
    *,
    nonce: UUID,
    control: ResultControlFence,
) -> str:
    """A fresh proof covers stable IDs and the exact frozen result MAC only."""
    try:
        frozen = cleanup._frozen(context)
        lookup = frozen.recovery
        if lookup is None or type(nonce) is not UUID or nonce.int == 0:
            raise ValueError
        with factory() as session:
            gate, authority = _control_authority(session, control)
            frozen = replace(frozen, authority=authority)
            # Recovery _snapshot takes Admission -> Execution -> Attempts ->
            # Slot -> Worker before reading the immutable operation/result.
            cleanup._snapshot(session, frozen)
            result = worker_recovery_v4.require_committed_result(
                session,
                lookup,
                worker_id=frozen.worker_id,
                instance_id=frozen.instance_id,
                generation=frozen.generation,
            )
            execution = session.get(Execution, frozen.execution_id)
            attempt = session.get(ExecutionAttempt, frozen.receipt.attempt_id)
            if execution is None or attempt is None or result.finalization_id is None:
                raise ValueError
            slot = session.get(AdapterExecutionSlot, (execution.adapter_id, 0))
            row = session.scalar(
                select(WorkerCleanupIntentV4)
                .where(WorkerCleanupIntentV4.attempt_id == frozen.receipt.attempt_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            completion = None
            if row is not None:
                cleanup._already_committed(session, row)
                if (
                    row.worker_id != frozen.worker_id
                    or row.worker_identity_id != frozen.receipt.worker_identity_id
                    or row.original_instance_id != frozen.receipt.original_instance_id
                    or row.original_generation != frozen.receipt.original_instance_generation
                    or row.fencing_token != frozen.receipt.fencing_token
                ):
                    raise ValueError
                completion = row.completion_id
            if completion is None:
                if attempt.cleanup_settled_at is not None or (
                    row is not None and row.completion_xid is not None
                ):
                    raise ValueError
            elif (
                completion.int == 0
                or row is None
                or row.completion_xid != row.writer_xid
                or attempt.cleanup_settled_at is None
                or slot is None
                or slot.active_attempt_id == attempt.id
                or not isinstance(attempt.cleanup_summary, dict)
                or attempt.cleanup_summary.get("workspace_cleanup_status") != "completed"
                or (
                    attempt.attempt_no == execution.attempt_count
                    and execution.workspace_cleanup_status != "completed"
                )
            ):
                raise ValueError
            candidate, before, now = cleanup._snapshot(session, frozen)
            claims = RecoveryClaims(
                authority.cluster_id,
                authority.authority_epoch,
                frozen.worker_id,
                frozen.receipt.worker_identity_id,
                frozen.execution_id,
                frozen.receipt.attempt_id,
                frozen.receipt.original_instance_id,
                frozen.receipt.original_instance_generation,
                frozen.instance_id,
                frozen.generation,
                frozen.receipt.fencing_token,
                candidate.revision,
                candidate.candidate_id,
                lookup.operation_id,
                bytes.fromhex(lookup.binding.prepared_sha256),
                result.intent_id,
                bytes(result.body_mac),
                result.finalization_id,
                completion,
                nonce,
                attempt.status,
                _timestamp(before),
                _timestamp(now),
            )
            token = sign_observation(claims, authority.key)
            gate.check()
            session.rollback()
            return token
    except Exception:
        raise cleanup.CleanupIntentUnavailable("recovery observation unavailable") from None
