"""Bounded request-driven coordination; each mutation and receipt share a transaction.

There is no background executor. A disconnected caller queries its original
ID, then explicitly resumes pending work. PostgreSQL serializes coordinators
for that ID, including across the independent per-item commits.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

from fastapi import HTTPException
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from dlr.control.models import (
    Adapter,
    AdapterBatch,
    AdapterBatchItem,
    EventSubscription,
    Execution,
    SubscriptionLease,
    User,
)
from dlr.control.schemas.adapter_batch import (
    BatchAction,
    BatchCreate,
    BatchExecution,
    BatchItemAction,
    BatchItemResponse,
    BatchMode,
    BatchResponse,
    BatchStatus,
)
from dlr.control.schemas.schedule import ScheduleMisfirePolicy, ScheduleUpsert
from dlr.control.schemas.webhook import WebhookUpsert
from dlr.control.security import Principal
from dlr.control.services import (
    adapter_access,
    execution,
    schedule,
    subscription_management,
    webhook,
)
from dlr.control.services.adapter import domain_error
from dlr.control.services.adapter_batch_config import BatchConfiguration, configurations
from dlr.control.services.execution_cancellation import (
    RABBITMQ_NONTERMINAL_STATUSES,
    lock_execution_in_admission_order,
)

RETRYABLE_CODES = frozenset(
    {
        "adapter_busy",
        "adapter_runtime_locked",
        "adapter_queue_full",
        "runtime_capacity_full",
        "rabbitmq_not_ready",
        "worker_offline",
        "subscription_capacity_exceeded",
        "subscription_draining",
        "outbox_capacity_full",
        "managed_file_hold_capacity_exceeded",
    }
)


def _owner(principal: Principal) -> str:
    return f"account:{principal.user_id}" if principal.kind == "account" else principal.kind


def _current_principal(session: Session, principal: Principal, *, lock: bool = False) -> Principal:
    # An execute request may contain 100 separate transactions. Do not retain
    # an administrator role or enabled state from the first HTTP dependency.
    if principal.kind != "account":
        return principal
    user = session.get(User, principal.user_id, with_for_update=lock, populate_existing=True)
    if user is None or not user.enabled or user.must_change_password:
        raise domain_error(403, "batch_account_unavailable", "Current account cannot operate")
    return Principal(
        kind="account",
        user_id=user.id,
        username=user.username,
        role="admin" if user.role == "admin" else "user",
    )


def _code(error: HTTPException) -> str:
    value = error.detail.get("code") if isinstance(error.detail, dict) else None
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-z0-9_]{1,128}", value)
        else ("batch_action_failed")
    )


def _batch(session: Session, batch_id: uuid.UUID, principal: Principal) -> AdapterBatch:
    result = session.get(AdapterBatch, batch_id, populate_existing=True)
    if result is None or result.owner_key != _owner(principal):
        raise domain_error(404, "batch_not_found", "Batch operation not found")
    return result


@contextmanager
def _coordinator(session: Session, batch_id: uuid.UUID) -> Iterator[None]:
    """Pin one connection so the session lock survives item commits.

    Always unlock before returning the connection to the pool. Invalidation
    closes the actual PostgreSQL connection if an unlock cannot be confirmed.
    The ORM uses that same connection: concurrent batches cannot each reserve
    one pool slot for coordination while waiting for another slot for work.
    """
    bind = session.get_bind()
    if not isinstance(bind, Engine):
        raise RuntimeError("Batch coordinator requires an Engine-backed Session")
    lock_key = int.from_bytes(
        hashlib.sha256(b"dlr-adapter-batch:" + batch_id.bytes).digest()[:8],
        byteorder="big",
        signed=True,
    )
    with bind.connect() as connection:
        acquired = False
        try:
            acquired = bool(
                connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key})
            )
            connection.commit()
            if not acquired:
                raise domain_error(409, "batch_operation_busy", "Query the operation in progress")
            session.bind = connection
            yield
        finally:
            try:
                session.rollback()
            finally:
                session.bind = bind
                if acquired:
                    try:
                        connection.rollback()
                        connection.execute(
                            text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
                        )
                        connection.commit()
                    except Exception:
                        connection.invalidate()
                        raise


def _items(session: Session, batch_id: uuid.UUID) -> list[AdapterBatchItem]:
    return list(
        session.scalars(
            select(AdapterBatchItem)
            .where(AdapterBatchItem.batch_id == batch_id)
            .order_by(AdapterBatchItem.position)
            .execution_options(populate_existing=True)
        )
    )


def _action(mode: str, action: str) -> str:
    if mode == "manual":
        return "run_once" if action == "start" else "cancel_executions"
    return f"{'enable' if action == 'start' else 'disable'}_{mode}"


def _checked_configuration(
    session: Session,
    item: AdapterBatchItem,
    principal: Principal,
) -> tuple[Adapter, BatchConfiguration]:
    access = adapter_access.require_adapter_access(
        session, item.adapter_id, principal, "edit", for_update=True
    )
    # Reload a prior identity-map instance after taking its serialization lock.
    session.refresh(access.adapter)
    config = configurations(session, [access.adapter])[item.adapter_id]
    if config.token != item.expected_token:
        raise domain_error(409, "batch_config_conflict", "Saved configuration changed")
    return access.adapter, config


def prepare(session: Session, payload: BatchCreate, principal: Principal) -> BatchResponse:
    digest = hashlib.sha256(
        json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    # Authentication may already have opened a read transaction on this
    # request Session. Release it before acquiring the coordinator connection.
    session.commit()
    with _coordinator(session, payload.id):
        existing = session.get(AdapterBatch, payload.id)
        if existing is not None:
            _batch(session, payload.id, principal)
            if existing.request_hash != digest:
                raise domain_error(
                    409, "batch_operation_conflict", "Operation ID has another intent"
                )
            return get_batch(session, payload.id, principal)
        batch = AdapterBatch(
            id=payload.id, owner_key=_owner(principal), action=payload.action, request_hash=digest
        )
        session.add(batch)
        session.flush()
        by_id = {
            target.adapter_id: (position, target) for position, target in enumerate(payload.targets)
        }
        # Preparation holds Adapter locks in stable order. It never changes
        # runtime state, and a denied target does not invalidate other targets.
        for adapter_id in sorted(by_id):
            position, target = by_id[adapter_id]
            item = AdapterBatchItem(
                batch_id=batch.id,
                adapter_id=adapter_id,
                position=position,
                expected_token=target.expected_token,
                status="pending",
                execution_ids=[],
            )
            session.add(item)
            session.flush()
            try:
                with session.begin_nested():
                    current = _current_principal(session, principal, lock=True)
                    _, config = _checked_configuration(session, item, current)
                    item.mode = config.mode
                    item.action = _action(config.mode, batch.action)
                    if item.action == "cancel_executions":
                        item.execution_ids = list(
                            session.scalars(
                                select(Execution.id)
                                .where(
                                    Execution.adapter_id == adapter_id,
                                    Execution.status.in_(RABBITMQ_NONTERMINAL_STATUSES),
                                )
                                .order_by(Execution.id)
                            )
                        )
                    if config.subscription is not None:
                        item.subscription_id = config.subscription.id
                        item.activation_id = config.subscription.activation_id
            except HTTPException as error:
                item.status, item.error_code = "failed", _code(error)
        session.commit()
    return get_batch(session, payload.id, principal)


def _schedule_action(
    session: Session,
    adapter: Adapter,
    config: BatchConfiguration,
    enabled: bool,
) -> str:
    row = config.schedule
    if row is None:
        raise domain_error(409, "schedule_not_configured", "Configure Schedule first")
    if enabled and row.last_blocked_reason:
        raise domain_error(409, "batch_target_requires_attention", "Review blocked Schedule first")
    if row.enabled == enabled:
        return "no_change" if not row.last_blocked_reason else "succeeded"
    blocked = (row.last_blocked_reason, row.last_blocked_detail, row.last_blocked_at)
    schedule.upsert_schedule(
        session,
        adapter.id,
        ScheduleUpsert(
            enabled=enabled,
            cron=row.cron,
            timezone=row.timezone,
            misfire_policy=cast(ScheduleMisfirePolicy, row.misfire_policy),
            max_catchup_count=row.max_catchup_count,
            max_catchup_age_seconds=row.max_catchup_age_seconds,
        ),
        commit=False,
    )
    if not enabled:
        row.last_blocked_reason, row.last_blocked_detail, row.last_blocked_at = blocked
    return "succeeded"


def _webhook_action(
    session: Session,
    adapter: Adapter,
    config: BatchConfiguration,
    enabled: bool,
) -> str:
    row = config.webhook
    if row is None:
        raise domain_error(409, "webhook_not_configured", "Configure Webhook first")
    if row.enabled == enabled:
        return "no_change"
    webhook.upsert_webhook(
        session,
        adapter.id,
        WebhookUpsert(
            enabled=enabled,
            public_id=row.public_id,
            credential_id=row.credential_id,
            response_mode=row.response_mode,
            response_timeout_seconds=row.response_timeout_seconds,
        ),
        commit=False,
    )
    return "succeeded"


def _subscription_action(
    session: Session,
    item: AdapterBatchItem,
    config: BatchConfiguration,
    principal: Principal,
    enabled: bool,
) -> str:
    row = config.subscription
    if row is None or row.id != item.subscription_id:
        raise domain_error(409, "batch_config_conflict", "Event source changed")
    if not enabled and row.activation_id != item.activation_id:
        raise domain_error(
            409, "batch_activation_changed", "A different receiving activation exists"
        )
    if enabled:
        if row.connection_state == "blocked" or row.delivery_state in {"blocked", "repair"}:
            raise domain_error(409, "batch_target_requires_attention", "Review event source first")
        if row.desired_state == "enabled":
            # The ordinary enable helper may otherwise turn blocked into an
            # implicit repair. A batch never takes that recovery shortcut.
            subscription_management.require_source(row.source_type)
            subscription_management.require_verified_identity(
                subscription_management.current_config(session, row)
            )
            return "no_change"
        subscription_management.enable_subscription(session, row.id, row.config_version, principal)
        item.activation_id = row.activation_id
        return "accepted"
    lease = session.get(SubscriptionLease, row.id, populate_existing=True)
    closed = not lease or (not lease.owner_id and lease.closing_generation is None)
    if row.desired_state == "disabled" and row.connection_state == "stopped" and closed:
        return "no_change" if not row.last_error_code else "succeeded"
    if (
        row.desired_state == "disabled"
        and row.connection_state == "blocked"
        and row.last_error_code == "source_close_unconfirmed"
    ):
        # A prior close failure is a diagnostic requiring explicit handling,
        # not a fresh stop request that may erase the blocked state.
        return "stopping"
    subscription_management.disable_subscription(session, row.id, row.config_version, principal)
    return "succeeded" if row.connection_state == "stopped" and closed else "stopping"


def _apply(
    session: Session, batch: AdapterBatch, item: AdapterBatchItem, principal: Principal
) -> None:
    current = _current_principal(session, principal, lock=True)
    adapter, config = _checked_configuration(session, item, current)
    if config.mode != item.mode or item.action != _action(config.mode, batch.action):
        raise domain_error(409, "batch_config_conflict", "Operation mode changed")
    if item.action == "run_once":
        result = execution._create_execution_locked(
            session,
            adapter,
            trigger="manual",
            idempotency_key=f"batch:{batch.id}:{adapter.id}",
            idempotency_body={},
        )
        item.execution_ids = [result.id]
        item.status = "accepted"
    elif item.action == "cancel_executions":
        waiting = False
        for execution_id in item.execution_ids:
            target = lock_execution_in_admission_order(session, execution_id)
            if target is None or target.adapter_id != adapter.id:
                raise domain_error(409, "batch_result_unavailable", "Frozen Execution unavailable")
            execution.cancel_execution_locked(session, target)
            waiting = waiting or target.status in RABBITMQ_NONTERMINAL_STATUSES
        item.status = "stopping" if waiting else "succeeded" if item.execution_ids else "no_change"
    elif config.mode == "schedule":
        item.status = _schedule_action(session, adapter, config, batch.action == "start")
    elif config.mode == "webhook":
        item.status = _webhook_action(session, adapter, config, batch.action == "start")
    elif config.mode == "subscription":
        item.status = _subscription_action(session, item, config, current, batch.action == "start")
    else:
        raise domain_error(409, "batch_config_conflict", "Operation mode is unavailable")
    item.error_code = None


def execute(
    session: Session,
    batch_id: uuid.UUID,
    principal: Principal,
    *,
    retry_ids: list[int] | None = None,
) -> BatchResponse:
    _batch(session, batch_id, principal)
    session.commit()
    with _coordinator(session, batch_id):
        batch = _batch(session, batch_id, principal)
        items = _items(session, batch_id)
        if retry_ids is not None:
            selected = {item.adapter_id: item for item in items}
            if any(
                value not in selected
                or selected[value].status != "failed"
                or selected[value].error_code not in RETRYABLE_CODES
                for value in retry_ids
            ):
                raise domain_error(
                    409, "batch_retry_not_allowed", "Only recoverable failures may retry"
                )
            target_ids = retry_ids
        else:
            target_ids = [item.adapter_id for item in items if item.status == "pending"]
        session.commit()
        for adapter_id in target_ids:
            item = session.get(
                AdapterBatchItem,
                (batch_id, adapter_id),
                with_for_update=True,
                populate_existing=True,
            )
            assert item is not None
            expected_status = "pending" if retry_ids is None else "failed"
            if item.status != expected_status or (
                retry_ids is not None and item.error_code not in RETRYABLE_CODES
            ):
                # Receipt facts win over a previously collected target list.
                session.rollback()
                continue
            try:
                _apply(session, batch, item, principal)
                session.commit()
            except HTTPException as error:
                # A domain rejection has not committed. Roll back all its
                # partial work, then persist only that known failure.
                session.rollback()
                item = session.get(
                    AdapterBatchItem,
                    (batch_id, adapter_id),
                    with_for_update=True,
                    populate_existing=True,
                )
                assert item is not None
                item.status, item.error_code = "failed", _code(error)
                session.commit()
            except Exception:
                # A connection/commit failure can mean committed-but-unseen.
                # Never overwrite that receipt with a guessed failure.
                session.rollback()
                raise
    return get_batch(session, batch_id, principal)


def _observe_item(
    session: Session,
    item: AdapterBatchItem,
    principal: Principal,
) -> BatchItemResponse:
    try:
        current = _current_principal(session, principal)
        access = adapter_access.require_adapter_access(session, item.adapter_id, current)
    except HTTPException:
        return BatchItemResponse(
            adapter_id=item.adapter_id, status="unknown", error_code="adapter_not_found"
        )
    response = BatchItemResponse(
        adapter_id=item.adapter_id,
        adapter_name=access.adapter.name,
        mode=cast(BatchMode | None, item.mode),
        action=cast(BatchItemAction | None, item.action),
        status=cast(BatchStatus, item.status),
        error_code=item.error_code,
        retryable=(
            item.status == "failed"
            and item.error_code in RETRYABLE_CODES
            and access.level in {"admin", "owner", "edit"}
        ),
        execution_ids=item.execution_ids,
        subscription_id=item.subscription_id,
    )
    if response.retryable:
        response.retryable = (
            configurations(session, [access.adapter])[item.adapter_id].token == item.expected_token
        )
    if item.execution_ids:
        rows = list(
            session.execute(
                select(Execution.id, Execution.status, Execution.cancel_requested)
                .where(
                    Execution.id.in_(item.execution_ids), Execution.adapter_id == item.adapter_id
                )
                .order_by(Execution.id)
            )
        )
        response.executions = [
            BatchExecution(id=row.id, status=row.status, cancel_requested=row.cancel_requested)
            for row in rows
        ]
        if len(rows) != len(item.execution_ids):
            response.status, response.error_code, response.retryable = (
                "unknown",
                "batch_result_unavailable",
                False,
            )
        elif item.status == "stopping" and all(
            row.status not in RABBITMQ_NONTERMINAL_STATUSES for row in rows
        ):
            response.status = "succeeded"
    if item.subscription_id is not None:
        source = session.get(EventSubscription, item.subscription_id, populate_existing=True)
        if source is None or source.deleted_at is not None or source.adapter_id != item.adapter_id:
            response.status, response.error_code, response.retryable = (
                "unknown",
                "batch_result_unavailable",
                False,
            )
        else:
            if source.activation_id == item.activation_id:
                response.subscription_state = source.connection_state
            if item.status in {"accepted", "stopping"}:
                if source.activation_id != item.activation_id:
                    response.status, response.error_code = "unknown", "batch_activation_changed"
                elif item.action == "disable_subscription":
                    lease = session.get(SubscriptionLease, source.id, populate_existing=True)
                    closed = not lease or (not lease.owner_id and lease.closing_generation is None)
                    if source.connection_state == "blocked":
                        response.status = "unknown"
                        response.error_code = source.last_error_code or "source_close_unconfirmed"
                        response.retryable = False
                    elif (
                        source.desired_state == "disabled"
                        and source.connection_state == "stopped"
                        and closed
                    ):
                        response.status = "succeeded"
                    else:
                        response.status = "stopping"
                elif source.connection_state == "listening" and source.desired_state == "enabled":
                    response.status = "succeeded"
                elif source.desired_state != "enabled":
                    # Another explicit action stopped this same activation.
                    # Preserve its original acceptance without polling forever
                    # for a listener that is no longer meant to start.
                    response.status, response.error_code = "unknown", "batch_result_unavailable"
                    response.retryable = False
                elif source.connection_state == "blocked":
                    response.status, response.error_code = "failed", source.last_error_code
    return response


def get_batch(session: Session, batch_id: uuid.UUID, principal: Principal) -> BatchResponse:
    batch = _batch(session, batch_id, principal)
    return BatchResponse(
        id=batch.id,
        action=cast(BatchAction, batch.action),
        created_at=batch.created_at,
        items=[_observe_item(session, item, principal) for item in _items(session, batch_id)],
    )
