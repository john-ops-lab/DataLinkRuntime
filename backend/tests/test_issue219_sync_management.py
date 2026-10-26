"""B4 management/API wiring on isolated PostgreSQL; no Kubernetes RPCs."""

import copy
import threading
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from dlr.common.config import Settings, settings
from dlr.control.models import AdapterPermission, Execution, User
from dlr.control.models.subscription import (
    DeliveryDisposition,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    SourceCheckpoint,
    SubscriptionConfigVersion,
    SubscriptionLease,
    SubscriptionQuota,
    SyncBatch,
)
from dlr.control.schemas.subscription import CompletionAbandonment, KubernetesSourceConfig
from dlr.control.security import Principal, require_principal
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_management as management
from dlr.control.services import subscription_runtime as runtime
from dlr.control.services import subscription_sync as sync
from dlr.control.services.subscription_registry import SOURCES
from dlr.control.services.subscription_source import ApplicationBufferBudget, SourceRunContext
from runtime_api_support import claim_execution, report_attempt
from test_adapters import create_adapter, save_version
from test_issue219_management import action, stop_and_ack
from test_issue219_runtime_management import RecordingSource


def kube_config(**changes):
    return {
        "source_type": "kubernetes",
        "cluster_id": "cluster-logical",
        "server": "https://cluster.example.test",
        "group": "",
        "version": "v1",
        "resource": "pods",
        "namespace": "default",
        **changes,
    }


def kube_subscription(client, monkeypatch, name="kubernetes-management"):
    monkeypatch.setattr(settings, "event_subscriptions_enabled", True)
    adapter = create_adapter(client, name=name)
    revision = save_version(client, adapter["id"])
    response = client.post(
        "/api/event-subscriptions",
        json={"adapter_id": adapter["id"], "config": kube_config()},
    )
    assert response.status_code == 201, response.text
    return adapter, revision, response.json()


def activate_kube(client, monkeypatch, sub):
    activated = action(client, sub, "enable")
    assert activated.status_code == 200, activated.text
    return activated.json()


def empty_batch(session_factory, sub, **changes):
    with session_factory.begin() as session:
        config = management.current_config(session, session.get(EventSubscription, sub["id"]))
        batch = SyncBatch(
            subscription_id=sub["id"],
            activation_id=sub["activation_id"],
            source_epoch=sub["source_epoch"],
            scope_fingerprint=config.source_identity["scope_fingerprint"],
            **changes,
        )
        session.add(batch)
        session.flush()
        return batch.id


@pytest.fixture(autouse=True)
def isolated_scan_cursors(monkeypatch):
    for name in (
        "_batch_cursor",
        "_batch_upper",
        "_delivery_cursor",
        "_delivery_upper",
        "_subscription_cursor",
        "_subscription_upper",
    ):
        monkeypatch.setattr(management, name, 0)


@pytest.mark.parametrize(
    "server",
    [
        "https://cluster.example.test/api",
        "https://cluster.example.test//",
        "https://user@cluster.example.test",
        "https://cluster.example.test?token=example",
        "https://cluster.example.test/#fragment",
    ],
)
def test_kubernetes_server_is_origin_not_arbitrary_api(server):
    with pytest.raises(ValidationError):
        KubernetesSourceConfig(**kube_config(server=server))
    assert KubernetesSourceConfig(**kube_config(server="https://cluster.example.test/")).server == (
        "https://cluster.example.test"
    )


def test_kubernetes_matrix_is_closed_before_transport():
    with pytest.raises(ValidationError):
        KubernetesSourceConfig(**kube_config(resource="secrets"))
    assert (
        KubernetesSourceConfig(**kube_config(group="apps", resource="deployments")).resource
        == "deployments"
    )


def test_completion_body_binds_explicit_null_and_bounded_impact():
    body = {
        "expected_config_version": 1,
        "source_epoch": "epoch",
        "receipt_id": 1,
        "expected_latest_delivery_id": 2,
        "expected_delivery_ordinal": 2,
        "request_id": "abandon-request",
        "reason": "Result remains unconfirmed",
        "confirm_impact": True,
    }
    with pytest.raises(ValidationError):
        CompletionAbandonment(**body)
    assert CompletionAbandonment(**body, expected_execution_id=None).expected_execution_id is None
    for change in (
        {"confirm_impact": False},
        {"confirm_impact": 1},
        {"expected_execution_id": True},
        {"reason": "界" * 1366},
    ):
        with pytest.raises(ValidationError):
            CompletionAbandonment(**{**body, "expected_execution_id": None, **change})


def test_identity_snapshot_is_immutable_and_metadata_does_not_refresh_trust(
    api_client, monkeypatch, session_factory
):
    from dlr.control.services import subscription_kubernetes_identity as identity

    _, _, sub = kube_subscription(api_client, monkeypatch)
    with session_factory() as session:
        original = management.current_config(session, session.get(EventSubscription, sub["id"]))
        config_id = original.id
        snapshot = copy.deepcopy(original.source_identity)
        assert snapshot is not None and snapshot["version"] == 1
    changed = {**snapshot, "trust_sha256": "a" * 64, "scope_fingerprint": "b" * 64}
    monkeypatch.setattr(identity, "kubernetes_scope_identity", lambda value: dict(changed))
    response = api_client.patch(
        f"/api/event-subscriptions/{sub['id']}", json={"name": "renamed without revalidation"}
    )
    assert response.status_code == 200
    monkeypatch.setattr(management, "AVAILABLE_SOURCES", frozenset({"kafka", "kubernetes"}))
    assert action(api_client, sub, "enable").json()["detail"]["code"] == "source_identity_changed"
    payload = {"expected_config_version": 1, "config": kube_config()}
    rejected = api_client.patch(f"/api/event-subscriptions/{sub['id']}", json=payload)
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "source_epoch_required"
    updated = api_client.patch(
        f"/api/event-subscriptions/{sub['id']}", json={**payload, "new_epoch": True}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["source_epoch"] != sub["source_epoch"]
    with session_factory() as session:
        assert session.get(SubscriptionConfigVersion, config_id).source_identity == snapshot
        current = management.current_config(session, session.get(EventSubscription, sub["id"]))
        assert current.source_identity == changed and current.version == 2


def test_unverified_legacy_kubernetes_identity_requires_explicit_epoch(
    api_client, monkeypatch, session_factory
):
    _, _, sub = kube_subscription(api_client, monkeypatch)
    with session_factory.begin() as session:
        config = session.scalar(
            select(SubscriptionConfigVersion).where(
                SubscriptionConfigVersion.subscription_id == sub["id"]
            )
        )
        config.source_identity = None
    monkeypatch.setattr(management, "AVAILABLE_SOURCES", frozenset({"kafka", "kubernetes"}))
    rejected = action(api_client, sub, "enable")
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "source_epoch_required"
    body = {"expected_config_version": 1, "config": kube_config()}
    assert api_client.patch(f"/api/event-subscriptions/{sub['id']}", json=body).status_code == 409
    updated = api_client.patch(
        f"/api/event-subscriptions/{sub['id']}", json={**body, "new_epoch": True}
    )
    assert updated.status_code == 200, updated.text
    with session_factory() as session:
        config = management.current_config(session, session.get(EventSubscription, sub["id"]))
        assert config.source_identity is not None
        assert config.source_epoch != sub["source_epoch"]


def test_empty_failed_batch_locks_until_closed_but_next_gap_does_not(
    api_client, monkeypatch, session_factory
):
    adapter, _, sub = kube_subscription(api_client, monkeypatch)
    sub = activate_kube(api_client, monkeypatch, sub)
    assert action(api_client, sub, "disable").status_code == 200
    bid = empty_batch(session_factory, sub, state="failed")
    detail = api_client.get(f"/api/event-subscriptions/{sub['id']}").json()
    page = api_client.get("/api/event-subscriptions").json()
    assert detail["delivery_state"] == page["items"][0]["delivery_state"] == "draining"
    assert detail["latest_batch"]["id"] == bid
    assert not detail["latest_batch"]["closed"]
    for response in (
        api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}),
        action(api_client, sub, "reset", request_id="still-open", reason="must not lose batch"),
        api_client.request(
            "DELETE",
            f"/api/event-subscriptions/{sub['id']}",
            json={"expected_config_version": 1},
        ),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "subscription_draining"
    with session_factory.begin() as session:
        batch = session.get(SyncBatch, bid)
        batch.finished_at = datetime.now(UTC)
        batch.pending_gap = {
            "code": "source_history_gap",
            "detected_at": datetime.now(UTC).isoformat(),
            "after_completion_committed": False,
            "relist_required": True,
        }
    drained = api_client.get(f"/api/event-subscriptions/{sub['id']}").json()
    assert drained["delivery_state"] == "idle"
    assert drained["latest_batch"]["closed"]
    assert api_client.post(f"/api/adapters/{adapter['id']}/executions", json={}).status_code == 202


def test_closed_old_epoch_batch_does_not_block_new_native_owner(
    api_client, monkeypatch, session_factory
):
    _, _, sub = kube_subscription(api_client, monkeypatch)
    sub = activate_kube(api_client, monkeypatch, sub)
    assert action(api_client, sub, "disable").status_code == 200
    bid = empty_batch(session_factory, sub, state="failed", finished_at=datetime.now(UTC))
    reset = action(api_client, sub, "reset", request_id="next-epoch", reason="Start a new epoch")
    assert reset.status_code == 200, reset.text
    current = activate_kube(api_client, monkeypatch, reset.json())
    source = RecordingSource()
    manager = runtime.SubscriptionRuntimeManager(session_factory, {"kubernetes": source})
    try:
        assert manager.start_once(current["id"])
        assert source.started.wait(3)
        assert source.contexts[0].source_epoch != sub["source_epoch"]
        with session_factory() as session:
            assert session.get(SyncBatch, bid).source_epoch == sub["source_epoch"]
    finally:
        assert action(api_client, current, "disable").status_code == 200
        manager.stop_single(current["id"])
        source.join()
    assert manager.active_count == 0


def test_batch_reads_are_bounded_authorized_and_hide_raw_diagnostics(
    api_client, monkeypatch, session_factory
):
    adapter, _, sub = kube_subscription(api_client, monkeypatch)
    _, _, hidden = kube_subscription(api_client, monkeypatch, name="hidden-kubernetes")
    sub = activate_kube(api_client, monkeypatch, sub)
    assert action(api_client, sub, "disable").status_code == 200
    with session_factory.begin() as session:
        for _ in range(2):
            session.add(
                SyncBatch(
                    subscription_id=sub["id"],
                    source_epoch="old-epoch",
                    scope_fingerprint="legacy-scope",
                    state="failed",
                    finished_at=datetime.now(UTC),
                )
            )
        foreign_batch = SyncBatch(
            subscription_id=hidden["id"],
            source_epoch="hidden-epoch",
            scope_fingerprint="hidden-scope",
            state="failed",
            finished_at=datetime.now(UTC),
        )
        session.add(foreign_batch)
        user = User(username="batch-reader", password_hash="unused-test-hash", role="user")
        session.add(user)
        session.flush()
        foreign_id = foreign_batch.id
        user_id = user.id
        session.add(AdapterPermission(adapter_id=adapter["id"], user_id=user.id, permission="read"))
    bid = empty_batch(
        session_factory,
        sub,
        state="processing",
        resource_version="opaque:rv/" + "x" * 4086,
        pending_gap={
            "code": "source_history_gap",
            "detected_at": datetime.now(UTC).isoformat(),
            "after_completion_committed": False,
            "relist_required": True,
            "raw_body": "never-return-body",
            "continue_token": "never-return-token",
        },
    )
    api_client.app.dependency_overrides[require_principal] = lambda: Principal(
        kind="account", role="user", user_id=user_id, username="batch-reader"
    )
    monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
    page = api_client.get(f"/api/event-subscriptions/{sub['id']}/sync-batches?limit=1").json()
    assert page["total"] == 3 and len(page["items"]) == 1
    assert page["items"][0]["id"] == bid and page["next_before_id"] == bid
    old = api_client.get(
        f"/api/event-subscriptions/{sub['id']}/sync-batches?source_epoch=old-epoch"
    ).json()
    assert old["total"] == 2 and len(old["items"]) == 2
    assert all(
        item["activation_id"] is None and item["config_version"] is None for item in old["items"]
    )
    detail = api_client.get(f"/api/event-subscriptions/{sub['id']}/sync-batches/{bid}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["resource_version"] == "opaque:rv/" + "x" * 4086
    assert "never-return" not in detail.text and "continue_token" not in detail.text
    foreign_response = api_client.get(
        f"/api/event-subscriptions/{sub['id']}/sync-batches/{foreign_id}"
    )
    missing_batch = api_client.get(f"/api/event-subscriptions/{sub['id']}/sync-batches/999999")
    assert foreign_response.status_code == missing_batch.status_code == 404
    assert foreign_response.json() == missing_batch.json()
    for suffix in ("/sync-batches", f"/sync-batches/{bid}"):
        hidden_response = api_client.get(f"/api/event-subscriptions/{hidden['id']}{suffix}")
        missing_response = api_client.get(f"/api/event-subscriptions/999999{suffix}")
        assert hidden_response.status_code == missing_response.status_code == 404
        assert hidden_response.json() == missing_response.json()
    assert (
        api_client.get(f"/api/event-subscriptions/{sub['id']}/sync-batches?limit=101").status_code
        == 422
    )
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SourceCheckpoint)) == 0
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        assert session.scalar(select(func.count()).select_from(SubscriptionLease)) == 0


def test_native_hold_precedes_credentials_lease_and_thread_creation(
    api_client, monkeypatch, session_factory
):
    _, _, sub = kube_subscription(api_client, monkeypatch)
    sub = activate_kube(api_client, monkeypatch, sub)
    manager = runtime.SubscriptionRuntimeManager(session_factory, {"kubernetes": RecordingSource()})
    monkeypatch.setattr(
        sync, "native_start_decision", lambda *args: sync.NativeStartDecision("hold")
    )
    calls = []
    with monkeypatch.context() as no_native:
        no_native.setattr(runtime, "credential_values", lambda *args: calls.append("decrypt"))
        no_native.setattr(threading, "Thread", lambda *args, **kwargs: calls.append("thread"))
        assert not manager.start_once(sub["id"])
    assert not calls and manager.active_count == 0
    with session_factory() as session:
        assert session.get(SubscriptionLease, sub["id"]) is None


def test_runtime_uses_deepcopy_of_original_config_identity(
    api_client, monkeypatch, session_factory
):
    _, _, sub = kube_subscription(api_client, monkeypatch)
    sub = activate_kube(api_client, monkeypatch, sub)
    source = RecordingSource()
    manager = runtime.SubscriptionRuntimeManager(session_factory, {"kubernetes": source})
    assert manager.start_once(sub["id"])
    assert source.started.wait(3)
    try:
        context = source.contexts[0]
        with session_factory() as session:
            original = management.current_config(session, session.get(EventSubscription, sub["id"]))
            frozen = copy.deepcopy(original.source_identity)
        assert context.identity_snapshot == frozen
        context.identity_snapshot["trust_sha256"] = "c" * 64
        with session_factory() as session:
            original = management.current_config(session, session.get(EventSubscription, sub["id"]))
            assert original.source_identity == frozen
    finally:
        assert action(api_client, sub, "disable").status_code == 200
        manager.stop_single(sub["id"])
        source.join()
    assert manager.active_count == 0


def test_internal_diagnostic_does_not_open_public_enablement_or_custody(
    api_client, monkeypatch, session_factory
):
    # A bounded diagnostic must still respect a deliberately unavailable public source.
    monkeypatch.setattr(management, "AVAILABLE_SOURCES", frozenset({"kafka"}))
    _, _, sub = kube_subscription(api_client, monkeypatch)
    assert "kubernetes" in runtime.implemented_sources()
    source = RecordingSource()
    source.test_connection = lambda config, credentials: {
        "cluster_id": config["cluster_id"],
        "identity_verified": True,
        "raw_sdk": object(),
        "kubernetes": {
            "scope_fingerprint": "a" * 64,
            "trust_verified": True,
            "list_verified": True,
            "watch_verified": True,
            "group": "",
            "version": "v1",
            "resource": "pods",
            "namespace": "default",
            "raw_body": "never-return-body",
            "token": "never-return-token",
        },
    }
    manager = runtime.SubscriptionRuntimeManager(session_factory, {"kubernetes": source})
    monkeypatch.setattr(runtime, "_runtime", manager)
    checked = action(api_client, sub, "test-connection")
    assert checked.status_code == 200, checked.text
    assert checked.json()["kubernetes"]["watch_verified"]
    assert "never-return" not in checked.text and "raw_sdk" not in checked.text
    assert api_client.get("/api/event-subscriptions/capabilities").json()["available_sources"] == [
        "kafka"
    ]
    assert action(api_client, sub, "enable").json()["detail"]["code"] == "source_not_available"
    assert manager.active_count == 0
    with session_factory() as session:
        for model in (SyncBatch, SourceCheckpoint, SubscriptionQuota, SubscriptionLease, Execution):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_public_candidate_sources_match_implementation_and_remain_default_off(
    api_client, monkeypatch, session_factory
):
    assert Settings.model_fields["event_subscriptions_enabled"].default is False
    assert Settings.model_fields["http_ingress_enabled"].default is False
    monkeypatch.setattr(settings, "http_ingress_enabled", False)
    assert (
        management.AVAILABLE_SOURCES
        == frozenset(runtime.implemented_sources())
        == {
            "kafka",
            "kubernetes",
            "rabbitmq",
            "rocketmq",
            "http",
        }
    )
    monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
    assert api_client.get("/api/event-subscriptions/capabilities").json() == {
        "enabled": False,
        "available_sources": [],
        "sources": [],
    }
    _, _, sub = kube_subscription(api_client, monkeypatch)
    sources = ["kafka", "kubernetes", "rabbitmq", "rocketmq"]
    assert api_client.get("/api/event-subscriptions/capabilities").json() == {
        "enabled": True,
        "available_sources": sources,
        "sources": [SOURCES[name].capabilities() for name in sources],
    }
    activated = action(api_client, sub, "enable")
    assert activated.status_code == 200, activated.text
    assert activated.json()["desired_state"] == "enabled"
    assert activated.json()["activation_id"] is not None
    with session_factory() as session:
        config = management.current_config(session, session.get(EventSubscription, sub["id"]))
        assert config.source_identity is not None
        for model in (SyncBatch, SourceCheckpoint, Execution):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    assert action(api_client, sub, "disable").status_code == 200
    monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
    rejected = action(api_client, sub, "enable")
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "source_not_available"


@pytest.mark.parametrize("resolution", ["abandon", "success"])
def test_closed_completion_prior_requests_survive_later_configuration_change(
    api_client, monkeypatch, session_factory, resolution
):
    _, _, sub = kube_subscription(api_client, monkeypatch)
    sub = activate_kube(api_client, monkeypatch, sub)
    with session_factory.begin() as session:
        lease = delivery.acquire_lease(session, sub["id"], "api-core-owner")
        config = management.current_config(session, session.get(EventSubscription, sub["id"]))
        context = SourceRunContext(
            subscription_id=sub["id"],
            owner_id=lease.owner_id,
            generation=lease.generation,
            activation_id=sub["activation_id"],
            config_version=config.version,
            source_epoch=sub["source_epoch"],
            source_config=copy.deepcopy(config.config),
            credential_fields={},
            stop_event=threading.Event(),
            session_factory=session_factory,
            buffer=ApplicationBufferBudget().scope(),
            connected=lambda: None,
            identity_snapshot=copy.deepcopy(config.source_identity),
        )
        ref = sync.begin_sync_batch(session, context, context.identity_snapshot)
        sync.finish_list_page(
            session, context, ref, 0, "opaque-empty-rv", [], expected_item_count=0, last_page=True
        )

    # Use the actual prepaid-control/T2/Attempt/terminal protocol, without SDK I/O.
    execution_ids = []
    for kind, outcome in (("sync.started", "succeeded"), ("sync.completed", "failed")):
        with session_factory.begin() as session:
            tickets = sync.select_ready_control_candidates(session)
            assert len(tickets) == 1 and tickets[0].kind == kind
            sync.materialize_ready_control(session, tickets[0])
            receipt = session.scalar(
                select(EventReceipt).where(
                    EventReceipt.batch_id == ref.batch_id, EventReceipt.source_phase == kind
                )
            )
            rid = receipt.id
            item = session.scalar(select(EventDelivery).where(EventDelivery.receipt_id == rid))
            did = item.id
            execution = delivery.dispatch_delivery(session, did)
            assert execution is not None
            eid, worker_id = execution.id, execution.target_worker_id
            execution_ids.append(eid)
        assert claim_execution(api_client, worker_id, execution_id=eid).status_code == 200
        result = report_attempt(
            api_client,
            worker_id,
            eid,
            {
                "status": outcome,
                **({"error": "completion business bug"} if outcome == "failed" else {}),
            },
        )
        assert result.status_code == 200, result.text
        with session_factory.begin() as session:
            delivery.observe_terminal(session, did)
            sync.maintain_batch(session, ref.batch_id)
    stop_and_ack(api_client, session_factory, sub)
    replay_body = {
        "expected_config_version": 1,
        "request_id": "completion-replay-before-settlement",
        "reason": "Try the original completion once more",
    }
    replay_url = f"/api/event-subscriptions/{sub['id']}/events/{rid}/replay"
    replay = api_client.post(replay_url, json=replay_body)
    assert replay.status_code == 201, replay.text
    pending_id = replay.json()["delivery_id"]
    body = {
        "expected_config_version": 1,
        "source_epoch": sub["source_epoch"],
        "receipt_id": rid,
        "expected_latest_delivery_id": pending_id,
        "expected_delivery_ordinal": 2,
        "expected_execution_id": None,
        "request_id": "settle-completion",
        "reason": "Explicitly release completion barrier without confirming business success",
        "confirm_impact": True,
    }
    url = f"/api/event-subscriptions/{sub['id']}/sync-batches/{ref.batch_id}/abandon-completion"
    replay_execution_id = None
    if resolution == "abandon":
        abandoned = api_client.post(url, json=body)
        assert abandoned.status_code == 200, abandoned.text
        assert abandoned.json()["cancelled_pending_delivery_id"] == pending_id
        assert abandoned.json()["business_result"] == "unconfirmed"
    else:
        with session_factory.begin() as session:
            execution = delivery.dispatch_delivery(session, pending_id)
            assert execution is not None
            replay_execution_id = execution.id
            execution_ids.append(execution.id)
        assert (
            claim_execution(api_client, worker_id, execution_id=replay_execution_id).status_code
            == 200
        )
        assert (
            report_attempt(
                api_client, worker_id, replay_execution_id, {"status": "succeeded"}
            ).status_code
            == 200
        )
        with session_factory.begin() as session:
            delivery.observe_terminal(session, pending_id)
            sync.maintain_batch(session, ref.batch_id)
    event = api_client.get(f"/api/event-subscriptions/{sub['id']}/events/{rid}").json()
    assert event["state"] == ("abandoned" if resolution == "abandon" else "succeeded")
    assert event["source_phase"] == "sync.completed"
    detail = api_client.get(
        f"/api/event-subscriptions/{sub['id']}/sync-batches/{ref.batch_id}"
    ).json()
    assert detail["closed"] and detail["completion_committed"]
    assert detail["completion_abandoned"] == (resolution == "abandon")
    if resolution == "abandon":
        assert detail["abandonment"] == abandoned.json()
    else:
        assert detail["abandonment"] is None
    updated = api_client.patch(
        f"/api/event-subscriptions/{sub['id']}",
        json={"expected_config_version": 1, "config": kube_config()},
    )
    assert updated.status_code == 200 and updated.json()["config_version"] == 2, updated.text
    monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
    if resolution == "abandon":
        assert api_client.post(url, json=body).json() == abandoned.json()
    assert api_client.post(replay_url, json=replay_body).json() == {
        "delivery_id": pending_id,
        "execution_id": replay_execution_id,
    }
    fresh = api_client.post(
        replay_url,
        json={**replay_body, "expected_config_version": 2, "request_id": "cannot-resurrect"},
    )
    assert fresh.status_code == 409, fresh.text
    assert fresh.json()["detail"]["code"] == "subscription_sync_completion_conflict"
    with session_factory() as session:
        assert session.get(EventDelivery, pending_id).state == (
            "abandoned" if resolution == "abandon" else "succeeded"
        )
        assert session.get(EventDelivery, pending_id).execution_id == replay_execution_id
        assert session.get(EventDelivery, did).state == "failed"
        assert session.get(Execution, eid).status == "dead_letter"
        assert session.scalar(select(func.count()).select_from(Execution)) == len(execution_ids)
        assert session.scalar(
            select(func.count())
            .select_from(DeliveryDisposition)
            .where(DeliveryDisposition.action == "abandon_completion")
        ) == (1 if resolution == "abandon" else 0)


def test_abandonment_forwards_old_request_before_version_feature_checks_but_after_acl(
    api_client, monkeypatch, session_factory
):
    adapter, _, sub = kube_subscription(api_client, monkeypatch)
    calls = []

    def resolved_prior(session, subscription_id, **values):
        calls.append((subscription_id, values))
        return sync.CompletionAbandonmentResult(
            disposition_id=7,
            batch_id=8,
            receipt_id=9,
            latest_delivery_id=10,
            execution_id=None,
            request_id=values["request_id"],
            actor_id=values["actor_id"],
            reason=values["reason"],
            closed_at=datetime.now(UTC),
            cancelled_pending_id=10,
        )

    # This case verifies API authority/order only; core tests verify the real audit transaction.
    monkeypatch.setattr(sync, "abandon_completion", resolved_prior, raising=False)
    monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
    body = {
        "expected_config_version": 99,
        "source_epoch": "original-epoch",
        "receipt_id": 9,
        "expected_latest_delivery_id": 10,
        "expected_delivery_ordinal": 2,
        "expected_execution_id": None,
        "request_id": "original-unknown-result-request",
        "reason": "Keep the original request semantics",
        "confirm_impact": True,
    }
    url = f"/api/event-subscriptions/{sub['id']}/sync-batches/8/abandon-completion"
    result = api_client.post(url, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["business_result"] == "unconfirmed"
    assert result.json()["cancelled_pending_delivery_id"] == 10
    assert calls[0][1]["expected_config_version"] == 99
    assert calls[0][1]["expected_execution_id"] is None
    assert calls[0][1]["actor_id"] is None  # authenticated token administrator
    with session_factory.begin() as session:
        user = User(username="abandon-reader", password_hash="unused-test-hash", role="user")
        session.add(user)
        session.flush()
        user_id = user.id
        session.add(AdapterPermission(adapter_id=adapter["id"], user_id=user.id, permission="read"))
    api_client.app.dependency_overrides[require_principal] = lambda: Principal(
        kind="account", role="user", user_id=user_id, username="abandon-reader"
    )
    assert api_client.post(url, json=body).status_code == 403
    assert len(calls) == 1


def test_control_maintenance_is_dedicated_bounded_and_runs_with_feature_off(
    api_client, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
    monkeypatch.setattr(management, "_subscription_cursor", 999)
    monkeypatch.setattr(management, "_subscription_upper", 999)
    monkeypatch.setattr(management, "_batch_cursor", 0)
    monkeypatch.setattr(management, "_batch_upper", 0)
    # This case verifies isolation/order after one failed responsibility. The
    # scheduler budget tests separately verify the real 50ms stop boundary.
    monkeypatch.setattr(management, "time", SimpleNamespace(monotonic=lambda: 100.0))
    calls = []
    sessions = []
    now = datetime.now(UTC)
    tickets = [
        sync.ControlTicket(11, "sync.failed", now, 11),
        sync.ControlTicket(12, "sync.completed", now, 12),
    ]

    def candidates(session, cursor, upper, limit):
        assert limit == 1
        if (cursor, upper) == (0, 0):
            return [11], 11, 12
        assert (cursor, upper) == (11, 12)
        return [12], 0, 0

    def maintain(session, batch_id):
        session.execute(select(1))
        sessions.append(session)
        calls.append(("maintain", batch_id))
        if batch_id == 11:
            raise RuntimeError("do-not-log-native-body")
        return sync.SyncStep()

    def ready(session, limit):
        assert limit == 100
        calls.append(("ready", None))
        return tickets

    def materialize(session, ticket):
        session.execute(select(1))
        sessions.append(session)
        calls.append(("materialize", ticket.batch_id))
        return sync.SyncStep()

    monkeypatch.setattr(sync, "select_batch_maintenance_candidates", candidates)
    monkeypatch.setattr(sync, "maintain_batch", maintain, raising=False)
    monkeypatch.setattr(sync, "select_ready_control_candidates", ready)
    monkeypatch.setattr(sync, "materialize_ready_control", materialize, raising=False)
    management.maintenance_tick()
    assert calls == [
        ("maintain", 11),
        ("maintain", 12),
        ("ready", None),
        ("materialize", 11),
        ("materialize", 12),
    ]
    assert len({id(session) for session in sessions}) == 4
    assert not any(session.in_transaction() for session in sessions)
    assert "do-not-log-native-body" not in caplog.text
