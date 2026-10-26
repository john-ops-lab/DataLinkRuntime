"""Rabbit owner coalescing: actual SDK tokens and actual PostgreSQL T1 custody."""

import threading
import weakref
from collections import deque
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from dlr.common.config import settings
from dlr.control.models import EventReceipt, SourceCheckpoint, SubscriptionConfigVersion
from dlr.control.services import ingress_capacity
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_queue as queue
from dlr.control.services.subscription_source import SourceFailure, SourceRunContext
from test_issue219_delivery import world as foundation_world
from test_issue222_business_batch import batch_world as queue_world
from test_issue222_rabbit_native_budget import NativeWire

world = foundation_world
batch_world = queue_world


class MemoryTransport:
    micro_t1 = True
    failed_message = None

    def __init__(self, context, count=4, *, before_ack=None, ack_error=False):
        self.context, self.count = context, count
        self.wire = NativeWire()
        self.pending = deque()
        self.references = []
        self.acked = []
        self.closed = False
        self.before_ack, self.ack_error = before_ack, ack_error
        for index in range(count):
            self.wire.delivery(index + 1, f"body-{index}".encode())
            message = queue.Message(
                f"message-{index}",
                self.wire.received[-1][2],
                {
                    "cluster_id": "fixture",
                    "vhost": "/",
                    "queue": "events",
                    "exchange": "",
                    "routing_key": "events",
                    "properties": {},
                },
                index + 1,
                native_token=self.wire.native.tokens[index + 1],
            )
            self.pending.append(message)
            self.references.append(weakref.ref(message))
        self.wire.received.clear()

    def poll(self):
        if self.pending:
            return self.pending.popleft()
        self.context.stop_event.set()
        return None

    def poll_bounded(self, seconds):
        assert 0 <= seconds <= 0.005
        return self.pending.popleft() if self.pending else None

    def ack_handles(self, tags):
        # No completed cohort body is retained by QueueRunner when it hands
        # only tags to the SDK. Unaccepted suffix bodies may remain pending.
        assert all(self.references[tag - 1]() is None for tag in tags)
        if self.before_ack:
            self.before_ack(tags)
        for tag in tags:
            self.wire.native.retiring(tag)
        if self.ack_error:
            raise TimeoutError("not exposed")
        self.acked.extend(tags)
        for tag in tags:
            self.wire.native.released(tag)

    def close(self):
        self.closed = True
        self.pending.clear()
        self.wire.close()


def run(context, transport):
    runner = queue.QueueSourceRunner()
    runner.open_context = lambda context: transport
    runner.run(context)


@pytest.fixture
def mocked_context(monkeypatch):
    monkeypatch.setattr(
        queue, "_capacity_decision", lambda context: queue._CapacityDecision(True, 16)
    )
    return SimpleNamespace(
        source_config={
            "source_type": "rabbitmq",
            "mode": "business_batch",
            "cluster_id": "fixture",
        },
        subscription_id=7,
        config_version=1,
        source_epoch="epoch-1",
        stop_event=threading.Event(),
        connected=lambda: None,
    )


@pytest.mark.parametrize("count", [4, 16])
def test_native_messages_form_one_unit_and_drop_bodies_before_ack(
    mocked_context, monkeypatch, count
):
    monkeypatch.setattr(queue, "capacity_ready", lambda context: True)
    groups = []

    def receive(context, group):
        groups.append([item.message.handle for item in group])
        return len(group), None, None

    monkeypatch.setattr(queue, "_receive_micro", receive)
    transport = MemoryTransport(mocked_context, count=count)
    run(mocked_context, transport)
    assert groups == [list(range(1, count + 1))]
    assert transport.acked == list(range(1, count + 1))
    assert transport.closed and transport.wire.scope.used_bytes == 0


@pytest.mark.parametrize("code", ["source_not_available", "subscription_owner_stale"])
def test_platform_or_lifecycle_error_is_never_a_bad_message(mocked_context, monkeypatch, code):
    monkeypatch.setattr(queue, "capacity_ready", lambda context: True)
    monkeypatch.setattr(
        queue,
        "_receive_micro",
        lambda context, group: (0, HTTPException(409, {"code": code}), 0),
    )
    recorded = []
    monkeypatch.setattr(queue, "record_fault", lambda *args: recorded.append(True))
    transport = MemoryTransport(mocked_context)
    with pytest.raises(SourceFailure, match=code):
        run(mocked_context, transport)
    assert transport.acked == [] and recorded == [] and transport.closed
    assert transport.wire.scope.used_bytes == 0


def test_bad_second_member_commits_and_acks_only_first_then_diagnoses_once(
    mocked_context, monkeypatch
):
    monkeypatch.setattr(queue, "capacity_ready", lambda context: True)
    error = HTTPException(422, {"code": "event_payload_too_large"})
    monkeypatch.setattr(queue, "_receive_micro", lambda context, group: (1, error, 1))
    recorded = []
    monkeypatch.setattr(
        queue,
        "record_fault",
        lambda context, failure, message: recorded.append((failure.detail["code"], message.handle)),
    )
    transport = MemoryTransport(mocked_context)
    with pytest.raises(SourceFailure, match="event_payload_too_large"):
        run(mocked_context, transport)
    assert transport.acked == [1]
    assert recorded == [("event_payload_too_large", 2)]
    assert transport.closed and transport.wire.scope.used_bytes == 0


def test_unknown_commit_never_acks_or_records_poison(mocked_context, monkeypatch):
    monkeypatch.setattr(queue, "capacity_ready", lambda context: True)

    def unknown(context, group):
        raise RuntimeError("private driver error")

    monkeypatch.setattr(queue, "_receive_micro", unknown)
    recorded = []
    monkeypatch.setattr(queue, "record_fault", lambda *args: recorded.append(True))
    transport = MemoryTransport(mocked_context)
    with pytest.raises(SourceFailure, match="source_unavailable"):
        run(mocked_context, transport)
    assert transport.acked == [] and recorded == [] and transport.closed


def test_byte_refused_second_member_is_carried_after_committing_existing_prefix(
    mocked_context, monkeypatch
):
    from dlr.control.services.subscription_rabbit_budget import NativeToken

    monkeypatch.setattr(queue, "capacity_ready", lambda context: True)
    original = NativeToken.begin_processing
    transport = MemoryTransport(mocked_context)

    def prepare(token, workspace=None):
        previous = transport.wire.native.tokens.get(1)
        if token.tag == 2 and previous is not None and previous.working:
            return False
        return original(token, workspace)

    monkeypatch.setattr(NativeToken, "begin_processing", prepare)
    groups = []

    def receive(context, group):
        groups.append([item.message.handle for item in group])
        return len(group), None, None

    monkeypatch.setattr(queue, "_receive_micro", receive)
    run(mocked_context, transport)
    assert groups == [[1], [2, 3, 4]] and transport.acked == [1, 2, 3, 4]
    assert transport.wire.scope.used_bytes == 0


def test_collection_deadline_only_drains_ready_without_extending_io_wait(
    mocked_context, monkeypatch
):
    monkeypatch.setattr(queue, "capacity_ready", lambda context: True)
    clock = [0.0]
    monkeypatch.setattr(queue.time, "monotonic", lambda: clock[0])
    original = queue._prepare_micro

    def prepare(context, message):
        value = original(context, message)
        clock[0] += 0.006
        return value

    monkeypatch.setattr(queue, "_prepare_micro", prepare)
    groups = []

    def receive(context, group):
        groups.append(len(group))
        return len(group), None, None

    monkeypatch.setattr(queue, "_receive_micro", receive)
    transport = MemoryTransport(mocked_context, count=16)
    waits = []
    original_poll = transport.poll_bounded

    def bounded(seconds):
        waits.append(seconds)
        return original_poll(seconds)

    transport.poll_bounded = bounded
    run(mocked_context, transport)
    assert groups == [16] and transport.acked == list(range(1, 17))
    assert waits == [0] * 15


def test_later_sdk_fault_still_commits_and_acks_captured_valid_prefix(mocked_context, monkeypatch):
    monkeypatch.setattr(queue, "capacity_ready", lambda context: True)
    monkeypatch.setattr(queue, "_receive_micro", lambda context, group: (len(group), None, None))
    recorded = []
    monkeypatch.setattr(
        queue,
        "record_fault",
        lambda context, error, message: recorded.append(queue.classify(error).code),
    )
    transport = MemoryTransport(mocked_context)

    def fault(seconds):
        raise SourceFailure("source_properties_invalid")

    transport.poll_bounded = fault
    with pytest.raises(SourceFailure, match="source_properties_invalid"):
        run(mocked_context, transport)
    assert transport.acked == [1] and recorded == ["source_properties_invalid"]


def test_ack_failure_does_not_erase_known_committed_prefix(mocked_context, monkeypatch):
    monkeypatch.setattr(queue, "capacity_ready", lambda context: True)
    commits = []

    def receive(context, group):
        commits.append([item.message.message_id for item in group])
        return len(group), None, None

    monkeypatch.setattr(queue, "_receive_micro", receive)
    recorded = []
    monkeypatch.setattr(queue, "record_fault", lambda *args: recorded.append(True))
    transport = MemoryTransport(mocked_context, ack_error=True)
    with pytest.raises(SourceFailure, match="source_unavailable"):
        run(mocked_context, transport)
    assert len(commits) == 1 and transport.acked == [] and recorded == []
    assert transport.closed and transport.wire.scope.used_bytes == 0


@pytest.fixture
def micro_context(batch_world, session_factory):
    with session_factory.begin() as session:
        ingress_capacity.activate_profile(session)
        config = session.get(SubscriptionConfigVersion, batch_world["config"]).config
        from dlr.control.models import EventSubscription

        activation_id = session.get(EventSubscription, batch_world["sub"]).activation_id
    return SourceRunContext(
        batch_world["sub"],
        "owner-a",
        batch_world["generation"],
        activation_id,
        1,
        "epoch-1",
        config,
        {},
        threading.Event(),
        session_factory,
        NativeWire().scope,
        lambda: None,
    )


def receipts(context):
    with context.session_factory() as session:
        return session.scalar(select(func.count()).select_from(EventReceipt))


def assert_ledger(context):
    with context.session_factory() as session:
        assert delivery.audit_quota(session, context.subscription_id)["differences"] == {}
        assert delivery.audit_global_quota(session)["differences"] == {}


@pytest.mark.parametrize("count", [4, 16])
def test_real_database_commit_before_ack_and_redelivery_deduplicates(micro_context, count):
    def committed(tags):
        assert receipts(micro_context) == count

    first = MemoryTransport(micro_context, count=count, before_ack=committed)
    run(micro_context, first)
    assert first.acked == list(range(1, count + 1)) and receipts(micro_context) == count
    micro_context.stop_event.clear()
    second = MemoryTransport(micro_context, count=count, before_ack=committed)
    run(micro_context, second)
    assert second.acked == list(range(1, count + 1)) and receipts(micro_context) == count
    assert_ledger(micro_context)


@pytest.mark.parametrize("bad_index,count", [(2, 4), (9, 16)])
def test_real_database_bad_item_preserves_committed_prefix(
    micro_context, monkeypatch, bad_index, count
):
    original = queue._prepare_micro

    def prepare(context, message):
        result = original(context, message)
        if message.handle == bad_index:
            result.value = None
            result.error = HTTPException(422, {"code": "event_payload_too_large"})
        return result

    monkeypatch.setattr(queue, "_prepare_micro", prepare)
    transport = MemoryTransport(micro_context, count=count)
    with pytest.raises(SourceFailure, match="event_payload_too_large"):
        run(micro_context, transport)
    assert transport.acked == list(range(1, bad_index))
    assert receipts(micro_context) == bad_index - 1
    with micro_context.session_factory() as session:
        checkpoint = session.scalar(
            select(SourceCheckpoint).where(
                SourceCheckpoint.subscription_id == micro_context.subscription_id
            )
        )
        assert checkpoint.blocked_unaccepted["code"] == "event_payload_too_large"
    assert_ledger(micro_context)


def test_real_database_disabled_then_reenabled_has_no_bad_message_barrier(
    micro_context, monkeypatch
):
    monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
    first = MemoryTransport(micro_context)
    with pytest.raises(SourceFailure, match="source_not_available"):
        run(micro_context, first)
    assert not first.acked and receipts(micro_context) == 0
    with micro_context.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SourceCheckpoint)) == 0
    monkeypatch.setattr(settings, "event_subscriptions_enabled", True)
    second = MemoryTransport(micro_context)
    run(micro_context, second)
    assert second.acked == [1, 2, 3, 4] and receipts(micro_context) == 4
    assert_ledger(micro_context)


def test_real_database_commit_error_rolls_back_every_member(micro_context, monkeypatch):
    factory = micro_context.session_factory

    def sessions():
        session = factory()

        def fail():
            raise RuntimeError("commit outcome unavailable")

        session.commit = fail
        return session

    context = replace(micro_context, session_factory=sessions)
    transport = MemoryTransport(context, count=16)
    with pytest.raises(SourceFailure, match="source_unavailable"):
        run(context, transport)
    assert not transport.acked and receipts(micro_context) == 0
    assert_ledger(micro_context)


def test_real_database_lost_commit_response_does_not_ack_and_redelivery_recovers(micro_context):
    factory = micro_context.session_factory

    def sessions():
        session = factory()
        commit = session.commit

        def unknown():
            commit()
            raise RuntimeError("commit response unavailable")

        session.commit = unknown
        return session

    context = replace(micro_context, session_factory=sessions)
    first = MemoryTransport(context, count=16)
    with pytest.raises(SourceFailure, match="source_unavailable"):
        run(context, first)
    assert not first.acked and receipts(micro_context) == 16
    second = MemoryTransport(micro_context, count=16)
    run(micro_context, second)
    assert second.acked == list(range(1, 17)) and receipts(micro_context) == 16
    assert_ledger(micro_context)
