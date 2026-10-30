"""Retained Receipt, prepaid Execution input and metadata have different TTLs."""

import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select

from dlr.control.models import (
    Adapter,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    Execution,
    ExecutionOutbox,
    SubscriptionQuota,
    SyncBatch,
)
from dlr.control.services import subscription_accounting as accounting
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_sync
from dlr.control.services.input_config import database_now
from test_issue219_delivery import receive
from test_issue219_delivery import world as foundation_world

world = foundation_world


def facts(session, world):
    assert delivery.audit_quota(session, world["sub"])["differences"] == {}
    assert delivery.audit_global_quota(session)["differences"] == {}
    return session.get(SubscriptionQuota, f"sub:{world['sub']}")


def test_t1_prepays_execution_copy_and_t2_failure_rolls_back_all_handoff(
    world, session_factory, monkeypatch
):
    with session_factory.begin() as session:
        receipt = receive(session, world)
        item = session.scalar(select(EventDelivery).where(EventDelivery.receipt_id == receipt.id))
        size = accounting.compact_bytes(receipt.envelope)
        assert receipt.payload_bytes == 2 * size + 4608
        assert receipt.retained_payload_bytes == size
        assert item.retained_execution_bytes == size
        assert facts(session, world).history_bytes == receipt.payload_bytes
        identity, delivery_id = receipt.id, item.id

    def fail_after_materialization(*args):
        raise RuntimeError("injected after execution/outbox insertion")

    original = subscription_sync.freeze_completion
    monkeypatch.setattr(subscription_sync, "freeze_completion", fail_after_materialization)
    with pytest.raises(RuntimeError), session_factory.begin() as session:
        delivery.dispatch_delivery(session, delivery_id)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        assert session.scalar(select(func.count()).select_from(ExecutionOutbox)) == 0
        assert session.get(EventReceipt, identity).state == "pending"
        assert session.get(EventDelivery, delivery_id).retained_execution_bytes == size
        assert facts(session, world).pending_count == 1
    monkeypatch.setattr(subscription_sync, "freeze_completion", original)
    with session_factory.begin() as session:
        assert delivery.dispatch_delivery(session, delivery_id) is not None
        assert facts(session, world).pending_count == 0


def test_seven_day_cleanup_keeps_real_execution_input_and_thirty_day_clears_it(
    world, session_factory
):
    with session_factory.begin() as session:
        receipt = receive(session, world)
        item = session.scalar(select(EventDelivery).where(EventDelivery.receipt_id == receipt.id))
        execution = delivery.dispatch_delivery(session, item.id)
        original_input = deepcopy(execution.input)
        execution.status = "succeeded"
        delivery.observe_terminal(session, item.id)
        receipt.terminal_at = database_now(session) - timedelta(days=8)
        receipt_id, execution_id, delivery_id = receipt.id, execution.id, item.id
    with session_factory.begin() as session:
        assert delivery.cleanup_history(session, world["sub"])["payloads_expired"] == 1
        receipt = session.get(EventReceipt, receipt_id)
        item = session.get(EventDelivery, delivery_id)
        assert receipt.envelope is None and receipt.retained_payload_bytes == 0
        assert session.get(Execution, execution_id).input == original_input
        quota = facts(session, world)
        assert quota.history_count == 1
        assert quota.history_bytes == item.retained_execution_bytes + 4608
        receipt.terminal_at = database_now(session) - timedelta(days=31)
    with session_factory.begin() as session:
        assert delivery.cleanup_history(session, world["sub"])["metadata_deleted"] == 1
        assert session.get(EventReceipt, receipt_id) is None
        assert session.get(Execution, execution_id).input is None
        quota = facts(session, world)
        assert quota.history_count == quota.history_bytes == quota.dedupe_count == 0


def test_unused_input_reserve_and_deleted_execution_are_not_silently_unbilled(
    world, session_factory
):
    with session_factory.begin() as session:
        receipt = receive(session, world)
        item = session.scalar(select(EventDelivery).where(EventDelivery.receipt_id == receipt.id))
        execution = delivery.dispatch_delivery(session, item.id)
        execution.status = "succeeded"
        delivery.observe_terminal(session, item.id)
        receipt.terminal_at = database_now(session) - timedelta(days=8)
        receipt_id, execution_id, delivery_id = receipt.id, execution.id, item.id
    with session_factory.begin() as session:
        delivery.cleanup_history(session, world["sub"])
        before = facts(session, world).history_bytes
        session.delete(session.get(Execution, execution_id))
    with session_factory.begin() as session:
        assert session.get(EventDelivery, delivery_id).execution_id is None
        assert facts(session, world).history_bytes == before
        delivery.cleanup_history(session, world["sub"])
        quota = facts(session, world)
        assert quota.history_count == 0 and quota.history_bytes == 4608
        assert session.get(EventDelivery, delivery_id).retained_execution_bytes == 0
        assert session.get(EventReceipt, receipt_id) is not None


def test_replay_only_charges_new_execution_copy_and_skip_releases_unused_reserve_at_ttl(
    world, session_factory
):
    with session_factory.begin() as session:
        receipt = receive(session, world)
        initial = facts(session, world).history_bytes
        size = accounting.compact_bytes(receipt.envelope)
        delivery.skip_event(session, receipt.id, "skip-initial", None, "no target effect")
        session.get(EventSubscription, world["sub"]).desired_state = "disabled"
        replay = delivery.replay_event(
            session, receipt.id, "replay-one", None, "retry original input"
        )
        assert facts(session, world).history_bytes == initial + size + 512
        assert replay.retained_execution_bytes == size
        delivery.skip_event(session, receipt.id, "skip-replay", None, "no target effect")
        receipt.terminal_at = database_now(session) - timedelta(days=8)
    with session_factory.begin() as session:
        delivery.cleanup_history(session, world["sub"])
        quota = facts(session, world)
        assert quota.history_count == 0
        assert quota.history_bytes == 4096 + 2 * 512
        assert quota.pending_count == quota.pending_bytes == 0


def test_unresolved_failure_never_expires_its_input_budget(world, session_factory):
    with session_factory.begin() as session:
        receipt = receive(session, world)
        item = session.scalar(select(EventDelivery).where(EventDelivery.receipt_id == receipt.id))
        execution = delivery.dispatch_delivery(session, item.id)
        execution.status = "dead_letter"
        delivery.observe_terminal(session, item.id)
        receipt.terminal_at = database_now(session) - timedelta(days=40)
        budget = facts(session, world).history_bytes
        delivery.cleanup_history(session, world["sub"])
        assert receipt.envelope is not None and facts(session, world).history_bytes == budget


def test_permanent_history_purge_returns_all_input_layers_to_global(world, session_factory):
    with session_factory.begin() as session:
        receipt = receive(session, world)
        delivery.skip_event(session, receipt.id, "skip-before-delete", None, "delete test")
        sub = session.get(EventSubscription, world["sub"])
        sub.desired_state = "disabled"
        sub.connection_state = "stopped"
        sub.deleted_at = database_now(session)
        delivery.purge_deleted_adapter_history(session, world["adapter"])
        quota = session.get(SubscriptionQuota, "global")
        assert (
            quota.history_count
            == quota.history_bytes
            == quota.pending_count
            == quota.pending_bytes
            == 0
        )
        assert delivery.audit_global_quota(session)["differences"] == {}


def test_metadata_allowances_cover_maximal_legal_utf8_identity_and_future_timestamps():
    maximum = 9223372036854775807
    stamp = datetime(9999, 12, 31, 23, 59, 59, 999999, UTC)
    receipt = EventReceipt(
        id=maximum,
        subscription_id=maximum,
        activation_id=maximum,
        source_epoch="x" * 64,
        source_event_id="x" * 128,
        content_hash="a" * 64,
        shard="𐐷" * 512,
        sequence=maximum,
        first_source_event_id="x" * 128,
        object_key_hash="a" * 64,
        coalesced_count=maximum,
        duplicate_count=maximum,
        superseded_by_id=maximum,
        batch_id=maximum,
        source_phase="sync.completed",
        list_page_index=maximum,
        state="processing",
        first_received_at=stamp,
        last_seen_at=stamp,
        terminal_at=stamp,
        payload_expired_at=stamp,
    )
    accounting.receipt_budget(receipt, 1024**2)
    item = EventDelivery(
        id=maximum,
        receipt_id=maximum,
        execution_id=maximum,
        ordinal=maximum,
        activation_override_id=maximum,
        revision_override_id=maximum,
        state="processing",
        retry_after=stamp,
        created_at=stamp,
        completed_at=stamp,
    )
    accounting.delivery_budget(item, 1024**2)


def test_existing_quota_rows_skip_redundant_insert_and_first_creation_is_concurrent_safe(
    world, session_factory
):
    with session_factory.begin() as session:
        delivery._quotas(session, world["sub"])
        other_adapter = Adapter(name="quota-second", language="python", adapter_type="task")
        session.add(other_adapter)
        session.flush()
        other_sub = EventSubscription(
            adapter_id=other_adapter.id, source_type="kafka", source_epoch="other-epoch"
        )
        session.add(other_sub)
        session.flush()
        other_id = other_sub.id
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with session_factory.begin() as session:
            delivery._quotas(session, world["sub"])
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert not any("INSERT INTO subscription_quotas" in statement for statement in statements)
    with session_factory.begin() as session:
        session.delete(session.get(SubscriptionQuota, f"sub:{world['sub']}"))
        session.delete(session.get(SubscriptionQuota, "global"))
    barrier = threading.Barrier(2)

    def create_scope(subscription_id):
        with session_factory.begin() as session:
            delivery.lock_subscription(session, subscription_id)
            barrier.wait(5)
            delivery._quotas(session, subscription_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(create_scope, [world["sub"], other_id]))
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SubscriptionQuota)) == 3


@pytest.mark.parametrize(
    "states,disabled,unfinished,repair,expected",
    [
        ([], False, False, False, "idle"),
        (["succeeded"], False, False, False, "idle"),
        (["pending"], False, False, False, "pending"),
        (["processing", "pending"], False, False, False, "processing"),
        (["failed", "processing", "pending"], False, False, False, "blocked"),
        (["failed", "pending"], True, False, False, "blocked"),
        (["pending"], True, False, False, "draining"),
        (["processing"], True, False, False, "draining"),
        ([], True, False, False, "idle"),
        ([], False, True, False, "pending"),
        ([], True, True, False, "draining"),
        (["failed"], False, False, True, "repair"),
    ],
)
def test_indexed_status_projection_preserves_original_priority(
    world, session_factory, states, disabled, unfinished, repair, expected
):
    with session_factory.begin() as session:
        sub = session.get(EventSubscription, world["sub"])
        sub.desired_state = "disabled" if disabled else "enabled"
        sub.delivery_state = "repair" if repair else "idle"
        for index, state in enumerate(states, 1):
            session.add(
                EventReceipt(
                    subscription_id=sub.id,
                    activation_id=sub.activation_id,
                    source_epoch=sub.source_epoch,
                    source_event_id=f"projection-{index}",
                    content_hash="a" * 64,
                    envelope={},
                    payload_bytes=0,
                    shard="projection",
                    sequence=index,
                    state=state,
                )
            )
        if unfinished:
            session.add(
                SyncBatch(
                    subscription_id=sub.id,
                    scope_fingerprint="legacy",
                    state="processing",
                    read_complete=False,
                )
            )
        session.flush()
        delivery.refresh_delivery_state(session, sub)
        assert sub.delivery_state == expected
