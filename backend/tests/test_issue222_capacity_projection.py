"""Read-only management pressure reflects durable quotas without changing custody."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from dlr.common.config import settings
from dlr.control.models import (
    EventReceipt,
    EventSubscription,
    SourceCheckpoint,
    SubscriptionConfigVersion,
    SubscriptionQuota,
)
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services.input_config import database_now
from test_issue222_http_ingress import http_world as foundation_http_world
from test_issue222_http_ingress import post
from test_issue222_http_ingress import world as foundation_world

pytestmark = pytest.mark.usefixtures("deterministic_capacity_space")

http_world = foundation_http_world
world = foundation_world

COUNTERS = ("pending_count", "pending_bytes", "history_count", "history_bytes", "dedupe_count")


def snapshot(session, scope):
    row = session.get(SubscriptionQuota, scope)
    return {
        key: getattr(row, key)
        for key in (*COUNTERS, "reserved_control_count", "reserved_control_bytes", "blocked_reason")
    }


def view(api_client, http_world):
    response = api_client.get(f"/api/event-subscriptions/{http_world['sub']}")
    assert response.status_code == 200, response.text
    return response.json()


def test_disabled_http_feature_is_unavailable_instead_of_capacity_paused(
    api_client, http_world, session_factory, monkeypatch
):
    with session_factory.begin() as session:
        config = session.get(SubscriptionConfigVersion, http_world["config"])
        config.config = {**config.config, "limits": {"pending_count": 1}}
    first = post(api_client, http_world)
    assert first.status_code == 202, first.text
    assert view(api_client, http_world)["capacity_paused"] is True
    with session_factory() as session:
        before = [snapshot(session, scope) for scope in ("global", f"sub:{http_world['sub']}")]
    monkeypatch.setattr(settings, "http_ingress_enabled", False)
    result = view(api_client, http_world)
    assert result["desired_state"] == "enabled"
    assert result["available"] is False and result["capacity_paused"] is False
    with session_factory() as session:
        assert [
            snapshot(session, scope) for scope in ("global", f"sub:{http_world['sub']}")
        ] == before
        assert session.get(EventReceipt, first.json()["receipt_id"]).state == "pending"


def test_full_http_can_read_original_without_poison_or_quota_changes(
    api_client, http_world, session_factory
):
    with session_factory.begin() as session:
        config = session.get(SubscriptionConfigVersion, http_world["config"])
        config.config = {**config.config, "limits": {"pending_count": 1}}
    first = post(api_client, http_world)
    assert first.status_code == 202, first.text
    with session_factory() as session:
        before = [snapshot(session, scope) for scope in ("global", f"sub:{http_world['sub']}")]
    assert view(api_client, http_world)["capacity_paused"] is True
    rejected = post(api_client, http_world, key="another")
    assert rejected.status_code == 429
    duplicate = post(api_client, http_world)
    assert duplicate.status_code == 200
    assert duplicate.json()["receipt_id"] == first.json()["receipt_id"]
    assert view(api_client, http_world)["capacity_paused"] is True
    with session_factory() as session:
        assert [
            snapshot(session, scope) for scope in ("global", f"sub:{http_world['sub']}")
        ] == before
        assert session.get(EventSubscription, http_world["sub"]).last_error_code is None
        checkpoint = session.scalar(
            select(SourceCheckpoint).where(SourceCheckpoint.subscription_id == http_world["sub"])
        )
        assert checkpoint.blocked_unaccepted is None
        assert delivery.audit_quota(session, http_world["sub"])["differences"] == {}
    stopped = api_client.post(
        f"/api/event-subscriptions/{http_world['sub']}/disable",
        json={"expected_config_version": 1},
    )
    assert stopped.status_code == 200, stopped.text
    assert view(api_client, http_world)["capacity_paused"] is False


@pytest.mark.parametrize("scope", ["local", "global"])
@pytest.mark.parametrize("key", COUNTERS)
def test_exhausted_local_or_global_limit_is_read_only_and_does_not_expose_global_usage(
    api_client, http_world, session_factory, scope, key
):
    initial = view(api_client, http_world)
    assert initial["capacity_paused"] is False
    actual_scope = "global" if scope == "global" else f"sub:{http_world['sub']}"
    limit_key = "global_limits" if scope == "global" else "limits"
    with session_factory.begin() as session:
        session.add(SubscriptionQuota(scope=actual_scope, **{key: initial[limit_key][key]}))
    with session_factory() as session:
        before = snapshot(session, actual_scope)
    result = view(api_client, http_world)
    assert result["capacity_paused"] is True
    assert set(result["quota"]) == {
        *COUNTERS,
        "reserved_control_count",
        "reserved_control_bytes",
        "rate_count",
    }
    if scope == "global":
        assert all(value == 0 for value in result["quota"].values())
        assert "global_quota" not in result
    with session_factory() as session:
        assert snapshot(session, actual_scope) == before
        assert session.get(EventSubscription, http_world["sub"]).last_error_code is None
        assert session.scalar(select(SourceCheckpoint)) is None


def test_prepaid_controls_and_existing_low_water_hold_are_projected_not_cleared(
    api_client, http_world, session_factory
):
    initial = view(api_client, http_world)
    scope = f"sub:{http_world['sub']}"
    limit = initial["limits"]["pending_count"]
    with session_factory.begin() as session:
        session.add(
            SubscriptionQuota(scope=scope, pending_count=limit - 1, reserved_control_count=1)
        )
    assert view(api_client, http_world)["capacity_paused"] is True
    with session_factory.begin() as session:
        quota = session.get(SubscriptionQuota, scope)
        quota.reserved_control_count = 0
        quota.pending_count = limit * 81 // 100
        quota.blocked_reason = "subscription_capacity_exceeded"
    assert view(api_client, http_world)["capacity_paused"] is True
    with session_factory.begin() as session:
        quota = session.get(SubscriptionQuota, scope)
        assert quota.blocked_reason == "subscription_capacity_exceeded"
        quota.pending_count = limit * 80 // 100
    assert view(api_client, http_world)["capacity_paused"] is False
    with session_factory() as session:
        assert (
            session.get(SubscriptionQuota, scope).blocked_reason == "subscription_capacity_exceeded"
        )


def test_rate_window_is_not_a_persistent_capacity_pause(api_client, http_world, session_factory):
    initial = view(api_client, http_world)
    with session_factory.begin() as session:
        session.add(
            SubscriptionQuota(
                scope=f"sub:{http_world['sub']}",
                rate_count=initial["limits"]["rate"],
                rate_window_at=database_now(session),
            )
        )
    assert view(api_client, http_world)["capacity_paused"] is False


def test_oldest_pending_and_existing_hold_match_durable_admission_boundaries(
    api_client, http_world, session_factory
):
    first = post(api_client, http_world)
    assert first.status_code == 202, first.text
    with session_factory.begin() as session:
        receipt = session.get(EventReceipt, first.json()["receipt_id"])
        receipt.first_received_at = database_now(session) - timedelta(minutes=6)
    assert view(api_client, http_world)["capacity_paused"] is False
    with session_factory.begin() as session:
        session.get(SubscriptionQuota, "global").blocked_reason = "subscription_capacity_exceeded"
    assert view(api_client, http_world)["capacity_paused"] is True
    with session_factory.begin() as session:
        session.get(SubscriptionQuota, "global").blocked_reason = None
        session.get(EventReceipt, first.json()["receipt_id"]).first_received_at = database_now(
            session
        ) - timedelta(minutes=31)
    assert view(api_client, http_world)["capacity_paused"] is True
