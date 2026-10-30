"""Legal single-message input must not starve behind its prepaid storage bill."""

import base64
from copy import deepcopy

import pytest
from sqlalchemy import select

from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    AdapterVersion,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionQuota,
)
from dlr.control.services import subscription_accounting as accounting
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_scheduler as scheduler
from dlr.control.services.event_envelope import validate_envelope
from test_issue219_delivery import envelope, receive
from test_issue219_delivery import world as delivery_world

world = delivery_world


def _later_subscription(session, original):
    original_sub = session.get(EventSubscription, original["sub"])
    worker_id = session.get(SubscriptionActivation, original_sub.activation_id).worker_id
    adapter = Adapter(name="later-healthy", language="python", adapter_type="task")
    session.add(adapter)
    session.flush()
    revision = AdapterVersion(adapter_id=adapter.id, seq=1, code="pass", requirements="")
    session.add(revision)
    session.flush()
    adapter.latest_version_id, adapter.runtime_worker_id = revision.id, worker_id
    sub = EventSubscription(
        adapter_id=adapter.id,
        source_type="kafka",
        source_epoch="epoch-1",
        desired_state="enabled",
    )
    session.add(sub)
    session.flush()
    config = SubscriptionConfigVersion(
        subscription_id=sub.id, version=1, source_epoch=sub.source_epoch, config={}
    )
    session.add(config)
    session.flush()
    activation = SubscriptionActivation(
        subscription_id=sub.id,
        config_version_id=config.id,
        revision_id=revision.id,
        worker_id=worker_id,
    )
    session.add(activation)
    session.flush()
    sub.activation_id = activation.id
    lease = delivery.acquire_lease(session, sub.id, "owner-a")
    return {"sub": sub.id, "generation": lease.generation}


def _quota_facts(session):
    return {
        row.scope: {key: getattr(row, key) for key in delivery.QUOTA_FACT_KEYS}
        for row in session.scalars(select(SubscriptionQuota))
    }


@pytest.mark.parametrize(
    ("kind", "raw_kib", "input_limit"),
    [
        pytest.param("pending", 400, 1024**2, id="pending"),
        pytest.param("processing", 400, 1024**2, id="processing"),
        pytest.param("pending", 382, 512 * 1024, id="pending-default-input-limit"),
    ],
)
def test_legal_large_per_event_responsibility_consumes_one_pass_and_next_subscription_progresses(
    world, session_factory, monkeypatch, kind, raw_kib, input_limit
):
    # 400 KiB uses an existing legal deployment setting; the 382 KiB case
    # also fits the default 512 KiB input limit. Scheduling stays at 1 MiB.
    monkeypatch.setattr(settings, "execution_input_max_bytes", input_limit)
    raw = b"x" * (raw_kib * 1024)
    large_envelope = envelope(world, value=base64.b64encode(raw).decode("ascii"))
    _, inline_bytes = validate_envelope(large_envelope)
    assert len(raw) < 512 * 1024
    assert inline_bytes <= input_limit
    assert inline_bytes < scheduler.MAX_PAYLOAD_BYTES
    prepaid = accounting.responsibility_bytes(inline_bytes)
    assert prepaid > scheduler.MAX_PAYLOAD_BYTES

    with session_factory.begin() as session:
        later = _later_subscription(session, world)
        assert world["sub"] < later["sub"]
        identities = []
        for source, body in ((world, large_envelope), (later, envelope(later))):
            receipt, duplicate = delivery.receive_event(
                session,
                source["sub"],
                "owner-a",
                source["generation"],
                body,
                "test:0",
                {"offset": "1"},
            )
            assert not duplicate
            item = session.scalar(
                select(EventDelivery).where(EventDelivery.receipt_id == receipt.id)
            )
            assert item.retained_batch_bytes == 0  # Actual per_event T1, not a batch fixture.
            identities.append((receipt.id, item.id))
        assert session.get(EventReceipt, identities[0][0]).payload_bytes == prepaid
        if kind == "processing":
            for _, identity in identities:
                execution = delivery.dispatch_delivery(session, identity)
                assert execution is not None
                execution.status = "succeeded"

    with session_factory() as session:
        before = _quota_facts(session)
        candidates = scheduler.select_candidates(session, kind, scheduler.FairCursor())
        assert [candidate.delivery_id for candidate in candidates] == [
            item[1] for item in identities
        ]
        small_prepaid = session.get(EventReceipt, identities[1][0]).payload_bytes

    cursor = scheduler.FairCursor()
    first = scheduler.run_delivery_pass(session_factory, kind, cursor, monotonic=lambda: 0.0)
    assert first.selected == 2
    assert first.attempted == first.progressed == 1
    assert first.budget_exhausted
    assert cursor.after_subscription_id == world["sub"]
    assert [candidate.payload_bytes for candidate in candidates] == [
        scheduler.MAX_PAYLOAD_BYTES,
        small_prepaid,
    ]
    with session_factory() as session:
        assert session.get(EventDelivery, identities[0][1]).state == (
            "processing" if kind == "pending" else "succeeded"
        )
        assert session.get(EventDelivery, identities[1][1]).state == kind
        expected = deepcopy(before)
        if kind == "pending":
            for scope in (f"sub:{world['sub']}", "global"):
                expected[scope]["pending_count"] -= 1
                expected[scope]["pending_bytes"] -= prepaid
        assert _quota_facts(session) == expected

    second = scheduler.run_delivery_pass(session_factory, kind, cursor, monotonic=lambda: 0.0)
    assert second.attempted == second.progressed == 1
    with session_factory() as session:
        assert session.get(EventDelivery, identities[1][1]).state == (
            "processing" if kind == "pending" else "succeeded"
        )
        expected = deepcopy(before)
        if kind == "pending":
            for source, bill in ((world, prepaid), (later, small_prepaid)):
                for scope in (f"sub:{source['sub']}", "global"):
                    expected[scope]["pending_count"] -= 1
                    expected[scope]["pending_bytes"] -= bill
        assert _quota_facts(session) == expected
        large = session.get(EventReceipt, identities[0][0])
        assert large.payload_bytes == prepaid
        assert large.envelope == large_envelope
        for source in (world, later):
            assert delivery.audit_quota(session, source["sub"])["differences"] == {}
        assert delivery.audit_global_quota(session)["differences"] == {}
        assert scheduler.select_candidates(session, kind, scheduler.FairCursor()) == []


def test_small_per_event_candidates_keep_their_cumulative_work_cost(world, session_factory):
    with session_factory.begin() as session:
        later = _later_subscription(session, world)
        expected = [receive(session, source).payload_bytes for source in (world, later)]
    with session_factory() as session:
        candidates = scheduler.select_candidates(session, "pending", scheduler.FairCursor())
        assert [item.payload_bytes for item in candidates] == expected
        assert sum(expected) < scheduler.MAX_PAYLOAD_BYTES
    result = scheduler.run_delivery_pass(
        session_factory, "pending", scheduler.FairCursor(), monotonic=lambda: 0.0
    )
    assert result.attempted == result.progressed == 2
    assert not result.budget_exhausted
    with session_factory() as session:
        assert list(session.scalars(select(EventDelivery.state).order_by(EventDelivery.id))) == [
            "processing",
            "processing",
        ]
        assert delivery.audit_global_quota(session)["differences"] == {}
