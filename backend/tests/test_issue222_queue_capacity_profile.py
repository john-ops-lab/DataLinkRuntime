"""The advisory queue precheck reads the same durable capacity as locked T1."""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from dlr.common.config import settings
from dlr.control.models import SubscriptionQuota
from dlr.control.services import ingress_capacity as capacity
from dlr.control.services import subscription_queue as queue
from dlr.control.services.input_config import database_now
from dlr.control.services.subscription_queue import capacity_ready
from test_ingress_capacity import formal_profile
from test_issue222_capacity_pending_age import ControlledFunctions

pytestmark = pytest.mark.usefixtures("deterministic_capacity_space")


@pytest.fixture
def fixed_now(session_factory: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    with session_factory() as session:
        now = database_now(session)
    monkeypatch.setattr(queue, "func", ControlledFunctions(now))
    return now


def context(factory: Any, limits: Any) -> Any:
    return SimpleNamespace(
        subscription_id=7,
        session_factory=factory,
        source_config={"source_type": "rabbitmq", "limits": limits},
    )


@pytest.mark.parametrize("threshold", ["rate", "pending_count"])
def test_raised_adopted_profile_does_not_stop_at_old_global_defaults(
    api_client: Any,
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    threshold: str,
    fixed_now: Any,
) -> None:
    monkeypatch.setattr(settings, "ingress_capacity_profile", formal_profile())
    limits = json.loads(formal_profile())["subscription_max"]
    with session_factory.begin() as session:
        assert capacity.activate_profile(session)
        for scope in ("sub:7", "global"):
            row = session.get(SubscriptionQuota, scope)
            if row is None:
                row = SubscriptionQuota(scope=scope)
                session.add(row)
            row.rate_window_at = fixed_now
            if threshold == "rate":
                row.rate_count = 60  # Above old global 50, below adopted entry/global.
            else:
                row.pending_count = row.history_count = row.dedupe_count = 9000
                row.pending_bytes = row.history_bytes = 9000
    assert capacity_ready(context(session_factory, limits)) is True


def test_saved_high_entry_limit_is_clamped_to_adopted_maximum(
    api_client: Any, session_factory: Any, monkeypatch: pytest.MonkeyPatch, fixed_now: Any
) -> None:
    monkeypatch.setattr(settings, "ingress_capacity_profile", None)
    with session_factory.begin() as session:
        assert capacity.activate_profile(session)
        session.add(SubscriptionQuota(scope="sub:7", rate_count=11, rate_window_at=fixed_now))
    # A retained source snapshot can outlive a deployment reduction; advisory
    # prechecks must clamp it to the adopted 10/s rather than accepting 250/s.
    assert capacity_ready(context(session_factory, {"rate": 250})) is False


def test_precheck_uses_durable_profile_but_cannot_bypass_new_custody_fingerprint_gate(
    api_client: Any, session_factory: Any, monkeypatch: pytest.MonkeyPatch, fixed_now: Any
) -> None:
    monkeypatch.setattr(settings, "ingress_capacity_profile", formal_profile())
    limits = json.loads(formal_profile())["subscription_max"]
    with session_factory.begin() as session:
        assert capacity.activate_profile(session)
        row = session.get(SubscriptionQuota, "global")
        row.rate_count, row.rate_window_at = 60, fixed_now
    monkeypatch.setattr(settings, "ingress_capacity_profile", None)
    assert capacity_ready(context(session_factory, limits)) is True
    with session_factory() as session, pytest.raises(HTTPException) as rejected:
        capacity.require_active_profile(session)
    assert rejected.value.detail["code"] == "ingress_capacity_unavailable"
