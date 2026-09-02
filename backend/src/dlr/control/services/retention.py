"""Retryable, batched retention cleanup for terminal Executions.

Retention is intentionally independent of Webhook receipt.  A request path
must never delete history as a side effect of accepting a new call; the
periodic worker below applies the same rules to Webhook, Task and Schedule
rows.  Only terminal rows are selected, so a retry after interruption cannot
remove pending/running work.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from dlr.common.config import settings
from dlr.control.models import Execution, ExecutionIdempotencyRecord, ExecutionOutbox
from dlr.control.services.execution import LEGACY_TERMINAL_STATUSES, RABBITMQ_TERMINAL_STATUSES
from dlr.control.services.idempotency import cleanup_expired_records

logger = logging.getLogger("dlr.control.retention")


@dataclass(frozen=True)
class RetentionPolicy:
    trigger: str
    days: int
    max_per_adapter: int


@dataclass(frozen=True)
class RetentionReport:
    deleted: int
    batches: int
    failures: int
    elapsed_ms: int


def retention_policies() -> tuple[RetentionPolicy, ...]:
    """Return the current deployment policy without reading any log fields."""

    return (
        RetentionPolicy(
            "webhook",
            settings.execution_retention_webhook_days,
            settings.execution_retention_webhook_max_per_adapter,
        ),
        RetentionPolicy(
            "manual",
            settings.execution_retention_task_days,
            settings.execution_retention_task_max_per_adapter,
        ),
        RetentionPolicy(
            "schedule",
            settings.execution_retention_schedule_days,
            settings.execution_retention_schedule_max_per_adapter,
        ),
    )


def _terminal_query(policy: RetentionPolicy, adapter_id: int) -> Select[tuple[int]]:
    return select(Execution.id).where(
        Execution.adapter_id == adapter_id,
        Execution.trigger == policy.trigger,
        ((Execution.dispatch_backend == "legacy") & Execution.status.in_(LEGACY_TERMINAL_STATUSES))
        | (
            (Execution.dispatch_backend == "rabbitmq")
            & Execution.status.in_(RABBITMQ_TERMINAL_STATUSES)
        ),
        ((Execution.dispatch_backend == "legacy") | Execution.admission_released_at.is_not(None)),
        ~exists(
            select(1).where(
                ExecutionIdempotencyRecord.execution_id == Execution.id,
            )
        ),
        ~exists(
            select(1).where(
                ExecutionOutbox.execution_id == Execution.id,
                ExecutionOutbox.status == "pending",
            )
        ),
    )


def _adapter_ids(session: Session, policy: RetentionPolicy) -> list[int]:
    return list(
        session.scalars(
            select(Execution.adapter_id)
            .where(
                Execution.trigger == policy.trigger,
                (
                    (Execution.dispatch_backend == "legacy")
                    & Execution.status.in_(LEGACY_TERMINAL_STATUSES)
                )
                | (
                    (Execution.dispatch_backend == "rabbitmq")
                    & Execution.status.in_(RABBITMQ_TERMINAL_STATUSES)
                ),
            )
            .distinct()
        )
    )


def _delete_batch(session: Session, ids: list[int]) -> int:
    if not ids:
        return 0
    result = cast(
        Any,
        session.execute(
            delete(Execution).where(
                Execution.id.in_(ids),
                (
                    (Execution.dispatch_backend == "legacy")
                    & Execution.status.in_(LEGACY_TERMINAL_STATUSES)
                )
                | (
                    (Execution.dispatch_backend == "rabbitmq")
                    & Execution.status.in_(RABBITMQ_TERMINAL_STATUSES)
                ),
                (
                    (Execution.dispatch_backend == "legacy")
                    | Execution.admission_released_at.is_not(None)
                ),
                ~exists(
                    select(1).where(
                        ExecutionIdempotencyRecord.execution_id == Execution.id,
                    )
                ),
                ~exists(
                    select(1).where(
                        ExecutionOutbox.execution_id == Execution.id,
                        ExecutionOutbox.status == "pending",
                    )
                ),
            )
        ),
    )
    deleted = int(result.rowcount or 0)
    session.commit()
    return deleted


def _cleanup_policy(
    session: Session,
    policy: RetentionPolicy,
    *,
    now: datetime,
    batch_size: int,
) -> tuple[int, int, int]:
    """Apply one policy, committing each small batch for safe interruption."""

    deleted = 0
    batches = 0
    failures = 0
    cutoff = now - timedelta(days=policy.days)

    for adapter_id in _adapter_ids(session, policy):
        while True:
            stale_ids = list(
                session.scalars(
                    _terminal_query(policy, adapter_id)
                    .where(Execution.created_at < cutoff)
                    .order_by(Execution.created_at, Execution.id)
                    .limit(batch_size)
                )
            )
            if not stale_ids:
                break
            try:
                deleted += _delete_batch(session, stale_ids)
                batches += 1
            except Exception:  # noqa: BLE001 - one batch must not stop retries
                session.rollback()
                failures += 1
                logger.warning(
                    "retention batch failed: trigger=%s adapter=%s reason=retry-next-cycle",
                    policy.trigger,
                    adapter_id,
                )
                break

        while True:
            over_limit_ids = list(
                session.scalars(
                    _terminal_query(policy, adapter_id)
                    .order_by(Execution.created_at.desc(), Execution.id.desc())
                    .offset(policy.max_per_adapter)
                    .limit(batch_size)
                )
            )
            if not over_limit_ids:
                break
            try:
                deleted += _delete_batch(session, over_limit_ids)
                batches += 1
            except Exception:  # noqa: BLE001 - one batch must not stop retries
                session.rollback()
                failures += 1
                logger.warning(
                    "retention batch failed: trigger=%s adapter=%s reason=retry-next-cycle",
                    policy.trigger,
                    adapter_id,
                )
                break

    return deleted, batches, failures


def cleanup_execution_retention(
    session: Session, *, now: datetime | None = None
) -> RetentionReport:
    """Delete eligible terminal rows in bounded, independently committed batches.

    The query only selects Execution metadata (id, adapter, trigger, status,
    timestamp).  Deleting the row removes its JSON input/output and Text
    error/stdout/stderr together; none of those fields are logged.
    """

    started = time.monotonic()
    if now is None:
        from dlr.control.services.input_config import database_now

        effective_now = database_now(session)
    else:
        effective_now = now
    # Expired records are removed first, but only when their Execution is
    # already terminal.  Remaining records keep their FK and therefore keep
    # the associated Execution inside the promised replay window.
    cleanup_expired_records(session, now=effective_now)
    total_deleted = 0
    total_batches = 0
    total_failures = 0
    for policy in retention_policies():
        deleted, batches, failures = _cleanup_policy(
            session,
            policy,
            now=effective_now,
            batch_size=settings.execution_retention_batch_size,
        )
        total_deleted += deleted
        total_batches += batches
        total_failures += failures

    elapsed_ms = int((time.monotonic() - started) * 1000)
    report = RetentionReport(total_deleted, total_batches, total_failures, elapsed_ms)
    logger.info(
        "retention cycle complete: deleted=%s batches=%s failures=%s elapsed_ms=%s",
        report.deleted,
        report.batches,
        report.failures,
        report.elapsed_ms,
    )
    return report


def _retention_tick() -> None:
    """Run one blocking cleanup cycle in a short-lived database session."""

    from dlr.control.db import SessionLocal

    session = SessionLocal()
    try:
        cleanup_execution_retention(session)
    finally:
        session.close()


async def retention_loop() -> None:
    """Periodic retention loop; a failed cycle is retried next interval."""

    logger.info(
        "retention loop started (interval=%.2fs batch_size=%s)",
        settings.execution_retention_interval_seconds,
        settings.execution_retention_batch_size,
    )
    while True:
        try:
            await asyncio.to_thread(_retention_tick)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the service loop alive
            logger.exception("retention cycle crashed; retrying next interval")
        await asyncio.sleep(settings.execution_retention_interval_seconds)
