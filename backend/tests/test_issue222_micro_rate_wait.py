"""Transient rate waits keep the bounded Rabbit owner; hard/racing refusals do not."""

import copy
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import DateTime, bindparam, func, literal, select

from dlr.control.models import EventReceipt, SourceCheckpoint, SubscriptionLease, SubscriptionQuota
from dlr.control.services import input_config, subscription_t1_unit
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_queue as queue
from dlr.control.services.input_config import database_now
from dlr.control.services.subscription_source import SourceFailure
from test_issue222_rabbit_micro_t1 import MemoryTransport, run
from test_issue222_rabbit_micro_t1 import batch_world as foundation_batch_world
from test_issue222_rabbit_micro_t1 import micro_context as foundation_micro_context
from test_issue222_rabbit_micro_t1 import world as foundation_world

world = foundation_world
batch_world = foundation_batch_world
micro_context = foundation_micro_context

NOW = datetime(2026, 10, 26, tzinfo=UTC)
LIMITS = {
    "pending_count": 100,
    "pending_bytes": 100000,
    "history_count": 200,
    "history_bytes": 200000,
    "dedupe_count": 300,
    "rate": 250,
}


def quota(scope, *, rate=0, window=None, **values):
    return SimpleNamespace(
        scope=scope,
        **{
            **dict.fromkeys(LIMITS, 0),
            "reserved_control_count": 0,
            "reserved_control_bytes": 0,
            "rate_count": rate,
            "rate_window_at": window,
            **values,
        },
    )


def decision_context(monkeypatch, rows, *, aged_pending=False, global_rate=1000):
    queries = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, statement):
            queries.append("clock/aged_pending")
            return SimpleNamespace(one=lambda: (NOW, aged_pending))

        def scalars(self, statement):
            queries.append("quotas")
            return rows

    monkeypatch.setattr(queue, "effective_profile", lambda _: object())
    monkeypatch.setattr(queue, "runtime_limits_for_profile", lambda *_: dict(LIMITS))
    monkeypatch.setattr(queue, "global_limits", lambda **_: LIMITS | {"rate": global_rate})
    return (
        SimpleNamespace(subscription_id=7, session_factory=Session, source_config={}),
        queries,
    )


@pytest.mark.parametrize(
    ("source_count", "global_count", "remaining"),
    [(240, 0, 10), (0, 999, 1), (240, 995, 5)],
)
def test_advisory_tokens_use_minimum_of_source_and_global_without_writes(
    monkeypatch, source_count, global_count, remaining
):
    rows = [
        quota("sub:7", rate=source_count, window=NOW - timedelta(seconds=0.6)),
        quota("global", rate=global_count, window=NOW - timedelta(seconds=0.3)),
    ]
    before = copy.deepcopy([vars(row) for row in rows])
    context, queries = decision_context(monkeypatch, rows)
    assert queue._capacity_decision(context) == queue._CapacityDecision(True, remaining)
    assert queries == ["clock/aged_pending", "quotas"]
    assert [vars(row) for row in rows] == before


@pytest.mark.parametrize(
    ("source_count", "global_count", "wait"), [(250, 0, 0.4), (0, 1000, 0.7), (250, 1000, 0.7)]
)
def test_rate_only_wait_is_bounded_by_blocking_windows(
    monkeypatch, source_count, global_count, wait
):
    context, _ = decision_context(
        monkeypatch,
        [
            quota("sub:7", rate=source_count, window=NOW - timedelta(seconds=0.6)),
            quota("global", rate=global_count, window=NOW - timedelta(seconds=0.3)),
        ],
    )
    result = queue._capacity_decision(context)
    assert result.hard_ready and result.rate_tokens == 0
    assert result.wait_seconds == pytest.approx(wait)
    assert queue.capacity_ready(context) is False  # Original per-event boolean remains closed.


def test_expired_rate_window_and_missing_rows_restore_full_advisory_rate(monkeypatch):
    context, _ = decision_context(
        monkeypatch,
        [quota("sub:7", rate=250, window=NOW - timedelta(seconds=1))],
    )
    assert queue._capacity_decision(context) == queue._CapacityDecision(True, 250)
    assert queue.capacity_ready(context) is True


@pytest.mark.parametrize("full_scope", ["sub:7", "global"])
def test_hard_high_water_takes_priority_over_rate_wait(monkeypatch, full_scope):
    rows = [quota("sub:7", rate=250, window=NOW), quota("global")]
    next(row for row in rows if row.scope == full_scope).pending_count = 81
    context, _ = decision_context(monkeypatch, rows)
    assert queue._capacity_decision(context) == queue._CapacityDecision(False, 0)


def test_reserved_hard_capacity_and_oldest_pending_keep_original_gate(monkeypatch):
    context, _ = decision_context(
        monkeypatch,
        [quota("sub:7", pending_count=80, reserved_control_count=1)],
    )
    assert not queue._capacity_decision(context).hard_ready
    context, queries = decision_context(monkeypatch, [], aged_pending=True)
    assert queue._capacity_decision(context) == queue._CapacityDecision(False, 0)
    assert queries == ["clock/aged_pending"]


@pytest.fixture
def owner(monkeypatch):
    context = SimpleNamespace(
        source_config={
            "source_type": "rabbitmq",
            "mode": "business_batch",
            "cluster_id": "fixture",
        },
        subscription_id=7,
        config_version=1,
        source_epoch="epoch-1",
        generation=9,
        stop_event=threading.Event(),
        connected=lambda: None,
    )
    monkeypatch.setattr(queue, "capacity_ready", lambda _: True)
    return context


def test_initial_rate_zero_waits_before_allocating_or_opening_sdk(owner, monkeypatch):
    decisions = iter([queue._CapacityDecision(True, 0, 0.25)])
    monkeypatch.setattr(
        queue, "_capacity_decision", lambda _: next(decisions, queue._CapacityDecision(True, 16))
    )
    order, transports = [], []

    def wait(context, seconds):
        assert not transports and context.generation == 9
        order.append(("wait", seconds))
        return False

    def open_context(context):
        assert order == [("wait", 0.25)]
        order.append(("open", None))
        transports.append(MemoryTransport(context))
        return transports[-1]

    monkeypatch.setattr(queue, "_wait_for_rate", wait)
    monkeypatch.setattr(queue, "_receive_micro", lambda _, group: (len(group), None, None))
    runner = queue.QueueSourceRunner()
    runner.open_context = open_context
    runner.run(owner)
    assert transports[0].acked == [1, 2, 3, 4] and transports[0].closed
    assert transports[0].wire.scope.used_bytes == 0 and owner.generation == 9


@pytest.mark.parametrize("cancel", [False, True])
def test_initial_starvation_or_stop_never_opens_sdk(owner, monkeypatch, cancel):
    monkeypatch.setattr(
        queue, "_capacity_decision", lambda _: queue._CapacityDecision(True, 0, 0.4)
    )
    elapsed, waits = [0.0], []
    monkeypatch.setattr(queue, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))

    def wait(context, seconds):
        waits.append(seconds)
        elapsed[0] += seconds
        if cancel:
            context.stop_event.set()
        return cancel

    monkeypatch.setattr(queue, "_wait_for_rate", wait)
    monkeypatch.setattr(queue, "record_fault", lambda *_: pytest.fail("Rate wait is not poison"))
    runner = queue.QueueSourceRunner()
    runner.open_context = lambda _: pytest.fail("SDK must not open before readiness")
    if cancel:
        runner.run(owner)
        assert waits == [0.4]
    else:
        with pytest.raises(SourceFailure, match="subscription_capacity_exceeded"):
            runner.run(owner)
        assert sum(waits) == pytest.approx(1)
    assert owner.generation == 9


def test_initial_hard_refusal_does_not_wait_or_open(owner, monkeypatch):
    monkeypatch.setattr(queue, "_capacity_decision", lambda _: queue._CapacityDecision(False, 0))
    monkeypatch.setattr(queue, "_wait_for_rate", lambda *_: pytest.fail("Hard limits cannot wait"))
    runner = queue.QueueSourceRunner()
    runner.open_context = lambda _: pytest.fail("Hard limits cannot open SDK")
    with pytest.raises(SourceFailure, match="subscription_capacity_exceeded"):
        runner.run(owner)


def test_unknown_sdk_after_startup_wait_retains_single_item_capacity_gate(owner, monkeypatch):
    decisions = iter([queue._CapacityDecision(True, 0, 0.25)])
    monkeypatch.setattr(
        queue, "_capacity_decision", lambda _: next(decisions, queue._CapacityDecision(True, 16))
    )
    order = []
    monkeypatch.setattr(queue, "_wait_for_rate", lambda *_: order.append("wait") or False)
    monkeypatch.setattr(queue, "capacity_ready", lambda _: order.append("legacy-check") or False)
    transport = SimpleNamespace(
        poll=lambda: pytest.fail("Legacy capacity refusal must precede poll"),
        close=lambda: order.append("close"),
    )
    runner = queue.QueueSourceRunner()
    runner.open_context = lambda _: order.append("open") or transport
    with pytest.raises(SourceFailure, match="subscription_capacity_exceeded"):
        runner.run(owner)
    assert order == ["wait", "open", "legacy-check", "close"]


@pytest.mark.parametrize(
    ("source_type", "mode"), [("rabbitmq", "per_event"), ("rocketmq", "business_batch")]
)
def test_other_sources_and_per_event_keep_original_startup_gate(
    owner, monkeypatch, source_type, mode
):
    owner.source_config.update(source_type=source_type, mode=mode)
    monkeypatch.setattr(queue, "capacity_ready", lambda _: False)
    monkeypatch.setattr(
        queue, "_capacity_after_rate_wait", lambda _: pytest.fail("Only requested micro waits")
    )
    runner = queue.QueueSourceRunner()
    runner.open_context = lambda _: pytest.fail("Original capacity refusal must precede open")
    with pytest.raises(SourceFailure, match="subscription_capacity_exceeded"):
        runner.run(owner)


def test_remaining_ten_tokens_forms_ten_then_six_without_rate_failure(owner, monkeypatch):
    groups = []
    decisions = iter([queue._CapacityDecision(True, 10)] * 2 + [queue._CapacityDecision(True, 6)])
    monkeypatch.setattr(
        queue, "_capacity_decision", lambda _: next(decisions, queue._CapacityDecision(True, 16))
    )

    def receive(_, group):
        groups.append([item.message.handle for item in group])
        return len(group), None, None

    monkeypatch.setattr(queue, "_receive_micro", receive)
    monkeypatch.setattr(queue, "record_fault", lambda *_: pytest.fail("Unexpected fault"))
    transport = MemoryTransport(owner, count=16)
    run(owner, transport)
    assert groups == [list(range(1, 11)), list(range(11, 17))]
    assert transport.acked == list(range(1, 17)) and transport.closed
    assert owner.generation == 9 and transport.wire.scope.used_bytes == 0


def test_rate_pause_keeps_credit_and_owner_until_same_connection_resumes(owner, monkeypatch):
    decisions = iter([queue._CapacityDecision(True, 16), queue._CapacityDecision(True, 0, 0.25)])
    monkeypatch.setattr(
        queue, "_capacity_decision", lambda _: next(decisions, queue._CapacityDecision(True, 16))
    )
    transport = MemoryTransport(owner, count=16)
    before = transport.wire.scope.used_bytes
    waits = []

    def wait(seconds):
        assert not transport.closed and not transport.acked
        assert len(transport.pending) == 16  # No native poll/decode during the pause.
        assert transport.wire.scope.used_bytes == before
        assert owner.generation == 9
        waits.append(seconds)
        return False

    monkeypatch.setattr(owner.stop_event, "wait", wait)
    monkeypatch.setattr(queue, "_receive_micro", lambda _, group: (len(group), None, None))
    run(owner, transport)
    assert waits == [0.25] and transport.acked == list(range(1, 17))
    assert transport.closed and transport.wire.scope.used_bytes == 0


def test_continuous_global_rate_starvation_closes_after_one_total_second(owner, monkeypatch):
    decisions = iter([queue._CapacityDecision(True, 16)])
    monkeypatch.setattr(
        queue,
        "_capacity_decision",
        lambda _: next(decisions, queue._CapacityDecision(True, 0, 0.4)),
    )
    clock = [0.0]
    monkeypatch.setattr(queue, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    transport = MemoryTransport(owner, count=16)
    before = transport.wire.scope.used_bytes
    waits = []

    def wait(seconds):
        assert transport.wire.scope.used_bytes == before
        assert not transport.closed and not transport.acked and len(transport.pending) == 16
        waits.append(seconds)
        clock[0] += seconds
        return False

    monkeypatch.setattr(owner.stop_event, "wait", wait)
    monkeypatch.setattr(queue, "record_fault", lambda *_: pytest.fail("Rate pause is not poison"))
    with pytest.raises(SourceFailure, match="subscription_capacity_exceeded"):
        run(owner, transport)
    assert len(waits) == 3 and sum(waits) == pytest.approx(1.0)
    assert all(0 < seconds <= 1 for seconds in waits)
    assert transport.closed and transport.acked == [] and transport.wire.scope.used_bytes == 0


def test_new_hard_refusal_after_rate_pause_closes_without_polling(owner, monkeypatch):
    decisions = iter(
        [
            queue._CapacityDecision(True, 16),
            queue._CapacityDecision(True, 0, 0.25),
            queue._CapacityDecision(False, 0),
        ]
    )
    monkeypatch.setattr(queue, "_capacity_decision", lambda _: next(decisions))
    transport = MemoryTransport(owner, count=16)
    waits = []
    monkeypatch.setattr(owner.stop_event, "wait", lambda seconds: waits.append(seconds) or False)
    monkeypatch.setattr(queue, "record_fault", lambda *_: pytest.fail("Precheck is not poison"))
    with pytest.raises(SourceFailure, match="subscription_capacity_exceeded"):
        run(owner, transport)
    assert waits == [0.25] and transport.closed and transport.acked == []
    assert transport.wire.scope.used_bytes == 0


def test_stop_interrupts_rate_wait_without_polling_or_custody(owner, monkeypatch):
    decisions = iter([queue._CapacityDecision(True, 16)])
    monkeypatch.setattr(
        queue,
        "_capacity_decision",
        lambda _: next(decisions, queue._CapacityDecision(True, 0, 1)),
    )
    transport = MemoryTransport(owner, count=16)
    waiting = threading.Event()
    original = owner.stop_event.wait
    failure = []

    def wait(seconds):
        waiting.set()
        return original(seconds)

    def target():
        try:
            run(owner, transport)
        except Exception as error:
            failure.append(error)

    monkeypatch.setattr(owner.stop_event, "wait", wait)
    monkeypatch.setattr(queue, "_receive_micro", lambda *_: pytest.fail("Unexpected T1"))
    thread = threading.Thread(target=target)
    thread.start()
    try:
        assert waiting.wait(5)
        assert not transport.closed and len(transport.pending) == 16
        assert transport.wire.scope.used_bytes > 0
    finally:
        owner.stop_event.set()
        thread.join(5)
    assert not thread.is_alive() and not failure
    assert transport.closed and transport.acked == [] and transport.wire.scope.used_bytes == 0


def test_concurrent_rate_race_keeps_original_prefix_ack_then_close(owner, monkeypatch):
    monkeypatch.setattr(queue, "_capacity_decision", lambda _: queue._CapacityDecision(True, 10))
    error = HTTPException(429, {"code": "subscription_capacity_exceeded"})
    monkeypatch.setattr(queue, "_receive_micro", lambda *_: (3, error, 3))
    faults = []
    monkeypatch.setattr(
        queue, "record_fault", lambda context, failure, message: faults.append(message.handle)
    )
    transport = MemoryTransport(owner, count=16)
    with pytest.raises(SourceFailure, match="subscription_capacity_exceeded"):
        run(owner, transport)
    assert transport.acked == [1, 2, 3] and faults == [4]
    assert transport.closed and transport.wire.scope.used_bytes == 0


def seed_rate(context, scope, count):
    with context.session_factory.begin() as session:
        row = session.get(SubscriptionQuota, scope, with_for_update=True)
        if row is None:
            row = SubscriptionQuota(scope=scope)
            session.add(row)
        row.rate_count, row.rate_window_at = count, database_now(session)


@pytest.fixture
def controlled_clock(micro_context, monkeypatch):
    # This case tests rate windows with real PostgreSQL transactions, not
    # wall-clock lease expiry. Keep all owner checks within the fixture lease.
    with micro_context.session_factory() as session:
        lease = session.get(SubscriptionLease, micro_context.subscription_id)
        now = lease.lease_until - timedelta(seconds=15)
    clock = SimpleNamespace(now=now, elapsed=0.0)

    class Functions:
        def clock_timestamp(self):
            return literal(clock.now, type_=DateTime(timezone=True))

        def __getattr__(self, name):
            return getattr(func, name)

    # Only these module-local SQL function proxies change; global SQLAlchemy
    # functions and the production database clock implementation stay intact.
    for module in (queue, subscription_t1_unit, input_config):
        monkeypatch.setattr(module, "func", Functions())
    # T1 caches its SQL shape at import time. Bind the same advancing test
    # clock there too, preserving the original control-priority expression.
    monkeypatch.setattr(
        subscription_t1_unit,
        "_CLOCK_QUERY",
        select(
            bindparam("controlled_now", callable_=lambda: clock.now, type_=DateTime(timezone=True)),
            subscription_t1_unit._CLOCK_QUERY.selected_columns[1],
        ),
    )
    monkeypatch.setattr(queue, "time", SimpleNamespace(monotonic=lambda: clock.elapsed))
    return clock


@pytest.mark.parametrize("global_scope", [False, True])
def test_real_database_ten_tokens_wait_then_commit_six_on_same_owner(
    micro_context, monkeypatch, global_scope, controlled_clock
):
    scope = "global" if global_scope else f"sub:{micro_context.subscription_id}"
    seed_rate(micro_context, scope, 990)
    groups, waits, committed = [], [], []
    original_receive = queue._receive_micro
    transport = MemoryTransport(micro_context, count=16)

    def receive(context, group):
        groups.append(len(group))
        return original_receive(context, group)

    def wait(context, seconds):
        assert not transport.closed and transport.acked == list(range(1, 11))
        assert context.session_factory.kw["bind"].pool.checkedout() == 0
        before = transport.wire.scope.used_bytes
        with context.session_factory() as session:
            lease = session.get(SubscriptionLease, context.subscription_id)
            assert lease.owner_id == context.owner_id and lease.generation == context.generation
        waits.append(seconds)
        controlled_clock.now += timedelta(seconds=seconds)
        controlled_clock.elapsed += seconds
        assert transport.wire.scope.used_bytes == before
        return context.stop_event.is_set()

    def before_ack(tags):
        with micro_context.session_factory() as session:
            committed.append(session.scalar(select(func.count()).select_from(EventReceipt)))

    transport.before_ack = before_ack
    monkeypatch.setattr(queue, "_receive_micro", receive)
    monkeypatch.setattr(queue, "_wait_for_rate", wait)
    run(micro_context, transport)
    assert groups == [10, 6] and committed == [10, 16]
    assert waits == [1] and controlled_clock.elapsed == 1
    assert transport.acked == list(range(1, 17)) and transport.closed
    with micro_context.session_factory() as session:
        assert delivery.audit_quota(session, micro_context.subscription_id)["differences"] == {}
        assert delivery.audit_global_quota(session)["differences"] == {}
        assert session.scalar(select(func.count()).select_from(SourceCheckpoint)) == 1
        assert session.scalar(select(SourceCheckpoint.blocked_unaccepted)) is None


def test_real_database_concurrent_rate_change_keeps_authoritative_prefix(
    micro_context, monkeypatch, controlled_clock
):
    original = queue._receive_micro
    groups = []

    def race(context, group):
        groups.append(len(group))
        seed_rate(context, f"sub:{context.subscription_id}", 998)
        return original(context, group)

    monkeypatch.setattr(queue, "_receive_micro", race)
    transport = MemoryTransport(micro_context, count=16)
    with pytest.raises(SourceFailure, match="subscription_capacity_exceeded"):
        run(micro_context, transport)
    assert groups == [16] and transport.acked == [1, 2] and transport.closed
    with micro_context.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 2
        assert delivery.audit_quota(session, micro_context.subscription_id)["differences"] == {}
        assert delivery.audit_global_quota(session)["differences"] == {}
        assert session.scalar(select(SourceCheckpoint.blocked_unaccepted))["code"] == (
            "subscription_capacity_exceeded"
        )
