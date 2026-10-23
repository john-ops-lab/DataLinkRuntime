"""B1 management gates against PostgreSQL; no source listener or external source service."""

import base64
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from dlr.common.config import settings
from dlr.control.models import Adapter, AdapterPermission, AdapterVersion, User
from dlr.control.models.subscription import (
    ActivationCredentialReference,
    DeliveryDisposition,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionLease,
    SubscriptionQuota,
)
from dlr.control.security import Principal, require_principal
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_management as management
from dlr.control.services.event_envelope import event_id
from runtime_api_support import claim_execution, report_attempt
from test_adapters import create_adapter, save_version


def config(**overrides):
    return {
        "source_type": "kafka",
        "cluster_id": "test-cluster",
        "bootstrap_servers": ["broker.example.test:9092"],
        "topics": ["events"],
        "initial_position": "earliest",
        "security_protocol": "PLAINTEXT",
        **overrides,
    }


def setup_subscription(client, monkeypatch, *, name="subscription", **overrides):
    monkeypatch.setattr(settings, "event_subscriptions_enabled", True)
    adapter = create_adapter(client, name=name)
    revision = save_version(client, adapter["id"], runtime_config={"keep": "original"})
    response = client.post(
        "/api/event-subscriptions",
        json={
            "adapter_id": adapter["id"],
            "name": name,
            "config": config(**overrides),
        },
    )
    assert response.status_code == 201, response.text
    return adapter, revision, response.json()


def action(client, sub, operation, **extra):
    return client.post(
        f"/api/event-subscriptions/{sub['id']}/{operation}",
        json={"expected_config_version": sub["config_version"], **extra},
    )


def activate(client, monkeypatch, sub):
    monkeypatch.setattr(management, "AVAILABLE_SOURCES", frozenset({"kafka"}))
    response = action(client, sub, "enable")
    assert response.status_code == 200, response.text
    return response.json()


def receive(session_factory, sub, offset):
    with session_factory.begin() as session:
        lease = session.get(SubscriptionLease, sub["id"])
        if lease is None:
            lease = delivery.acquire_lease(session, sub["id"], "test-owner")
        envelope = {
            "schema": "dlr.event.v1",
            "subscription": {
                "id": str(sub["id"]),
                "config_version": 1,
                "source_epoch": sub["source_epoch"],
            },
            "event": {"id": "", "kind": "message", "object_key": None},
            "source": {
                "type": "kafka",
                "identity": "test-source",
                "position": {"topic": "events", "partition": "0", "offset": str(offset)},
                "attributes": {
                    "cluster_id": "test-cluster",
                    "topic_incarnation": "topic-id",
                    "timestamp_ms": None,
                    "timestamp_type": None,
                    "key_base64": None,
                    "headers": [],
                },
            },
            "received_at": datetime.now(UTC).isoformat(),
            "data": {
                "encoding": "base64",
                "content_type": "application/octet-stream",
                "value": base64.b64encode(b"business-event").decode(),
            },
            "sync": None,
        }
        envelope["event"]["id"] = event_id(envelope)
        receipt, _ = delivery.receive_event(
            session,
            sub["id"],
            "test-owner",
            lease.generation,
            envelope,
            "events:0",
            {"offset": str(offset + 1)},
        )
        item = session.scalar(select(EventDelivery).where(EventDelivery.receipt_id == receipt.id))
        return receipt.id, item.id


def stop_and_ack(client, session_factory, sub):
    with session_factory() as session:
        lease = session.get(SubscriptionLease, sub["id"])
        original = (lease.owner_id, lease.generation) if lease else None
    response = action(client, sub, "disable")
    assert response.status_code == 200, response.text
    with session_factory.begin() as session:
        if original and original[0]:
            management.listener_closed(session, sub["id"], *original)


def test_default_off_and_registry_empty(api_client, monkeypatch, session_factory):
    from dlr.control.services import subscription_runtime

    monkeypatch.setattr(
        subscription_runtime,
        "_runtime",
        subscription_runtime.SubscriptionRuntimeManager(session_factory, {}),
    )
    monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
    adapter = create_adapter(api_client)
    response = api_client.post(
        "/api/event-subscriptions",
        json={
            "adapter_id": adapter["id"],
            "config": config(),
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_not_available"
    assert api_client.get("/api/event-subscriptions/capabilities").json() == {
        "enabled": False,
        "available_sources": [],
    }
    _, _, sub = setup_subscription(api_client, monkeypatch)
    assert not sub["available"] and sub["desired_state"] == "disabled"
    for operation in ("enable", "test-connection"):
        response = action(api_client, sub, operation)
        assert (
            response.status_code == 409
            and response.json()["detail"]["code"] == "source_not_available"
        )


@pytest.mark.parametrize("field", ["password", "token", "sasl_password", "kubeconfig", "tls_key"])
def test_source_schema_rejects_secret_values(api_client, monkeypatch, field):
    monkeypatch.setattr(settings, "event_subscriptions_enabled", True)
    adapter = create_adapter(api_client)
    response = api_client.post(
        "/api/event-subscriptions",
        json={
            "adapter_id": adapter["id"],
            "config": config(**{field: "private-test-value"}),
        },
    )
    assert response.status_code == 422
    assert "private-test-value" not in response.text


def test_config_versions_identity_and_metadata(api_client, monkeypatch, session_factory):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    endpoint = f"/api/event-subscriptions/{sub['id']}"
    updated_config = config(group_id=sub["config"]["group_id"], topics=["changed"])
    assert api_client.patch(endpoint, json={"config": updated_config}).status_code == 409
    assert (
        api_client.patch(
            endpoint, json={"config": updated_config, "expected_config_version": 1}
        ).status_code
        == 409
    )
    response = api_client.patch(
        endpoint, json={"config": updated_config, "expected_config_version": 1, "new_epoch": True}
    )
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed["source_epoch"] != sub["source_epoch"] and changed["config_version"] == 2
    assert (
        api_client.patch(
            endpoint, json={"expected_config_version": 1, "config": updated_config}
        ).status_code
        == 409
    )
    with session_factory() as session:
        versions = session.scalars(
            select(SubscriptionConfigVersion).order_by(SubscriptionConfigVersion.version)
        ).all()
        assert len(versions) == 2 and versions[0].config["topics"] == ["events"]
    activate(api_client, monkeypatch, changed)
    metadata = api_client.patch(endpoint, json={"name": "new name", "description": "metadata"})
    assert metadata.status_code == 200 and metadata.json()["config_version"] == 2
    assert (
        api_client.patch(
            endpoint, json={"config": updated_config, "expected_config_version": 2}
        ).status_code
        == 409
    )


def test_binding_and_group_uniqueness(api_client, monkeypatch):
    adapter, _, sub = setup_subscription(api_client, monkeypatch, group_id="exclusive-group")
    duplicate = api_client.post(
        "/api/event-subscriptions",
        json={
            "adapter_id": adapter["id"],
            "config": config(group_id="another-group"),
        },
    )
    assert duplicate.status_code == 409
    another = create_adapter(api_client, name="another")
    response = api_client.post(
        "/api/event-subscriptions",
        json={
            "adapter_id": another["id"],
            "config": config(group_id="exclusive-group"),
        },
    )
    assert response.status_code == 409, response.text
    target = create_adapter(api_client, name="clone-target")
    clone = action(api_client, sub, "clone", target_adapter_id=target["id"])
    assert clone.status_code == 201, clone.text
    value = clone.json()
    assert value["source_epoch"] != sub["source_epoch"]
    assert value["config"]["group_id"] != sub["config"]["group_id"]
    assert value["desired_state"] == "disabled" and value["activation_id"] is None


def test_acl_filter_before_count_and_paging(api_client, monkeypatch, session_factory):
    first, _, one = setup_subscription(api_client, monkeypatch, name="visible")
    _, _, two = setup_subscription(api_client, monkeypatch, name="hidden")
    with session_factory.begin() as session:
        user = User(username="viewer", password_hash="unused-test-hash", role="user")
        session.add(user)
        session.flush()
        session.add(AdapterPermission(adapter_id=first["id"], user_id=user.id, permission="read"))
        user_id = user.id
    api_client.app.dependency_overrides[require_principal] = lambda: Principal(
        kind="account", role="user", user_id=user_id, username="viewer"
    )
    response = api_client.get("/api/event-subscriptions?limit=1")
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1 and response.json()["items"][0]["id"] == one["id"]
    for suffix in ("", "/events", "/quota-audit"):
        hidden = api_client.get(f"/api/event-subscriptions/{two['id']}{suffix}")
        missing = api_client.get(f"/api/event-subscriptions/999999{suffix}")
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json()
    assert action(api_client, one, "disable").status_code == 403
    assert action(api_client, one, "test-connection").status_code == 403
    hidden_test = action(api_client, two, "test-connection")
    missing_test = api_client.post(
        "/api/event-subscriptions/999999/test-connection",
        json={"expected_config_version": 1},
    )
    assert hidden_test.status_code == missing_test.status_code == 404
    assert hidden_test.json() == missing_test.json()
    assert (
        action(
            api_client, one, "quota-repair", request_id="viewer", reason="not permitted"
        ).status_code
        == 403
    )


def test_activation_freezes_refs_and_disable_fences(api_client, monkeypatch, session_factory):
    adapter, revision, sub = setup_subscription(api_client, monkeypatch)
    credential = api_client.post(
        "/api/credentials",
        json={
            "name": "source-token",
            "type": "token",
            "fields": {"token": "not-returned"},
        },
    )
    assert credential.status_code == 201, credential.text
    cid = credential.json()["id"]
    bound = api_client.put(
        f"/api/adapters/{adapter['id']}/credential-bindings",
        json={
            "bindings": [{"env_key": "TARGET", "credential_id": cid, "field": "token"}],
        },
    )
    assert bound.status_code == 200, bound.text
    sub = activate(api_client, monkeypatch, sub)
    with session_factory.begin() as session:
        activation = session.get(SubscriptionActivation, sub["activation_id"])
        assert activation.revision_id == revision["id"]
        reference = session.scalar(select(ActivationCredentialReference))
        assert reference.credential_id == cid
        lease = delivery.acquire_lease(session, sub["id"], "active-owner")
        old_generation = lease.generation
    stopped = action(api_client, sub, "disable")
    assert stopped.status_code == 200 and stopped.json()["connection_state"] == "stopping"
    with session_factory.begin() as session:
        lease = session.get(SubscriptionLease, sub["id"])
        assert lease.generation == old_generation + 1
        with pytest.raises(HTTPException, match="409"):
            delivery.renew_lease(session, sub["id"], "active-owner", old_generation)
        management.listener_closed(session, sub["id"], "active-owner", old_generation)
    assert (
        api_client.get(f"/api/event-subscriptions/{sub['id']}").json()["connection_state"]
        == "stopped"
    )


def test_stop_ack_token_is_stable_and_rejects_late_owner(api_client, monkeypatch, session_factory):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    sub = activate(api_client, monkeypatch, sub)
    with session_factory.begin() as session:
        first = delivery.acquire_lease(session, sub["id"], "owner-instance-a")
        first_generation = first.generation
    assert action(api_client, sub, "disable").status_code == 200
    assert action(api_client, sub, "disable").status_code == 200
    with session_factory.begin() as session:
        assert session.get(SubscriptionLease, sub["id"]).generation == first_generation + 1
        management.listener_closed(session, sub["id"], "owner-instance-a", first_generation)
    assert action(api_client, sub, "enable").status_code == 200
    with session_factory.begin() as session:
        second = delivery.acquire_lease(session, sub["id"], "owner-instance-b")
        second_generation = second.generation
    assert action(api_client, sub, "disable").status_code == 200
    with session_factory.begin() as session:
        with pytest.raises(HTTPException, match="409"):
            management.listener_closed(session, sub["id"], "owner-instance-a", first_generation)
        management.listener_closed(session, sub["id"], "owner-instance-b", second_generation)


def test_reset_idempotence_retains_versions(api_client, monkeypatch, session_factory):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    payload = {"request_id": "reset-request", "reason": "explicit history reset"}
    reset = action(api_client, sub, "reset", **payload)
    assert reset.status_code == 200, reset.text
    repeat = action(api_client, sub, "reset", **payload)
    assert (
        repeat.status_code == 200 and repeat.json()["source_epoch"] == reset.json()["source_epoch"]
    )
    assert action(api_client, sub, "reset", **{**payload, "reason": "different"}).status_code == 409
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SubscriptionConfigVersion)) == 2
        assert session.scalar(select(func.count()).select_from(DeliveryDisposition)) == 1


def test_repeated_fences_keep_native_closing_token(api_client, monkeypatch, session_factory):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    sub = activate(api_client, monkeypatch, sub)
    with session_factory.begin() as session:
        lease = delivery.acquire_lease(session, sub["id"], "native-owner")
        native_generation = lease.generation
        session.get(EventSubscription, sub["id"]).connection_state = "blocked"
    assert action(api_client, sub, "resume").status_code == 200
    assert action(api_client, sub, "disable").status_code == 200
    with session_factory.begin() as session:
        lease = session.get(SubscriptionLease, sub["id"])
        assert lease.generation == native_generation + 2
        assert lease.closing_generation == native_generation
        management.listener_closed(session, sub["id"], "native-owner", native_generation)
        assert session.get(EventSubscription, sub["id"]).connection_state == "stopped"
        assert lease.closing_generation is None


def test_same_owner_reacquisition_rejects_old_ack(api_client, monkeypatch, session_factory):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    sub = activate(api_client, monkeypatch, sub)
    with session_factory.begin() as session:
        lease = delivery.acquire_lease(session, sub["id"], "reused-owner")
        old_generation = lease.generation
        lease.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    with session_factory.begin() as session:
        lease = delivery.acquire_lease(session, sub["id"], "reused-owner")
        new_generation = lease.generation
        assert lease.closing_generation is None
    assert action(api_client, sub, "disable").status_code == 200
    with session_factory.begin() as session:
        with pytest.raises(HTTPException, match="409"):
            management.listener_closed(session, sub["id"], "reused-owner", old_generation)
        management.listener_closed(session, sub["id"], "reused-owner", new_generation)


def test_resuming_native_close_keeps_starting_and_cannot_clear_new_owner(
    api_client, monkeypatch, session_factory
):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    sub = activate(api_client, monkeypatch, sub)
    with session_factory.begin() as session:
        lease = delivery.acquire_lease(session, sub["id"], "old-native")
        old_generation = lease.generation
        session.get(EventSubscription, sub["id"]).connection_state = "blocked"
    assert action(api_client, sub, "resume").status_code == 200
    with session_factory.begin() as session:
        management.listener_closed(session, sub["id"], "old-native", old_generation)
        current = session.get(EventSubscription, sub["id"])
        assert current.desired_state == "enabled" and current.connection_state == "starting"
        delivery.acquire_lease(session, sub["id"], "new-native")
    with session_factory.begin() as session:
        with pytest.raises(HTTPException, match="409"):
            management.listener_closed(session, sub["id"], "old-native", old_generation)
        assert session.get(SubscriptionLease, sub["id"]).owner_id == "new-native"


def test_enabled_budget_rejection_rolls_back_activation(api_client, monkeypatch, session_factory):
    monkeypatch.setattr(management, "MAX_ENABLED_SUBSCRIPTIONS", 1)
    _, _, one = setup_subscription(api_client, monkeypatch, name="first")
    _, _, two = setup_subscription(api_client, monkeypatch, name="second")
    activate(api_client, monkeypatch, one)
    rejected = action(api_client, two, "enable")
    assert rejected.status_code == 429, rejected.text
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SubscriptionActivation)) == 1
        assert session.get(EventSubscription, two["id"]).activation_id is None
        assert session.get(EventSubscription, two["id"]).desired_state == "disabled"


def test_repair_with_backlog_preserves_old_activation(api_client, monkeypatch, session_factory):
    adapter, revision, sub = setup_subscription(api_client, monkeypatch)
    sub = activate(api_client, monkeypatch, sub)
    rid, did = receive(session_factory, sub, 0)
    next_rid, next_did = receive(session_factory, sub, 1)
    with session_factory.begin() as session:
        execution = delivery.dispatch_delivery(session, did)
        eid = execution.id
        worker_id = execution.target_worker_id
    assert claim_execution(api_client, worker_id, execution_id=eid).status_code == 200
    assert (
        report_attempt(
            api_client, worker_id, eid, {"status": "failed", "error": "business bug"}
        ).status_code
        == 200
    )
    with session_factory.begin() as session:
        delivery.observe_terminal(session, did)
    stop_and_ack(api_client, session_factory, sub)
    assert action(api_client, sub, "repair/enter").status_code == 200
    repaired = action(
        api_client, sub, "repair/revisions", code="def handle(context, input):\n    return input\n"
    )
    assert repaired.status_code == 201, repaired.text
    repair_id = repaired.json()["revision_id"]
    replay = action(
        api_client,
        sub,
        f"events/{rid}/replay",
        request_id="repair-0",
        reason="fix blocking bug",
        revision_id=repair_id,
    )
    assert replay.status_code == 201, replay.text
    with session_factory.begin() as session:
        repaired_execution = delivery.dispatch_delivery(session, replay.json()["delivery_id"])
        assert repaired_execution is not None and repaired_execution.version_id == repair_id
        assert delivery.dispatch_delivery(session, next_did) is None
        assert (
            session.get(SubscriptionActivation, sub["activation_id"]).revision_id == revision["id"]
        )
        assert session.get(EventReceipt, next_rid).activation_id == sub["activation_id"]
        assert session.get(Adapter, adapter["id"]).latest_version_id == revision["id"]
        assert session.get(AdapterVersion, repair_id).runtime_config == {"keep": "original"}
        repaired_eid = repaired_execution.id
    assert action(api_client, sub, "repair/exit").status_code == 409
    assert claim_execution(api_client, worker_id, execution_id=repaired_eid).status_code == 200
    assert (
        report_attempt(api_client, worker_id, repaired_eid, {"status": "succeeded"}).status_code
        == 200
    )
    with session_factory.begin() as session:
        delivery.observe_terminal(session, replay.json()["delivery_id"])
    assert action(api_client, sub, "repair/exit").status_code == 200
    with session_factory.begin() as session:
        following = delivery.dispatch_delivery(session, next_did)
        assert following is not None and following.version_id == revision["id"]


def test_management_state_follows_responsibility(api_client, monkeypatch, session_factory):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    sub = activate(api_client, monkeypatch, sub)
    rid, did = receive(session_factory, sub, 0)
    endpoint = f"/api/event-subscriptions/{sub['id']}"

    def state():
        detail = api_client.get(endpoint).json()
        listed = api_client.get("/api/event-subscriptions").json()["items"][0]
        assert detail["delivery_state"] == listed["delivery_state"]
        return detail["delivery_state"]

    assert state() == "pending"
    with session_factory.begin() as session:
        original = delivery.dispatch_delivery(session, did)
        eid, worker_id = original.id, original.target_worker_id
    assert state() == "processing"
    assert claim_execution(api_client, worker_id, execution_id=eid).status_code == 200
    assert (
        report_attempt(api_client, worker_id, eid, {"status": "failed", "error": "bug"}).status_code
        == 200
    )
    with session_factory.begin() as session:
        delivery.observe_terminal(session, did)
    assert state() == "blocked"
    replay = action(
        api_client,
        sub,
        f"events/{rid}/replay",
        request_id="retry-original",
        reason="temporary target fault recovered",
    )
    assert replay.status_code == 201, replay.text
    assert state() == "pending"
    with session_factory.begin() as session:
        repaired = delivery.dispatch_delivery(session, replay.json()["delivery_id"])
        repaired_id = repaired.id
    assert state() == "processing"
    stop_and_ack(api_client, session_factory, sub)
    assert state() == "draining"
    assert claim_execution(api_client, worker_id, execution_id=repaired_id).status_code == 200
    assert (
        report_attempt(api_client, worker_id, repaired_id, {"status": "succeeded"}).status_code
        == 200
    )
    with session_factory.begin() as session:
        delivery.observe_terminal(session, replay.json()["delivery_id"])
    assert state() == "idle"


def test_delete_only_drained_and_preserves_config_audit(api_client, monkeypatch, session_factory):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    sub = activate(api_client, monkeypatch, sub)
    rid, _ = receive(session_factory, sub, 0)
    stop_and_ack(api_client, session_factory, sub)
    endpoint = f"/api/event-subscriptions/{sub['id']}"
    body = {"expected_config_version": 1}
    assert api_client.request("DELETE", endpoint, json=body).status_code == 409
    skipped = action(
        api_client,
        sub,
        f"events/{rid}/skip",
        request_id="skip-before-delete",
        reason="explicitly abandon this accepted event",
    )
    assert skipped.status_code == 200, skipped.text
    assert api_client.request("DELETE", endpoint, json=body).status_code == 204
    assert api_client.get(endpoint).status_code == 404
    assert api_client.get("/api/event-subscriptions").json()["total"] == 0
    with session_factory() as session:
        assert session.get(EventSubscription, sub["id"]).deleted_at is not None
        assert session.scalar(select(func.count()).select_from(SubscriptionConfigVersion)) == 1
        assert session.get(EventReceipt, rid).state == "skipped"


def test_source_credential_fk_and_no_plaintext(api_client, monkeypatch, session_factory):
    credential = api_client.post(
        "/api/credentials",
        json={
            "name": "listener-source",
            "type": "token",
            "fields": {"token": "source-secret-value"},
        },
    ).json()
    _, _, sub = setup_subscription(api_client, monkeypatch, credential_id=credential["id"])
    assert "source-secret-value" not in str(sub)
    assert sub["config"]["credential_id"] == credential["id"]
    response = api_client.delete(f"/api/credentials/{credential['id']}")
    assert response.status_code == 409
    with session_factory() as session:
        assert (
            session.scalar(select(SubscriptionConfigVersion.source_credential_id))
            == credential["id"]
        )


def test_repair_waits_for_existing_execution(api_client, monkeypatch, session_factory):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    sub = activate(api_client, monkeypatch, sub)
    _, did = receive(session_factory, sub, 0)
    with session_factory.begin() as session:
        assert delivery.dispatch_delivery(session, did) is not None
    stop_and_ack(api_client, session_factory, sub)
    assert action(api_client, sub, "repair/enter").status_code == 200
    assert (
        action(
            api_client, sub, "repair/revisions", code="def handle(context, input): return input"
        ).status_code
        == 409
    )
    assert action(api_client, sub, "repair/exit").status_code == 409


def test_quota_readonly_audit_and_stopped_repair_with_pending(
    api_client, monkeypatch, session_factory
):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    sub = activate(api_client, monkeypatch, sub)
    rid, did = receive(session_factory, sub, 0)
    endpoint = f"/api/event-subscriptions/{sub['id']}"
    with session_factory.begin() as session:
        # Independent local corruption must not damage the still-correct global counter.
        session.get(SubscriptionQuota, f"sub:{sub['id']}").pending_count = 777
    audit = api_client.get(endpoint + "/quota-audit")
    assert audit.status_code == 200, audit.text
    assert audit.json()["differences"]["pending_count"] == {"actual": 777, "expected": 1}
    assert not audit.json()["repaired"]
    assert "global" not in audit.json()
    payload = {"request_id": "repair-quota", "reason": "repair verified counter drift"}
    assert action(api_client, sub, "quota-repair", **payload).status_code == 409
    with session_factory() as session:
        assert session.get(SubscriptionQuota, f"sub:{sub['id']}").pending_count == 777
    stop_and_ack(api_client, session_factory, sub)
    repaired = action(api_client, sub, "quota-repair", **payload)
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["repaired"]
    repeated = action(api_client, sub, "quota-repair", **payload)
    assert repeated.status_code == 200 and repeated.json() == repaired.json()
    with session_factory() as session:
        assert session.get(SubscriptionQuota, f"sub:{sub['id']}").pending_count == 1
        assert session.get(SubscriptionQuota, "global").pending_count == 1
        assert session.get(EventReceipt, rid).state == "pending"
        assert session.get(EventDelivery, did).state == "pending"
        assert session.scalar(select(func.count()).select_from(DeliveryDisposition)) == 1
    assert api_client.get(endpoint + "/quota-audit").json()["differences"] == {}


def test_readonly_quota_audit_creates_no_ledger(api_client, monkeypatch, session_factory):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    assert api_client.get(f"/api/event-subscriptions/{sub['id']}/quota-audit").status_code == 200
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SubscriptionQuota)) == 0


def test_deleted_subscription_keeps_bounded_retention_gc(api_client, monkeypatch, session_factory):
    source_credential = api_client.post(
        "/api/credentials",
        json={"name": "retained-source", "type": "token", "fields": {"token": "source-fixture"}},
    ).json()
    adapter, _, sub = setup_subscription(
        api_client, monkeypatch, credential_id=source_credential["id"]
    )
    sub = activate(api_client, monkeypatch, sub)
    rid, _ = receive(session_factory, sub, 0)
    stop_and_ack(api_client, session_factory, sub)
    assert (
        action(
            api_client,
            sub,
            f"events/{rid}/skip",
            request_id="terminal",
            reason="explicitly settle before deletion",
        ).status_code
        == 200
    )
    endpoint = f"/api/event-subscriptions/{sub['id']}"
    assert (
        api_client.request("DELETE", endpoint, json={"expected_config_version": 1}).status_code
        == 204
    )
    baseline = datetime.now(UTC)
    monkeypatch.setattr(delivery, "database_now", lambda session: baseline + timedelta(days=8))
    monkeypatch.setattr(management, "_subscription_cursor", 0)
    management.maintenance_tick()
    with session_factory() as session:
        assert session.get(Adapter, adapter["id"]) is not None
        assert session.get(EventReceipt, rid).envelope is None
        assert session.get(SubscriptionQuota, "global").history_count == 0
        assert session.get(SubscriptionQuota, "global").dedupe_count == 1
    assert api_client.delete(f"/api/credentials/{source_credential['id']}").status_code == 409
    monkeypatch.setattr(delivery, "database_now", lambda session: baseline + timedelta(days=31))
    monkeypatch.setattr(management, "_subscription_cursor", 0)
    management.maintenance_tick()
    with session_factory() as session:
        assert session.get(Adapter, adapter["id"]) is not None
        assert session.get(EventReceipt, rid) is None
        assert session.get(SubscriptionQuota, "global").history_count == 0
        assert session.get(SubscriptionQuota, "global").dedupe_count == 0
        assert session.scalar(select(func.count()).select_from(SubscriptionActivation)) == 0
        assert session.scalar(select(func.count()).select_from(SubscriptionConfigVersion)) == 0
    assert api_client.delete(f"/api/credentials/{source_credential['id']}").status_code == 204


def test_disabled_live_subscription_keeps_current_activation(
    api_client, monkeypatch, session_factory
):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    sub = activate(api_client, monkeypatch, sub)
    stop_and_ack(api_client, session_factory, sub)
    future = datetime.now(UTC) + timedelta(days=40)
    monkeypatch.setattr(delivery, "database_now", lambda session: future)
    monkeypatch.setattr(management, "_subscription_cursor", 0)
    management.maintenance_tick()
    with session_factory() as session:
        assert session.get(SubscriptionActivation, sub["activation_id"]) is not None
        assert session.scalar(select(func.count()).select_from(SubscriptionConfigVersion)) == 1
