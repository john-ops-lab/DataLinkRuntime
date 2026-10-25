"""C14/C15 compatibility and bypass guards for the subscription foundation."""

from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.models import Adapter, Execution
from dlr.control.models.subscription import (
    ActivationCredentialReference,
    EventSubscription,
    SubscriptionActivation,
    SubscriptionConfigVersion,
)
from dlr.control.services import adapter_runtime
from dlr.control.services.attempt import _replay_availability
from dlr.control.services.reliable_execution import (
    _subscription_credential_snapshot,
    accept_execution,
)
from dlr.control.services.subscription_guards import runtime_guard
from test_adapters import create_adapter, save_version


def subscription(session: Session, adapter_id: int, **states: Any) -> EventSubscription:
    item = EventSubscription(
        adapter_id=adapter_id,
        source_type="kafka",
        source_epoch=uuid4().hex,
        desired_state="disabled",
        connection_state="stopped",
        delivery_state="idle",
    )
    for key, value in states.items():
        setattr(item, key, value)
    session.add(item)
    session.flush()
    return item


@pytest.mark.parametrize(
    "state",
    [
        {"desired_state": "enabled"},
        {"connection_state": "starting"},
        {"connection_state": "stopping"},
        {"delivery_state": "draining"},
        {"delivery_state": "blocked"},
        {"delivery_state": "repair"},
    ],
)
def test_single_and_bulk_runtime_guards_agree(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    state: dict[str, str],
) -> None:
    item = create_adapter(api_client)
    save_version(api_client, item["id"])
    with session_factory.begin() as session:
        subscription(session, item["id"], **state)
    with session_factory() as session:
        adapter = session.get(Adapter, item["id"])
        assert adapter is not None
        assert adapter_runtime.runtime_state(session, adapter).locked
        assert adapter_runtime.runtime_states(session, [adapter])[adapter.id].locked
    detail = api_client.get(f"/api/adapters/{item['id']}").json()
    assert detail["runtime_locked"] is True
    for method, path, data in [
        ("POST", "executions", {}),
        ("POST", "versions", {"code": "# blocked"}),
        ("PUT", "input-config", {"expected_revision": 1, "source_type": "json", "json_value": {}}),
        ("PATCH", "", {"timeout_seconds": 15}),
    ]:
        response = api_client.request(
            method, f"/api/adapters/{item['id']}" + (f"/{path}" if path else ""), json=data
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "subscription_draining"


def test_disabled_subscription_allows_manual_but_blocks_mode_and_stop_delete(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    item = create_adapter(api_client)
    save_version(api_client, item["id"])
    with session_factory.begin() as session:
        subscription(session, item["id"])
    with session_factory() as session:
        assert runtime_guard(session, item["id"]) is False
    response = api_client.post(f"/api/adapters/{item['id']}/executions", json={})
    assert response.status_code == 202, response.text
    response = api_client.patch(f"/api/adapters/{item['id']}", json={"run_mode": "schedule"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "subscription_binding_conflict"
    for query in ("", "?stop=true"):
        response = api_client.delete(f"/api/adapters/{item['id']}{query}")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "subscription_binding_conflict"
    with session_factory() as session:
        assert (
            session.scalar(select(Execution.status).where(Execution.adapter_id == item["id"]))
            == "queued"
        )


def test_subscription_public_snapshot_and_trigger_history(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    item = create_adapter(api_client)
    save_version(api_client, item["id"])
    response = api_client.post(f"/api/adapters/{item['id']}/executions", json={})
    assert response.status_code == 202, response.text
    execution_id = response.json()["id"]
    with session_factory.begin() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None
        execution.trigger = "subscription"
        execution.input_snapshot = {"source_type": "json", "revision": 1}
    detail = api_client.get(f"/api/executions/{execution_id}").json()
    assert detail["trigger"] == "subscription"
    assert detail["input_config_revision"] is None
    assert detail["input_snapshot"] == {"source_type": "json", "revision": None}
    history = api_client.get(f"/api/adapters/{item['id']}/executions?trigger=subscription")
    assert history.status_code == 200, history.text
    assert [row["id"] for row in history.json()["items"]] == [execution_id]
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        assert execution is not None and execution.input_config_revision == 1


def test_generic_replay_rejects_subscription_without_reading_current_bindings() -> None:
    execution = Execution(trigger="subscription", status="dead_letter", dispatch_backend="rabbitmq")
    assert _replay_availability(cast(Session, None), execution) == (
        False,
        "subscription_replay_required",
    )


def test_override_is_rejected_for_ordinary_trigger_before_any_database_work() -> None:
    with pytest.raises(HTTPException) as error:
        accept_execution(
            cast(Session, None),
            Adapter(id=1),
            trigger="manual",
            runtime_input={},
            input_source_type="json",
            input_config_revision=1,
            input_snapshot={"source_type": "json", "revision": 1},
            credential_bindings_override=[],
        )
    assert error.value.detail["code"] == "subscription_activation_invalid"


def test_frozen_activation_refs_are_closed_and_do_not_follow_current_bindings(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    item = create_adapter(api_client)
    version = save_version(api_client, item["id"])
    credential = api_client.post(
        "/api/credentials",
        json={"name": "activation-ref", "type": "token", "fields": {"token": "local-test-value"}},
    )
    assert credential.status_code == 201, credential.text
    credential_id = credential.json()["id"]
    with session_factory.begin() as session:
        adapter = session.get(Adapter, item["id"])
        assert adapter is not None
        sub = subscription(session, adapter.id)
        config = SubscriptionConfigVersion(
            subscription_id=sub.id, version=1, source_epoch=sub.source_epoch, config={}
        )
        session.add(config)
        session.flush()
        activation = SubscriptionActivation(
            subscription_id=sub.id,
            config_version_id=config.id,
            revision_id=version["id"],
            worker_id=adapter.runtime_worker_id,
        )
        session.add(activation)
        session.flush()
        snapshot = {
            "binding_id": 999,
            "credential_id": credential_id,
            "env_key": "TOKEN",
            "field": "token",
        }
        session.add(ActivationCredentialReference(activation_id=activation.id, **snapshot))
        session.flush()
        assert _subscription_credential_snapshot(
            session,
            adapter,
            activation.id,
            [snapshot],
            version_id=version["id"],
            worker_id=adapter.runtime_worker_id,
        ) == [snapshot]
        for changed in (
            [],
            [{**snapshot, "field": "password"}],
            [{**snapshot, "extra": "forbidden"}],
            [snapshot, snapshot],
        ):
            with pytest.raises(HTTPException):
                _subscription_credential_snapshot(
                    session,
                    adapter,
                    activation.id,
                    changed,
                    version_id=version["id"],
                    worker_id=adapter.runtime_worker_id,
                )
    deleted = api_client.delete(f"/api/credentials/{credential_id}")
    assert deleted.status_code == 409
    assert deleted.json()["detail"]["code"] == "credential_in_use"


def test_retention_rechecks_subscription_reference_after_candidate_selection(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    from datetime import UTC, datetime, timedelta

    from dlr.control.models.subscription import EventDelivery, EventReceipt
    from dlr.control.services.retention import RetentionPolicy, _delete_batch, _terminal_query

    item = create_adapter(api_client)
    version = save_version(api_client, item["id"])
    now = datetime.now(UTC)
    old = now - timedelta(days=40)
    with session_factory() as session:
        adapter = session.get(Adapter, item["id"])
        assert adapter is not None
        execution = Execution(
            adapter_id=adapter.id,
            version_id=version["id"],
            trigger="subscription",
            status="succeeded",
            input={},
            created_at=old,
            admission_released_at=old,
        )
        session.add(execution)
        session.flush()
        execution_id = execution.id
        policy = RetentionPolicy("subscription", 7, 100)
        assert execution_id in session.scalars(_terminal_query(policy, adapter.id)).all()
        sub = subscription(session, adapter.id)
        config = SubscriptionConfigVersion(
            subscription_id=sub.id, version=1, source_epoch=sub.source_epoch, config={}
        )
        session.add(config)
        session.flush()
        activation = SubscriptionActivation(
            subscription_id=sub.id,
            config_version_id=config.id,
            revision_id=version["id"],
            worker_id=adapter.runtime_worker_id,
        )
        session.add(activation)
        session.flush()
        receipt = EventReceipt(
            subscription_id=sub.id,
            activation_id=activation.id,
            source_epoch=sub.source_epoch,
            source_event_id="stable-event",
            content_hash="a" * 64,
            envelope={"schema": "dlr.event.v1"},
            payload_bytes=25,
            shard="partition-0",
            sequence=1,
            state="failed",
            terminal_at=None,
        )
        session.add(receipt)
        session.flush()
        # Persisted responsibilities guard writes even if a cached UI state is idle.
        assert runtime_guard(session, adapter.id) is True
        receipt.state = "succeeded"
        receipt.terminal_at = old
        session.flush()
        assert runtime_guard(session, adapter.id) is False
        session.add(
            EventDelivery(
                receipt_id=receipt.id,
                execution_id=execution_id,
                state="succeeded",
                completed_at=old,
            )
        )
        session.flush()
        # The final DELETE must recheck even a previously selected candidate.
        assert _delete_batch(session, [execution_id]) == 0
        assert execution_id not in session.scalars(_terminal_query(policy, adapter.id)).all()
        receipt.envelope = None
        receipt.payload_expired_at = now
        session.flush()
        assert _delete_batch(session, [execution_id]) == 1


def test_adapter_clone_copies_disabled_subscription_without_runtime_responsibility(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    from dlr.control.models.subscription import SourceCheckpoint

    item = create_adapter(api_client)
    save_version(api_client, item["id"])
    with session_factory.begin() as session:
        sub = subscription(
            session, item["id"], desired_state="enabled", connection_state="listening"
        )
        original_epoch = sub.source_epoch
        session.add(
            SubscriptionConfigVersion(
                subscription_id=sub.id,
                version=1,
                source_epoch=sub.source_epoch,
                config={
                    "source_type": "kafka",
                    "cluster_id": "test-cluster",
                    "bootstrap_servers": ["broker.example.invalid:9092"],
                    "topics": ["test-topic"],
                    "initial_position": "earliest",
                    "group_id": "original-private-group",
                },
            )
        )
        session.add(
            SourceCheckpoint(
                subscription_id=sub.id,
                source_epoch=sub.source_epoch,
                shard="partition-0",
                position={"offset": "23"},
            )
        )
    response = api_client.post(
        f"/api/adapters/{item['id']}/clone", json={"name": "cloned-subscription"}
    )
    assert response.status_code == 201, response.text
    clone_id = response.json()["id"]
    with session_factory() as session:
        clone = session.scalar(
            select(EventSubscription).where(EventSubscription.adapter_id == clone_id)
        )
        assert clone is not None
        assert clone.desired_state == "disabled" and clone.connection_state == "stopped"
        assert clone.delivery_state == "idle" and clone.activation_id is None
        assert clone.source_epoch != original_epoch
        config = session.scalar(
            select(SubscriptionConfigVersion).where(
                SubscriptionConfigVersion.subscription_id == clone.id
            )
        )
        assert config is not None and config.config["group_id"] != "original-private-group"
        assert (
            session.scalar(
                select(SourceCheckpoint.id).where(SourceCheckpoint.subscription_id == clone.id)
            )
            is None
        )
    executed = api_client.post(f"/api/adapters/{clone_id}/executions", json={})
    assert executed.status_code == 202, executed.text


def test_feature_off_creates_no_default_subscription_or_listener_fact(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dlr.common.config import settings
    from dlr.control.models.subscription import SubscriptionLease

    monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
    item = create_adapter(api_client)
    save_version(api_client, item["id"])
    executed = api_client.post(f"/api/adapters/{item['id']}/executions", json={})
    assert executed.status_code == 202, executed.text
    with session_factory() as session:
        assert session.scalar(select(EventSubscription.id).limit(1)) is None
        assert session.scalar(select(SubscriptionLease.subscription_id).limit(1)) is None
