"""Opt-in business batches use the existing custody, execution and Slot authority.

Every function joins the caller's transaction. Source I/O, sleeps and commits
are deliberately absent. Member facts are canonical only after strict finish.
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, defer

from dlr.control.models import (
    ActivationCredentialReference,
    AdapterVersion,
    EventDelivery,
    EventReceipt,
    Execution,
    ExecutionAttempt,
    SourceCheckpoint,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionExecutionBatch,
    SubscriptionExecutionBatchMember,
)
from dlr.control.services import subscription_accounting as accounting
from dlr.control.services.adapter import domain_error
from dlr.control.services.input_config import database_now
from dlr.control.services.subscription_batch_protocol import (
    BATCH_MEMBER_CODES,
    INPUT_SCHEMA,
    MAX_INPUT_BYTES,
    MAX_MEMBERS,
    MAX_RESULT_BYTES,
    MEMBER_RESULT_BYTES,
    RESULT_HEADER_BYTES,
    BatchFinishDecision,
    control_decision,
    digest,
    encode,
    input_frame,
    singleton_reserve,
    validate_result,
)

BATCH_MODE = "business_batch"


def activation_mode(session: Session, activation: SubscriptionActivation) -> str:
    config = session.get(SubscriptionConfigVersion, activation.config_version_id)
    if config is None:
        raise domain_error(
            409, "subscription_config_invalid", "Activation configuration is missing"
        )
    mode = str(config.config.get("mode", "per_event"))
    if mode == BATCH_MODE and config.config.get("source_type") not in {"http", "rabbitmq"}:
        raise domain_error(
            409, "batch_source_unsupported", "Business batch is unavailable for this source"
        )
    return mode


def mode_for_delivery(session: Session, receipt: EventReceipt, delivery: EventDelivery) -> str:
    activation = session.get(
        SubscriptionActivation, delivery.activation_override_id or receipt.activation_id
    )
    if activation is None:
        raise domain_error(409, "subscription_activation_missing", "Activation is unavailable")
    return activation_mode(session, activation)


def require_revision_schema(session: Session, revision_id: int) -> None:
    revision = session.get(AdapterVersion, revision_id)
    if (
        revision is None
        or (revision.runtime_config or {}).get("event_batch_schema") != INPUT_SCHEMA
    ):
        raise domain_error(
            409,
            "subscription_batch_code_required",
            "Saved code must explicitly support batch input",
        )


def effective_execution_id(delivery: EventDelivery) -> int | None:
    return delivery.execution_id or delivery.batch_execution_id


def effective_execution_expr() -> Any:
    return func.coalesce(EventDelivery.execution_id, EventDelivery.batch_execution_id)


def receipt_group_protected(receipt_id: Any) -> Any:
    return exists().where(SubscriptionExecutionBatchMember.receipt_id == receipt_id)


def execution_protected(execution_id: Any) -> Any:
    return exists().where(SubscriptionExecutionBatch.execution_id == execution_id)


def recompute_blocker(session: Session, subscription_id: int, checkpoint: SourceCheckpoint) -> None:
    from dlr.control.services.subscription_delivery import TERMINAL

    session.flush()
    checkpoint.blocked_receipt_id = session.scalar(
        select(EventReceipt.id)
        .where(
            EventReceipt.subscription_id == subscription_id,
            EventReceipt.source_epoch == checkpoint.source_epoch,
            EventReceipt.shard == checkpoint.shard,
            EventReceipt.state.not_in(TERMINAL),
        )
        .order_by(EventReceipt.sequence)
        .limit(1)
    )


def materialize_batch(session: Session, head_delivery_id: int) -> Execution | None:
    from dlr.control.services import subscription_delivery as delivery_service
    from dlr.control.services.reliable_execution import accept_execution

    adapter, sub, checkpoint, head_receipt, head = delivery_service._delivery_context(
        session, head_delivery_id
    )
    assigned = effective_execution_id(head)
    if assigned is not None:
        return session.get(Execution, assigned)
    if head.state != "pending" or head_receipt.source_phase is not None:
        return None
    activation = session.get(
        SubscriptionActivation, head.activation_override_id or head_receipt.activation_id
    )
    assert activation is not None
    if activation_mode(session, activation) != BATCH_MODE:
        return None
    revision_id = head.revision_override_id or activation.revision_id
    require_revision_schema(session, revision_id)
    if sub.delivery_state == "repair" and (
        head.revision_override_id != sub.repair_revision_id
        or checkpoint.blocked_receipt_id != head_receipt.id
    ):
        return None
    now = database_now(session)
    if head.retry_after is not None and head.retry_after > now:
        return None
    if session.scalar(
        select(
            exists().where(
                EventReceipt.subscription_id == sub.id,
                EventReceipt.source_epoch == head_receipt.source_epoch,
                EventReceipt.shard == head_receipt.shard,
                EventReceipt.sequence < head_receipt.sequence,
                EventReceipt.state.not_in(delivery_service.TERMINAL),
            )
        )
    ):
        return None
    latest_ordinal = (
        select(func.max(EventDelivery.ordinal))
        .where(EventDelivery.receipt_id == EventReceipt.id)
        .correlate(EventReceipt)
        .scalar_subquery()
    )
    lightweight = (
        session.execute(
            select(
                EventReceipt.id.label("receipt_id"),
                EventReceipt.state.label("receipt_state"),
                EventReceipt.activation_id,
                EventReceipt.source_phase,
                EventDelivery.id.label("delivery_id"),
                EventDelivery.state.label("delivery_state"),
                EventDelivery.activation_override_id,
                EventDelivery.revision_override_id,
                EventDelivery.retry_after,
                EventDelivery.execution_id,
                EventDelivery.batch_execution_id,
                EventDelivery.retained_execution_bytes,
                EventDelivery.retained_batch_bytes,
            )
            .join(
                EventDelivery,
                (EventDelivery.receipt_id == EventReceipt.id)
                & (EventDelivery.ordinal == latest_ordinal),
            )
            .where(
                EventReceipt.subscription_id == sub.id,
                EventReceipt.source_epoch == head_receipt.source_epoch,
                EventReceipt.shard == head_receipt.shard,
                EventReceipt.sequence >= head_receipt.sequence,
                EventReceipt.state.not_in(delivery_service.TERMINAL),
            )
            .order_by(EventReceipt.sequence)
            .limit(MAX_MEMBERS)
            .with_for_update(of=(EventReceipt, EventDelivery))
        )
        .mappings()
        .all()
    )
    selected_ids: list[int] = []
    prepaid_total = 0
    for row in lightweight:
        if (
            row["receipt_state"] != "pending"
            or row["delivery_state"] != "pending"
            or row["execution_id"] is not None
            or row["batch_execution_id"] is not None
            or row["source_phase"] is not None
        ):
            break
        if (row["activation_override_id"] or row["activation_id"], row["revision_override_id"]) != (
            activation.id,
            head.revision_override_id,
        ) or (row["retry_after"] is not None and row["retry_after"] > now):
            break
        if (
            row["retained_execution_bytes"] <= 0
            or row["retained_batch_bytes"] != accounting.BATCH_METADATA_BYTES
        ):
            raise domain_error(
                409, "batch_budget_missing", "Complete member budget was not prepaid"
            )
        # Each prepaid singleton contains a complete input frame. Their sum
        # conservatively bounds the combined frame before any suffix body is
        # loaded. Never filter across the first incompatible or blocked row.
        if prepaid_total + row["retained_execution_bytes"] > MAX_INPUT_BYTES:
            break
        prepaid_total += row["retained_execution_bytes"]
        selected_ids.append(row["delivery_id"])
    candidates = (
        session.execute(
            select(EventReceipt, EventDelivery)
            .options(defer(EventReceipt.source_raw_body))
            .join(EventDelivery, EventDelivery.receipt_id == EventReceipt.id)
            .where(EventDelivery.id.in_(selected_ids))
            .order_by(EventReceipt.sequence)
        ).all()
        if selected_ids
        else []
    )
    config = session.get(SubscriptionConfigVersion, activation.config_version_id)
    assert config is not None
    metadata = {
        "id": str(sub.id),
        "config_version": config.version,
        "source_epoch": head_receipt.source_epoch,
    }
    batch_id = uuid.uuid4().hex
    fixed_size = len(encode(input_frame(batch_id, "f" * 64, metadata, [])))
    members: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    accepted: list[tuple[EventReceipt, EventDelivery]] = []
    size = fixed_size
    for receipt, item in candidates:
        if (
            receipt.state != "pending"
            or item.state != "pending"
            or effective_execution_id(item) is not None
        ):
            break
        if (item.activation_override_id or receipt.activation_id, item.revision_override_id) != (
            activation.id,
            head.revision_override_id,
        ) or (item.retry_after is not None and item.retry_after > now):
            break
        if receipt.envelope is None:
            raise domain_error(409, "event_payload_expired", "Event payload expired")
        if (
            item.retained_execution_bytes < singleton_reserve(receipt.envelope)
            or item.retained_batch_bytes < accounting.BATCH_METADATA_BYTES
        ):
            raise domain_error(
                409,
                "batch_budget_missing",
                "Complete batch input and member budget were not prepaid",
            )
        index = len(members)
        identity = {
            "receipt_id": receipt.id,
            "delivery_id": item.id,
            "delivery_ordinal": item.ordinal,
            "member_index": index,
        }
        input_member = identity | {"input": deepcopy(receipt.envelope)}
        candidate_size = size + len(encode(input_member)) + (1 if members else 0)
        if candidate_size > MAX_INPUT_BYTES:
            break
        manifest.append(
            identity
            | {"content_hash": receipt.content_hash, "envelope_hash": digest(receipt.envelope)}
        )
        members.append(input_member)
        accepted.append((receipt, item))
        size = candidate_size
    if not accepted:
        raise domain_error(
            409, "batch_input_too_large", "The pending head cannot fit in one business batch"
        )
    manifest_hash = digest(manifest)
    frame = input_frame(batch_id, manifest_hash, metadata, members)
    assert len(encode(frame)) == size
    retained_input = sum(item.retained_execution_bytes for _, item in accepted)
    retained_batch = sum(item.retained_batch_bytes for _, item in accepted)
    result_limit = min(MAX_RESULT_BYTES, RESULT_HEADER_BYTES + MEMBER_RESULT_BYTES * len(members))
    structure_upper = 4096 + len(encode(manifest)) + len(members) * 512 + 2 * result_limit
    if retained_input < size or retained_batch < structure_upper:
        raise domain_error(
            409,
            "batch_budget_missing",
            "Retained batch shares do not cover its complete representation",
        )
    refs = list(
        session.scalars(
            select(ActivationCredentialReference).where(
                ActivationCredentialReference.activation_id == activation.id
            )
        )
    )
    quotas = delivery_service._quotas(session, sub.id)
    try:
        with session.begin_nested():
            execution = accept_execution(
                session,
                adapter,
                trigger="subscription",
                runtime_input=frame,
                input_source_type="json",
                input_config_revision=1,
                input_snapshot={"source_type": "json", "revision": None},
                version_id=revision_id,
                worker_id_override=activation.worker_id,
                subscription_activation_id=activation.id,
                subscription_delivery_kind="batch",
                credential_bindings_override=[
                    {
                        "binding_id": row.binding_id,
                        "credential_id": row.credential_id,
                        "env_key": row.env_key,
                        "field": row.field,
                    }
                    for row in refs
                ],
            )
            batch = SubscriptionExecutionBatch(
                execution_id=execution.id,
                batch_id=batch_id,
                subscription_id=sub.id,
                source_epoch=head_receipt.source_epoch,
                shard=head_receipt.shard,
                activation_id=activation.id,
                revision_id=revision_id,
                revision_override_id=head.revision_override_id,
                manifest=manifest,
                manifest_hash=manifest_hash,
                member_count=len(members),
                input_bytes=size,
                retained_input_bytes=retained_input,
                retained_batch_bytes=retained_batch,
            )
            session.add(batch)
            for index, (receipt, item) in enumerate(accepted):
                item.batch_execution_id = execution.id
                item.state = receipt.state = "processing"
                session.add(
                    SubscriptionExecutionBatchMember(
                        delivery_id=item.id,
                        receipt_id=receipt.id,
                        execution_id=execution.id,
                        member_index=index,
                    )
                )
                for quota in quotas:
                    quota.pending_count -= 1
                    quota.pending_bytes -= receipt.payload_bytes
            checkpoint.blocked_receipt_id = head_receipt.id
            session.flush()
    except HTTPException as error:
        if error.status_code not in {429, 503}:
            raise
        head.retry_after = now + timedelta(seconds=5)
        return None
    delivery_service.refresh_delivery_state(session, sub)
    return execution


def validate_finish(
    session: Session,
    execution: Execution,
    raw_output: Any,
    normalized_output: Any,
    worker_truncated: bool,
    control_truncated: bool,
    proposed_state: str,
) -> BatchFinishDecision | None:
    with session.no_autoflush:
        batch = session.get(SubscriptionExecutionBatch, execution.id)
    if batch is None or proposed_state in {"queued", "running", "retry_wait"}:
        return None
    if proposed_state == "succeeded":
        return validate_result(
            batch.batch_id,
            batch.manifest_hash,
            batch.manifest,
            raw_output,
            normalized_output,
            worker_truncated=worker_truncated,
            control_truncated=control_truncated,
        )
    code = (
        execution.error_code if execution.error_code in BATCH_MEMBER_CODES else "result_unavailable"
    )
    return control_decision(batch.batch_id, batch.manifest_hash, batch.manifest, code)


def persist_finish(
    session: Session, execution: Execution, attempt_id: int | None, decision: BatchFinishDecision
) -> None:
    with session.no_autoflush:
        batch = session.get(SubscriptionExecutionBatch, execution.id, with_for_update=True)
        if batch is None:
            return
        if attempt_id is not None:
            attempt = session.get(ExecutionAttempt, attempt_id)
            if attempt is None or attempt.execution_id != execution.id:
                raise domain_error(
                    409, "batch_attempt_invalid", "Batch result belongs to a different Attempt"
                )
        elif decision.provenance == "worker_validated":
            raise domain_error(
                409, "batch_attempt_invalid", "Worker result requires an authoritative Attempt"
            )
    if batch.result is not None:
        if batch.result != decision.result or batch.result_attempt_id != attempt_id:
            raise domain_error(
                409, "batch_result_conflict", "Final member facts cannot be replaced"
            )
        return
    batch.result = deepcopy(decision.result)
    batch.result_counts = dict(decision.counts)
    batch.result_provenance = decision.provenance
    batch.result_attempt_id = attempt_id
    batch.finalized_at = database_now(session)


def finalize_on_terminal(
    session: Session,
    execution: Execution,
    attempt_id: int | None = None,
    *,
    not_started: bool = False,
) -> BatchFinishDecision | None:
    with session.no_autoflush:
        batch = session.get(SubscriptionExecutionBatch, execution.id, with_for_update=True)
        if (
            batch is None
            or batch.result is not None
            or execution.status not in {"succeeded", "dead_letter", "cancelled", "expired"}
        ):
            return None
        if execution.status == "succeeded":
            raise domain_error(
                409,
                "batch_result_missing",
                "Successful business batch requires strict member validation",
            )
        if not_started and session.scalar(
            select(exists().where(ExecutionAttempt.execution_id == execution.id))
        ):
            not_started = False
    code = (
        execution.error_code if execution.error_code in BATCH_MEMBER_CODES else "result_unavailable"
    )
    decision = control_decision(
        batch.batch_id, batch.manifest_hash, batch.manifest, code, not_started=not_started
    )
    persist_finish(session, execution, attempt_id, decision)
    execution.output = deepcopy(decision.output)
    execution.output_size = len(encode(decision.output))
    execution.output_truncated = False
    execution.output_preview = None
    return decision


def settle_batch(session: Session, execution_id: int) -> int:
    from dlr.control.services import subscription_delivery as delivery_service

    identity = session.scalar(
        select(SubscriptionExecutionBatch.subscription_id).where(
            SubscriptionExecutionBatch.execution_id == execution_id
        )
    )
    if identity is None:
        return 0
    _, sub = delivery_service.lock_subscription(session, identity)
    batch = session.get(SubscriptionExecutionBatch, execution_id, with_for_update=True)
    assert batch is not None
    execution = session.get(Execution, execution_id)
    if execution is None or execution.status not in delivery_service.EXECUTION_TERMINAL:
        return 0
    if batch.result is None:
        finalize_on_terminal(session, execution)
    assert batch.result is not None
    checkpoint = session.scalar(
        select(SourceCheckpoint)
        .where(
            SourceCheckpoint.subscription_id == sub.id,
            SourceCheckpoint.source_epoch == batch.source_epoch,
            SourceCheckpoint.shard == batch.shard,
        )
        .with_for_update()
    )
    assert checkpoint is not None
    members = session.execute(
        select(SubscriptionExecutionBatchMember, EventReceipt, EventDelivery)
        .options(defer(EventReceipt.source_raw_body))
        .join(EventReceipt, EventReceipt.id == SubscriptionExecutionBatchMember.receipt_id)
        .join(EventDelivery, EventDelivery.id == SubscriptionExecutionBatchMember.delivery_id)
        .where(SubscriptionExecutionBatchMember.execution_id == execution_id)
        .order_by(SubscriptionExecutionBatchMember.member_index)
        .with_for_update(of=(EventReceipt, EventDelivery))
    ).all()
    now = database_now(session)
    changed = 0
    for member, receipt, item in members:
        if item.state != "processing":
            continue
        outcome = batch.result["members"][member.member_index]
        item.batch_member_outcome = outcome["status"]
        item.batch_member_code = outcome["error_code"]
        item.completed_at = now
        if outcome["status"] == "succeeded":
            item.state = receipt.state = "succeeded"
            receipt.terminal_at = now
        else:
            item.state = receipt.state = "failed"
            receipt.terminal_at = None
        changed += 1
    recompute_blocker(session, sub.id, checkpoint)
    delivery_service.refresh_delivery_state(session, sub)
    return changed


def member_snapshot(session: Session, delivery: EventDelivery) -> dict[str, Any]:
    if delivery.batch_execution_id is None:
        return {}
    member = session.get(SubscriptionExecutionBatchMember, delivery.id)
    batch = session.get(SubscriptionExecutionBatch, delivery.batch_execution_id)
    if member is None or batch is None:
        return {}
    return {
        "batch_execution_id": batch.execution_id,
        "batch_member_index": member.member_index,
        "batch_member_count": batch.member_count,
        "batch_member_outcome": delivery.batch_member_outcome,
        "batch_member_code": delivery.batch_member_code,
    }


from dlr.control.services.subscription_batch_retention import (  # noqa: E402
    cleanup_batches,
    purge_batches,
)

__all__ = [
    "activation_mode",
    "cleanup_batches",
    "effective_execution_expr",
    "effective_execution_id",
    "execution_protected",
    "finalize_on_terminal",
    "materialize_batch",
    "member_snapshot",
    "mode_for_delivery",
    "persist_finish",
    "purge_batches",
    "receipt_group_protected",
    "recompute_blocker",
    "require_revision_schema",
    "validate_finish",
]
