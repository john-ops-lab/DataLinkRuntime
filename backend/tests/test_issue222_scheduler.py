"""PostgreSQL head/fairness and bounded publisher recovery regressions."""

import json
import threading
import time
from datetime import timedelta

import pika
import pytest
from sqlalchemy import func, select

from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    AdapterVersion,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    Execution,
    ExecutionOutbox,
    SourceCheckpoint,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionExecutionBatch,
    Worker,
)
from dlr.control.services import outbox, rabbitmq
from dlr.control.services import subscription_scheduler as scheduler
from dlr.control.services.ingress_capacity import DEFAULT_GLOBAL_LIMITS, DEFAULT_SUBSCRIPTION_LIMITS
from dlr.control.services.input_config import database_now
from test_issue130_b1_runtime import _fake_dispatch_outbox_row, _HangingConnection
from test_issue222_business_batch import world as business_batch_foundation
from test_issue222_http_ingress import http_world as http_custody_world

world = business_batch_foundation
http_world = http_custody_world


def _subscription(session, number):
    worker = Worker(name=f"scheduler-{number}", status="online", capabilities=["python"])
    adapter = Adapter(name=f"scheduler-{number}", language="python", adapter_type="task")
    session.add_all([worker, adapter])
    session.flush()
    revision = AdapterVersion(adapter_id=adapter.id, seq=1, code="pass", requirements="")
    session.add(revision)
    session.flush()
    adapter.latest_version_id, adapter.runtime_worker_id = revision.id, worker.id
    sub = EventSubscription(adapter_id=adapter.id, source_type="kafka", source_epoch="epoch")
    session.add(sub)
    session.flush()
    config = SubscriptionConfigVersion(
        subscription_id=sub.id, version=1, source_epoch="epoch", config={}
    )
    session.add(config)
    session.flush()
    activation = SubscriptionActivation(
        subscription_id=sub.id,
        config_version_id=config.id,
        revision_id=revision.id,
        worker_id=worker.id,
    )
    session.add(activation)
    session.flush()
    sub.activation_id = activation.id
    session.add(SourceCheckpoint(subscription_id=sub.id, source_epoch="epoch", shard="a"))
    session.flush()
    return sub, activation


def _receipt(session, sub, activation, sequence, *, state="pending", shard="a"):
    receipt = EventReceipt(
        subscription_id=sub.id,
        activation_id=activation.id,
        source_epoch="epoch",
        source_event_id=f"{shard}-{sequence}",
        content_hash="0" * 64,
        envelope={},
        payload_bytes=2048,
        shard=shard,
        sequence=sequence,
        state=state,
    )
    session.add(receipt)
    session.flush()
    item = EventDelivery(receipt_id=receipt.id, state=state)
    session.add(item)
    session.flush()
    return receipt, item


def test_full_pending_page_cannot_hide_a_terminal_execution(api_client, session_factory):
    with session_factory.begin() as session:
        sub, activation = _subscription(session, 1)
        for i in range(150):
            _receipt(session, sub, activation, i)
        other, other_activation = _subscription(session, 2)
        _, completed = _receipt(session, other, other_activation, 0, state="processing")
        execution = Execution(
            adapter_id=other.adapter_id,
            version_id=other_activation.revision_id,
            trigger="subscription",
            input={},
            status="succeeded",
        )
        session.add(execution)
        session.flush()
        completed.execution_id = execution.id
        expected = completed.id
    with session_factory() as session:
        found = scheduler.select_candidates(session, "processing", scheduler.FairCursor())
        assert [item.delivery_id for item in found] == [expected]
    result = scheduler.DeliveryScheduler().complete(session_factory)
    assert result.progressed == 1
    with session_factory() as session:
        assert session.get(EventDelivery, expected).state == "succeeded"


def test_shared_terminal_settles_once_despite_large_retained_raw_and_rotates(
    http_world, session_factory, monkeypatch
):
    from dlr.control.services import http_ingress
    from dlr.control.services import subscription_business_batch as batch
    from dlr.control.services import subscription_delivery as delivery

    monkeypatch.setattr(
        settings,
        "ingress_capacity_profile",
        json.dumps(
            {
                "global": DEFAULT_GLOBAL_LIMITS | {"rate": 1000},
                "subscription_max": DEFAULT_SUBSCRIPTION_LIMITS | {"rate": 1000},
            }
        ),
    )
    with session_factory.begin() as session:
        config = session.get(SubscriptionConfigVersion, http_world["config"])
        config.config = config.config | {"mode": "business_batch", "limits": {"rate": 1000}}
        session.get(AdapterVersion, http_world["revision"]).runtime_config = {
            "event_batch_schema": "dlr.event.batch.v1"
        }
        payload = b'{"value":1}'
        raw = payload + b" " * (64 * 1024 - len(payload))
        ids = [
            http_ingress.receive(
                session, http_world["public_id"], "Bearer source-token", f"large-raw-{index}", raw
            )[1]["receipt_id"]
            for index in range(100)
        ]
        head = session.scalar(
            select(func.min(EventDelivery.id)).where(EventDelivery.receipt_id.in_(ids))
        )
        execution = batch.materialize_batch(session, head)
        group = session.get(SubscriptionExecutionBatch, execution.id)
        assert group.member_count == 100
        assert group.input_bytes < 256 * 1024
        prepaid = session.scalar(
            select(func.sum(EventReceipt.payload_bytes)).where(EventReceipt.id.in_(ids))
        )
        retained = session.scalar(
            select(func.sum(EventReceipt.retained_payload_bytes)).where(EventReceipt.id.in_(ids))
        )
        assert prepaid > scheduler.MAX_PAYLOAD_BYTES
        assert retained > 6 * scheduler.MAX_PAYLOAD_BYTES
        execution.status = "cancelled"
        batch.finalize_on_terminal(session, execution)
        batch_execution_id = execution.id
        batch_hint_id = session.scalar(
            select(func.min(EventDelivery.id)).where(
                EventDelivery.batch_execution_id == execution.id
            )
        )
        other, activation = _subscription(session, 99)
        _, single = _receipt(session, other, activation, 0, state="processing")
        completed = Execution(
            adapter_id=other.adapter_id,
            version_id=activation.revision_id,
            trigger="subscription",
            input={},
            status="succeeded",
        )
        session.add(completed)
        session.flush()
        single.execution_id = completed.id
        single_id = single.id
    with session_factory() as session:
        found = scheduler.select_candidates(session, "processing", scheduler.FairCursor())
        assert [candidate.delivery_id for candidate in found] == [batch_hint_id, single_id]
        assert found[0].payload_bytes == scheduler.MAX_PAYLOAD_BYTES

    # The batch's 100 members consume one candidate/transaction. The next
    # subscription waits only for the next bounded pass, not for another visit
    # for each member. The byte budget is still enforced before that transaction.
    cursor = scheduler.FairCursor()
    first = scheduler.run_delivery_pass(
        session_factory, "processing", cursor, monotonic=lambda: 0.0
    )
    assert first.selected == 2
    assert first.attempted == first.progressed == 1
    assert first.budget_exhausted
    with session_factory() as session:
        members = list(
            session.scalars(
                select(EventDelivery).where(EventDelivery.batch_execution_id == batch_execution_id)
            )
        )
        assert len(members) == 100
        assert all(
            item.state == "failed" and item.batch_member_outcome == "unknown" for item in members
        )
        assert session.get(EventDelivery, single_id).state == "processing"
        assert delivery.audit_quota(session, http_world["sub"])["differences"] == {}
    second = scheduler.run_delivery_pass(
        session_factory, "processing", cursor, monotonic=lambda: 0.0
    )
    assert second.attempted == second.progressed == 1
    with session_factory() as session:
        assert session.get(EventDelivery, single_id).state == "succeeded"
        assert scheduler.select_candidates(session, "processing", scheduler.FairCursor()) == []


def test_failed_shard_does_not_fill_page_or_hide_another_shard(api_client, session_factory):
    with session_factory.begin() as session:
        sub, activation = _subscription(session, 1)
        _receipt(session, sub, activation, 0, state="failed")
        for i in range(1, 150):
            _receipt(session, sub, activation, i)
        session.add(SourceCheckpoint(subscription_id=sub.id, source_epoch="epoch", shard="b"))
        _, ready = _receipt(session, sub, activation, 0, shard="b")
        expected = ready.id
    with session_factory() as session:
        found = scheduler.select_candidates(session, "pending", scheduler.FairCursor())
        assert [item.delivery_id for item in found] == [expected]


def test_ten_subscriptions_rotate_without_continuously_growing_tail_starvation(
    api_client, session_factory, monkeypatch
):
    monkeypatch.setattr(scheduler, "MAX_CANDIDATES", 3)
    with session_factory.begin() as session:
        expected = []
        for number in range(10):
            sub, activation = _subscription(session, number)
            _, item = _receipt(session, sub, activation, 0)
            expected.append(item.id)
    cursor = scheduler.FairCursor()
    selected = []
    for _ in range(4):
        with session_factory() as session:
            page = scheduler.select_candidates(session, "pending", cursor)
            selected.extend(item.delivery_id for item in page)
            cursor.after_subscription_id = page[-1].subscription_id
        if len(selected) == 3:
            with session_factory.begin() as session:
                new, activation = _subscription(session, 20)
                _receipt(session, new, activation, 0)
    assert selected == expected
    # A restarted scheduler independently reconstructs all ready heads.
    with session_factory() as session:
        again = scheduler.select_candidates(session, "pending", scheduler.FairCursor())
        assert [item.delivery_id for item in again] == expected[:3]


def test_reused_selection_binds_current_budgets_cursor_and_database_state(
    api_client, session_factory, monkeypatch
):
    identities = []
    with session_factory.begin() as session:
        for number in range(3):
            sub, activation = _subscription(session, number)
            _, item = _receipt(session, sub, activation, 0)
            item.retained_batch_bytes = 8192
            identities.append((sub.id, item.id))
    statement = scheduler._selection_statement("pending")
    monkeypatch.setattr(scheduler, "MAX_CANDIDATES", 1)
    monkeypatch.setattr(scheduler, "MAX_PAYLOAD_BYTES", 4096)
    with session_factory() as session:
        first = scheduler.select_candidates(session, "pending", scheduler.FairCursor())
        assert [(item.delivery_id, item.payload_bytes) for item in first] == [
            (identities[0][1], 4096)
        ]
    monkeypatch.setattr(scheduler, "MAX_CANDIDATES", 2)
    monkeypatch.setattr(scheduler, "MAX_PAYLOAD_BYTES", 8192)
    cursor = scheduler.FairCursor(identities[0][0], identities[2][0])
    with session_factory() as session:
        page = scheduler.select_candidates(session, "pending", cursor)
        assert [(item.delivery_id, item.payload_bytes) for item in page] == [
            (identity[1], 8192) for identity in identities[1:]
        ]
    with session_factory.begin() as session:
        head = session.get(EventDelivery, identities[1][1])
        head.retry_after = database_now(session) + timedelta(seconds=60)
    with session_factory() as session:
        ready = scheduler.select_candidates(session, "pending", cursor)
        assert [item.delivery_id for item in ready] == [identities[2][1]]
        cursor.upper_subscription_id = identities[1][0]
        assert scheduler.select_candidates(session, "pending", cursor) == []
    assert scheduler._selection_statement("pending") is statement


def test_delayed_head_preserves_order_instead_of_dispatching_its_successor(
    api_client, session_factory
):
    with session_factory.begin() as session:
        sub, activation = _subscription(session, 1)
        _, head = _receipt(session, sub, activation, 0)
        head.retry_after = database_now(session) + timedelta(seconds=60)
        _receipt(session, sub, activation, 1)
    with session_factory() as session:
        assert scheduler.select_candidates(session, "pending", scheduler.FairCursor()) == []


def test_ten_batch_entries_dispatch_one_per_bounded_pass_without_tail_starvation(
    api_client, session_factory, monkeypatch
):
    identities = []
    with session_factory.begin() as session:
        for number in range(10):
            sub, activation = _subscription(session, number)
            _, item = _receipt(session, sub, activation, 0)
            # T1's immutable opt-in budget distinguishes a potential complete
            # batch from an ordinary single-event head; no mutable config read.
            item.retained_batch_bytes = 8192
            identities.append(item.id)
    with session_factory() as session:
        candidates = scheduler.select_candidates(session, "pending", scheduler.FairCursor())
        assert [candidate.payload_bytes for candidate in candidates] == [1024 * 1024] * 10
    calls = []

    def dispatch(session, identity):
        calls.append(identity)
        session.get(EventDelivery, identity).state = "processing"
        return object()

    monkeypatch.setattr(scheduler.delivery, "dispatch_delivery", dispatch)
    cursor = scheduler.FairCursor()
    clock = [0.0]
    for index in range(10):
        result = scheduler.run_delivery_pass(
            session_factory, "pending", cursor, monotonic=lambda: clock[0]
        )
        assert result.attempted == result.progressed == 1
        assert result.continue_immediately
        assert calls == identities[: index + 1]
        # Simulate a complete 50 ms scheduling budget per pass. The progress
        # signal requests the next pass immediately, with no 1 s idle fallback.
        clock[0] += scheduler.MAX_PASS_SECONDS
    assert clock[0] < 2.0
    assert calls == identities


def test_dispatch_pass_stops_before_starting_another_transaction_at_deadline(
    api_client, session_factory, monkeypatch
):
    with session_factory.begin() as session:
        for number in range(2):
            sub, activation = _subscription(session, number)
            _receipt(session, sub, activation, 0)
    clock = [0.0]
    calls = []

    def dispatch(session, identity):
        calls.append(identity)
        clock[0] += 0.051
        session.get(EventDelivery, identity).state = "processing"
        return object()

    monkeypatch.setattr(scheduler.delivery, "dispatch_delivery", dispatch)
    result = scheduler.run_delivery_pass(
        session_factory, "pending", scheduler.FairCursor(), monotonic=lambda: clock[0]
    )
    assert len(calls) == result.attempted == result.progressed == 1
    assert result.budget_exhausted
    with session_factory() as session:
        assert list(session.scalars(select(EventDelivery.state).order_by(EventDelivery.id))) == [
            "processing",
            "pending",
        ]


def test_pass_rollback_retains_responsibility_and_does_not_report_progress(
    api_client, session_factory, monkeypatch
):
    with session_factory.begin() as session:
        sub, activation = _subscription(session, 1)
        _, item = _receipt(session, sub, activation, 0)
        identity = item.id

    def fail(session, delivery_id):
        session.get(EventDelivery, delivery_id).state = "processing"
        session.flush()
        raise RuntimeError("transaction fault")

    monkeypatch.setattr(scheduler.delivery, "dispatch_delivery", fail)
    result = scheduler.DeliveryScheduler().dispatch(session_factory)
    assert result.attempted == 1 and result.progressed == 0
    with session_factory() as session:
        assert session.get(EventDelivery, identity).state == "pending"


class _Channel:
    is_open = True

    def __init__(self):
        self.confirm_calls = 0
        self.publish_calls = 0

    def confirm_delivery(self):
        self.confirm_calls += 1

    def basic_publish(self, **kwargs):
        assert kwargs["mandatory"] is True
        assert kwargs["properties"].delivery_mode == pika.DeliveryMode.Persistent.value
        self.publish_calls += 1


class _Connection:
    def __init__(self):
        self.is_open = True
        self.channel_instance = _Channel()
        self.channel_calls = 0
        self.heartbeat_calls = 0

    def channel(self):
        self.channel_calls += 1
        return self.channel_instance

    def close(self):
        self.is_open = False

    def process_data_events(self, *, time_limit):
        assert time_limit == 0
        self.heartbeat_calls += 1


@pytest.fixture
def bounded_publisher(monkeypatch):
    monkeypatch.setattr(outbox, "_schedule_confirm_deadline", lambda *args: None)
    monkeypatch.setattr(outbox, "_remove_confirm_deadline", lambda *args: None)
    monkeypatch.setattr(rabbitmq, "bootstrap_worker_topology", lambda *args: None)
    publisher = outbox.RelayPublisher(_Connection)
    yield publisher
    publisher.close()


def test_relay_cache_reuses_single_channel_and_services_idle_heartbeat(bounded_publisher):
    publisher = bounded_publisher
    before = outbox.publisher_health()["channels_in_use"]
    connection = publisher.connection({1})
    publisher.prepare(connection, {1})
    outbox._publish_row(connection, _fake_dispatch_outbox_row(), publisher=publisher)
    assert publisher.connection({1}) is connection
    outbox._publish_row(connection, _fake_dispatch_outbox_row(), publisher=publisher)
    publisher.expire_idle()
    assert connection.channel_calls == connection.channel_instance.confirm_calls == 1
    assert connection.channel_instance.publish_calls == 2
    assert connection.heartbeat_calls == 1
    assert outbox.publisher_health()["channels_in_use"] == before + 1
    publisher.close()
    assert outbox.publisher_health()["channels_in_use"] == before


def test_relay_cache_target_change_rebuilds_before_topology_channel(bounded_publisher):
    publisher = bounded_publisher
    old = publisher.connection({1})
    publisher.prepare(old, {1})
    outbox._publish_row(old, _fake_dispatch_outbox_row(), publisher=publisher)
    replacement = publisher.connection({2})
    assert replacement is not old and not old.is_open
    assert publisher.channel is None


def test_relay_cache_expires_idle_without_releasing_a_live_channel(bounded_publisher):
    publisher = bounded_publisher
    connection = publisher.connection({1})
    publisher._last_used_at = time.monotonic() - 5.1
    publisher.expire_idle()
    assert not connection.is_open and publisher.channel is None


def test_relay_publisher_rejects_cross_thread_use(bounded_publisher):
    publisher = bounded_publisher
    publisher.connection({1})
    errors = []

    def wrong_thread():
        try:
            publisher.connection({1})
        except RuntimeError as error:
            errors.append(str(error))

    thread = threading.Thread(target=wrong_thread)
    thread.start()
    thread.join(1)
    assert len(errors) == 1


@pytest.mark.parametrize(
    "error",
    [
        pika.exceptions.NackError([]),
        pika.exceptions.UnroutableError([]),
        pika.exceptions.AMQPConnectionError(),
    ],
)
def test_persistent_relay_failure_preserves_each_row_and_rebuilds_for_retry(
    api_client, session_factory, monkeypatch, error
):
    monkeypatch.setattr(settings, "rabbitmq_url", "amqp://unused-local-test")
    monkeypatch.setattr(outbox, "_schedule_confirm_deadline", lambda *args: None)
    monkeypatch.setattr(outbox, "_remove_confirm_deadline", lambda *args: None)
    monkeypatch.setattr(rabbitmq, "bootstrap_worker_topology", lambda *args: None)
    with session_factory.begin() as session:
        sub, activation = _subscription(session, 1)
        for _ in range(2):
            execution = Execution(
                adapter_id=sub.adapter_id,
                version_id=activation.revision_id,
                target_worker_id_snapshot=activation.worker_id,
                resource_class="standard",
                trigger="subscription",
                input={},
            )
            session.add(execution)
            session.flush()
            outbox.create_dispatch_outbox(session, execution)
    connections = []
    active_session = []

    def connect():
        connection = _Connection()
        connections.append(connection)
        original = connection.channel_instance.basic_publish

        def publish(**kwargs):
            assert not active_session[0].in_transaction()
            if len(connections) == 1:
                raise error
            original(**kwargs)

        connection.channel_instance.basic_publish = publish
        return connection

    publisher = outbox.RelayPublisher(connect)
    try:
        with session_factory() as session:
            active_session[:] = [session]
            result = outbox.relay_once(session, "first", limit=2, publisher=publisher)
            assert result == outbox.OutboxRelayResult(2, 0, 2)
            rows = list(session.scalars(select(ExecutionOutbox).order_by(ExecutionOutbox.id)))
            identities = {row.id: row.message_id for row in rows}
            assert all(row.status == "pending" and row.lease_owner is None for row in rows)
            assert all(row.publish_attempts == 1 for row in rows)
            assert not connections[0].is_open
            for row in rows:
                row.available_at = database_now(session) - timedelta(seconds=1)
            session.commit()
            result = outbox.relay_once(session, "second", limit=2, publisher=publisher)
            assert result == outbox.OutboxRelayResult(2, 2, 0)
            session.expire_all()
            rows = list(session.scalars(select(ExecutionOutbox)))
            assert all(row.status == "published" and row.publish_attempts == 2 for row in rows)
            assert {row.id: row.message_id for row in rows} == identities
            assert len(connections) == 2
    finally:
        publisher.close()


def test_persistent_relay_keeps_confirmed_prefix_and_retries_only_unconfirmed_suffix(
    api_client, session_factory, monkeypatch
):
    monkeypatch.setattr(settings, "rabbitmq_url", "amqp://unused-local-test")
    monkeypatch.setattr(outbox, "_schedule_confirm_deadline", lambda *args: None)
    monkeypatch.setattr(outbox, "_remove_confirm_deadline", lambda *args: None)
    monkeypatch.setattr(rabbitmq, "bootstrap_worker_topology", lambda *args: None)
    identities = []
    with session_factory.begin() as session:
        sub, activation = _subscription(session, 1)
        due = database_now(session) - timedelta(seconds=3)
        for index in range(3):
            execution = Execution(
                adapter_id=sub.adapter_id,
                version_id=activation.revision_id,
                target_worker_id_snapshot=activation.worker_id,
                resource_class="standard",
                trigger="subscription",
                input={},
            )
            session.add(execution)
            session.flush()
            row = outbox.create_dispatch_outbox(session, execution)
            row.available_at = due + timedelta(seconds=index)
            identities.append((row.id, row.message_id))
    connections = []
    attempted = []
    active_session = []
    confirmed_prefix = []

    def connect():
        connection = _Connection()
        connections.append(connection)
        generation = len(connections)
        original = connection.channel_instance.basic_publish

        def publish(**kwargs):
            assert not active_session[0].in_transaction()
            attempted.append((generation, kwargs["properties"].message_id))
            if generation == 1 and len(attempted) == 2:
                # Observe the first confirm's durable mark before the later
                # Broker failure, rather than trusting the Relay's return value.
                with session_factory() as observer:
                    prefix = observer.get(ExecutionOutbox, identities[0][0])
                    assert prefix.status == "published" and prefix.lease_owner is None
                    assert prefix.published_at is not None and prefix.publish_attempts == 1
                    confirmed_prefix.append((prefix.message_id, prefix.published_at))
                raise pika.exceptions.NackError([])
            original(**kwargs)

        connection.channel_instance.basic_publish = publish
        return connection

    publisher = outbox.RelayPublisher(connect)
    try:
        with session_factory() as session:
            active_session[:] = [session]
            result = outbox.relay_once(session, "partial", limit=3, publisher=publisher)
            assert result == outbox.OutboxRelayResult(3, 1, 2)
        assert attempted == [(1, str(item[1])) for item in identities[:2]]
        assert not connections[0].is_open and len(confirmed_prefix) == 1
        with session_factory.begin() as session:
            rows = [session.get(ExecutionOutbox, identity) for identity, _ in identities]
            assert [row.status for row in rows] == ["published", "pending", "pending"]
            assert all(row.lease_owner is None and row.lease_expires_at is None for row in rows)
            assert all(row.publish_attempts == 1 for row in rows)
            assert [row.last_error_code for row in rows] == [
                None,
                "publisher_nack",
                "publish_timeout_or_connection",
            ]
            for row in rows[1:]:
                row.available_at = database_now(session) - timedelta(seconds=1)
        with session_factory() as session:
            active_session[:] = [session]
            result = outbox.relay_once(session, "retry-suffix", limit=3, publisher=publisher)
            assert result == outbox.OutboxRelayResult(2, 2, 0)
        assert len(connections) == 2
        assert attempted == [
            (generation, str(identities[index][1]))
            for generation, index in ((1, 0), (1, 1), (2, 1), (2, 2))
        ]
        with session_factory() as session:
            rows = [session.get(ExecutionOutbox, identity) for identity, _ in identities]
            assert all(row.status == "published" and row.lease_owner is None for row in rows)
            assert [row.publish_attempts for row in rows] == [1, 2, 2]
            assert [(row.id, row.message_id) for row in rows] == identities
            assert (rows[0].message_id, rows[0].published_at) == confirmed_prefix[0]
    finally:
        publisher.close()


@pytest.mark.parametrize("hang_at", ("channel", "confirm", "publish"))
def test_reused_channel_keeps_per_publish_deadline(monkeypatch, hang_at):
    monkeypatch.setattr(settings, "rabbitmq_publish_timeout_seconds", 0.05)
    connection = _HangingConnection(hang_at)
    publisher = outbox.RelayPublisher(lambda: connection)
    publisher.connection({1})
    start = time.monotonic()
    try:
        with pytest.raises(outbox.OutboxPublishError) as error:
            outbox._publish_row(connection, _fake_dispatch_outbox_row(), publisher=publisher)
        assert error.value.code == "publisher_confirm_timeout"
        assert time.monotonic() - start < 0.75
        assert connection.is_closed is True
    finally:
        publisher.close()


def test_lost_notifications_still_return_to_durable_polling():
    scheduler._work_available.clear()
    start = time.monotonic()
    assert scheduler.wait_delivery_work(0.01) is False
    assert time.monotonic() - start < 0.5
    scheduler.notify_delivery_work()
    assert scheduler.wait_delivery_work(0.01) is True


def test_outbox_wake_is_finite_and_consumed():
    outbox._outbox_work_available.clear()
    outbox.notify_outbox()
    start = time.monotonic()
    outbox._wait_outbox()
    assert time.monotonic() - start < 0.5
    assert not outbox._outbox_work_available.is_set()
