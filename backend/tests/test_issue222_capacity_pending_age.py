"""Advisory capacity age keeps the persisted Delivery/replay clock and source scope."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import DateTime, func, literal, select

from dlr.control.models import (
    Adapter,
    AdapterVersion,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    SubscriptionActivation,
    SubscriptionConfigVersion,
)
from dlr.control.services import ingress_capacity
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_queue as queue
from test_issue219_delivery import receive
from test_issue219_delivery import world as foundation_world
from test_issue219_queue_contract import queue_config, queue_envelope

pytestmark = pytest.mark.usefixtures("deterministic_capacity_space")

world = foundation_world


class ControlledFunctions:
    """Keep the age boundary exact while PostgreSQL executes the actual predicate."""

    def __init__(self, now):
        self.now = now

    def clock_timestamp(self):
        return literal(self.now, type_=DateTime(timezone=True))

    def __getattr__(self, name):
        return getattr(func, name)


@pytest.fixture
def age_context(world, session_factory, monkeypatch):
    with session_factory.begin() as session:
        assert ingress_capacity.activate_profile(session)
        now = session.scalar(select(func.clock_timestamp()))
        config = session.get(SubscriptionConfigVersion, world["config"])
        source_config = dict(config.config)
    # Replace only this module's SQL clock expression, never the DB/driver or
    # global sqlalchemy.func. The age EXISTS/min query still runs in real PG.
    monkeypatch.setattr(queue, "func", ControlledFunctions(now))
    return SimpleNamespace(
        subscription_id=world["sub"],
        source_config=source_config,
        session_factory=session_factory,
        world=world,
        now=now,
    )


def pending(session, context, *, age, offset=0):
    receipt = receive(session, context.world, offset)
    item = session.scalar(select(EventDelivery).where(EventDelivery.receipt_id == receipt.id))
    assert item is not None
    item.created_at = context.now - age
    session.flush()
    return receipt, item


def terminal(session, context, *, age, status):
    receipt, item = pending(session, context, age=age)
    execution = delivery.dispatch_delivery(session, item.id)
    assert execution is not None
    execution.status = "succeeded" if status == "succeeded" else "dead_letter"
    session.flush()
    delivery.observe_terminal(session, item.id)
    session.flush()
    assert receipt.state == item.state == status
    return receipt, item


@pytest.mark.parametrize(
    ("age", "hard_ready"),
    [
        (timedelta(minutes=5) - timedelta(microseconds=1), True),
        (timedelta(minutes=5), False),
        (timedelta(minutes=5) + timedelta(microseconds=1), False),
    ],
    ids=["one-microsecond-young", "exactly-five-minutes", "one-microsecond-old"],
)
def test_real_database_delivery_age_has_inclusive_five_minute_boundary(
    age_context, age, hard_ready
):
    with age_context.session_factory.begin() as session:
        pending(session, age_context, age=age)
    assert queue._capacity_decision(age_context).hard_ready is hard_ready


def test_real_database_no_pending_delivery_does_not_close_age_gate(age_context):
    assert queue._capacity_decision(age_context).hard_ready


def test_real_database_other_source_old_pending_does_not_age_this_subscription(age_context):
    with age_context.session_factory.begin() as session:
        pending(session, age_context, age=timedelta(minutes=4))
        first_activation = session.scalar(
            select(SubscriptionActivation).where(
                SubscriptionActivation.subscription_id == age_context.subscription_id
            )
        )
        adapter = Adapter(
            name="other-age-source", language="python", adapter_type="task", run_mode="manual"
        )
        session.add(adapter)
        session.flush()
        revision = AdapterVersion(adapter_id=adapter.id, seq=1, code="pass", requirements="")
        session.add(revision)
        session.flush()
        adapter.latest_version_id = revision.id
        adapter.runtime_worker_id = first_activation.worker_id
        sub = EventSubscription(
            adapter_id=adapter.id,
            source_type="rabbitmq",
            name="other-age-source",
            source_epoch="epoch-1",
            desired_state="enabled",
        )
        session.add(sub)
        session.flush()
        config = SubscriptionConfigVersion(
            subscription_id=sub.id,
            version=1,
            source_epoch="epoch-1",
            config=queue_config(),
        )
        session.add(config)
        session.flush()
        activation = SubscriptionActivation(
            subscription_id=sub.id,
            config_version_id=config.id,
            revision_id=revision.id,
            worker_id=first_activation.worker_id,
        )
        session.add(activation)
        session.flush()
        sub.activation_id = activation.id
        lease = delivery.acquire_lease(session, sub.id, "other-age-owner")
        other = {"sub": sub.id}
        receipt, duplicate = delivery.receive_event(
            session,
            sub.id,
            "other-age-owner",
            lease.generation,
            queue_envelope(other),
            "messages",
            {},
        )
        assert not duplicate
        old = session.scalar(select(EventDelivery).where(EventDelivery.receipt_id == receipt.id))
        old.created_at = age_context.now - timedelta(days=1)
        session.flush()
        assert old.state == "pending" and receipt.subscription_id != age_context.subscription_id
    assert queue._capacity_decision(age_context).hard_ready


@pytest.mark.parametrize("status", ["failed", "succeeded"])
def test_real_database_old_terminal_delivery_does_not_close_age_gate(age_context, status):
    with age_context.session_factory.begin() as session:
        terminal(session, age_context, age=timedelta(days=1), status=status)
    assert queue._capacity_decision(age_context).hard_ready


def test_real_database_replay_uses_new_delivery_age_not_original_receipt_age(age_context):
    with age_context.session_factory.begin() as session:
        receipt, original = terminal(session, age_context, age=timedelta(days=1), status="failed")
        receipt.first_received_at = age_context.now - timedelta(days=1)
        session.flush()
        replay = delivery.replay_event(
            session, receipt.id, "age-replay", None, "retry original receipt"
        )
        replay.created_at = age_context.now - timedelta(minutes=4)
        session.flush()
        assert replay.receipt_id == original.receipt_id == receipt.id
        assert replay.ordinal == 2 and replay.state == "pending" and original.state == "failed"
        assert receipt.first_received_at < age_context.now - timedelta(minutes=5)
        replay_id = replay.id
    assert queue._capacity_decision(age_context).hard_ready
    with age_context.session_factory() as session:
        replay = session.get(EventDelivery, replay_id)
        receipt = session.get(EventReceipt, replay.receipt_id)
        assert receipt.state == "pending" and replay.created_at > receipt.first_received_at
        assert delivery.audit_quota(session, age_context.subscription_id)["differences"] == {}
        assert delivery.audit_global_quota(session)["differences"] == {}
