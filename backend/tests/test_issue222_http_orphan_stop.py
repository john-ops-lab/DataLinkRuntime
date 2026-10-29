"""Passive HTTP shutdown fences intake; native work and durable custody keep their lifetimes."""

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from dlr.control.api import ingress
from dlr.control.models import (
    EventDelivery,
    EventReceipt,
    EventSubscription,
    SubscriptionLease,
    SubscriptionQuota,
)
from dlr.control.security import SUPERADMIN_PRINCIPAL
from dlr.control.services import http_ingress as service
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_guards
from dlr.control.services import subscription_management as management
from dlr.control.services.input_config import database_now
from dlr.control.services.subscription_http import HttpSourceRunner
from dlr.control.services.subscription_runtime import SubscriptionRuntimeManager, _Owner
from dlr.control.services.subscription_source import SourceFailure, SourceRunContext
from test_http_ingress_limits import request
from test_issue222_http_ingress import http_world as foundation_http_world
from test_issue222_http_ingress import world as foundation_world

world = foundation_world
http_world = foundation_http_world


@pytest.fixture
def memory_world(monkeypatch):
    now = datetime(2026, 10, 26, tzinfo=UTC)
    sub = SimpleNamespace(
        id=9,
        adapter_id=1,
        source_type="http",
        desired_state="enabled",
        connection_state="listening",
        deleted_at=None,
        activation_id=1,
        source_epoch="epoch",
        config_version=1,
        delivery_state="idle",
    )
    lease = SimpleNamespace(
        subscription_id=9,
        owner_id="original-http-owner",
        generation=3,
        closing_generation=None,
        lease_until=now + timedelta(seconds=30),
    )

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, model, key, **kwargs):
            assert model is SubscriptionLease and key == sub.id
            return lease

        def execute(self, *args, **kwargs):
            return SimpleNamespace(one=lambda: (False, False, False, False))

    class Factory:
        def __call__(self):
            return Session()

        def begin(self):
            return Session()

    monkeypatch.setattr(delivery, "lock_subscription", lambda *args: (object(), sub))
    monkeypatch.setattr(management, "get_subscription", lambda *args, **kwargs: sub)
    monkeypatch.setattr(management, "database_now", lambda _: now)
    monkeypatch.setattr(delivery, "database_now", lambda _: now)
    return SimpleNamespace(sub=sub, lease=lease, session=Session(), factory=Factory(), now=now)


def owner_for(manager, state):
    context = SourceRunContext(
        state.sub.id,
        state.lease.owner_id,
        state.lease.generation,
        state.sub.activation_id,
        state.sub.config_version,
        state.sub.source_epoch,
        {"source_type": "http", "mode": "business_batch"},
        {},
        threading.Event(),
        state.factory,
        manager.buffer.scope(),
        lambda: manager._connected(state.sub.id),
    )
    owner = _Owner(context, HttpSourceRunner())
    assert manager._permits.acquire(blocking=False)
    manager._owners[state.sub.id] = owner
    return owner


def test_passive_http_disable_synchronously_closes_an_orphan_and_is_idempotent(memory_world):
    state = memory_world
    # No local runtime or owner is required: the crashed process cannot ACK it.
    management.disable_subscription(state.session, state.sub.id, 1, SUPERADMIN_PRINCIPAL)
    assert state.sub.desired_state == "disabled" and state.sub.connection_state == "stopped"
    assert state.lease.generation == 4 and state.lease.closing_generation is None
    assert state.lease.owner_id == "" and state.lease.lease_until == state.now
    management.disable_subscription(state.session, state.sub.id, 1, SUPERADMIN_PRINCIPAL)
    assert state.lease.generation == 4 and state.lease.owner_id == ""


def test_stop_single_cannot_reopen_a_closed_http_lifecycle(memory_world):
    state = memory_world
    manager = SubscriptionRuntimeManager(state.factory, {})
    owner = owner_for(manager, state)
    management.disable_subscription(state.session, state.sub.id, 1, SUPERADMIN_PRINCIPAL)
    manager.stop_single(state.sub.id)
    assert owner.context.stop_event.is_set() and state.sub.connection_state == "stopped"
    manager._finish_owner(owner, None)
    assert manager.active_count == 0 and state.sub.connection_state == "stopped"
    assert state.lease.owner_id == "" and state.lease.closing_generation is None


def test_stop_single_can_close_legacy_disabled_local_http_owner(memory_world):
    state = memory_world
    manager = SubscriptionRuntimeManager(state.factory, {})
    owner = owner_for(manager, state)
    state.sub.desired_state = "disabled"
    state.sub.connection_state = "stopping"
    manager.stop_single(state.sub.id)
    assert owner.context.stop_event.is_set() and state.sub.connection_state == "stopped"
    assert state.lease.generation == 4 and state.lease.owner_id == ""
    assert state.lease.closing_generation is None
    manager._finish_owner(owner, None)
    assert manager.active_count == 0


def test_disable_before_builtin_http_connected_releases_returned_passive_owner(
    memory_world, monkeypatch
):
    state = memory_world
    manager = SubscriptionRuntimeManager(state.factory, {})
    owner = owner_for(manager, state)
    entered, resume = threading.Event(), threading.Event()
    connected = manager._connected

    def gated(subscription_id):
        entered.set()
        assert resume.wait(5)
        connected(subscription_id)

    monkeypatch.setattr(manager, "_connected", gated)
    thread = threading.Thread(target=manager._run_owner, args=(owner,))
    thread.start()
    try:
        assert entered.wait(5)
        management.disable_subscription(state.session, state.sub.id, 1, SUPERADMIN_PRINCIPAL)
    finally:
        resume.set()
        thread.join(5)
    assert not thread.is_alive()
    assert manager.active_count == 0 and state.sub.connection_state == "stopped"
    assert owner.native_closed and not owner.close_unconfirmed
    assert owner.failure.code == "subscription_owner_stale" and owner.failure.closed
    assert owner.failure.disposition == "backoff"


def test_generic_mq_failure_keeps_unknown_close_and_credits(memory_world):
    state = memory_world
    state.sub.source_type = "rabbitmq"
    manager = SubscriptionRuntimeManager(state.factory, {})
    owner = owner_for(manager, state)
    owner.context.source_config["source_type"] = "rabbitmq"
    assert owner.context.buffer.reserve(128)

    class BrokenNative:
        def run(self, _context):
            raise RuntimeError("untrusted driver text")

    owner.runner = BrokenNative()
    manager._run_owner(owner)
    assert owner.close_unconfirmed and not owner.native_closed
    assert not owner.failure.closed and owner.failure.code == "source_runtime_error"
    assert manager.active_count == 1 and manager.buffer.used_bytes == 128
    assert state.lease.owner_id and state.sub.connection_state == "blocked"


def test_builtin_http_explicit_unknown_close_stays_conservative(memory_world, monkeypatch):
    state = memory_world
    manager = SubscriptionRuntimeManager(state.factory, {})
    owner = owner_for(manager, state)
    assert owner.context.buffer.reserve(128)

    def unknown(_subscription_id):
        raise SourceFailure("source_explicit_unknown", "backoff", closed=False)

    monkeypatch.setattr(manager, "_connected", unknown)
    manager._run_owner(owner)
    assert owner.close_unconfirmed and not owner.native_closed
    assert owner.failure.code == "source_explicit_unknown" and not owner.failure.closed
    assert manager.active_count == 1 and manager.buffer.used_bytes == 128


@pytest.mark.parametrize("source_type", ["rabbitmq", "rocketmq"])
def test_passive_close_helper_never_clears_mq_owner_even_if_expired(memory_world, source_type):
    state = memory_world
    state.sub.source_type = source_type
    state.lease.lease_until = state.now - timedelta(minutes=5)
    management.disable_subscription(state.session, state.sub.id, 1, SUPERADMIN_PRINCIPAL)
    assert state.sub.connection_state == "stopping"
    assert state.lease.owner_id == "original-http-owner"
    assert state.lease.generation == 4 and state.lease.closing_generation == 3
    assert not management._close_disabled_http_lease(state.session, state.sub, state.lease)
    assert state.lease.owner_id and state.sub.connection_state == "stopping"


def test_stale_local_http_owner_cannot_clear_another_owner(memory_world):
    state = memory_world
    manager = SubscriptionRuntimeManager(state.factory, {})
    owner = owner_for(manager, state)
    state.lease.owner_id = "replacement-owner"
    state.lease.generation = 5
    state.sub.desired_state = "disabled"
    state.sub.connection_state = "stopping"
    manager.stop_single(state.sub.id)
    assert owner.context.stop_event.is_set()
    assert state.lease.owner_id == "replacement-owner" and state.lease.generation == 5
    manager._finish_owner(owner, None)
    assert manager.active_count == 0 and state.lease.owner_id == "replacement-owner"


def test_http_disable_does_not_release_or_cancel_real_ingress_native_work(
    memory_world, monkeypatch
):
    async def check():
        state = memory_world
        manager = SubscriptionRuntimeManager(state.factory, {})
        owner = owner_for(manager, state)
        pool = ingress.IngressResources(buffer=manager.buffer, session_factory=state.factory)
        started, unblock = threading.Event(), threading.Event()

        def blocked(_pool, _public, items):
            started.set()
            assert unblock.wait(5)
            return [(202, {"receipt_id": index}) for index in range(len(items))]

        monkeypatch.setattr(ingress, "resources", lambda: pool)
        monkeypatch.setattr(ingress, "_run_group_sync", blocked)
        from dlr.control.services import subscription_runtime

        monkeypatch.setattr(
            subscription_runtime,
            "get_runtime",
            lambda: SimpleNamespace(http_batch_hint=lambda _: True),
        )
        tasks = [
            asyncio.create_task(
                ingress._request(
                    "route",
                    request([b"{}"], [(b"content-type", b"application/json")]),
                    "Bearer source-token",
                    str(index),
                )
            )
            for index in range(4)
        ]
        try:
            async with asyncio.timeout(5):
                while not started.is_set():
                    await asyncio.sleep(0.001)
                # Four fully read bodies retain their raw + native reservations.
                held = 4 * (2 + 4 * 1024 + 96 * 2 + 64 * 1024)
                assert pool.raw_buffer.used_bytes == 4 * (2 + 4 * 1024)
                assert pool.workspace_buffer.used_bytes == 4 * (96 * 2 + 64 * 1024)
                assert manager.buffer.used_bytes == pool.buffer.used_bytes == held
                management.disable_subscription(
                    state.session, state.sub.id, 1, SUPERADMIN_PRINCIPAL
                )
                manager.stop_single(state.sub.id)
                manager._finish_owner(owner, None)
                assert state.sub.connection_state == "stopped" and manager.active_count == 0
                assert manager.buffer.used_bytes == pool.buffer.used_bytes == held
                assert pool.work.used == 1 and pool.requests._value == 124
                assert all(not task.done() for task in tasks)
                unblock.set()
                assert [response.status_code for response in await asyncio.gather(*tasks)] == [
                    202
                ] * 4
                while manager.buffer.used_bytes:
                    await asyncio.sleep(0.001)
                assert pool.work.used == 0 and pool.requests._value == 128
        finally:
            unblock.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            pool.close()

    asyncio.run(check())


def disable_in_database(session_factory, world):
    with session_factory.begin() as session:
        return management.disable_subscription(
            session, world["sub"], 1, SUPERADMIN_PRINCIPAL
        ).connection_state


def test_real_database_orphan_http_lease_closes_and_old_generation_cannot_receive(
    http_world, session_factory
):
    with session_factory() as session:
        lease = session.get(SubscriptionLease, http_world["sub"])
        old_owner, old_generation = lease.owner_id, lease.generation
    assert disable_in_database(session_factory, http_world) == "stopped"
    with session_factory() as session:
        sub = session.get(EventSubscription, http_world["sub"])
        lease = session.get(SubscriptionLease, sub.id)
        assert lease.owner_id == "" and lease.closing_generation is None
        assert lease.generation == old_generation + 1
        with pytest.raises(HTTPException) as failure:
            delivery._lease(session, sub, old_owner, old_generation)
        assert failure.value.detail["code"] == "subscription_owner_stale"
        with pytest.raises(HTTPException) as failure:
            service.receive(session, http_world["public_id"], "Bearer source-token", "new", b"{}")
        assert failure.value.detail["code"] == "ingress_disabled"
    assert disable_in_database(session_factory, http_world) == "stopped"
    with session_factory() as session:
        assert session.get(SubscriptionLease, http_world["sub"]).generation == old_generation + 1
        assert not delivery.has_unfinished(session, http_world["sub"])


@pytest.mark.parametrize("disable_before_connected", [False, True])
def test_real_database_active_passive_http_owner_stops_without_stopping_regression(
    http_world, session_factory, monkeypatch, disable_before_connected
):
    with session_factory.begin() as session:
        lease = session.get(SubscriptionLease, http_world["sub"])
        lease.lease_until = database_now(session) - timedelta(seconds=1)
    manager = SubscriptionRuntimeManager(session_factory, {"http": HttpSourceRunner()})
    connected, entered, resume = threading.Event(), threading.Event(), threading.Event()
    original = manager._connected

    def ready(subscription_id):
        entered.set()
        if disable_before_connected:
            assert resume.wait(5)
        original(subscription_id)
        connected.set()

    monkeypatch.setattr(manager, "_connected", ready)
    try:
        # The fixture has one legacy activation: its first tick freezes policy;
        # the next tick starts the owner through the same production gate.
        assert manager.start_once(http_world["sub"]) or manager.start_once(http_world["sub"])
        assert entered.wait(5)
        if not disable_before_connected:
            assert connected.wait(5)
        with manager._lock:
            owner = manager._owners[http_world["sub"]]
        assert disable_in_database(session_factory, http_world) == "stopped"
        if disable_before_connected:
            resume.set()
        else:
            manager.stop_single(http_world["sub"])
        deadline = time.monotonic() + 5
        while manager.active_count and time.monotonic() < deadline:
            time.sleep(0.005)
        assert manager.active_count == 0
        if disable_before_connected:
            assert owner.failure.code == "subscription_owner_stale" and owner.failure.closed
        with session_factory() as session:
            assert session.get(EventSubscription, http_world["sub"]).connection_state == "stopped"
            lease = session.get(SubscriptionLease, http_world["sub"])
            assert lease.owner_id == "" and lease.closing_generation is None
    finally:
        resume.set()
        with manager._lock:
            owners = list(manager._owners.values())
        for owner in owners:
            owner.context.stop_event.set()
            if owner.thread:
                owner.thread.join(5)


def test_real_database_http_stop_preserves_pending_history_quota_and_edit_lock(
    http_world, session_factory
):
    with session_factory.begin() as session:
        status, accepted = service.receive(
            session, http_world["public_id"], "Bearer source-token", "kept", b'{"value":1}'
        )
        assert status == 202
        receipt_id = accepted["receipt_id"]
    with session_factory() as session:
        local = session.get(SubscriptionQuota, f"sub:{http_world['sub']}")
        before = (
            local.pending_count,
            local.pending_bytes,
            local.history_count,
            local.history_bytes,
        )
        receipt = session.get(EventReceipt, receipt_id)
        content = (receipt.content_hash, receipt.source_raw_body, receipt.envelope)
        delivery_id = session.scalar(
            select(EventDelivery.id).where(EventDelivery.receipt_id == receipt_id)
        )
    assert disable_in_database(session_factory, http_world) == "stopped"
    with session_factory.begin() as session:
        sub = session.get(EventSubscription, http_world["sub"])
        assert sub.delivery_state == "draining" and delivery.has_unfinished(session, sub.id)
        assert subscription_guards.runtime_guard(session, http_world["adapter"])
        with pytest.raises(HTTPException) as failure:
            management._require_drained(session, sub)
        assert failure.value.detail["code"] == "subscription_draining"
        receipt = session.get(EventReceipt, receipt_id)
        assert (receipt.content_hash, receipt.source_raw_body, receipt.envelope) == content
        assert receipt.state == session.get(EventDelivery, delivery_id).state == "pending"
        local = session.get(SubscriptionQuota, f"sub:{sub.id}")
        assert (
            local.pending_count,
            local.pending_bytes,
            local.history_count,
            local.history_bytes,
        ) == before
        status, duplicate = service.receive(
            session, http_world["public_id"], "Bearer source-token", "kept", b'{"value":1}'
        )
        assert status == 200 and duplicate["receipt_id"] == receipt_id
        assert delivery.audit_quota(session, sub.id)["differences"] == {}
        assert delivery.audit_global_quota(session)["differences"] == {}


@pytest.mark.parametrize("source_type", ["rabbitmq", "rocketmq"])
def test_real_database_mq_disable_keeps_actual_close_requirement(
    world, session_factory, source_type
):
    with session_factory.begin() as session:
        session.get(EventSubscription, world["sub"]).source_type = source_type
        old = session.get(SubscriptionLease, world["sub"])
        owner, generation = old.owner_id, old.generation
    assert disable_in_database(session_factory, world) == "stopping"
    with session_factory.begin() as session:
        sub = session.get(EventSubscription, world["sub"])
        lease = session.get(SubscriptionLease, sub.id)
        assert lease.owner_id == owner and lease.closing_generation == generation
        lease.lease_until = database_now(session) - timedelta(minutes=5)
        with pytest.raises(HTTPException) as failure:
            management._require_drained(session, sub)
        assert failure.value.detail["code"] == "subscription_draining"
    assert disable_in_database(session_factory, world) == "stopping"
    with session_factory() as session:
        lease = session.get(SubscriptionLease, world["sub"])
        assert lease.owner_id == owner and lease.closing_generation == generation
