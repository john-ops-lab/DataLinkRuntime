"""Cross-source configuration and retained-receipt semantics on isolated PostgreSQL."""

from copy import deepcopy

from sqlalchemy import select

from dlr.control.models import (
    EventReceipt,
    EventSubscription,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SyncBatch,
)
from dlr.control.services import subscription_delivery as delivery
from test_issue219_delivery import (  # noqa: F401
    delivery_id,
    receive,
    world,
)
from test_issue219_management import config as kafka_config
from test_issue219_sync import (  # noqa: F401
    execute,
    kube,
    latest,
    ready_completed,
    stop,
)
from test_issue219_sync_management import kube_config


def switch_source(client, subscription_id, config, *, new_epoch=True):
    before = client.get(f"/api/event-subscriptions/{subscription_id}").json()
    response = client.patch(
        f"/api/event-subscriptions/{subscription_id}",
        json={
            "expected_config_version": before["config_version"],
            "config": config,
            "new_epoch": new_epoch,
        },
    )
    return before, response


def test_cross_source_roundtrip_requires_new_epoch_and_keeps_generated_kafka_group(
    api_client,
    kube,  # noqa: F811
    session_factory,
):
    with session_factory() as session:
        stop(session, kube)
        original = deepcopy(session.get(SubscriptionConfigVersion, 1).config)
        original_identity = deepcopy(session.get(SubscriptionConfigVersion, 1).source_identity)
    before, rejected = switch_source(
        api_client, kube.subscription_id, kafka_config(), new_epoch=False
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "source_epoch_required"
    assert api_client.get(f"/api/event-subscriptions/{kube.subscription_id}").json() == before
    _, changed = switch_source(api_client, kube.subscription_id, kafka_config())
    assert changed.status_code == 200, changed.text
    kafka = changed.json()
    assert kafka["source_type"] == kafka["config"]["source_type"] == "kafka"
    assert kafka["config_version"] == 2 and kafka["source_epoch"] != before["source_epoch"]
    assert kafka["config"]["group_id"].startswith("dlr-subscription-")
    group = kafka["config"]["group_id"]
    # A same-source edit still inherits its established group, rather than resetting it.
    _, edited = switch_source(api_client, kube.subscription_id, kafka_config(), new_epoch=False)
    assert edited.status_code == 200, edited.text
    assert edited.json()["config"]["group_id"] == group
    assert edited.json()["source_epoch"] == kafka["source_epoch"]
    _, returned = switch_source(api_client, kube.subscription_id, kube_config())
    assert returned.status_code == 200, returned.text
    restored = returned.json()
    assert restored["source_type"] == restored["config"]["source_type"] == "kubernetes"
    assert restored["config_version"] == 4 and restored["source_epoch"] != kafka["source_epoch"]
    with session_factory() as session:
        sub = session.get(EventSubscription, kube.subscription_id)
        versions = session.scalars(
            select(SubscriptionConfigVersion)
            .where(SubscriptionConfigVersion.subscription_id == sub.id)
            .order_by(SubscriptionConfigVersion.version)
        ).all()
        assert len(versions) == 4 and sub.source_group_key is None
        assert versions[0].config == original and versions[0].source_identity == original_identity
        assert versions[1].config["group_id"] == versions[2].config["group_id"] == group
        assert versions[3].source_identity is not None


def test_old_kubernetes_list_replay_skip_keeps_frozen_completion_after_switch_to_kafka(
    api_client,
    kube,  # noqa: F811
    session_factory,
):
    with session_factory() as session:
        ref, completed = ready_completed(session, kube)
        execute(session, completed)
        member = session.scalar(
            select(EventReceipt).where(
                EventReceipt.batch_id == ref.batch_id, EventReceipt.source_phase == "list"
            )
        )
        member_id, original_activation = member.id, member.activation_id
        original_envelope = deepcopy(member.envelope)
        stop(session, kube)
    _, changed = switch_source(api_client, kube.subscription_id, kafka_config(group_id="new-group"))
    assert changed.status_code == 200, changed.text
    with session_factory() as session:
        replay = delivery.replay_event(
            session,
            member_id,
            "old-list-replay",
            None,
            "Review retained object",
            expected_config_version=2,
        )
        session.commit()
        assert replay.execution_id is None
        delivery.skip_event(session, member_id, "old-list-skip", None, "Explicitly omit replay")
        session.commit()
        batch = session.get(SyncBatch, ref.batch_id)
        # A skipped replay cannot rewrite an already executed completion's historical result.
        assert batch.state == "completed" and batch.failure_code is None
        assert batch.completion_committed and batch.finished_at is not None
        assert batch.pending_gap["after_completion_committed"]
        assert batch.pending_gap["code"] == "source_member_skipped"
        member = session.get(EventReceipt, member_id)
        assert member.state == "skipped" and member.envelope == original_envelope
        assert member.activation_id == original_activation
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}


def test_old_kubernetes_replay_uses_retained_activation_through_failure_and_success(
    api_client,
    kube,  # noqa: F811
    session_factory,
):
    with session_factory() as session:
        ref, completed = ready_completed(session, kube)
        execute(session, completed)
        member = session.scalar(
            select(EventReceipt).where(
                EventReceipt.batch_id == ref.batch_id, EventReceipt.source_phase == "list"
            )
        )
        rid, original_activation, original_envelope = (
            member.id,
            member.activation_id,
            deepcopy(member.envelope),
        )
        stop(session, kube)
    _, changed = switch_source(api_client, kube.subscription_id, kafka_config(group_id="new-group"))
    assert changed.status_code == 200, changed.text
    with session_factory() as session:
        delivery.replay_event(session, rid, "old-list-fails", None, "Retained target failure")
        session.commit()
        failed = execute(session, rid, "dead_letter")
        assert failed.input == original_envelope
        activation = session.get(SubscriptionActivation, original_activation)
        assert failed.version_id == activation.revision_id
        assert failed.target_worker_id == activation.worker_id
        assert session.get(EventSubscription, kube.subscription_id).delivery_state == "blocked"
        delivery.replay_event(session, rid, "old-list-recovers", None, "Retry retained object")
        session.commit()
        succeeded = execute(session, rid)
        assert succeeded.input == original_envelope
        assert succeeded.version_id == activation.revision_id
        assert succeeded.target_worker_id == activation.worker_id
        assert session.get(EventReceipt, rid).activation_id == original_activation
        assert latest(session, rid).ordinal == 3
        assert session.get(EventSubscription, kube.subscription_id).delivery_state == "idle"
        assert session.get(SyncBatch, ref.batch_id).state == "completed"
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}


def test_old_kafka_replay_remains_kafka_after_switch_to_kubernetes(
    api_client,
    world,  # noqa: F811
    session_factory,
):
    with session_factory() as session:
        config = session.get(SubscriptionConfigVersion, world["config"])
        config.config = kafka_config(group_id="original-group")
        receipt = receive(session, world)
        rid, activation_id, original_envelope = (
            receipt.id,
            receipt.activation_id,
            deepcopy(receipt.envelope),
        )
        assert delivery_id(session, receipt) is not None
        execute(session, rid)
        sub = session.get(EventSubscription, world["sub"])
        sub.desired_state, sub.connection_state = "disabled", "stopped"
        session.commit()
    _, changed = switch_source(api_client, world["sub"], kube_config())
    assert changed.status_code == 200, changed.text
    with session_factory() as session:
        delivery.replay_event(session, rid, "old-kafka", None, "Retain original message")
        session.commit()
        execution = execute(session, rid)
        assert execution.input == original_envelope and execution.input["source"]["type"] == "kafka"
        activation = session.get(SubscriptionActivation, activation_id)
        assert execution.version_id == activation.revision_id
        assert execution.target_worker_id == activation.worker_id
        assert session.get(EventReceipt, rid).activation_id == activation_id
        assert session.get(EventSubscription, world["sub"]).delivery_state == "idle"
        assert delivery.audit_quota(session, world["sub"])["differences"] == {}
