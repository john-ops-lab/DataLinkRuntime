"""Runtime binding survives deployment changes, replay and legacy upgrade on real PG."""

from copy import deepcopy
from datetime import timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.models import Adapter, EventSubscription, Execution, SubscriptionActivation
from dlr.control.services import subscription_activation_policy as policy
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_management as management
from test_issue219_delivery import delivery_id, receive
from test_issue219_delivery import world as foundation_world

world = foundation_world


def test_policy_is_closed_bounded_and_has_detached_immutable_runtime_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = Adapter(timeout_seconds=17)
    snapshot = policy.snapshot_policy(adapter)
    assert snapshot["captured_from"] == "activation"
    assert snapshot["execution_id"] is None
    assert snapshot["resource_profile"]["execution_timeout_seconds"] == 17
    saved = deepcopy(snapshot)
    monkeypatch.setattr(settings, "execution_retry_max_attempts", 7)
    monkeypatch.setattr(settings, "sandbox_memory_bytes", 128 * 1024**2)
    assert policy.validate_policy(snapshot) == saved
    detached = policy.validate_policy(snapshot)
    detached["retry_policy"]["retryable_error_classes"].append("business_error")
    assert snapshot == saved
    assert "business_error" not in snapshot["retry_policy"]["retryable_error_classes"]


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"extra": "unrecognized"},
        {"captured_from": "invented-history"},
        {"captured_from": "legacy_execution", "execution_id": None},
        {"execution_id": 1},
        {"resource_profile": {}},
        {"retry_policy": {}},
    ],
)
def test_invalid_policy_never_silently_falls_back_to_current_settings(
    change: dict[str, Any],
) -> None:
    snapshot = policy.snapshot_policy(Adapter(timeout_seconds=17))
    with pytest.raises(HTTPException) as error:
        policy.validate_policy({**snapshot, **change})
    assert error.value.detail["code"] == "subscription_activation_invalid"


def test_new_activation_t1_binding_survives_restart_with_changed_runtime_parameters(
    world, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_factory.begin() as session:
        adapter = session.get(Adapter, world["adapter"])
        adapter.timeout_seconds = 17
        sub = session.get(EventSubscription, world["sub"])
        activation = management._activate(session, sub)
        activation_id = activation.id
        frozen = deepcopy(activation.execution_policy_snapshot)
        receipt = receive(session, world)
        did = delivery_id(session, receipt)
    # Same persisted database, a new Session, different deployment parameters.
    # These changes bypass only the public editing guard to model a restart.
    monkeypatch.setattr(settings, "execution_retry_max_attempts", 7)
    monkeypatch.setattr(settings, "sandbox_cpu_cores", 0.5)
    monkeypatch.setattr(settings, "sandbox_memory_bytes", 128 * 1024**2)
    monkeypatch.setattr(settings, "execution_claim_timeout_seconds", 60)
    monkeypatch.setattr(settings, "execution_recovery_grace_seconds", 30)
    monkeypatch.setattr(settings, "workspace_cleanup_attempt_timeout_seconds", 4)
    monkeypatch.setattr(settings, "workspace_cleanup_total_timeout_seconds", 15)
    with session_factory.begin() as session:
        session.get(Adapter, world["adapter"]).timeout_seconds = 99
        execution = delivery.dispatch_delivery(session, did)
        assert execution is not None
        assert execution.retry_policy_snapshot == frozen["retry_policy"]
        assert execution.resource_profile_snapshot == frozen["resource_profile"]
        assert execution.timeout_seconds_snapshot == 17
        assert (
            execution.recovery_grace_seconds_snapshot
            == frozen["resource_profile"]["recovery_grace_seconds"]
        )
        assert (
            execution.workspace_cleanup_attempt_timeout_seconds_snapshot
            == frozen["resource_profile"]["workspace_cleanup_attempt_timeout_seconds"]
        )
        assert (
            execution.workspace_cleanup_total_timeout_seconds_snapshot
            == frozen["resource_profile"]["workspace_cleanup_total_timeout_seconds"]
        )
        assert execution.claim_deadline_at - execution.created_at == timedelta(
            seconds=frozen["resource_profile"]["claim_timeout_seconds"]
        )
        assert (
            session.get(SubscriptionActivation, activation_id).execution_policy_snapshot == frozen
        )


def test_original_replay_keeps_policy_and_current_replay_explicitly_selects_new_policy(
    world, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_factory.begin() as session:
        adapter = session.get(Adapter, world["adapter"])
        adapter.timeout_seconds = 17
        management._activate(session, session.get(EventSubscription, world["sub"]))
        receipt = receive(session, world)
        receipt_id = receipt.id
        initial = delivery.dispatch_delivery(session, delivery_id(session, receipt))
        old_policy = deepcopy(initial.resource_profile_snapshot)
        old_retry_policy = deepcopy(initial.retry_policy_snapshot)
        initial.status = "succeeded"
        session.flush()
        delivery.observe_terminal(session, delivery_id(session, receipt))
        session.get(EventSubscription, world["sub"]).desired_state = "disabled"
    monkeypatch.setattr(settings, "execution_retry_max_attempts", 7)
    with session_factory.begin() as session:
        session.get(Adapter, world["adapter"]).timeout_seconds = 99
        replay = delivery.replay_event(
            session, receipt_id, "original-policy", None, "original binding"
        )
        original = delivery.dispatch_delivery(session, replay.id)
        assert original.resource_profile_snapshot == old_policy
        assert original.retry_policy_snapshot == old_retry_policy
        assert original.timeout_seconds_snapshot == 17
        original.status = "succeeded"
        session.flush()
        delivery.observe_terminal(session, replay.id)
    with session_factory.begin() as session:
        sub = session.get(EventSubscription, world["sub"])
        context, preview = delivery.current_replay_context(session, sub)
        assert (
            context["execution_policy_snapshot"]["resource_profile"]["execution_timeout_seconds"]
            == 99
        )
        current = delivery.replay_event(
            session,
            receipt_id,
            "current-policy",
            None,
            "current binding",
            use_current_configuration=True,
            expected_preview_token=preview,
        )
        execution = delivery.dispatch_delivery(session, current.id)
        assert execution.timeout_seconds_snapshot == 99
        assert execution.retry_policy_snapshot["max_attempts"] == 7
        assert current.activation_override_id != receipt.activation_id


def test_current_replay_preview_fences_runtime_policy_changes(
    world, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_factory.begin() as session:
        receipt = receive(session, world)
        receipt_id = receipt.id
        delivery.skip_event(session, receipt_id, "skip-before-preview", None, "test preview")
        sub = session.get(EventSubscription, world["sub"])
        sub.desired_state = "disabled"
        _, preview = delivery.current_replay_context(session, sub)
    monkeypatch.setattr(settings, "sandbox_memory_bytes", 128 * 1024**2)
    with session_factory.begin() as session:
        with pytest.raises(HTTPException) as error:
            delivery.replay_event(
                session,
                receipt_id,
                "stale-policy-preview",
                None,
                "current binding",
                use_current_configuration=True,
                expected_preview_token=preview,
            )
        assert error.value.detail["code"] == "subscription_config_conflict"


def test_legacy_upgrade_prefers_recorded_execution_and_freezes_no_execution_once(
    world, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_factory.begin() as session:
        receipt = receive(session, world)
        execution = delivery.dispatch_delivery(session, delivery_id(session, receipt))
        recorded = deepcopy(execution.resource_profile_snapshot)
        recorded_retry_policy = deepcopy(execution.retry_policy_snapshot)
        sub = session.get(EventSubscription, world["sub"])
        activation = session.get(SubscriptionActivation, sub.activation_id)
        activation.execution_policy_snapshot = None
        historical_id = activation.id
        newer = SubscriptionActivation(
            subscription_id=sub.id,
            config_version_id=activation.config_version_id,
            revision_id=activation.revision_id,
            worker_id=activation.worker_id,
        )
        session.add(newer)
        session.flush()
        no_execution_id = newer.id
    monkeypatch.setattr(settings, "execution_retry_max_attempts", 7)
    with session_factory.begin() as session:
        session.get(Adapter, world["adapter"]).timeout_seconds = 99
        assert policy.freeze_legacy_policies(session, limit=1) == 1
        old = session.get(SubscriptionActivation, historical_id).execution_policy_snapshot
        assert old["resource_profile"] == recorded
        assert old["retry_policy"] == recorded_retry_policy
        assert old["captured_from"] == "legacy_execution"
        assert old["execution_id"] == execution.id
        assert (
            session.get(SubscriptionActivation, no_execution_id).execution_policy_snapshot is None
        )
    with session_factory.begin() as session:
        assert policy.legacy_policy_gate(session) is False
        captured = deepcopy(
            session.get(SubscriptionActivation, no_execution_id).execution_policy_snapshot
        )
        assert captured["captured_from"] == "legacy_upgrade"
        assert captured["resource_profile"]["execution_timeout_seconds"] == 99
        assert captured["retry_policy"]["max_attempts"] == 7
    monkeypatch.setattr(settings, "execution_retry_max_attempts", 2)
    with session_factory.begin() as session:
        session.get(Adapter, world["adapter"]).timeout_seconds = 5
        assert policy.legacy_policy_gate(session) is True
        assert (
            session.get(SubscriptionActivation, no_execution_id).execution_policy_snapshot
            == captured
        )
        assert session.scalar(select(Execution.id)) == execution.id
