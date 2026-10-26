"""Fair, bounded selection of durable subscription work.

Candidates are hints, never authority: the existing T2/terminal transactions
recheck the responsibility under their original locks. Notifications only reduce
idle latency; a restarted process reconstructs all work from PostgreSQL.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from sqlalchemy import BigInteger, Integer, Select, bindparam, case, func, or_, select, union_all
from sqlalchemy.orm import Session, aliased

from dlr.control.models import (
    EventDelivery,
    EventReceipt,
    EventSubscription,
    Execution,
    SourceCheckpoint,
)
from dlr.control.services import subscription_delivery as delivery

logger = logging.getLogger("dlr.control.subscription_scheduler")
Kind = Literal["pending", "processing"]
MAX_CANDIDATES = 100
MAX_PAYLOAD_BYTES = 1024**2
MAX_PASS_SECONDS = 0.050
_work_available = threading.Event()


def notify_delivery_work() -> None:
    """Call after a producer/Execution result transaction has committed."""
    _work_available.set()


def wait_delivery_work(timeout: float = 1.0) -> bool:
    """A lost notification is harmless: the durable fallback is at most 1s."""
    signalled = _work_available.wait(max(0.0, min(timeout, 1.0)))
    _work_available.clear()
    return signalled


@dataclass
class FairCursor:
    after_subscription_id: int = 0
    upper_subscription_id: int = 0


@dataclass(frozen=True)
class Candidate:
    delivery_id: int
    subscription_id: int
    payload_bytes: int


@dataclass(frozen=True)
class PassResult:
    selected: int
    attempted: int
    progressed: int
    budget_exhausted: bool

    @property
    def continue_immediately(self) -> bool:
        return self.progressed > 0 or self.budget_exhausted


def _candidate_query(kind: Kind) -> Select[Any]:
    if kind == "processing":
        # A full page of pending/running work can never hide completed work.
        # A shared Execution is one settlement transaction, even with 100
        # members. Deduplicate before assigning the per-subscription fair rank.
        ready_executions = (
            select(
                func.min(EventDelivery.id).label("delivery_id"),
                EventReceipt.subscription_id,
                EventDelivery.batch_execution_id,
                func.sum(EventReceipt.payload_bytes).label("single_payload_bytes"),
            )
            .join(EventReceipt, EventReceipt.id == EventDelivery.receipt_id)
            .join(
                Execution,
                Execution.id
                == func.coalesce(EventDelivery.execution_id, EventDelivery.batch_execution_id),
            )
            .where(
                EventDelivery.state == "processing",
                Execution.status.in_(delivery.EXECUTION_TERMINAL),
            )
            .group_by(
                Execution.id,
                EventReceipt.subscription_id,
                EventDelivery.batch_execution_id,
            )
        ).subquery()
        # One complete batch consumes the entire logical processing-byte pass.
        # Materialize/settle defer source_raw_body: bounded Envelope prefix and
        # shared Execution input are each <=256 KiB, result <=64 KiB, leaving
        # room for <=100 immutable manifest/member records within 1 MiB.
        # Neither prepaid copies/metadata nor retained-but-unread HTTP raw bytes
        # can make a legitimate batch permanently larger than this pass budget.
        eligible = (
            select(
                ready_executions.c.delivery_id,
                ready_executions.c.subscription_id,
                case(
                    (
                        ready_executions.c.batch_execution_id.is_not(None),
                        bindparam("batch_payload_budget", type_=BigInteger()),
                    ),
                    else_=ready_executions.c.single_payload_bytes,
                ).label("payload_bytes"),
            )
        ).subquery()
    else:
        first = aliased(EventReceipt)
        # One indexed seek per shard, not one predecessor query per pending
        # message. Failed heads remain in this index and block their own shard.
        head_id = (
            select(first.id)
            .where(
                first.subscription_id == SourceCheckpoint.subscription_id,
                first.source_epoch == SourceCheckpoint.source_epoch,
                first.shard == SourceCheckpoint.shard,
                first.state.not_in(delivery.TERMINAL),
            )
            .order_by(first.sequence)
            .limit(1)
            .correlate(SourceCheckpoint)
            .scalar_subquery()
        )
        heads = (
            select(
                EventDelivery.id.label("delivery_id"),
                EventReceipt.subscription_id,
                EventReceipt.payload_bytes,
            )
            .select_from(SourceCheckpoint)
            .join(EventReceipt, EventReceipt.id == head_id)
            .join(EventDelivery, EventDelivery.receipt_id == EventReceipt.id)
            .where(
                EventDelivery.state == "pending",
                or_(
                    EventReceipt.source_phase.is_(None),
                    EventReceipt.source_phase.not_in(("sync.completed", "sync.failed")),
                ),
            )
        )
        # Closing controls have an explicit predecessor exception in the
        # original protocol. Do not hide them behind an ordinary failed head.
        closings = (
            select(
                EventDelivery.id.label("delivery_id"),
                EventReceipt.subscription_id,
                EventReceipt.payload_bytes,
            )
            .join(EventReceipt, EventReceipt.id == EventDelivery.receipt_id)
            .where(
                EventDelivery.state == "pending",
                EventReceipt.source_phase.in_(("sync.completed", "sync.failed")),
            )
        )
        possible = union_all(heads, closings).subquery()
        eligible = (
            select(
                possible.c.delivery_id,
                possible.c.subscription_id,
                # Reserve one entire pass for a complete batch, using the same
                # bounded logical-work upper bound as terminal settlement.
                case(
                    (
                        EventDelivery.retained_batch_bytes > 0,
                        bindparam("batch_payload_budget", type_=BigInteger()),
                    ),
                    else_=possible.c.payload_bytes,
                ).label("payload_bytes"),
            )
            .join(EventDelivery, EventDelivery.id == possible.c.delivery_id)
            .join(EventSubscription, EventSubscription.id == possible.c.subscription_id)
            .where(
                or_(EventDelivery.retry_after.is_(None), EventDelivery.retry_after <= func.now()),
                or_(
                    EventSubscription.delivery_state != "repair",
                    (EventDelivery.revision_override_id.is_not(None))
                    & (EventDelivery.revision_override_id == EventSubscription.repair_revision_id),
                ),
            )
        ).subquery()
    return select(
        eligible.c.delivery_id,
        eligible.c.subscription_id,
        eligible.c.payload_bytes,
        func.row_number()
        .over(partition_by=eligible.c.subscription_id, order_by=eligible.c.delivery_id)
        .label("subscription_rank"),
    )


@lru_cache(maxsize=2)
def _selection_statement(kind: Kind) -> Select[Any]:
    """Reuse SQL structure only; every execution binds fresh cursor/budgets."""
    ranked = _candidate_query(kind).subquery()
    return (
        select(ranked.c.delivery_id, ranked.c.subscription_id, ranked.c.payload_bytes)
        .where(
            ranked.c.subscription_rank == 1,
            ranked.c.subscription_id > bindparam("after_subscription_id", type_=BigInteger()),
            ranked.c.subscription_id <= bindparam("upper_subscription_id", type_=BigInteger()),
        )
        .order_by(ranked.c.subscription_id)
        .limit(bindparam("candidate_limit", type_=Integer()))
    )


def select_candidates(session: Session, kind: Kind, cursor: FairCursor) -> list[Candidate]:
    """One ready head per subscription, over a finite rotating subscription sweep."""
    if kind not in {"pending", "processing"}:
        raise ValueError("invalid subscription scheduling kind")
    if not cursor.upper_subscription_id:
        cursor.upper_subscription_id = int(
            session.scalar(select(func.max(EventSubscription.id))) or 0
        )
    if not cursor.upper_subscription_id:
        return []
    rows = session.execute(
        _selection_statement(kind),
        {
            "after_subscription_id": cursor.after_subscription_id,
            "upper_subscription_id": cursor.upper_subscription_id,
            "candidate_limit": MAX_CANDIDATES,
            "batch_payload_budget": MAX_PAYLOAD_BYTES,
        },
    ).all()
    return [Candidate(int(row[0]), int(row[1]), int(row[2])) for row in rows]


def run_delivery_pass(
    session_factory: Callable[[], Session],
    kind: Kind,
    cursor: FairCursor,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> PassResult:
    """Independent 100-candidate/1MiB/50ms budget, one atomic transaction per item."""
    start = monotonic()
    with session_factory() as session:
        candidates = select_candidates(session, kind, cursor)
    attempted = progressed = payload_bytes = 0
    exhausted = False
    for candidate in candidates:
        if monotonic() - start >= MAX_PASS_SECONDS:
            exhausted = True
            break
        if payload_bytes + candidate.payload_bytes > MAX_PAYLOAD_BYTES:
            exhausted = True
            break
        payload_bytes += candidate.payload_bytes
        attempted += 1
        cursor.after_subscription_id = candidate.subscription_id
        with session_factory() as session:
            try:
                item = session.get(EventDelivery, candidate.delivery_id)
                changed = False
                if item is not None and item.state == kind:
                    if kind == "processing":
                        delivery.observe_terminal(session, candidate.delivery_id)
                        changed = item.state != "processing"
                    else:
                        changed = (
                            delivery.dispatch_delivery(session, candidate.delivery_id) is not None
                        )
                session.commit()
                if changed:
                    progressed += 1
                    if kind == "pending":
                        from dlr.control.services.outbox import notify_outbox

                        notify_outbox()
            except Exception:
                session.rollback()
                logger.warning(
                    "subscription %s scheduling transaction failed", kind, exc_info=False
                )
    if not exhausted and len(candidates) < MAX_CANDIDATES:
        cursor.after_subscription_id = cursor.upper_subscription_id = 0
    elif len(candidates) == MAX_CANDIDATES:
        exhausted = True
    return PassResult(len(candidates), attempted, progressed, exhausted)


class DeliveryScheduler:
    """Independent cursors prevent intake backlog from delaying completion."""

    def __init__(self) -> None:
        self.completion_cursor = FairCursor()
        self.dispatch_cursor = FairCursor()

    def complete(self, session_factory: Callable[[], Session]) -> PassResult:
        return run_delivery_pass(session_factory, "processing", self.completion_cursor)

    def dispatch(self, session_factory: Callable[[], Session]) -> PassResult:
        return run_delivery_pass(session_factory, "pending", self.dispatch_cursor)
