"""Capacity adoption uses the real quota lock and preserves retained responsibility."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import EventSubscription, SubscriptionLease, SubscriptionQuota
from dlr.control.models.ingress_capacity import IngressCapacityAudit, IngressCapacityConfiguration
from dlr.control.services import ingress_capacity as capacity
from dlr.control.services.input_config import database_now
from test_adapters import create_adapter
from test_ingress_capacity import formal_profile


def test_capacity_adoption_audits_once_and_invalid_local_json_keeps_retained_budget(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ingress_capacity_profile", None)
    with session_factory.begin() as session:
        session.add(
            SubscriptionQuota(
                scope="global",
                pending_count=3,
                pending_bytes=333,
                history_count=5,
                history_bytes=555,
                dedupe_count=7,
            )
        )
        assert capacity.activate_profile(session)
    with session_factory.begin() as session:
        assert capacity.activate_profile(session)
        assert session.scalar(select(func.count()).select_from(IngressCapacityAudit)) == 1
        quota = session.get(SubscriptionQuota, "global")
        assert quota is not None
        assert (
            quota.pending_count,
            quota.pending_bytes,
            quota.history_count,
            quota.history_bytes,
            quota.dedupe_count,
        ) == (3, 333, 5, 555, 7)
    monkeypatch.setattr(settings, "ingress_capacity_profile", "invalid")
    with session_factory() as session:
        assert capacity.global_limits(session) == capacity.DEFAULT_GLOBAL_LIMITS
        assert (
            capacity.runtime_subscription_limits(session, {})
            == capacity.DEFAULT_SUBSCRIPTION_LIMITS
        )
        with pytest.raises(HTTPException) as error:
            capacity.require_active_profile(session)
        assert error.value.status_code == 503
    assert api_client.get("/api/adapters").status_code == 200


def test_profile_change_waits_for_old_owners_then_lower_budget_preserves_counters(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ingress_capacity_profile", None)
    adapter = create_adapter(api_client)
    with session_factory.begin() as session:
        assert capacity.activate_profile(session)
        sub = EventSubscription(
            adapter_id=adapter["id"], source_type="rabbitmq", source_epoch=uuid4().hex
        )
        session.add(sub)
        session.flush()
        sub_id = sub.id
        session.add(
            SubscriptionLease(
                subscription_id=sub_id,
                owner_id="old-process",
                generation=1,
                lease_until=database_now(session) + timedelta(seconds=30),
            )
        )
        quota = session.get(SubscriptionQuota, "global")
        assert quota is not None
        # More than the newly lowered global history maximum: adoption must
        # neither trim this ledger nor stop terminal settlement.
        quota.history_count = 70000
        quota.history_bytes = 600 * 1024**2
        quota.dedupe_count = 70000
        quota.reserved_control_count = 2
        quota.reserved_control_bytes = 4096
    monkeypatch.setattr(settings, "ingress_capacity_profile", formal_profile())
    with session_factory.begin() as session:
        assert capacity.activate_profile(session) is False
        row = session.get(IngressCapacityConfiguration, 1)
        assert row is not None
        assert row.fingerprint == capacity.parse_profile(None).fingerprint
    with session_factory.begin() as session:
        lease = session.get(SubscriptionLease, sub_id)
        assert lease is not None
        lease.lease_until = database_now(session) - timedelta(seconds=1)
    with session_factory.begin() as session:
        assert capacity.activate_profile(session)
        assert capacity.global_limits(session)["history_count"] == 60000
        assert (
            capacity.runtime_subscription_limits(session, {"limits": {"rate": 1000}})["rate"] == 250
        )
        quota = session.get(SubscriptionQuota, "global")
        assert quota is not None
        assert (quota.history_count, quota.history_bytes, quota.dedupe_count) == (
            70000,
            600 * 1024**2,
            70000,
        )
        assert (quota.reserved_control_count, quota.reserved_control_bytes) == (2, 4096)
        audits = session.scalars(
            select(IngressCapacityAudit).order_by(IngressCapacityAudit.id)
        ).all()
        assert len(audits) == 2
        assert audits[1].previous_fingerprint == audits[0].fingerprint
    # An old process cannot keep receiving under its own settings after the
    # durable adoption, but it can read the accepted budget for retained work.
    monkeypatch.setattr(settings, "ingress_capacity_profile", None)
    with session_factory() as session:
        assert capacity.global_limits(session)["rate"] == 500
        with pytest.raises(HTTPException) as error:
            capacity.require_active_profile(session)
        assert error.value.detail["code"] == "ingress_capacity_unavailable"


def test_adoption_and_audit_roll_back_together(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ingress_capacity_profile", formal_profile())
    with session_factory() as session:
        assert capacity.activate_profile(session)
        session.rollback()
    with session_factory() as session:
        assert session.get(IngressCapacityConfiguration, 1) is None
        assert session.scalar(select(func.count()).select_from(IngressCapacityAudit)) == 0
        assert session.get(SubscriptionQuota, "global") is None
    with session_factory.begin() as session:
        capacity.require_active_profile(session)
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(IngressCapacityAudit)) == 1


def test_concurrent_first_adoption_serializes_on_real_global_quota(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ingress_capacity_profile", formal_profile())
    barrier = Barrier(2)

    def adopt() -> bool:
        barrier.wait(timeout=5)
        with session_factory.begin() as session:
            return capacity.activate_profile(session)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: adopt(), range(2)))
    assert results == [True, True]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(IngressCapacityAudit)) == 1
        row = session.get(IngressCapacityConfiguration, 1)
        assert row is not None
        assert row.fingerprint == capacity.parse_profile(formal_profile()).fingerprint
        assert session.scalar(select(func.count()).select_from(SubscriptionQuota)) == 1


def test_adopted_profile_restart_does_not_reserve_its_full_history_budget_again(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    from types import SimpleNamespace

    gib = 1024**3
    monkeypatch.setattr(
        settings, "ingress_capacity_profile", json.dumps({"global": {"history_bytes": 16 * gib}})
    )
    monkeypatch.setattr(capacity.shutil, "disk_usage", lambda _: SimpleNamespace(free=32 * gib))
    with session_factory.begin() as session:
        assert capacity.activate_profile(session)
    # Retained history may already occupy space after an ordinary restart.
    # Re-adopting the same fingerprint does not allocate a second full budget.
    monkeypatch.setattr(capacity.shutil, "disk_usage", lambda _: SimpleNamespace(free=10 * gib))
    with session_factory.begin() as session:
        assert capacity.activate_profile(session)
    monkeypatch.setattr(
        settings, "ingress_capacity_profile", json.dumps({"global": {"history_bytes": 17 * gib}})
    )
    with session_factory.begin() as session:
        assert capacity.activate_profile(session) is False
        assert capacity.global_limits(session)["history_bytes"] == 16 * gib
