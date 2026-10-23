"""B3 product regressions on isolated PostgreSQL; no source SDK or live Worker."""

import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from dlr.control.models import (
    Adapter,
    AdapterPermission,
    DeliveryDisposition,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    Execution,
    SourceCheckpoint,
    SubscriptionConfigVersion,
    SubscriptionLease,
    SubscriptionQuota,
    User,
)
from dlr.control.security import Principal, require_principal
from dlr.control.services import secrets, subscription_runtime
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_kafka as kafka
from dlr.control.services import subscription_management as management
from dlr.control.services.input_config import database_now
from dlr.control.services.subscription_source import ApplicationBufferBudget
from test_issue219_delivery import delivery_id, receive
from test_issue219_delivery import world as world
from test_issue219_kafka import P0, FakeTransport, record
from test_issue219_kafka import context as context
from test_issue219_management import action, activate, setup_subscription
from test_issue219_management import receive as receive_managed


@pytest.fixture
def fresh_product_scan(monkeypatch):
    # Each test owns its database and complete scan lifecycle. Support the
    # pre-fix module too, so the old moving-tail behavior remains reproducible.
    for name in (
        "_delivery_cursor",
        "_delivery_upper",
        "_subscription_cursor",
        "_subscription_upper",
        "_quota_cursor",
        "_quota_upper",
    ):
        if hasattr(management, name):
            monkeypatch.setattr(management, name, 0)


def empty_subscription(session, suffix):
    adapter = Adapter(
        name=f"product-gc-{suffix}", language="python", adapter_type="task", run_mode="manual"
    )
    session.add(adapter)
    session.flush()
    subscription = EventSubscription(
        adapter_id=adapter.id,
        name=f"product-gc-{suffix}",
        source_type="kafka",
        source_epoch=f"product-epoch-{suffix}",
        desired_state="disabled",
    )
    session.add(subscription)
    session.flush()
    return subscription.id


def test_continuous_arrivals_do_not_starve_completed_partition_heads(
    world, fresh_product_scan, session_factory
):
    heads = []
    with session_factory.begin() as session:
        for partition in range(4):
            receipt = receive(session, world, partition=partition)
            did = delivery_id(session, receipt)
            execution = delivery.dispatch_delivery(session, did)
            assert execution is not None
            heads.append((receipt.id, execution.id))

    # Visit the processing heads before the simulated Worker result arrives.
    # Thus their IDs lie behind the next page when the producer continues.
    management.maintenance_tick()
    with session_factory.begin() as session:
        for _, eid in heads:
            execution = session.get(Execution, eid)
            execution.status = "succeeded"
            execution.ended_at = database_now(session)

    following = []
    for partition in range(4):
        with session_factory.begin() as session:
            receipt = receive(session, world, offset=1, partition=partition)
            following.append(delivery_id(session, receipt))
        management.maintenance_tick()

    # New higher IDs arrived before every tick. Completion projection must
    # progress while reception continues, rather than waiting for an empty tail.
    with session_factory() as session:
        assert all(session.get(EventReceipt, rid).state == "succeeded" for rid, _ in heads)
        dispatched = session.scalars(
            select(EventDelivery).where(EventDelivery.id.in_(following))
        ).all()
        assert len(dispatched) == 4
        assert any(item.execution_id is not None for item in dispatched)


def test_history_scan_wraps_deleted_upper_and_new_tail_without_starving_old_payload(
    world, fresh_product_scan, session_factory, monkeypatch
):
    with session_factory.begin() as session:
        extra = [empty_subscription(session, index) for index in range(104)]

    observed_pages = []
    original_cleanup = delivery.cleanup_history

    def observed_cleanup(session, subscription_id):
        observed_pages[-1].append(subscription_id)
        return original_cleanup(session, subscription_id)

    monkeypatch.setattr(delivery, "cleanup_history", observed_cleanup)

    def tick():
        observed_pages.append([])
        management.maintenance_tick()

    tick()
    assert len(observed_pages[0]) == 100
    assert world["sub"] in observed_pages[0]
    assert extra[-1] not in observed_pages[0]

    # A previously visited subscription gains an actually eligible retained
    # payload. Its source identity and 30-day metadata must survive payload GC.
    with session_factory.begin() as session:
        receipt = receive(session, world)
        rid = receipt.id
        source_event_id = receipt.source_event_id
        did = delivery_id(session, receipt)
        execution = delivery.dispatch_delivery(session, did)
        assert execution is not None
        execution.status = "succeeded"
        execution.ended_at = database_now(session)
        session.flush()
        delivery.observe_terminal(session, did)
        receipt.terminal_at = database_now(session) - timedelta(days=8)
        assert receipt.envelope is not None
        session.delete(session.get(EventSubscription, extra[-1]))

    # Delete the captured upper row and keep extending the tail on every tick.
    # Neither event may prevent completing a bounded pass and revisiting old IDs.
    for index in range(4):
        with session_factory.begin() as session:
            empty_subscription(session, f"tail-{index}")
        tick()

    assert all(len(page) <= 100 for page in observed_pages)
    assert world["sub"] in set().union(*(set(page) for page in observed_pages[1:]))
    with session_factory() as session:
        receipt = session.get(EventReceipt, rid)
        assert receipt is not None and receipt.state == "succeeded"
        assert receipt.envelope is None and receipt.payload_expired_at is not None
        assert receipt.source_event_id == source_event_id
        assert session.get(EventDelivery, did) is not None


def account(session_factory, name, *, adapter_id=None, permission="read", role="user"):
    with session_factory.begin() as session:
        user = User(username=name, password_hash="unused-product-hash", role=role)
        session.add(user)
        session.flush()
        if adapter_id is not None:
            session.add(
                AdapterPermission(adapter_id=adapter_id, user_id=user.id, permission=permission)
            )
        return Principal(kind="account", role=role, user_id=user.id, username=name)


def test_runtime_diagnostics_use_live_process_ledger_and_preserve_admin_boundary(
    api_client, session_factory, monkeypatch
):
    budget = ApplicationBufferBudget()
    assert budget.reserve(2048)
    budget.release(1024)
    monkeypatch.setattr(
        subscription_runtime, "_runtime", SimpleNamespace(active_count=0, buffer=budget)
    )
    response = api_client.get("/api/event-subscriptions/runtime-status")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "active_count": 0,
        "application_buffer_used_bytes": 1024,
        "application_buffer_peak_bytes": 2048,
        "application_buffer_limit_bytes": 64 * 1024**2,
    }
    viewer = account(session_factory, "runtime-viewer")
    api_client.app.dependency_overrides[require_principal] = lambda: viewer
    assert api_client.get("/api/event-subscriptions/runtime-status").status_code == 403


def test_checkpoint_page_is_current_epoch_readonly_precise_and_bounded(
    api_client, session_factory, monkeypatch
):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    with session_factory.begin() as session:
        for index, epoch in enumerate([sub["source_epoch"]] * 3 + ["older-epoch"]):
            session.add(
                SourceCheckpoint(
                    subscription_id=sub["id"],
                    source_epoch=epoch,
                    shard=f"events:{index}",
                    position={"offset": str(2**64 + index), "password": "private-position"},
                    confirmed_position={"offset": "1", "payload": "private-body"},
                    assignment_generation=index + 1,
                    blocked_unaccepted={
                        "code": "event_payload_too_large",
                        "source_event_id": f"unaccepted-{index}",
                        "position": {"offset": "4", "password": "private-fault"},
                        "size": 524289,
                        "content_hash": None,
                        "duplicate_count": 0,
                        "payload": "private-fault-body",
                    },
                )
            )

    def cardinalities():
        with session_factory() as session:
            return [
                session.scalar(select(func.count()).select_from(model))
                for model in (
                    SourceCheckpoint,
                    SubscriptionLease,
                    SubscriptionQuota,
                    DeliveryDisposition,
                )
            ]

    before = cardinalities()
    path = f"/api/event-subscriptions/{sub['id']}/checkpoints"
    first = api_client.get(path, params={"limit": 2})
    assert first.status_code == 200, first.text
    page = first.json()
    assert page["total"] == 3 and len(page["items"]) == 2
    assert page["next_before_id"] == page["items"][-1]["id"]
    second = api_client.get(path, params={"before_id": page["next_before_id"], "limit": 2})
    assert second.status_code == 200, second.text
    rows = page["items"] + second.json()["items"]
    assert len({row["id"] for row in rows}) == 3
    assert second.json()["next_before_id"] is None
    assert all(row["source_epoch"] == sub["source_epoch"] for row in rows)
    assert all(row["source_lag"] is None for row in rows)
    assert all(isinstance(row["position"]["offset"], str) for row in rows)
    assert {int(row["position"]["offset"]) for row in rows} == {2**64 + n for n in range(3)}
    assert "private-" not in first.text + second.text
    assert api_client.get(path, params={"limit": 101}).status_code == 422
    assert cardinalities() == before


def test_product_diagnostics_obey_adapter_acl_and_activation_parentage(
    api_client, session_factory, monkeypatch
):
    adapter, _, visible = setup_subscription(api_client, monkeypatch, name="visible-product")
    _, _, hidden = setup_subscription(api_client, monkeypatch, name="hidden-product")
    visible = activate(api_client, monkeypatch, visible)
    hidden = activate(api_client, monkeypatch, hidden)
    viewer = account(session_factory, "product-viewer", adapter_id=adapter["id"])
    api_client.app.dependency_overrides[require_principal] = lambda: viewer
    assert (
        api_client.get(f"/api/event-subscriptions/{visible['id']}").json()["access_level"] == "read"
    )
    for sid in (hidden["id"], 999999):
        assert api_client.get(f"/api/event-subscriptions/{sid}/checkpoints").status_code == 404
        assert (
            api_client.get(
                f"/api/event-subscriptions/{sid}/activations/{hidden['activation_id']}"
            ).status_code
            == 404
        )
    assert (
        api_client.get(f"/api/event-subscriptions/{visible['id']}/checkpoints").status_code == 200
    )
    assert (
        api_client.get(
            f"/api/event-subscriptions/{visible['id']}/activations/{visible['activation_id']}"
        ).status_code
        == 200
    )
    assert (
        api_client.get(
            f"/api/event-subscriptions/{visible['id']}/activations/{hidden['activation_id']}"
        ).status_code
        == 404
    )
    assert (
        action(
            api_client,
            visible,
            "skip-unaccepted",
            request_id="readonly-skip",
            reason="not authorized",
            shard="events:0",
            source_event_id="fault",
        ).status_code
        == 403
    )


def test_activation_and_credential_usage_expose_refs_without_decrypting(
    api_client, session_factory, monkeypatch
):
    def credential(name, fields, kind):
        response = api_client.post(
            "/api/credentials", json={"name": name, "type": kind, "fields": fields}
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    source = credential(
        "source-product",
        {"username": "fixture-user", "password": "private-source-value"},
        "password",
    )
    target = credential("target-product", {"token": "private-target-value"}, "token")
    adapter, revision, sub = setup_subscription(api_client, monkeypatch, credential_id=source)
    binding = api_client.put(
        f"/api/adapters/{adapter['id']}/credential-bindings",
        json={"bindings": [{"env_key": "TARGET", "credential_id": target, "field": "token"}]},
    )
    assert binding.status_code == 200, binding.text
    sub = activate(api_client, monkeypatch, sub)
    receipt_id, _ = receive_managed(session_factory, sub, 0)

    def forbidden_decryption(*args, **kwargs):
        raise AssertionError("A metadata-only product route tried to decrypt a Secret")

    monkeypatch.setattr(secrets, "decrypt_fields", forbidden_decryption)
    path = f"/api/event-subscriptions/{sub['id']}/activations/{sub['activation_id']}"
    activation = api_client.get(path)
    assert activation.status_code == 200, activation.text
    detail = activation.json()
    event = api_client.get(f"/api/event-subscriptions/{sub['id']}/events/{receipt_id}")
    assert event.status_code == 200, event.text
    assert event.json()["activation_id"] == sub["activation_id"]
    original_delivery = event.json()["deliveries"][0]
    assert original_delivery["activation_override_id"] is None
    assert original_delivery["effective_activation_id"] == sub["activation_id"]
    assert detail["config_version"] == 1 and detail["source_epoch"] == sub["source_epoch"]
    assert detail["revision_id"] == revision["id"]
    assert (
        detail["worker_id"]
        == api_client.get(f"/api/adapters/{adapter['id']}").json()["runtime_worker_id"]
    )
    assert detail["source_credential_id"] == source
    assert detail["credential_refs"] == [
        {"credential_id": target, "env_key": "TARGET", "field": "token"}
    ]
    source_usage = api_client.get(f"/api/credentials/{source}/usage")
    target_usage = api_client.get(f"/api/credentials/{target}/usage")
    assert source_usage.status_code == target_usage.status_code == 200
    assert {row["kind"] for row in source_usage.json()["items"]} >= {
        "subscription_source",
        "subscription_activation",
    }
    assert "subscription_activation" in {row["kind"] for row in target_usage.json()["items"]}
    assert "private-" not in activation.text + source_usage.text + target_usage.text
    viewer = account(session_factory, "credential-product-viewer", adapter_id=adapter["id"])
    api_client.app.dependency_overrides[require_principal] = lambda: viewer
    assert api_client.get(path).status_code == 200
    assert api_client.get(f"/api/credentials/{source}/usage").status_code == 403


def rejection(session_factory, sub):
    with session_factory.begin() as session:
        lease = delivery.acquire_lease(session, sub["id"], "product-poison")
        checkpoint = SourceCheckpoint(
            subscription_id=sub["id"],
            source_epoch=sub["source_epoch"],
            shard="events:0",
            position={"offset": "0", "cluster_id": "test-cluster", "topic_id": "physical-topic"},
            confirmed_position={"offset": "0"},
        )
        session.add(checkpoint)
        session.flush()
        delivery.record_rejection(
            session,
            sub["id"],
            lease.owner_id,
            lease.generation,
            "events:0",
            "event_payload_too_large",
            "poison-0",
            {"offset": "0"},
            size=524289,
        )
        return checkpoint.id


def test_unaccepted_skip_derives_exact_offset_and_is_actor_bound_idempotent(
    api_client, session_factory, monkeypatch
):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    sub = activate(api_client, monkeypatch, sub)
    cid = rejection(session_factory, sub)
    body = {
        "request_id": "poison-skip",
        "reason": "Explicitly discard this unreadable event",
        "shard": "events:0",
        "source_event_id": "poison-0",
    }
    rejected = action(api_client, sub, "skip-unaccepted", **body, next_position={"offset": "100"})
    assert rejected.status_code == 422
    stale = api_client.post(
        f"/api/event-subscriptions/{sub['id']}/skip-unaccepted",
        json={"expected_config_version": 2, **body},
    )
    assert stale.status_code == 409
    first = action(api_client, sub, "skip-unaccepted", **body)
    assert first.status_code == 200, first.text
    again = action(api_client, sub, "skip-unaccepted", **body)
    assert again.status_code == 200 and again.json() == first.json()
    assert first.json()["outcome"] == "skipped_without_processing"
    changed = action(api_client, sub, "skip-unaccepted", **{**body, "reason": "different intent"})
    assert changed.status_code == 409
    actor = account(session_factory, "other-product-admin", role="admin")
    api_client.app.dependency_overrides[require_principal] = lambda: actor
    assert action(api_client, sub, "skip-unaccepted", **body).status_code == 409
    with session_factory() as session:
        cp = session.get(SourceCheckpoint, cid)
        assert cp.position["offset"] == "1" and cp.confirmed_position["offset"] == "0"
        assert cp.position["topic_id"] == "physical-topic" and cp.blocked_unaccepted is None
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0
        assert session.scalar(select(func.count()).select_from(DeliveryDisposition)) == 1


@pytest.mark.parametrize("changed", ["epoch", "fault"])
def test_unaccepted_skip_rejects_stale_epoch_or_replaced_diagnosis(
    api_client, session_factory, monkeypatch, changed
):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    sub = activate(api_client, monkeypatch, sub)
    cid = rejection(session_factory, sub)
    with session_factory.begin() as session:
        if changed == "epoch":
            stored = session.get(EventSubscription, sub["id"])
            previous = management.current_config(session, stored)
            stored.source_epoch = "new-product-epoch"
            stored.config_version = 2
            session.add(
                SubscriptionConfigVersion(
                    subscription_id=stored.id,
                    version=2,
                    source_epoch=stored.source_epoch,
                    config=previous.config,
                )
            )
            sub = {**sub, "config_version": 2}
        else:
            cp = session.get(SourceCheckpoint, cid)
            cp.blocked_unaccepted = {
                **cp.blocked_unaccepted,
                "source_event_id": "poison-1",
                "position": {"offset": "1"},
            }
    response = action(
        api_client,
        sub,
        "skip-unaccepted",
        request_id="stale-product-skip",
        reason="The diagnostic displayed earlier",
        shard="events:0",
        source_event_id="poison-0",
    )
    assert response.status_code == 409, response.text
    with session_factory() as session:
        assert session.get(SourceCheckpoint, cid).position["offset"] == "0"
        assert session.scalar(select(func.count()).select_from(DeliveryDisposition)) == 0


def test_last_poison_skip_confirms_durable_next_position_without_another_record(context):
    class SkippedLastRecord(FakeTransport):
        skipped = False
        deadline = time.monotonic() + 5

        def poll(self):
            if self.paused:
                if not self.skipped:
                    with context.session_factory.begin() as session:
                        cp = session.scalar(select(SourceCheckpoint))
                        assert cp.blocked_unaccepted is not None
                        delivery.skip_unaccepted(
                            session,
                            context.subscription_id,
                            P0.shard,
                            "last-poison",
                            None,
                            "Verified deliberate skip",
                            cp.blocked_unaccepted["source_event_id"],
                            {**cp.position, "offset": "1"},
                        )
                    self.skipped = True
                if time.monotonic() > self.deadline:
                    context.stop_event.set()
                time.sleep(0.01)
                return {}
            return super().poll()

    transport = SkippedLastRecord(context, [record(0, value=b"x" * (512 * 1024 + 1))])
    try:
        kafka.KafkaSourceRunner()._run(context, transport)
    finally:
        transport.close()
    assert transport.skipped and transport.closed
    assert [item[P0] for item in transport.commits] == [0, 1]
    with context.session_factory() as session:
        cp = session.scalar(select(SourceCheckpoint))
        assert cp.position["offset"] == cp.confirmed_position["offset"] == "1"
        assert cp.blocked_unaccepted is None
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0
