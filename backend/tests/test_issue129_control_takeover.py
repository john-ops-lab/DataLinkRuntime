"""Durable takeover boundaries exercised by separate PostgreSQL transactions."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from dlr.common.config import settings
from dlr.control import db
from dlr.control.models import (
    Adapter,
    EventReceipt,
    ExecutionOutbox,
    IngressCapacityConfiguration,
    RabbitMQRuntimeCapability,
    RuntimeReconciliationCursor,
    SubscriptionLease,
)
from dlr.control.services import (
    background_control,
    http_ingress,
    infrastructure_dlq,
    ingress_capacity,
    outbox,
    rabbitmq,
    subscription_delivery,
)
from dlr.control.services import (
    control_cluster_identity as identity,
)
from dlr.control.services import (
    control_ha_capacity as capacity,
)
from dlr.control.services.input_config import database_now
from test_adapters import create_adapter
from test_issue129_control_ha_foundation import ha_authority as ha_authority
from test_issue152_infrastructure_dlq import _published_execution
from test_issue222_http_ingress import http_world as http_world
from test_issue222_http_ingress import world as world


def test_committed_scan_page_survives_exit_and_wraps_unprocessed_rows(api_client, session_factory):
    ids = [create_adapter(api_client, name=f"takeover-scan-{i}")["id"] for i in range(3)]
    query = select(Adapter.id).where(Adapter.id.in_(ids))
    observed = []
    for _ in range(4):
        with session_factory() as scan:
            observed.extend(
                background_control.reserve_page(
                    scan,
                    "schedule_due",
                    query,
                    Adapter.id,
                    limit=1,
                )
            )
    # The first process closed after reserving its page without working it.
    assert observed == [*ids, ids[0]]

    with session_factory.begin() as session:
        cursor = session.get(RuntimeReconciliationCursor, "schedule_due")
        cursor.after_id = 0
        cursor.upper_id = ids[0]
    with session_factory() as scan:
        # The original upper-bound candidate ceased to qualify before its page was selected.
        assert background_control.reserve_page(
            scan, "schedule_due", query.where(Adapter.id > ids[0]), Adapter.id, limit=1
        ) == (ids[1],)


def test_passive_http_uses_current_peer_owner_and_records_real_receiver(
    session_factory, http_world, ha_authority, monkeypatch
):
    a, b, a_token, b_token = (
        uuid4(),
        uuid4(),
        identity.new_instance_token(),
        identity.new_instance_token(),
    )
    with session_factory.begin() as session:
        profile = ingress_capacity.configured_profile()
        session.add(
            IngressCapacityConfiguration(
                id=1, fingerprint=profile.fingerprint, profile=profile.as_dict()
            )
        )
        for slot, instance, token in (("a", a, a_token), ("b", b, b_token)):
            identity.register_control(
                session, ha_authority, instance_id=instance, instance_token=token, ha_slot=slot
            )
        lease = session.get(SubscriptionLease, http_world["sub"])
        lease.control_instance_id = a
        original = lease.owner_id, lease.generation

    class Receiver:
        instance_id = b

        def assert_live(self, session):
            identity.assert_live_control(
                session, ha_authority, instance_id=b, instance_token=b_token
            )

    monkeypatch.setattr(settings, "control_ha_slot", "b")
    monkeypatch.setattr(capacity, "_local_process", Receiver())
    monkeypatch.setattr(capacity, "require_storage", lambda *_args: None)
    route = http_world["public_id"]
    with session_factory.begin() as session:
        status, accepted = http_ingress.receive(session, route, "Bearer source-token", "one", b"{}")
        assert status == 202
        saved = session.get(EventReceipt, accepted["receipt_id"])
        assert saved.receiver_control_instance_id == b
        assert (saved.source_owner_id, saved.source_owner_generation) == original
        assert saved.sequence == 1
    with session_factory.begin() as session:
        session.execute(
            text("UPDATE control_process_instances SET revoked=true WHERE instance_id=:a"), {"a": a}
        )
    with session_factory.begin() as session:
        status, old = http_ingress.receive(session, route, "Bearer source-token", "one", b"{}")
        assert (status, old["receipt_id"]) == (200, accepted["receipt_id"])
    with pytest.raises(HTTPException) as error, session_factory.begin() as session:
        http_ingress.receive(session, route, "Bearer source-token", "two", b"{}")
    assert error.value.status_code == 503

    with session_factory.begin() as session:
        lease = session.get(SubscriptionLease, http_world["sub"])
        lease.lease_until = database_now(session) - timedelta(seconds=1)
        next_lease = subscription_delivery.acquire_lease(session, http_world["sub"], "owner-b")
        assert next_lease.generation > original[1] and next_lease.control_instance_id == b
    with session_factory.begin() as session:
        status, new = http_ingress.receive(session, route, "Bearer source-token", "two", b"{}")
        assert status == 202
        saved = session.get(EventReceipt, new["receipt_id"])
        assert (saved.source_owner_id, saved.sequence) == ("owner-b", 2)


def test_old_dlq_delivery_does_not_clear_the_successor_publish_lease(
    api_client, session_factory, monkeypatch
):
    worker, execution, body = _published_execution(
        api_client, session_factory, monkeypatch, "dlq-ha"
    )
    monkeypatch.setattr(settings, "control_ha_slot", "a")
    monkeypatch.setattr(
        capacity, "_local_process", SimpleNamespace(instance_id=uuid4(), assert_live=lambda _: None)
    )
    queue = rabbitmq.topology_names(worker["id"]).queue
    headers = {
        "x-dlr-publish-attempt": 1,
        "x-death": [{"queue": queue, "reason": "rejected", "count": 1}],
    }
    query = select(ExecutionOutbox).where(ExecutionOutbox.execution_id == execution["id"])
    with session_factory.begin() as session:
        session.scalar(query).publish_attempts = 1
    with session_factory() as session:
        first = infrastructure_dlq.reconcile_message(session, body, headers=headers)
        assert first.action == "requeue"
    with session_factory() as session:
        next_publish = outbox.lease_due_outbox(session, "successor", limit=1)[0]
        assert next_publish.lease_attempt == 2
        repeated = infrastructure_dlq.reconcile_message(session, body, headers=headers)
        assert (repeated.action, repeated.incident_id) == ("duplicate", first.incident_id)
        assert session.scalar(query).lease_owner == "successor"


def test_expired_ordinary_outbox_confirm_preserves_pending_responsibility(
    api_client, session_factory, monkeypatch
):
    _, execution, _ = _published_execution(api_client, session_factory, monkeypatch, "confirm")
    with session_factory.begin() as session:
        row = session.scalar(
            select(ExecutionOutbox).where(ExecutionOutbox.execution_id == execution["id"])
        )
        assert row.reservation_id is None
        row.status = "pending"
        row.published_at = None
    with session_factory() as session:
        first = outbox.lease_due_outbox(session, "first", limit=1)[0]
    with session_factory.begin() as session:
        session.get(ExecutionOutbox, first.id).lease_expires_at = database_now(session) - timedelta(
            seconds=1
        )
    with session_factory() as session:
        assert not outbox.mark_outbox_published(
            session, first.id, "first", lease_attempt=first.lease_attempt
        )
        second = outbox.lease_due_outbox(session, "second", limit=1)[0]
        assert second.id == first.id and second.lease_attempt > first.lease_attempt
        assert outbox.mark_outbox_published(
            session, second.id, "second", lease_attempt=second.lease_attempt
        )


def test_late_topology_result_cannot_discard_a_newer_observation(session_factory, monkeypatch):
    a, b = [SimpleNamespace(instance_id=uuid4(), assert_live=lambda _: None) for _ in range(2)]
    monkeypatch.setattr(settings, "control_ha_slot", "a")
    monkeypatch.setattr(capacity, "_local_process", a)
    monkeypatch.setattr(db, "SessionLocal", session_factory)
    with session_factory() as session:
        old_failure = rabbitmq.reserve_topology_observation(session)
        old_success = rabbitmq.reserve_topology_observation(session)
    monkeypatch.setattr(capacity, "_local_process", b)
    with session_factory() as session:
        current = rabbitmq.reserve_topology_observation(session)
    facts = rabbitmq.RabbitMQRuntimeCapabilities(
        rabbitmq.RABBITMQ_BASELINE_VERSION, rabbitmq.REQUIRED_FEATURE_FLAGS
    )
    assert rabbitmq._persist_runtime_capabilities([], facts, current)
    monkeypatch.setattr(capacity, "_local_process", a)
    rabbitmq._invalidate_persisted_capability(old_failure)
    assert not rabbitmq._persist_runtime_capabilities([42], facts, old_success)
    with session_factory() as session:
        assert session.get(RabbitMQRuntimeCapability, 1).worker_ids == []
