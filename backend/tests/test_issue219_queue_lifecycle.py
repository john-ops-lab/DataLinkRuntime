"""Queue lifecycle races use actual PostgreSQL T1, leases and recovery audits.

Only native transport calls are controlled; the two public source runners,
runtime manager, custody and persistence code remain real. Broker/config-restart
acceptance is a separate deployment check, not an ENV hot-reload claim.
"""

import asyncio
import hashlib
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    AdapterVersion,
    DeliveryDisposition,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    SourceCheckpoint,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionLease,
    Worker,
)
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_management as management
from dlr.control.services.event_envelope import canonical
from dlr.control.services.subscription_queue import Message, classify, take_custody
from dlr.control.services.subscription_rabbitmq import RabbitMQSourceRunner
from dlr.control.services.subscription_rocketmq import RocketMQSourceRunner
from dlr.control.services.subscription_runtime import SubscriptionRuntimeManager
from dlr.control.services.subscription_source import SourceFailure


def eventually(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "Queue owner did not reach expected state"
        time.sleep(0.005)


class Transport:
    def __init__(self, message):
        self.message = message
        self.entered = threading.Event()
        self.deliver = threading.Event()
        self.closing = threading.Event()
        self.close_allowed = threading.Event()
        self.closed = False
        self.acked = []
        self.ack_error = None

    def poll(self):
        self.entered.set()
        assert self.deliver.wait(10)
        self.deliver.clear()
        return self.message

    def ack(self, message):
        if self.ack_error is not None:
            raise self.ack_error
        self.acked.append(message.message_id)

    def close(self):
        self.closing.set()
        assert self.close_allowed.wait(10)
        self.closed = True


@pytest.fixture(params=["rabbitmq", "rocketmq"])
def world(request, api_client, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "event_subscriptions_enabled", True)
    source = request.param
    runner = RabbitMQSourceRunner() if source == "rabbitmq" else RocketMQSourceRunner()
    config = {
        "source_type": source,
        "cluster_id": "lifecycle-fixture",
        "server": "amqp://broker.invalid:5672"
        if source == "rabbitmq"
        else "grpc://broker.invalid:8081",
        **(
            {"vhost": "/", "queue": "events", "credential_id": 1}
            if source == "rabbitmq"
            else {
                "topic": "events",
                "group_id": "lifecycle-fixture",
            }
        ),
    }
    attributes = {
        "cluster_id": "lifecycle-fixture",
        **(
            {
                "vhost": "/",
                "queue": "events",
                "exchange": "",
                "routing_key": "events",
                "properties": {},
            }
            if source == "rabbitmq"
            else {
                "topic": "events",
                "group_id": "lifecycle-fixture",
                "tag": "",
                "keys": [],
                "properties": {},
            }
        ),
    }
    with session_factory.begin() as session:
        worker = Worker(
            name="lifecycle-worker",
            status="online",
            capabilities=["python"],
            protocol_version=3,
            rabbitmq_execution_v3=True,
        )
        adapter = Adapter(name="lifecycle-" + source, language="python", adapter_type="task")
        session.add_all([adapter, worker])
        session.flush()
        revision = AdapterVersion(adapter_id=adapter.id, seq=1, code="pass", requirements="")
        session.add(revision)
        session.flush()
        adapter.latest_version_id = revision.id
        adapter.runtime_worker_id = worker.id
        sub = EventSubscription(
            adapter_id=adapter.id,
            source_type=source,
            name=source,
            source_epoch="lifecycle-epoch",
            desired_state="enabled",
            connection_state="starting",
        )
        session.add(sub)
        session.flush()
        cv = SubscriptionConfigVersion(
            subscription_id=sub.id,
            version=1,
            source_epoch=sub.source_epoch,
            config=config,
        )
        session.add(cv)
        session.flush()
        activation = SubscriptionActivation(
            subscription_id=sub.id,
            config_version_id=cv.id,
            revision_id=revision.id,
            worker_id=worker.id,
        )
        session.add(activation)
        session.flush()
        sub.activation_id = activation.id
        sub_id, activation_id, config_id = sub.id, activation.id, cv.id
    transports = []
    contexts = []
    now = [0.0]
    manager = SubscriptionRuntimeManager(
        session_factory,
        {source: runner},
        clock=lambda: now[0],
        jitter=lambda a, b: 1,
    )
    native_run = runner.run

    def run(context):
        contexts.append(context)
        native_run(context)

    def opened(*args, **kwargs):
        transport = Transport(Message("same-message", b"payload", attributes, None))
        transports.append(transport)
        return transport

    monkeypatch.setattr(runner, "run", run)
    monkeypatch.setattr(runner, "open", opened)
    value = SimpleNamespace(
        sf=session_factory,
        manager=manager,
        sub_id=sub_id,
        activation_id=activation_id,
        config_id=config_id,
        transports=transports,
        contexts=contexts,
        now=now,
    )
    yield value
    manager.stop_all()
    for transport in transports:
        transport.deliver.set()
        transport.close_allowed.set()
    asyncio.run(manager.aclose(timeout=5))


def checkpoint(world):
    with world.sf() as session:
        return session.scalar(
            select(SourceCheckpoint).where(SourceCheckpoint.subscription_id == world.sub_id)
        )


def receipt_count(world):
    with world.sf() as session:
        return session.scalar(
            select(func.count())
            .select_from(EventReceipt)
            .where(EventReceipt.subscription_id == world.sub_id)
        )


def start(world):
    assert world.manager.start_once(world.sub_id)
    eventually(lambda: world.transports and world.transports[-1].entered.is_set())
    return world.transports[-1]


def assert_closed(world, transport):
    eventually(lambda: world.manager.active_count == 0)
    assert transport.closed and world.manager.buffer.used_bytes == 0
    with world.sf() as session:
        lease = session.get(SubscriptionLease, world.sub_id)
        assert lease.owner_id == "" and lease.closing_generation is None


def resume_and_receive(world):
    world.now[0] += 61
    world.manager.reconcile_once()
    eventually(lambda: len(world.transports) >= 2 and world.transports[-1].entered.is_set())
    transport = world.transports[-1]
    transport.deliver.set()
    eventually(lambda: transport.acked == ["same-message"])
    assert receipt_count(world) == 1
    with world.sf() as session:
        sub = session.get(EventSubscription, world.sub_id)
        activation = session.get(SubscriptionActivation, sub.activation_id)
        assert (
            sub.source_epoch,
            sub.config_version,
            sub.activation_id,
            activation.config_version_id,
        ) == (
            "lifecycle-epoch",
            1,
            world.activation_id,
            world.config_id,
        )
        assert session.scalar(select(func.count()).select_from(EventDelivery)) == 1
    world.manager.stop_single(world.sub_id)
    transport.deliver.set()
    transport.close_allowed.set()
    assert_closed(world, transport)


@pytest.mark.parametrize("reenable_before_close", [False, True])
def test_feature_off_inflight_then_same_epoch_recovery(world, monkeypatch, reenable_before_close):
    transport = start(world)
    monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
    # Poll is already in flight; deliberately do not fence via reconcile first.
    transport.deliver.set()
    assert transport.closing.wait(5)
    assert not transport.acked and receipt_count(world) == 0
    cp = checkpoint(world)
    assert cp is None or cp.blocked_unaccepted is None
    if reenable_before_close:
        monkeypatch.setattr(settings, "event_subscriptions_enabled", True)
    transport.close_allowed.set()
    assert_closed(world, transport)
    monkeypatch.setattr(settings, "event_subscriptions_enabled", True)
    resume_and_receive(world)


@pytest.mark.parametrize("change", ["missing_activation", "changed_config"])
def test_inflight_activation_change_never_becomes_message_poison(world, change):
    transport = start(world)
    with world.sf.begin() as session:
        if change == "missing_activation":
            session.get(EventSubscription, world.sub_id).activation_id = None
        else:
            old = session.get(SubscriptionConfigVersion, world.config_id)
            cv = SubscriptionConfigVersion(
                subscription_id=world.sub_id,
                version=2,
                source_epoch=old.source_epoch,
                config=old.config,
            )
            session.add(cv)
            session.flush()
            session.get(SubscriptionActivation, world.activation_id).config_version_id = cv.id
    transport.deliver.set()
    assert transport.closing.wait(5)
    transport.close_allowed.set()
    assert_closed(world, transport)
    assert not transport.acked and receipt_count(world) == 0
    cp = checkpoint(world)
    assert cp is None or cp.blocked_unaccepted is None
    with world.sf.begin() as session:
        sub = session.get(EventSubscription, world.sub_id)
        assert sub.connection_state == "blocked"
        sub.activation_id = world.activation_id
        session.get(SubscriptionActivation, world.activation_id).config_version_id = world.config_id
        # Same source-retry state used by explicit enable after correcting configuration.
        sub.connection_state = "starting"
    resume_and_receive(world)


def seed_fault(world, code, *, closed=True):
    with world.sf.begin() as session:
        lease = delivery.acquire_lease(session, world.sub_id, "old-owner")
        generation = lease.generation
        delivery.record_rejection(
            session,
            world.sub_id,
            lease.owner_id,
            generation,
            "messages",
            code,
            "old-message-hash",
            {},
            size=7,
        )
        sub = session.get(EventSubscription, world.sub_id)
        management._fence(session, sub)
        if closed:
            management.listener_closed(session, sub.id, "old-owner", generation)


@pytest.mark.parametrize(
    "code",
    ["source_not_available", "subscription_activation_missing", "subscription_config_conflict"],
)
def test_legacy_lifecycle_barrier_is_audited_once_before_reception(world, code):
    seed_fault(world, code)
    world.manager.reconcile_once()
    eventually(lambda: world.transports and world.transports[-1].entered.is_set())
    with world.sf() as session:
        audit = session.scalar(
            select(DeliveryDisposition).where(DeliveryDisposition.action == "source_fault_recovery")
        )
        assert audit is not None and audit.details["previous_fault"]["code"] == code
        assert audit.receipt_id is None and audit.actor_id is None
        assert not delivery.audit_quota(session, world.sub_id)["differences"]
    assert checkpoint(world).blocked_unaccepted is None
    world.manager.reconcile_once()
    with world.sf() as session:
        assert session.scalar(select(func.count()).select_from(DeliveryDisposition)) == 1
    # Keep the same epoch/config/activation when the formerly blocked message is redelivered.
    transport = world.transports[-1]
    transport.deliver.set()
    eventually(lambda: transport.acked == ["same-message"])
    assert receipt_count(world) == 1


@pytest.mark.parametrize(
    "boundary",
    ["real_poison", "closing", "quota", "invalid_config", "other_source_fault", "audit_collision"],
)
def test_recovery_never_bypasses_fault_close_config_or_budget(world, boundary):
    code = "event_identity_conflict" if boundary == "real_poison" else "source_not_available"
    seed_fault(world, code, closed=boundary != "closing")
    before = checkpoint(world).blocked_unaccepted
    with world.sf.begin() as session:
        if boundary == "quota":
            config = session.get(SubscriptionConfigVersion, world.config_id)
            config.config = {**config.config, "limits": {"dedupe_count": 1}}
            delivery.reserve_disposition_metadata(session, world.sub_id)
            session.add(
                DeliveryDisposition(
                    subscription_id=world.sub_id,
                    request_id="existing",
                    action="quota_repair",
                    reason="fixture",
                )
            )
        elif boundary == "invalid_config":
            session.get(SubscriptionConfigVersion, world.config_id).source_epoch = "other-epoch"
        elif boundary == "other_source_fault":
            session.get(
                EventSubscription, world.sub_id
            ).last_error_code = "source_close_unconfirmed"
        elif boundary == "audit_collision":
            lease = session.get(SubscriptionLease, world.sub_id)
            details = {
                "source_epoch": "lifecycle-epoch",
                "shard": "messages",
                "closed_generation": lease.generation,
                "activation_id": world.activation_id,
                "config_version": 1,
                "previous_fault": before,
            }
            request_id = "source-fault-recovery:" + hashlib.sha256(canonical(details)).hexdigest()
            delivery.reserve_disposition_metadata(session, world.sub_id)
            session.add(
                DeliveryDisposition(
                    subscription_id=world.sub_id,
                    request_id=request_id,
                    action="quota_repair",
                    reason="Existing unrelated request must not authorize recovery",
                )
            )
    world.manager.reconcile_once()
    assert world.manager.active_count == 0 and not world.transports
    assert checkpoint(world).blocked_unaccepted == before
    with world.sf() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(DeliveryDisposition)
                .where(DeliveryDisposition.action == "source_fault_recovery")
            )
            == 0
        )
        assert not delivery.audit_quota(session, world.sub_id)["differences"]


@pytest.mark.parametrize("diagnosis_fenced", [False, True])
def test_content_conflict_preserves_receipt_and_confirmed_close(
    world, monkeypatch, diagnosis_fenced
):
    transport = start(world)
    assert take_custody(world.contexts[0], transport.message)
    transport.message = replace(transport.message, body=b"conflicting-body")
    if diagnosis_fenced:
        original = delivery.record_rejection

        def record_after_fence(*args, **kwargs):
            with world.sf.begin() as session:
                _, sub = delivery.lock_subscription(session, world.sub_id)
                management._fence(session, sub)
            return original(*args, **kwargs)

        monkeypatch.setattr(delivery, "record_rejection", record_after_fence)
    transport.deliver.set()
    assert transport.closing.wait(5)
    transport.close_allowed.set()
    assert_closed(world, transport)
    assert receipt_count(world) == 1 and not transport.acked
    with world.sf() as session:
        original_receipt = session.scalar(select(EventReceipt))
        assert original_receipt.envelope["data"]["value"] == "cGF5bG9hZA=="
        assert not delivery.audit_quota(session, world.sub_id)["differences"]
    cp = checkpoint(world)
    if diagnosis_fenced:
        assert cp.blocked_unaccepted is None
    else:
        assert cp.blocked_unaccepted["code"] == "event_identity_conflict"
        world.manager.reconcile_once()
        assert world.manager.active_count == 0 and len(world.transports) == 1


def test_postcommit_ack_failure_never_creates_unaccepted_diagnosis(world):
    transport = start(world)
    transport.ack_error = HTTPException(422, {"code": "event_content_invalid"})
    transport.deliver.set()
    assert transport.closing.wait(5)
    transport.close_allowed.set()
    assert_closed(world, transport)
    assert receipt_count(world) == 1
    assert checkpoint(world).blocked_unaccepted is None


def test_capacity_summary_clears_only_after_successful_custody(world):
    transport = start(world)
    assert take_custody(world.contexts[0], transport.message)
    with world.sf.begin() as session:
        config = session.get(SubscriptionConfigVersion, world.config_id)
        config.config = {**config.config, "limits": {"pending_count": 1}}
    transport.message = replace(transport.message, message_id="second-message")
    transport.deliver.set()
    assert transport.closing.wait(5)
    transport.close_allowed.set()
    assert_closed(world, transport)
    assert receipt_count(world) == 1 and not transport.acked
    assert checkpoint(world).blocked_unaccepted["code"] == "subscription_capacity_exceeded"
    with world.sf.begin() as session:
        config = session.get(SubscriptionConfigVersion, world.config_id)
        config.config = {**config.config, "limits": {"pending_count": 4}}
    world.now[0] += 61
    world.manager.reconcile_once()
    eventually(lambda: len(world.transports) == 2 and world.transports[-1].entered.is_set())
    resumed = world.transports[-1]
    resumed.message = replace(resumed.message, message_id="second-message")
    resumed.deliver.set()
    eventually(lambda: resumed.acked == ["second-message"])
    assert receipt_count(world) == 2 and checkpoint(world).blocked_unaccepted is None
    with world.sf() as session:
        assert not delivery.audit_quota(session, world.sub_id)["differences"]
        assert not delivery.audit_global_quota(session)["differences"]


@pytest.mark.parametrize(
    "status,detail,expected",
    [
        (409, {"code": "source_not_available"}, "backoff"),
        (409, {"code": "subscription_owner_stale"}, "backoff"),
        (409, {"code": "subscription_activation_missing"}, "blocked"),
        (409, {"code": "subscription_config_conflict"}, "blocked"),
        (409, {"code": "event_identity_conflict"}, "blocked"),
        (429, {"code": "subscription_capacity_exceeded"}, "capacity"),
        (429, {"code": "upstream_rate_limited"}, "backoff"),
        (500, {"code": "future_runtime_error"}, "backoff"),
        (422, "opaque sensitive error", "backoff"),
        (422, {"code": ["invalid"]}, "backoff"),
    ],
)
def test_http_failure_meaning_is_independent_of_status(status, detail, expected):
    failure = classify(HTTPException(status, detail))
    assert failure.disposition == expected and failure.closed
    assert "sensitive" not in failure.code
    native = classify(SourceFailure("source_not_available", closed=False))
    assert native.disposition == "backoff" and not native.closed
