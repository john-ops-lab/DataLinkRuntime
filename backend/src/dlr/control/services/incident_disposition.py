"""Transactional manual disposition of persisted infrastructure Incidents."""

from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from dlr.common.jcs import canonicalize
from dlr.control.models import (
    Adapter,
    Execution,
    ExecutionIncidentDisposition,
    ExecutionInfrastructureIncident,
    ExecutionOutbox,
)
from dlr.control.schemas.reliable_runtime import (
    IncidentDispositionBody,
    IncidentDispositionPage,
    IncidentDispositionReceipt,
    IncidentDispositionResponse,
)
from dlr.control.security import Principal
from dlr.control.services import adapter_access, outbox
from dlr.control.services.adapter import domain_error
from dlr.control.services.dispatch import (
    deserialize_dispatch_message,
    serialize_dispatch_message,
    worker_routing_key,
)
from dlr.control.services.execution_cancellation import (
    ExecutionLockTail,
    lock_execution_in_admission_order,
    lock_execution_tail,
)
from dlr.control.services.incident_materials import (
    RecoveryMaterialProof,
    close_material_guards,
    preflight_recovery_materials,
    validate_recovery_materials,
)
from dlr.control.services.input_config import database_now


@dataclass(frozen=True)
class DispositionIdempotency:
    """Canonical request identity and an optional existing durable receipt."""

    key: uuid.UUID
    request_hash: str
    receipt: ExecutionIncidentDisposition | None


@dataclass(frozen=True)
class IncidentDispositionResult:
    """Committed response plus the HTTP status selected by the stable outcome."""

    response: IncidentDispositionResponse
    status_code: int


@dataclass(frozen=True)
class DispatchReplacementPlan:
    generation: int
    message_id: uuid.UUID
    routing_key: str
    payload_json: dict[str, object]
    payload_bytes: int


@dataclass(frozen=True)
class IncidentActionCapabilities:
    recover_available: bool
    recover_reason: str | None
    terminate_available: bool
    terminate_reason: str | None


_SETTLEMENT_CODES = frozenset({"execution_cancelled", "execution_expired", "execution_deleted"})
_REJECTION_STATUS = {
    "outbox_backlog_full": 503,
    "cancellation_requested": 202,
}


def list_dispositions(
    session: Session,
    *,
    execution_id: int,
    incident_id: int,
    before_id: uuid.UUID | None,
    limit: int,
) -> IncidentDispositionPage:
    """Return one stable newest-first page without changing audit counters."""

    incident = session.get(ExecutionInfrastructureIncident, incident_id)
    if incident is None or incident.execution_id != execution_id:
        raise domain_error(404, "incident_not_found", "Infrastructure Incident not found")
    query = select(ExecutionIncidentDisposition).where(
        ExecutionIncidentDisposition.incident_id == incident_id
    )
    if before_id is not None:
        cursor = session.get(ExecutionIncidentDisposition, before_id)
        if cursor is None or cursor.incident_id != incident_id:
            raise domain_error(422, "disposition_cursor_invalid", "Disposition cursor is invalid")
        query = query.where(
            or_(
                ExecutionIncidentDisposition.created_at < cursor.created_at,
                (
                    (ExecutionIncidentDisposition.created_at == cursor.created_at)
                    & (ExecutionIncidentDisposition.id < cursor.id)
                ),
            )
        )
    bounded = max(1, min(limit, 100))
    rows = list(
        session.scalars(
            query.order_by(
                ExecutionIncidentDisposition.created_at.desc(),
                ExecutionIncidentDisposition.id.desc(),
            ).limit(bounded + 1)
        )
    )
    has_more = len(rows) > bounded
    page = rows[:bounded]
    return IncidentDispositionPage(
        items=[IncidentDispositionReceipt.model_validate(row) for row in page],
        next_before_id=page[-1].id if has_more else None,
    )


def inspect_incident_disposition(
    execution: Execution,
    incident: ExecutionInfrastructureIncident,
    *,
    active_attempts: bool,
    current_outbox: ExecutionOutbox | None,
    can_edit: bool,
) -> IncidentActionCapabilities:
    """Compute advisory UI capabilities; POST always revalidates under locks."""

    if not can_edit:
        return IncidentActionCapabilities(False, "adapter_read_only", False, "adapter_read_only")
    if incident.status != "open":
        return IncidentActionCapabilities(False, "incident_closed", False, "incident_closed")
    if execution.status in {"succeeded", "dead_letter", "cancelled", "expired"}:
        return IncidentActionCapabilities(False, "execution_terminal", False, "execution_terminal")
    if incident.dispatch_generation != execution.dispatch_generation:
        if (
            incident.dispatch_generation is not None
            and incident.dispatch_generation < execution.dispatch_generation
        ):
            return IncidentActionCapabilities(False, "incident_stale_generation", True, None)
        return IncidentActionCapabilities(
            False,
            "incident_dispatch_identity_invalid",
            False,
            "incident_dispatch_identity_invalid",
        )
    if current_outbox is None or incident.message_id != current_outbox.message_id:
        return IncidentActionCapabilities(
            False,
            "incident_dispatch_identity_unverifiable",
            False,
            "incident_dispatch_identity_unverifiable",
        )
    if active_attempts:
        return IncidentActionCapabilities(
            False,
            "incident_execution_active",
            execution.status == "running",
            None if execution.status == "running" else "incident_execution_active",
        )
    if execution.cancel_requested:
        return IncidentActionCapabilities(False, "incident_cancellation_pending", True, None)
    if execution.status == "retry_wait":
        return IncidentActionCapabilities(False, "incident_execution_not_queued", True, None)
    if execution.status != "queued":
        return IncidentActionCapabilities(
            False, "incident_execution_active", False, "incident_execution_active"
        )
    return IncidentActionCapabilities(True, None, True, None)


def request_hash(body: IncidentDispositionBody) -> str:
    """Hash only the closed disposition intent, never actor or material facts."""

    canonical = canonicalize(
        {
            "action": body.action,
            "expected_generation": body.expected_generation,
            "reason_code": body.reason_code,
        }
    )
    return hashlib.sha256(canonical).hexdigest()


def lookup_idempotency(
    session: Session,
    *,
    incident_id: int,
    idempotency_key: uuid.UUID,
    body: IncidentDispositionBody,
) -> DispositionIdempotency:
    """Lock and compare a receipt before mutable generation/material checks."""

    digest = request_hash(body)
    receipt = session.scalar(
        select(ExecutionIncidentDisposition)
        .where(
            ExecutionIncidentDisposition.incident_id == incident_id,
            ExecutionIncidentDisposition.idempotency_key == idempotency_key,
        )
        .with_for_update()
    )
    if receipt is not None and receipt.request_hash != digest:
        raise domain_error(
            409,
            "idempotency_key_conflict",
            "Idempotency-Key was already used for a different disposition request",
        )
    return DispositionIdempotency(idempotency_key, digest, receipt)


def converge_terminal_dispositions_locked(
    session: Session,
    execution: Execution,
    locked_incidents: tuple[ExecutionInfrastructureIncident, ...],
    *,
    now: datetime,
) -> int:
    """Resolve pending terminate receipts in place after a real terminal transition."""

    if execution.status not in {"succeeded", "dead_letter", "cancelled", "expired"}:
        return 0
    incident_by_id = {
        incident.id: incident
        for incident in locked_incidents
        if incident.execution_id == execution.id
    }
    if not incident_by_id:
        return 0
    receipts = tuple(
        session.scalars(
            select(ExecutionIncidentDisposition)
            .where(
                ExecutionIncidentDisposition.incident_id.in_(incident_by_id),
                ExecutionIncidentDisposition.action == "terminate",
                ExecutionIncidentDisposition.outcome == "cancellation_requested",
            )
            .order_by(
                ExecutionIncidentDisposition.created_at.asc(),
                ExecutionIncidentDisposition.id.asc(),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    code = execution.error_code or execution.last_error_code or f"execution_{execution.status}"
    for receipt in receipts:
        receipt.outcome = "execution_terminal"
        receipt.code = code[:64]
        receipt.execution_status = execution.status
        incident = incident_by_id[receipt.incident_id]
        incident.status = "resolved"
        incident.resolved_at = now
    return len(receipts)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _status_for_outcome(outcome: str) -> int:
    if outcome in _REJECTION_STATUS:
        return _REJECTION_STATUS[outcome]
    if outcome.startswith("incident_"):
        return 409
    return 200


def _result(
    receipt: ExecutionIncidentDisposition,
    incident: ExecutionInfrastructureIncident,
    execution: Execution,
    *,
    retry_after_seconds: int | None = None,
) -> IncidentDispositionResult:
    return IncidentDispositionResult(
        response=IncidentDispositionResponse(
            receipt=IncidentDispositionReceipt.model_validate(receipt),
            incident_status=incident.status,  # type: ignore[arg-type]
            execution_status=execution.status,
            retry_after_seconds=retry_after_seconds,
        ),
        status_code=_status_for_outcome(receipt.outcome),
    )


def _record(
    session: Session,
    *,
    execution: Execution,
    incident: ExecutionInfrastructureIncident,
    idempotency: DispositionIdempotency,
    body: IncidentDispositionBody,
    principal: Principal,
    outcome: str,
    code: str | None = None,
    from_generation: int | None = None,
    to_generation: int | None = None,
    from_outbox_id: uuid.UUID | None = None,
    to_outbox_id: uuid.UUID | None = None,
) -> ExecutionIncidentDisposition:
    receipt = ExecutionIncidentDisposition(
        incident_id=incident.id,
        execution_id=execution.id,
        idempotency_key=idempotency.key,
        request_hash=idempotency.request_hash,
        actor_kind=principal.kind,
        user_id=principal.user_id,
        action=body.action,
        reason_code=body.reason_code,
        outcome=outcome,
        code=code or outcome,
        from_generation=from_generation,
        to_generation=to_generation,
        from_outbox_id=from_outbox_id,
        to_outbox_id=to_outbox_id,
        execution_status=execution.status,
    )
    session.add(receipt)
    session.flush()
    return receipt


def _commit_result(
    session: Session,
    receipt: ExecutionIncidentDisposition,
    incident: ExecutionInfrastructureIncident,
    execution: Execution,
    *,
    retry_after_seconds: int | None = None,
) -> IncidentDispositionResult:
    result = _result(
        receipt,
        incident,
        execution,
        retry_after_seconds=retry_after_seconds,
    )
    try:
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        close_material_guards(session)


def _current_outbox(execution: Execution, tail: ExecutionLockTail) -> ExecutionOutbox | None:
    return next(
        (
            row
            for row in tail.outbox_rows
            if row.dispatch_generation == execution.dispatch_generation
        ),
        None,
    )


def _valid_dispatch_identity(
    adapter: Adapter,
    execution: Execution,
    incident: ExecutionInfrastructureIncident,
    row: ExecutionOutbox,
) -> bool:
    try:
        message = deserialize_dispatch_message(row.payload_json)
        body = serialize_dispatch_message(message)
    except Exception:  # noqa: BLE001 - persisted corruption is a stable rejection
        return False
    return bool(
        message.execution_id == execution.id
        and message.dispatch_generation == execution.dispatch_generation
        and message.adapter_id == execution.adapter_id
        and message.language == adapter.language
        and message.target_worker_id == execution.target_worker_id_snapshot
        and execution.target_worker_id == execution.target_worker_id_snapshot
        and message.resource_class == execution.resource_class
        and message.message_id == row.message_id
        and len(body) == row.payload_bytes
        and row.routing_key == worker_routing_key(message.target_worker_id)
        and incident.message_id == row.message_id
    )


def _reject(
    session: Session,
    *,
    execution: Execution,
    incident: ExecutionInfrastructureIncident,
    idempotency: DispositionIdempotency,
    body: IncidentDispositionBody,
    principal: Principal,
    code: str,
    row: ExecutionOutbox | None = None,
    retry_after_seconds: int | None = None,
) -> IncidentDispositionResult:
    receipt = _record(
        session,
        execution=execution,
        incident=incident,
        idempotency=idempotency,
        body=body,
        principal=principal,
        outcome=code,
        from_generation=execution.dispatch_generation,
        to_generation=execution.dispatch_generation,
        from_outbox_id=row.id if row is not None else None,
        to_outbox_id=row.id if row is not None else None,
    )
    return _commit_result(
        session,
        receipt,
        incident,
        execution,
        retry_after_seconds=retry_after_seconds,
    )


def _inflight_retry_after(row: ExecutionOutbox | None, now: datetime) -> int | None:
    if row is None or row.lease_expires_at is None:
        return None
    seconds = math.ceil((_as_utc(row.lease_expires_at) - _as_utc(now)).total_seconds())
    return max(1, min(seconds, 86_400))


def _terminal_code(execution: Execution) -> str:
    return (execution.error_code or execution.last_error_code or f"execution_{execution.status}")[
        :64
    ]


def _plan_published_dispatch(
    session: Session,
    execution: Execution,
    row: ExecutionOutbox,
    *,
    now: datetime,
) -> DispatchReplacementPlan:
    message = deserialize_dispatch_message(row.payload_json)
    next_generation = execution.dispatch_generation + 1
    next_message = message.model_copy(
        update={"dispatch_generation": next_generation, "message_id": uuid.uuid4()}
    )
    body = serialize_dispatch_message(next_message)
    outbox.require_outbox_capacity(session, additional_count=1, additional_bytes=len(body), now=now)
    return DispatchReplacementPlan(
        generation=next_generation,
        message_id=next_message.message_id,
        routing_key=worker_routing_key(next_message.target_worker_id),
        payload_json=next_message.model_dump(mode="json"),
        payload_bytes=len(body),
    )


def _install_published_dispatch(
    session: Session,
    execution: Execution,
    plan: DispatchReplacementPlan,
    *,
    now: datetime,
) -> ExecutionOutbox:
    execution.dispatch_generation = plan.generation
    created = ExecutionOutbox(
        execution_id=execution.id,
        dispatch_generation=plan.generation,
        message_id=plan.message_id,
        routing_key=plan.routing_key,
        payload_json=plan.payload_json,
        payload_bytes=plan.payload_bytes,
        available_at=now,
    )
    session.add(created)
    session.flush()
    return created


def _dispose_incident_transaction(
    session: Session,
    execution_id: int,
    incident_id: int,
    action: Literal["recover", "terminate"],
    expected_generation: int,
    idempotency_key: uuid.UUID,
    reason_code: Literal[
        "capacity_repaired", "routing_repaired", "operator_cancel", "verified_terminal"
    ],
    principal: Principal,
) -> IncidentDispositionResult:
    """Revalidate and atomically dispose one Incident on its original Execution."""

    close_material_guards(session)
    body = IncidentDispositionBody(
        action=action,
        expected_generation=expected_generation,
        reason_code=reason_code,
    )
    proof = RecoveryMaterialProof(fingerprint=None, valid=False)
    try:
        preflight_execution = adapter_access.require_execution_access(
            session, execution_id, principal, "edit"
        )
        if action == "recover":
            proof = preflight_recovery_materials(session, preflight_execution)
    finally:
        session.rollback()
    execution = lock_execution_in_admission_order(session, execution_id)
    if execution is None:
        session.rollback()
        raise domain_error(404, "execution_not_found", "Execution not found")
    try:
        access = adapter_access.require_adapter_access(
            session, execution.adapter_id, principal, "edit"
        )
    except HTTPException:
        session.rollback()
        raise
    tail = lock_execution_tail(session, execution)
    incident = next((row for row in tail.incidents if row.id == incident_id), None)
    if incident is None:
        session.rollback()
        raise domain_error(404, "incident_not_found", "Infrastructure Incident not found")
    try:
        idempotency = lookup_idempotency(
            session,
            incident_id=incident.id,
            idempotency_key=idempotency_key,
            body=body,
        )
    except HTTPException:
        session.rollback()
        raise
    current_row = _current_outbox(execution, tail)
    if idempotency.receipt is not None:
        retry_after = None
        if idempotency.receipt.outcome == "incident_dispatch_inflight":
            retry_after = _inflight_retry_after(current_row, database_now(session))
        result = _result(
            idempotency.receipt,
            incident,
            execution,
            retry_after_seconds=retry_after,
        )
        session.rollback()
        return result

    if expected_generation != execution.dispatch_generation:
        return _reject(
            session,
            execution=execution,
            incident=incident,
            idempotency=idempotency,
            body=body,
            principal=principal,
            code="incident_generation_conflict",
            row=current_row,
        )

    if execution.status in {"succeeded", "dead_letter", "cancelled", "expired"}:
        incident.status = "resolved"
        incident.resolved_at = database_now(session)
        receipt = _record(
            session,
            execution=execution,
            incident=incident,
            idempotency=idempotency,
            body=body,
            principal=principal,
            outcome="execution_terminal",
            code=_terminal_code(execution),
            from_generation=execution.dispatch_generation,
            to_generation=execution.dispatch_generation,
            from_outbox_id=current_row.id if current_row is not None else None,
            to_outbox_id=current_row.id if current_row is not None else None,
        )
        return _commit_result(session, receipt, incident, execution)

    incident_generation = incident.dispatch_generation
    if incident_generation is None or incident_generation > execution.dispatch_generation:
        return _reject(
            session,
            execution=execution,
            incident=incident,
            idempotency=idempotency,
            body=body,
            principal=principal,
            code="incident_dispatch_identity_invalid",
            row=current_row,
        )
    if incident_generation < execution.dispatch_generation:
        if action == "recover":
            return _reject(
                session,
                execution=execution,
                incident=incident,
                idempotency=idempotency,
                body=body,
                principal=principal,
                code="incident_stale_generation",
                row=current_row,
            )
        incident.status = "ignored"
        incident.resolved_at = database_now(session)
        receipt = _record(
            session,
            execution=execution,
            incident=incident,
            idempotency=idempotency,
            body=body,
            principal=principal,
            outcome="stale_incident_ignored",
            from_generation=incident_generation,
            to_generation=execution.dispatch_generation,
            from_outbox_id=current_row.id if current_row is not None else None,
            to_outbox_id=current_row.id if current_row is not None else None,
        )
        return _commit_result(session, receipt, incident, execution)

    if execution.dispatch_backend != "rabbitmq":
        return _reject(
            session,
            execution=execution,
            incident=incident,
            idempotency=idempotency,
            body=body,
            principal=principal,
            code="incident_execution_unsupported",
            row=current_row,
        )
    if current_row is None:
        return _reject(
            session,
            execution=execution,
            incident=incident,
            idempotency=idempotency,
            body=body,
            principal=principal,
            code="incident_dispatch_identity_unverifiable",
        )
    if not _valid_dispatch_identity(access.adapter, execution, incident, current_row):
        return _reject(
            session,
            execution=execution,
            incident=incident,
            idempotency=idempotency,
            body=body,
            principal=principal,
            code="incident_dispatch_identity_invalid",
            row=current_row,
        )

    active_attempts = tuple(
        attempt for attempt in tail.attempts if attempt.status in {"claimed", "running"}
    )
    if action == "terminate":
        if execution.status != "running" and active_attempts:
            return _reject(
                session,
                execution=execution,
                incident=incident,
                idempotency=idempotency,
                body=body,
                principal=principal,
                code="incident_execution_active",
                row=current_row,
            )
        from dlr.control.services.execution import cancel_execution_locked

        cancel_execution_locked(session, execution, lock_tail=tail)
        if execution.status == "running":
            receipt = _record(
                session,
                execution=execution,
                incident=incident,
                idempotency=idempotency,
                body=body,
                principal=principal,
                outcome="cancellation_requested",
                from_generation=execution.dispatch_generation,
                to_generation=execution.dispatch_generation,
                from_outbox_id=current_row.id,
                to_outbox_id=current_row.id,
            )
        else:
            incident.status = "resolved"
            incident.resolved_at = database_now(session)
            receipt = _record(
                session,
                execution=execution,
                incident=incident,
                idempotency=idempotency,
                body=body,
                principal=principal,
                outcome="execution_terminal",
                code=_terminal_code(execution),
                from_generation=execution.dispatch_generation,
                to_generation=execution.dispatch_generation,
                from_outbox_id=current_row.id,
                to_outbox_id=current_row.id,
            )
        return _commit_result(session, receipt, incident, execution)

    if execution.cancel_requested:
        return _reject(
            session,
            execution=execution,
            incident=incident,
            idempotency=idempotency,
            body=body,
            principal=principal,
            code="incident_cancellation_pending",
            row=current_row,
        )
    if execution.status == "retry_wait":
        return _reject(
            session,
            execution=execution,
            incident=incident,
            idempotency=idempotency,
            body=body,
            principal=principal,
            code="incident_execution_not_queued",
            row=current_row,
        )
    if execution.status != "queued" or active_attempts:
        return _reject(
            session,
            execution=execution,
            incident=incident,
            idempotency=idempotency,
            body=body,
            principal=principal,
            code="incident_execution_active",
            row=current_row,
        )
    if current_row.last_error_code in _SETTLEMENT_CODES:
        return _reject(
            session,
            execution=execution,
            incident=incident,
            idempotency=idempotency,
            body=body,
            principal=principal,
            code="incident_dispatch_settled",
            row=current_row,
        )
    if current_row.status == "published" and (
        current_row.published_at is None
        or current_row.lease_owner is not None
        or current_row.lease_expires_at is not None
    ):
        return _reject(
            session,
            execution=execution,
            incident=incident,
            idempotency=idempotency,
            body=body,
            principal=principal,
            code="incident_dispatch_identity_unverifiable",
            row=current_row,
        )

    now = database_now(session)
    if current_row.status == "pending":
        if (
            current_row.published_at is not None
            or ((current_row.lease_owner is None) != (current_row.lease_expires_at is None))
            or (current_row.lease_owner is not None and not current_row.lease_owner.strip())
        ):
            return _reject(
                session,
                execution=execution,
                incident=incident,
                idempotency=idempotency,
                body=body,
                principal=principal,
                code="incident_dispatch_identity_unverifiable",
                row=current_row,
            )
        if (
            current_row.lease_owner is not None
            and current_row.lease_expires_at is not None
            and _as_utc(current_row.lease_expires_at) > _as_utc(now)
        ):
            retry_after = _inflight_retry_after(current_row, now)
            return _reject(
                session,
                execution=execution,
                incident=incident,
                idempotency=idempotency,
                body=body,
                principal=principal,
                code="incident_dispatch_inflight",
                row=current_row,
                retry_after_seconds=retry_after,
            )
        if not validate_recovery_materials(session, execution, proof):
            return _reject(
                session,
                execution=execution,
                incident=incident,
                idempotency=idempotency,
                body=body,
                principal=principal,
                code="incident_materials_unavailable",
                row=current_row,
            )
        incident.status = "resolved"
        incident.resolved_at = now
        receipt = _record(
            session,
            execution=execution,
            incident=incident,
            idempotency=idempotency,
            body=body,
            principal=principal,
            outcome="dispatch_already_pending",
            from_generation=execution.dispatch_generation,
            to_generation=execution.dispatch_generation,
            from_outbox_id=current_row.id,
            to_outbox_id=current_row.id,
        )
        return _commit_result(session, receipt, incident, execution)

    try:
        plan = _plan_published_dispatch(session, execution, current_row, now=now)
    except HTTPException as error:
        detail = error.detail
        code = detail.get("code") if isinstance(detail, dict) else None
        if code != "outbox_backlog_full":
            session.rollback()
            raise
        return _reject(
            session,
            execution=execution,
            incident=incident,
            idempotency=idempotency,
            body=body,
            principal=principal,
            code="outbox_backlog_full",
            row=current_row,
        )
    if not validate_recovery_materials(session, execution, proof):
        return _reject(
            session,
            execution=execution,
            incident=incident,
            idempotency=idempotency,
            body=body,
            principal=principal,
            code="incident_materials_unavailable",
            row=current_row,
        )
    replacement = _install_published_dispatch(session, execution, plan, now=now)
    incident.status = "resolved"
    incident.resolved_at = now
    receipt = _record(
        session,
        execution=execution,
        incident=incident,
        idempotency=idempotency,
        body=body,
        principal=principal,
        outcome="recovery_dispatched",
        from_generation=current_row.dispatch_generation,
        to_generation=replacement.dispatch_generation,
        from_outbox_id=current_row.id,
        to_outbox_id=replacement.id,
    )
    return _commit_result(session, receipt, incident, execution)


def dispose_incident(
    session: Session,
    execution_id: int,
    incident_id: int,
    action: Literal["recover", "terminate"],
    expected_generation: int,
    idempotency_key: uuid.UUID,
    reason_code: Literal[
        "capacity_repaired", "routing_repaired", "operator_cancel", "verified_terminal"
    ],
    principal: Principal,
) -> IncidentDispositionResult:
    """Dispose one Incident and release transferred file guards on every failure."""

    try:
        return _dispose_incident_transaction(
            session,
            execution_id,
            incident_id,
            action,
            expected_generation,
            idempotency_key,
            reason_code,
            principal,
        )
    except Exception:
        try:
            session.rollback()
        finally:
            close_material_guards(session)
        raise
