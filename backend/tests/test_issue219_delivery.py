"""Real PostgreSQL T1/T2, fencing, retained responsibility and v1 protocol regressions."""

from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    AdapterVersion,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    Execution,
    ExecutionOutbox,
    SourceCheckpoint,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionLease,
    SubscriptionQuota,
    Worker,
)
from dlr.control.services import outbox, rabbitmq
from dlr.control.services import subscription_delivery as service
from dlr.control.services.event_envelope import event_id, validate_envelope
from dlr.control.services.input_config import database_now


@pytest.fixture
def world(api_client, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "event_subscriptions_enabled", True)
    monkeypatch.setattr(rabbitmq, "ingress_configuration_ready", lambda *a, **k: True)
    with session_factory() as session:
        worker = Worker(
            name="subscription-test-worker",
            status="online",
            capabilities=["python"],
            protocol_version=3,
            rabbitmq_execution_v3=True,
        )
        adapter = Adapter(
            name="subscription-test", language="python", adapter_type="task", run_mode="manual"
        )
        session.add_all([worker, adapter])
        session.flush()
        revision = AdapterVersion(adapter_id=adapter.id, seq=1, code="pass", requirements="")
        session.add(revision)
        session.flush()
        adapter.latest_version_id = revision.id
        adapter.runtime_worker_id = worker.id
        sub = EventSubscription(
            adapter_id=adapter.id,
            source_type="kafka",
            name="test",
            source_epoch="epoch-1",
            desired_state="enabled",
        )
        session.add(sub)
        session.flush()
        config = SubscriptionConfigVersion(
            subscription_id=sub.id, version=1, source_epoch=sub.source_epoch, config={}
        )
        session.add(config)
        session.flush()
        activation = SubscriptionActivation(
            subscription_id=sub.id,
            config_version_id=config.id,
            revision_id=revision.id,
            worker_id=worker.id,
        )
        session.add(activation)
        session.flush()
        sub.activation_id = activation.id
        lease = service.acquire_lease(session, sub.id, "owner-a")
        session.commit()
        return {
            "sub": sub.id,
            "generation": lease.generation,
            "adapter": adapter.id,
            "revision": revision.id,
            "config": config.id,
        }


def envelope(world, offset=0, partition=0, value="eA=="):
    result = {
        "schema": "dlr.event.v1",
        "subscription": {"id": str(world["sub"]), "config_version": 1, "source_epoch": "epoch-1"},
        "event": {"id": "", "kind": "message", "object_key": None},
        "source": {
            "type": "kafka",
            "identity": "logical-test-source",
            "position": {"topic": "test", "partition": str(partition), "offset": str(offset)},
            "attributes": {
                "cluster_id": "test-cluster",
                "topic_incarnation": "topic-uuid",
                "timestamp_ms": 1,
                "timestamp_type": "create_time",
                "key_base64": "",
                "headers": [
                    {"name": "duplicate", "value_base64": None},
                    {"name": "duplicate", "value_base64": ""},
                ],
            },
        },
        "received_at": "2026-10-23T00:00:00Z",
        "data": {"encoding": "base64", "content_type": "application/octet-stream", "value": value},
        "sync": None,
    }
    result["event"]["id"] = event_id(result)
    return result


def receive(session, world, offset=0, partition=0):
    return service.receive_event(
        session,
        world["sub"],
        "owner-a",
        world["generation"],
        envelope(world, offset, partition),
        f"test:{partition}",
        {"offset": str(offset + 1)},
    )[0]


def delivery_id(session, receipt):
    return session.scalar(select(EventDelivery.id).where(EventDelivery.receipt_id == receipt.id))


def test_duplicate_precedes_capacity_feature_gate_and_rabbit(world, session_factory, monkeypatch):
    with session_factory() as session:
        original = receive(session, world)
        session.commit()
        quota = session.get(SubscriptionQuota, f"sub:{world['sub']}")
        quota.pending_count = service.DEFAULT_LIMITS["pending_count"]
        session.commit()
        monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
        monkeypatch.setattr(rabbitmq, "ingress_configuration_ready", lambda *a, **k: False)
        changed = envelope(world)
        changed["received_at"] = "2026-10-24T00:00:00Z"
        changed["subscription"]["config_version"] = 999
        same, duplicate = service.receive_event(
            session,
            world["sub"],
            "owner-a",
            world["generation"],
            changed,
            "test:0",
            {"offset": "1"},
        )
        assert duplicate and same.id == original.id and same.duplicate_count == 1
        assert same.envelope["received_at"] != changed["received_at"]
        assert session.scalar(select(func.count()).select_from(Execution)) == 0


def test_conflicting_content_keeps_first_and_records_bounded_fault(world, session_factory):
    with session_factory() as session:
        original = receive(session, world)
        session.commit()
        altered = envelope(world, value="eQ==")
        with pytest.raises(HTTPException, match="409"):
            service.receive_event(
                session,
                world["sub"],
                "owner-a",
                world["generation"],
                altered,
                "test:0",
                {"offset": "1"},
            )
        session.rollback()
        service.record_rejection(
            session,
            world["sub"],
            "owner-a",
            world["generation"],
            "test:0",
            "event_identity_conflict",
            altered["event"]["id"],
            {"offset": "0"},
        )
        session.commit()
        cp = session.scalar(
            select(SourceCheckpoint).where(SourceCheckpoint.subscription_id == world["sub"])
        )
        assert cp.blocked_unaccepted["code"] == "event_identity_conflict"
        assert session.get(EventReceipt, original.id).envelope["data"]["value"] == "eA=="


def test_t1_rollback_does_not_advance_checkpoint_or_counters(world, session_factory):
    with session_factory() as session:
        receive(session, world)
        session.rollback()
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0
        assert session.scalar(select(func.count()).select_from(SourceCheckpoint)) == 0
        assert session.get(SubscriptionQuota, "global") is None


def test_lease_takeover_rejects_old_owner(world, session_factory):
    with session_factory() as session:
        lease = session.get(SubscriptionLease, world["sub"])
        lease.lease_until = database_now(session) - timedelta(seconds=1)
        session.commit()
        current = service.acquire_lease(session, world["sub"], "owner-b")
        session.commit()
        assert current.generation == world["generation"] + 1
        with pytest.raises(HTTPException):
            receive(session, world)
        session.rollback()
        service.receive_event(
            session,
            world["sub"],
            "owner-b",
            current.generation,
            envelope(world),
            "test:0",
            {"offset": "1"},
        )
        session.commit()


def test_t2_is_atomic_repeatable_and_keeps_activation(world, session_factory):
    with session_factory() as session:
        receipt = receive(session, world)
        session.commit()
        did = delivery_id(session, receipt)
        old = session.get(Adapter, world["adapter"])
        newer = AdapterVersion(adapter_id=old.id, seq=2, code="new", requirements="")
        session.add(newer)
        session.flush()
        old.latest_version_id = newer.id
        session.commit()
        execution = service.dispatch_delivery(session, did)
        session.commit()
        assert execution.version_id == world["revision"]
        assert execution.input_snapshot == {"source_type": "json", "revision": None}
        assert execution.trigger == "subscription"
        assert service.dispatch_delivery(session, did).id == execution.id
        assert session.scalar(select(func.count()).select_from(ExecutionOutbox)) == 1
        quota = session.get(SubscriptionQuota, "global")
        assert quota.pending_count == 0 and quota.history_count == 1 and quota.dedupe_count == 1


def test_t2_partial_outbox_failure_rolls_back_execution_and_transfer(
    world, session_factory, monkeypatch
):
    with session_factory() as session:
        receipt = receive(session, world)
        session.commit()
        did = delivery_id(session, receipt)

        def fail(*a, **k):
            raise RuntimeError("injected after Execution")

        monkeypatch.setattr(outbox, "create_dispatch_outbox", fail)
        with pytest.raises(RuntimeError):
            service.dispatch_delivery(session, did)
        session.commit()
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        assert session.get(EventDelivery, did).state == "pending"
        assert session.get(SubscriptionQuota, "global").pending_count == 1


def test_admission_unavailable_retains_pending_and_sets_backoff(
    world, session_factory, monkeypatch
):
    with session_factory() as session:
        receipt = receive(session, world)
        session.commit()
        did = delivery_id(session, receipt)
        monkeypatch.setattr(rabbitmq, "ingress_configuration_ready", lambda *a, **k: False)
        assert service.dispatch_delivery(session, did) is None
        session.commit()
        assert session.get(EventDelivery, did).retry_after > database_now(session)
        assert session.get(SubscriptionQuota, "global").pending_count == 1


def test_failed_partition_blocks_retry_chain_other_partition_can_advance(world, session_factory):
    with session_factory() as session:
        first = receive(session, world)
        second = receive(session, world, 1)
        other = receive(session, world, 0, 1)
        session.commit()
        firstdid = delivery_id(session, first)
        execution = service.dispatch_delivery(session, firstdid)
        session.commit()
        execution.status = "retry_wait"
        session.commit()
        assert service.dispatch_delivery(session, delivery_id(session, second)) is None
        assert service.dispatch_delivery(session, delivery_id(session, other)) is not None
        session.commit()
        execution.status = "dead_letter"
        session.commit()
        service.observe_terminal(session, firstdid)
        session.commit()
        replay = service.replay_event(session, first.id, "fix-1", None, "retry fixed target")
        session.commit()
        assert (
            service.replay_event(session, first.id, "fix-1", None, "retry fixed target").id
            == replay.id
        )
        reexec = service.dispatch_delivery(session, replay.id)
        session.commit()
        reexec.status = "succeeded"
        session.commit()
        service.observe_terminal(session, replay.id)
        session.commit()
        assert service.dispatch_delivery(session, delivery_id(session, second)) is not None
        assert session.get(Execution, execution.id).status == "dead_letter"


def test_pending_skip_frees_quota_but_never_erases_original_history(world, session_factory):
    with session_factory() as session:
        receipt = receive(session, world)
        session.commit()
        disposition = service.skip_event(session, receipt.id, "skip-1", None, "reviewed omission")
        session.commit()
        assert (
            service.skip_event(session, receipt.id, "skip-1", None, "reviewed omission").id
            == disposition.id
        )
        quota = session.get(SubscriptionQuota, "global")
        assert quota.pending_count == 0 and quota.history_count == 1 and quota.dedupe_count == 1
        assert session.get(EventReceipt, receipt.id).state == "skipped"


def test_retention_windows_payload_expiry_and_dedupe_release(world, session_factory):
    with session_factory() as session:
        receipt = receive(session, world)
        session.commit()
        service.skip_event(session, receipt.id, "skip-1", None, "reviewed")
        session.commit()
        receipt.terminal_at = database_now(session) - timedelta(days=8)
        session.commit()
        assert service.cleanup_history(session, world["sub"]) == {
            "payloads_expired": 1,
            "metadata_deleted": 0,
        }
        session.commit()
        assert session.get(SubscriptionQuota, "global").history_count == 0
        with pytest.raises(HTTPException):
            service.replay_event(session, receipt.id, "replay", None, "too late")
        session.rollback()
        receipt = session.get(EventReceipt, receipt.id)
        receipt.terminal_at = database_now(session) - timedelta(days=31)
        session.commit()
        assert service.cleanup_history(session, world["sub"])["metadata_deleted"] == 1
        session.commit()
        assert session.get(SubscriptionQuota, "global").dedupe_count == 0


def test_reset_epoch_keeps_old_shard_sequence_history(world, session_factory):
    with session_factory() as session:
        first = receive(session, world)
        session.commit()
        sub = session.get(EventSubscription, world["sub"])
        sub.source_epoch = "epoch-2"
        config = SubscriptionConfigVersion(
            subscription_id=sub.id, version=2, source_epoch="epoch-2", config={}
        )
        session.add(config)
        session.flush()
        original = session.get(SubscriptionActivation, sub.activation_id)
        activation = SubscriptionActivation(
            subscription_id=sub.id,
            config_version_id=config.id,
            revision_id=original.revision_id,
            worker_id=original.worker_id,
        )
        session.add(activation)
        session.flush()
        sub.activation_id = activation.id
        sub.config_version = 2
        changed = envelope(world)
        changed["subscription"].update(source_epoch="epoch-2", config_version=2)
        second, _ = service.receive_event(
            session, sub.id, "owner-a", world["generation"], changed, "test:0", {"offset": "1"}
        )
        session.commit()
        assert first.sequence == second.sequence == 1 and first.id != second.id


def test_low_water_is_persistent_and_unaccepted_skip_is_audited(world, session_factory):
    with session_factory() as session:
        receive(session, world)
        session.commit()
        service.record_rejection(
            session,
            world["sub"],
            "owner-a",
            world["generation"],
            "test:0",
            "subscription_capacity_exceeded",
            "blocked",
            {"offset": "1"},
        )
        session.commit()
        quota = session.get(SubscriptionQuota, f"sub:{world['sub']}")
        quota.pending_count = 900
        session.commit()
        with pytest.raises(HTTPException):
            receive(session, world, 1)
        session.rollback()
        quota = session.get(SubscriptionQuota, f"sub:{world['sub']}")
        quota.pending_count = 1
        session.commit()
        receive(session, world, 1)
        session.commit()
        assert session.get(SubscriptionQuota, f"sub:{world['sub']}").blocked_reason is None
        service.record_rejection(
            session,
            world["sub"],
            "owner-a",
            world["generation"],
            "test:0",
            "event_payload_too_large",
            "poison",
            {"offset": "2"},
            size=999999,
        )
        session.commit()
        disposition = service.skip_unaccepted(
            session,
            world["sub"],
            "test:0",
            "skip-poison",
            None,
            "explicit source omission",
            "poison",
            {"offset": "3"},
        )
        session.commit()
        assert disposition.action == "skip_unaccepted"
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 2


def k8s_envelope(kind="object.present", watch_type="MODIFIED", batch="batch-1"):
    item = envelope({"sub": 1})
    item["event"].update(kind=kind, object_key="cluster/pods/ns/uid")
    item["source"] = {
        "type": "kubernetes",
        "identity": "cluster",
        "position": {"resource_version": "opaque"},
        "attributes": {
            "cluster_id": "cluster",
            "group": "",
            "version": "v1",
            "resource": "pods",
            "namespace": "ns",
            "name": "name",
            "uid": "uid",
            "watch_type": watch_type,
        },
    }
    item["data"] = {
        "encoding": "json",
        "content_type": "application/json",
        "value": {"metadata": {"uid": "uid"}},
    }
    item["sync"] = {
        "batch_id": batch,
        "scope_fingerprint": "scope",
        "phase": "watch",
        "read_complete": False,
        "item_count": 1,
        "failure_code": None,
    }
    if kind.startswith("sync."):
        item["event"]["object_key"] = None
        item["source"]["attributes"].update(name=None, uid=None, watch_type=None)
        item["data"]["value"] = None
    item["event"]["id"] = event_id(item)
    return item


def test_v1_watch_identity_ignores_batch_but_list_and_controls_include_it():
    watch = k8s_envelope()
    changed = deepcopy(watch)
    changed["sync"]["batch_id"] = "batch-2"
    assert event_id(watch) == event_id(changed)
    assert validate_envelope(watch)[0] == validate_envelope(changed)[0]
    listed = k8s_envelope(watch_type="LIST")
    changed = deepcopy(listed)
    changed["sync"]["batch_id"] = "batch-2"
    assert event_id(listed) != event_id(changed)
    completed = k8s_envelope(kind="sync.completed", watch_type=None)
    changed = deepcopy(completed)
    changed["sync"]["item_count"] = 2
    assert event_id(completed) == event_id(changed)
    assert validate_envelope(completed)[0] != validate_envelope(changed)[0]


def test_v1_rejects_before_existing_execution_json_limit(monkeypatch):
    monkeypatch.setattr(settings, "execution_input_max_bytes", 512)
    with pytest.raises(HTTPException):
        validate_envelope(envelope({"sub": 1}))


def test_v1_literal_escape_is_valid_but_actual_nul_is_rejected():
    from dlr.control.services.event_envelope import canonical

    assert canonical({"text": r"\u0000"})
    with pytest.raises(HTTPException):
        canonical({"text": "\x00"})
    assert validate_envelope(envelope({"sub": 1}, value="AA=="))


def test_concurrent_t1_dedup_and_t2_have_single_execution(world, session_factory):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2)

    def ingest():
        with session_factory() as session:
            barrier.wait(timeout=3)
            receipt, duplicate = service.receive_event(
                session,
                world["sub"],
                "owner-a",
                world["generation"],
                envelope(world),
                "test:0",
                {"offset": "1"},
            )
            session.commit()
            return receipt.id, duplicate

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ingest(), range(2)))
    assert results[0][0] == results[1][0]
    assert sorted(result[1] for result in results) == [False, True]
    with session_factory() as session:
        did = session.scalar(select(EventDelivery.id))
    barrier = threading.Barrier(2)

    def dispatch():
        with session_factory() as session:
            barrier.wait(timeout=3)
            execution = service.dispatch_delivery(session, did)
            session.commit()
            return execution.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: dispatch(), range(2)))
    assert ids[0] == ids[1]
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Execution)) == 1
        assert session.scalar(select(func.count()).select_from(ExecutionOutbox)) == 1
        assert session.get(SubscriptionQuota, "global").pending_count == 0


def test_repair_dispatches_only_selected_fix_and_preserves_later_activation(world, session_factory):
    with session_factory() as session:
        first = receive(session, world)
        later = receive(session, world, 1)
        session.commit()
        execution = service.dispatch_delivery(session, delivery_id(session, first))
        session.commit()
        execution.status = "dead_letter"
        session.commit()
        service.observe_terminal(session, delivery_id(session, first))
        sub = session.get(EventSubscription, world["sub"])
        sub.desired_state = "disabled"
        sub.delivery_state = "repair"
        fixed = AdapterVersion(adapter_id=world["adapter"], seq=2, code="fixed", requirements="")
        session.add(fixed)
        session.flush()
        sub.repair_revision_id = fixed.id
        session.commit()
        assert service.dispatch_delivery(session, delivery_id(session, later)) is None
        repair = service.replay_event(session, first.id, "repair-1", None, "fix code", fixed.id)
        session.commit()
        repaired_execution = service.dispatch_delivery(session, repair.id)
        session.commit()
        assert repaired_execution.version_id == fixed.id
        assert session.get(EventReceipt, later.id).activation_id == first.activation_id
        repaired_execution.status = "succeeded"
        session.commit()
        service.observe_terminal(session, repair.id)
        sub.delivery_state = "draining"
        session.commit()
        assert (
            service.dispatch_delivery(session, delivery_id(session, later)).version_id
            == world["revision"]
        )
        assert session.get(Execution, execution.id).status == "dead_letter"


def test_current_config_replay_is_independent_and_gc_charges_all_history(world, session_factory):
    with session_factory() as session:
        receipt = receive(session, world)
        session.commit()
        service.skip_event(session, receipt.id, "initial-skip", None, "out of band followup")
        sub = session.get(EventSubscription, world["sub"])
        sub.desired_state = "disabled"
        newer = AdapterVersion(adapter_id=world["adapter"], seq=2, code="newer", requirements="")
        session.add(newer)
        session.flush()
        session.get(Adapter, world["adapter"]).latest_version_id = newer.id
        session.commit()
        replay = service.replay_event(
            session,
            receipt.id,
            "current-1",
            None,
            "current selection",
            use_current_configuration=True,
        )
        session.commit()
        execution = service.dispatch_delivery(session, replay.id)
        session.commit()
        assert execution.version_id == newer.id
        assert replay.activation_override_id != receipt.activation_id
        assert session.get(SubscriptionQuota, "global").history_count == 2
        execution.status = "succeeded"
        session.commit()
        service.observe_terminal(session, replay.id)
        receipt.terminal_at = database_now(session) - timedelta(days=8)
        session.commit()
        service.cleanup_history(session, world["sub"])
        session.commit()
        # Receipt payload TTL does not erase the immutable Execution input.
        # The skipped first Delivery releases its unused input reservation;
        # the replay's actual input and both bounded metadata rows remain.
        import json

        expected_input_bytes = len(
            json.dumps(execution.input, ensure_ascii=False, separators=(",", ":")).encode()
        )
        rows = session.scalars(
            select(EventDelivery)
            .where(EventDelivery.receipt_id == receipt.id)
            .order_by(EventDelivery.ordinal)
        ).all()
        assert receipt.envelope is None and receipt.retained_payload_bytes == 0
        assert receipt.retained_metadata_bytes == 4096
        assert [row.retained_execution_bytes for row in rows] == [0, expected_input_bytes]
        assert [row.retained_metadata_bytes for row in rows] == [512, 512]
        quota = session.get(SubscriptionQuota, "global")
        assert quota.history_count == 1
        assert quota.history_bytes == 4096 + 2 * 512 + expected_input_bytes
        assert service.audit_quota(session, world["sub"])["differences"] == {}
        assert service.audit_global_quota(session)["differences"] == {}


def test_checkpoint_prevents_recreating_expired_kafka_history(world, session_factory):
    with session_factory() as session:
        receipt = receive(session, world)
        session.commit()
        service.skip_event(session, receipt.id, "skip-1", None, "reviewed")
        receipt.terminal_at = database_now(session) - timedelta(days=31)
        session.commit()
        service.cleanup_history(session, world["sub"])
        session.commit()
        old, duplicate = service.receive_event(
            session,
            world["sub"],
            "owner-a",
            world["generation"],
            envelope(world),
            "test:0",
            {"offset": "1"},
        )
        assert duplicate and old is None
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0


def test_feature_off_still_drains_accepted_work(world, session_factory, monkeypatch):
    with session_factory() as session:
        receipt = receive(session, world)
        session.commit()
        monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
        assert service.dispatch_delivery(session, delivery_id(session, receipt)) is not None


def test_purge_deleted_subscription_returns_global_retained_capacity(world, session_factory):
    with session_factory() as session:
        receipt = receive(session, world)
        session.commit()
        service.skip_event(session, receipt.id, "skip-1", None, "reviewed")
        sub = session.get(EventSubscription, world["sub"])
        sub.desired_state = "disabled"
        sub.deleted_at = database_now(session)
        session.commit()
        service.purge_deleted_adapter_history(session, world["adapter"])
        session.commit()
        quota = session.get(SubscriptionQuota, "global")
        assert (
            quota.pending_count
            == quota.history_count
            == quota.history_bytes
            == quota.dedupe_count
            == 0
        )
        assert session.get(EventSubscription, world["sub"]) is None


def test_gc_pages_do_not_stall_on_expired_payload_shells(world, session_factory, monkeypatch):
    monkeypatch.setattr(
        settings,
        "ingress_capacity_profile",
        '{"global":{"rate":1000},"subscription_max":{"rate":1000}}',
    )
    with session_factory() as session:
        config = session.get(SubscriptionConfigVersion, world["config"])
        config.config = {"limits": {"rate": 1000}}
        for offset in range(101):
            receipt = receive(session, world, offset)
            service.skip_event(session, receipt.id, f"skip-{offset}", None, "reviewed")
            receipt.terminal_at = database_now(session) - timedelta(days=8)
        session.commit()
        assert service.cleanup_history(session, world["sub"])["payloads_expired"] == 100
        session.commit()
        assert service.cleanup_history(session, world["sub"])["payloads_expired"] == 1
        session.commit()
        assert service.cleanup_history(session, world["sub"])["payloads_expired"] == 0
        assert session.get(SubscriptionQuota, "global").history_count == 0
        assert session.get(SubscriptionQuota, "global").dedupe_count == 101


def test_gc_filters_protected_batch_without_blocking_later_receipts(world, session_factory):
    from dlr.control.models import SyncBatch

    with session_factory() as session:
        first = receive(session, world)
        second = receive(session, world, 1)
        for receipt in (first, second):
            service.skip_event(session, receipt.id, f"skip-{receipt.id}", None, "reviewed")
            receipt.terminal_at = database_now(session) - timedelta(days=8)
        batch = SyncBatch(subscription_id=world["sub"], scope_fingerprint="scope", state="reading")
        session.add(batch)
        session.flush()
        first.batch_id = batch.id
        session.commit()
        assert service.cleanup_history(session, world["sub"])["payloads_expired"] == 1
        session.commit()
        assert session.get(EventReceipt, first.id).envelope is not None
        assert session.get(EventReceipt, second.id).envelope is None
        batch.state = "completed"
        session.commit()
        assert service.cleanup_history(session, world["sub"])["payloads_expired"] == 1


@pytest.mark.parametrize("ttl", [0, -1, 31])
def test_renew_lease_rejects_outside_fixed_budget(world, session_factory, ttl):
    with session_factory() as session:
        with pytest.raises(HTTPException) as error:
            service.renew_lease(session, world["sub"], "owner-a", world["generation"], ttl)
        assert error.value.status_code == 422


@pytest.mark.parametrize("has_other_pending", [False, True])
def test_current_configuration_replay_excludes_only_selected_failed_receipt(
    world, session_factory, has_other_pending
):
    with session_factory() as session:
        receipt = receive(session, world)
        if has_other_pending:
            receive(session, world, 1)
        session.commit()
        did = delivery_id(session, receipt)
        execution = service.dispatch_delivery(session, did)
        session.commit()
        execution.status = "dead_letter"
        session.commit()
        service.observe_terminal(session, did)
        sub = session.get(EventSubscription, world["sub"])
        sub.desired_state = "disabled"
        newer = AdapterVersion(adapter_id=world["adapter"], seq=2, code="newer", requirements="")
        replacement = Worker(
            name="replacement",
            status="online",
            capabilities=["python"],
            protocol_version=3,
            rabbitmq_execution_v3=True,
        )
        session.add_all([newer, replacement])
        session.flush()
        adapter = session.get(Adapter, world["adapter"])
        adapter.latest_version_id, adapter.runtime_worker_id = newer.id, replacement.id
        session.commit()
        if has_other_pending:
            with pytest.raises(HTTPException) as error:
                service.replay_event(
                    session,
                    receipt.id,
                    "current-fix",
                    None,
                    "current config",
                    use_current_configuration=True,
                )
            assert error.value.status_code == 409
        else:
            replay = service.replay_event(
                session,
                receipt.id,
                "current-fix",
                None,
                "current config",
                use_current_configuration=True,
            )
            session.commit()
            fixed = service.dispatch_delivery(session, replay.id)
            assert fixed.version_id == newer.id and fixed.target_worker_id == replacement.id
            assert session.get(Execution, execution.id).status == "dead_letter"
            assert (
                session.get(EventReceipt, receipt.id).activation_id != replay.activation_override_id
            )


def test_explicit_quota_repair_can_restore_pending_zero_and_drain(world, session_factory):
    from sqlalchemy.exc import IntegrityError

    with session_factory() as session:
        receipt = receive(session, world)
        session.commit()
        scope = session.get(SubscriptionQuota, f"sub:{world['sub']}")
        scope.pending_count = 0  # Independently corrupt local counter; global is already correct.
        session.commit()
        with pytest.raises(IntegrityError):
            service.dispatch_delivery(session, delivery_id(session, receipt))
        session.rollback()
        report = service.audit_quota(session, world["sub"])
        assert report["differences"]["pending_count"] == {"actual": 0, "expected": 1}
        assert session.get(SubscriptionQuota, f"sub:{world['sub']}").pending_count == 0
        sub = session.get(EventSubscription, world["sub"])
        sub.desired_state = "disabled"
        sub.connection_state = (
            "stopped"  # B1 has no native listener; manager verifies ACK separately.
        )
        session.commit()
        assert service.audit_quota(session, sub.id, repair=True)["repaired"]
        session.commit()
        assert service.audit_quota(session, sub.id)["differences"] == {}
        assert service.audit_global_quota(session)["differences"] == {}
        execution = service.dispatch_delivery(session, delivery_id(session, receipt))
        session.commit()
        assert execution is not None
        assert session.get(SubscriptionQuota, "global").pending_count == 0


def test_standalone_disposition_budget_and_thirty_day_gc(world, session_factory):
    from dlr.control.models import DeliveryDisposition

    with session_factory() as session:
        receive(session, world)
        session.commit()
        service.record_rejection(
            session,
            world["sub"],
            "owner-a",
            world["generation"],
            "test:0",
            "event_payload_too_large",
            "poison",
            {"offset": "1"},
            size=999999,
        )
        session.commit()
        disposition = service.skip_unaccepted(
            session,
            world["sub"],
            "test:0",
            "poison-skip",
            None,
            "explicit omission",
            "poison",
            {"offset": "2"},
        )
        session.commit()
        assert service.audit_quota(session, world["sub"])["differences"] == {}
        assert session.get(SubscriptionQuota, "global").dedupe_count == 2
        disposition.created_at = database_now(session) - timedelta(days=31)
        session.commit()
        service.cleanup_history(session, world["sub"])
        session.commit()
        assert session.scalar(select(func.count()).select_from(DeliveryDisposition)) == 0
        assert session.get(SubscriptionQuota, "global").dedupe_count == 1
        assert service.audit_quota(session, world["sub"])["differences"] == {}
