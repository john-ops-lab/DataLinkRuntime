"""Duplicate reads are removed while real PostgreSQL still fences each T1."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from dlr.common.config import settings
from dlr.control.models import SubscriptionConfigVersion, SubscriptionQuota
from dlr.control.models.ingress_capacity import IngressCapacityConfiguration
from dlr.control.services import ingress_capacity as capacity
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services.input_config import database_now
from dlr.control.services.subscription_queue import capacity_ready
from test_ingress_capacity import formal_profile
from test_issue219_delivery import receive
from test_issue219_delivery import world as foundation_world
from test_issue222_queue_capacity_profile import context

world = foundation_world


@contextmanager
def selects(factory):
    statements = []
    engine = factory.kw["bind"]

    def record(connection, cursor, statement, parameters, execution_context, many):
        if statement.startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_queue_precheck_reads_one_profile_and_both_scopes_in_three_queries(
    api_client, session_factory, monkeypatch
):
    monkeypatch.setattr(settings, "ingress_capacity_profile", formal_profile())
    with session_factory.begin() as session:
        assert capacity.activate_profile(session)
        session.add(SubscriptionQuota(scope="sub:7"))
    with selects(session_factory) as queries:
        assert capacity_ready(
            context(session_factory, json.loads(formal_profile())["subscription_max"])
        )
    assert len(queries) == 3
    assert "clock_timestamp()" in queries[0] and "min(event_deliveries.created_at)" in queries[0]
    assert sum("FROM ingress_capacity_configurations" in query for query in queries) == 1
    assert sum("FROM subscription_quotas" in query for query in queries) == 1
    assert "subscription_quotas.scope IN" in queries[-1]


def test_each_real_t1_reuses_its_one_fresh_adopted_profile_and_checks_disk(
    world, session_factory, monkeypatch
):
    monkeypatch.setattr(settings, "ingress_capacity_profile", formal_profile())
    limits = json.loads(formal_profile())["subscription_max"]
    with session_factory.begin() as session:
        assert capacity.activate_profile(session)
        session.get(SubscriptionConfigVersion, world["config"]).config = {"limits": limits}
        for quota in delivery._quotas(session, world["sub"]):
            quota.rate_count, quota.rate_window_at = 60, database_now(session)
    disk_calls = []
    original_disk = capacity.require_disk_capacity

    def disk():
        disk_calls.append(True)
        original_disk()

    monkeypatch.setattr(capacity, "require_disk_capacity", disk)
    for index in range(2):
        with selects(session_factory) as queries, session_factory.begin() as session:
            receive(session, world, index)
            assert sum("FROM ingress_capacity_configurations" in query for query in queries) == 1
            assert delivery.audit_quota(session, world["sub"])["differences"] == {}
    assert len(disk_calls) == 2


@pytest.mark.parametrize("scope", ["sub:7", "global"])
@pytest.mark.parametrize("reason", ["rate", "reserved_count"])
def test_bulk_precheck_preserves_both_scope_rate_and_reserved_low_water_guards(
    api_client, session_factory, monkeypatch, scope, reason
):
    monkeypatch.setattr(settings, "ingress_capacity_profile", None)
    maximum = (
        capacity.DEFAULT_GLOBAL_LIMITS
        if scope == "global"
        else capacity.DEFAULT_SUBSCRIPTION_LIMITS
    )
    with session_factory.begin() as session:
        assert capacity.activate_profile(session)
        quota = session.get(SubscriptionQuota, scope)
        if quota is None:
            quota = SubscriptionQuota(scope=scope)
            session.add(quota)
        if reason == "rate":
            quota.rate_count, quota.rate_window_at = maximum["rate"], database_now(session)
        else:
            quota.pending_count = int(maximum["pending_count"] * 0.8)
            quota.reserved_control_count = 1
    assert capacity_ready(context(session_factory, {})) is False


def test_read_committed_fresh_t1_gate_rejects_profile_adopted_after_advisory_read(
    api_client, session_factory, monkeypatch
):
    old = capacity.parse_profile(formal_profile())
    new = capacity.parse_profile(None)
    local = threading.local()
    local.profile = old
    monkeypatch.setattr(capacity, "configured_profile", lambda: local.profile)
    with session_factory.begin() as session:
        assert capacity.activate_profile(session)
    with session_factory() as stale:
        assert stale.connection().get_isolation_level() == "READ COMMITTED"
        retained = stale.get(IngressCapacityConfiguration, 1)
        assert retained.fingerprint == old.fingerprint
        assert capacity.effective_profile(stale).fingerprint == old.fingerprint

        def adopt_new():
            local.profile = new
            with session_factory.begin() as session:
                assert capacity.activate_profile(session)

        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(adopt_new).result(timeout=5)
        # Identity-map state from the advisory read must never supply T1.
        delivery._quotas(stale, 7)
        with pytest.raises(HTTPException) as rejected:
            capacity.require_active_profile(stale)
        assert rejected.value.detail["code"] == "ingress_capacity_unavailable"
        assert retained.fingerprint == new.fingerprint
        stale.rollback()
    with session_factory() as session:
        assert session.get(IngressCapacityConfiguration, 1).fingerprint == new.fingerprint
        assert session.get(SubscriptionQuota, "global").pending_count == 0


def test_global_quota_lock_prevents_profile_change_between_two_t1_limit_calculations(
    api_client, session_factory, monkeypatch
):
    old = capacity.parse_profile(formal_profile())
    new = capacity.parse_profile(None)
    local = threading.local()
    local.profile = old
    monkeypatch.setattr(capacity, "configured_profile", lambda: local.profile)
    with session_factory.begin() as session:
        assert capacity.activate_profile(session)
    entering_lock = threading.Event()
    original_lock = capacity._lock_global_quota

    def global_lock(session):
        entering_lock.set()
        original_lock(session)

    monkeypatch.setattr(capacity, "_lock_global_quota", global_lock)

    def adopt_new():
        local.profile = new
        with session_factory.begin() as session:
            return capacity.activate_profile(session)

    with session_factory() as custody, ThreadPoolExecutor(max_workers=1) as executor:
        delivery._quotas(custody, 7)
        accepted = capacity.require_active_profile(custody)
        future = executor.submit(adopt_new)
        try:
            assert entering_lock.wait(timeout=5)
            assert not future.done()
            with selects(session_factory) as queries:
                assert (
                    capacity.runtime_limits_for_profile(accepted, {"limits": {"rate": 250}})["rate"]
                    == 250
                )
                assert capacity.global_limits(profile=accepted)["rate"] == 500
            assert not queries
            custody.commit()
        finally:
            custody.rollback()
        assert future.result(timeout=5)
    with session_factory() as session:
        assert session.get(IngressCapacityConfiguration, 1).fingerprint == new.fingerprint
