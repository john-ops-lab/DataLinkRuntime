"""Real PostgreSQL batch qualification, prepaid controls and explicit settlement."""

import threading
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from dlr.common.config import settings
from dlr.control.models import (
    DeliveryDisposition,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    ExecutionOutbox,
    SourceCheckpoint,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionLease,
    SubscriptionQuota,
    SyncBatch,
)
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_sync as sync
from dlr.control.services.input_config import database_now
from dlr.control.services.subscription_kubernetes import build_envelope
from dlr.control.services.subscription_source import ApplicationBufferBudget, SourceRunContext
from test_issue219_delivery import world  # noqa: F401


@pytest.fixture
def kube(world, session_factory):  # noqa: F811
    cfg = {
        "source_type": "kubernetes",
        "cluster_id": "logical-cluster",
        "server": "https://cluster.example.test",
        "group": "",
        "version": "v1",
        "resource": "pods",
        "namespace": "default",
        "mode": "per_event",
    }
    identity = {
        "version": 1,
        "origin": "https://cluster.example.test",
        "trust_sha256": "a" * 64,
        "scope_fingerprint": "b" * 64,
    }
    with session_factory.begin() as session:
        sub = session.get(EventSubscription, world["sub"])
        sub.source_type = "kubernetes"
        config = session.get(SubscriptionConfigVersion, world["config"])
        config.config = cfg
        config.source_identity = identity
        activation_id = sub.activation_id
    return SourceRunContext(
        world["sub"],
        "owner-a",
        world["generation"],
        activation_id,
        1,
        "epoch-1",
        cfg,
        {},
        threading.Event(),
        session_factory,
        ApplicationBufferBudget().scope(),
        lambda: None,
        identity,
    )


def object_envelope(context, ref=None, rv="item-rv", uid="uid-1", watch_type="LIST", value=1):
    obj = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "uid": uid,
            "name": "pod-" + uid,
            "namespace": "default",
            "resourceVersion": rv,
        },
        "spec": {"value": value},
    }
    association = (
        None
        if ref is None
        else {
            "batch_id": str(ref.batch_id),
            "scope_fingerprint": ref.scope_fingerprint,
            "phase": "list" if watch_type == "LIST" else "watch",
            "read_complete": False,
            "item_count": 0,
            "failure_code": None,
        }
    )
    return build_envelope(context, obj, watch_type, sync=association)


def begin(session, context):
    ref = sync.begin_sync_batch(session, context, context.identity_snapshot)
    session.commit()
    assert sync.materialize_ready_control(
        session, sync.select_ready_control_candidates(session)[0]
    ).changed
    session.commit()
    return ref


def latest(session, rid):
    return session.scalar(
        select(EventDelivery)
        .where(EventDelivery.receipt_id == rid)
        .order_by(EventDelivery.ordinal.desc())
        .limit(1)
    )


def execute(session, rid, status="succeeded"):
    item = latest(session, rid)
    execution = delivery.dispatch_delivery(session, item.id)
    assert execution is not None
    session.commit()
    execution.status = status
    session.commit()
    delivery.observe_terminal(session, item.id)
    session.commit()
    return execution


def ready_completed(session, context, *, empty=False):
    ref = begin(session, context)
    batch = session.get(SyncBatch, ref.batch_id)
    execute(session, batch.started_receipt_id)
    ids = []
    if not empty:
        item = sync.receive_list_item(
            session, context, ref, object_envelope(context, ref), 0, "collection-rv"
        )
        session.commit()
        ids.append(item.receipt_id)
    sync.finish_list_page(
        session, context, ref, 0, "collection-rv", ids, expected_item_count=len(ids), last_page=True
    )
    session.commit()
    for rid in ids:
        execute(session, rid)
    sync.maintain_batch(session, ref.batch_id)
    session.commit()
    ticket = sync.select_ready_control_candidates(session)[0]
    assert ticket.kind == "sync.completed"
    sync.materialize_ready_control(session, ticket)
    session.commit()
    return ref, session.get(SyncBatch, ref.batch_id).completed_receipt_id


def stop(session, context):
    sub = session.get(EventSubscription, context.subscription_id)
    sub.desired_state, sub.connection_state = "disabled", "stopped"
    lease = session.get(SubscriptionLease, sub.id)
    lease.owner_id, lease.closing_generation = "", None
    session.commit()


def abandon_args(session, context, ref, rid, request_id="abandon-1"):
    item = latest(session, rid)
    return dict(
        batch_id=ref.batch_id,
        source_epoch=context.source_epoch,
        receipt_id=rid,
        expected_config_version=1,
        expected_latest_delivery_id=item.id,
        expected_delivery_ordinal=item.ordinal,
        expected_execution_id=item.execution_id,
        request_id=request_id,
        actor_id=None,
        reason="Confirm manual exception after checking target",
        confirm_impact=True,
    )


def test_page_proof_item_commit_and_opaque_collection_rv(kube, session_factory):
    with session_factory() as session:
        ref = begin(session, kube)
        env = object_envelope(kube, ref)
        first = sync.receive_list_item(session, kube, ref, env, 0, "collection-rv")
        session.rollback()
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 1
        accepted = sync.receive_list_item(session, kube, ref, env, 0, "collection-rv")
        session.commit()
        again = sync.receive_list_item(session, kube, ref, env, 0, "collection-rv")
        assert again.duplicate and again.receipt_id == accepted.receipt_id
        session.commit()
        with pytest.raises(HTTPException):
            sync.finish_list_page(
                session, kube, ref, 0, "collection-rv", [], expected_item_count=0, last_page=True
            )
        session.rollback()
        sync.finish_list_page(
            session,
            kube,
            ref,
            0,
            "collection-rv",
            [accepted.receipt_id],
            expected_item_count=1,
            last_page=True,
        )
        session.commit()
        cp = session.scalar(select(SourceCheckpoint))
        assert cp.position == {"resource_version": "collection-rv"}
        assert cp.position != env["source"]["position"]
        assert sync.advance_bookmark(session, kube, "bookmark-z", cp.next_sequence - 2) is False
        assert sync.advance_bookmark(session, kube, "bookmark-a", cp.next_sequence - 1) is True
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}
        assert first.receipt_id != accepted.receipt_id


def test_normal_completion_freezes_atomically_and_opens_watch_only_on_success(
    kube, session_factory
):
    with session_factory() as session:
        ref, rid = ready_completed(session, kube)
        watch = sync.receive_watch_event(
            session, kube, object_envelope(kube, ref, "watch-rv", watch_type="MODIFIED")
        )
        session.commit()
        assert delivery.dispatch_delivery(session, latest(session, watch.receipt_id).id) is None
        completed = delivery.dispatch_delivery(session, latest(session, rid).id)
        assert completed is not None
        session.rollback()
        assert not session.get(SyncBatch, ref.batch_id).completion_committed
        assert latest(session, rid).execution_id is None
        execute(session, rid)
        batch = session.get(SyncBatch, ref.batch_id)
        assert batch.completion_committed and batch.drain_open and batch.finished_at is not None
        assert batch.reserved_control_count == 0
        assert delivery.dispatch_delivery(session, latest(session, watch.receipt_id).id) is not None
        session.commit()
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}


def test_empty_interrupted_batch_keeps_lock_featureoff_materializes_failed(
    kube, session_factory, monkeypatch
):
    with session_factory() as session:
        ref = sync.begin_sync_batch(session, kube, kube.identity_snapshot)
        session.commit()
        assert delivery.has_unfinished(session, kube.subscription_id)
        monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
        stop(session, kube)
        sync.maintain_batch(session, ref.batch_id)
        session.commit()
        ticket = sync.select_ready_control_candidates(session)[0]
        assert ticket.kind == "sync.failed"
        sync.materialize_ready_control(session, ticket)
        session.commit()
        batch = session.get(SyncBatch, ref.batch_id)
        assert batch.failed_receipt_id and batch.completed_receipt_id is None
        execute(session, batch.failed_receipt_id)
        assert not delivery.has_unfinished(session, kube.subscription_id)
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}


def test_tight_four_capacity_abandon_uses_prepaid_audit_and_never_reexecutes(kube, session_factory):
    with session_factory() as session:
        cfg = session.scalar(select(SubscriptionConfigVersion))
        cfg.config = {**cfg.config, "limits": {"history_count": 4}}
        session.commit()
        ref, rid = ready_completed(session, kube)
        original = execute(session, rid, "dead_letter")
        stop(session, kube)
        args = abandon_args(session, kube, ref, rid)
        quota = session.get(SubscriptionQuota, f"sub:{kube.subscription_id}")
        assert quota.history_count == 3 and quota.reserved_control_count == 1
        result = sync.abandon_completion(session, kube.subscription_id, **args)
        session.commit()
        assert quota.history_count == 4 and quota.reserved_control_count == 0
        assert (
            original.status == "dead_letter" and session.get(EventReceipt, rid).state == "abandoned"
        )
        assert session.get(SyncBatch, ref.batch_id).completion_committed
        assert not delivery.has_unfinished(session, kube.subscription_id)
        sub = session.get(EventSubscription, kube.subscription_id)
        sub.config_version = 2
        session.commit()
        assert (
            sync.abandon_completion(session, kube.subscription_id, **args).disposition_id
            == result.disposition_id
        )
        with pytest.raises(HTTPException):
            sync.abandon_completion(session, kube.subscription_id, **{**args, "reason": "changed"})
        session.rollback()
        with pytest.raises(HTTPException):
            delivery.replay_event(session, rid, "new-replay", None, "No stale cleanup")
        session.rollback()
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}


def test_pending_replay_abandon_binds_latest_and_cancels_only_noexec(kube, session_factory):
    with session_factory() as session:
        ref, rid = ready_completed(session, kube)
        execute(session, rid, "dead_letter")
        old_args = abandon_args(session, kube, ref, rid)
        replay = delivery.replay_event(
            session, rid, "replay-before-abandon", None, "Retry completion"
        )
        session.commit()
        stop(session, kube)
        with pytest.raises(HTTPException):
            sync.abandon_completion(session, kube.subscription_id, **old_args)
        session.rollback()
        args = abandon_args(session, kube, ref, rid)
        assert args["expected_execution_id"] is None
        result = sync.abandon_completion(session, kube.subscription_id, **args)
        session.commit()
        assert result.cancelled_pending_id == replay.id
        assert replay.state == "abandoned" and replay.execution_id is None
        before = session.scalar(select(func.count()).select_from(ExecutionOutbox))
        assert delivery.dispatch_delivery(session, replay.id) is None
        assert session.scalar(select(func.count()).select_from(ExecutionOutbox)) == before
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}


def test_gap_before_freeze_supersedes_completion_and_after_freeze_keeps_original(
    kube, session_factory
):
    with session_factory() as session:
        ref, rid = ready_completed(session, kube)
        sync.note_sync_gap(session, kube, "source_history_gap")
        session.commit()
        batch = session.get(SyncBatch, ref.batch_id)
        assert session.get(EventReceipt, rid).state == "superseded"
        assert latest(session, rid).execution_id is None
        assert delivery.dispatch_delivery(session, latest(session, rid).id) is None
        assert batch.ready_kind == "sync.failed" and not batch.completion_committed
        sync.materialize_ready_control(session, sync.select_ready_control_candidates(session)[0])
        session.commit()
        execute(session, batch.failed_receipt_id)
        assert batch.drain_open and not delivery.has_unfinished(session, kube.subscription_id)
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}


def test_gap_after_freeze_never_generates_failed_or_generic_skip(kube, session_factory):
    with session_factory() as session:
        ref, rid = ready_completed(session, kube, empty=True)
        execution = delivery.dispatch_delivery(session, latest(session, rid).id)
        session.commit()
        sync.note_sync_gap(session, kube, "source_history_gap")
        session.commit()
        batch = session.get(SyncBatch, ref.batch_id)
        assert batch.completion_committed and batch.failure_code is None
        assert batch.pending_gap["after_completion_committed"] is True
        assert batch.failed_receipt_id is None and batch.ready_kind is None
        execution.status = "dead_letter"
        session.commit()
        delivery.observe_terminal(session, latest(session, rid).id)
        session.commit()
        with pytest.raises(HTTPException):
            delivery.skip_event(session, rid, "no-generic-skip", None, "No false success")
        session.rollback()
        assert batch.failed_receipt_id is None


def test_member_failure_can_repair_but_skip_permanently_invalidates(kube, session_factory):
    with session_factory() as session:
        ref = begin(session, kube)
        batch = session.get(SyncBatch, ref.batch_id)
        execute(session, batch.started_receipt_id)
        received = sync.receive_list_item(
            session, kube, ref, object_envelope(kube, ref), 0, "collection-rv"
        )
        session.commit()
        sync.finish_list_page(
            session,
            kube,
            ref,
            0,
            "collection-rv",
            [received.receipt_id],
            expected_item_count=1,
            last_page=True,
        )
        session.commit()
        original = execute(session, received.receipt_id, "dead_letter")
        assert batch.failure_code is None and batch.ready_kind is None
        replay = delivery.replay_event(
            session, received.receipt_id, "member-repair", None, "Fixed target"
        )
        session.commit()
        repaired = delivery.dispatch_delivery(session, replay.id)
        session.commit()
        repaired.status = "succeeded"
        session.commit()
        delivery.observe_terminal(session, replay.id)
        session.commit()
        assert batch.ready_kind == "sync.completed" and original.status == "dead_letter"
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}


def test_failed_control_restores_original_member_blocker(kube, session_factory):
    with session_factory() as session:
        ref = begin(session, kube)
        batch = session.get(SyncBatch, ref.batch_id)
        execute(session, batch.started_receipt_id)
        received = sync.receive_list_item(
            session, kube, ref, object_envelope(kube, ref), 0, "collection-rv"
        )
        session.commit()
        execute(session, received.receipt_id, "dead_letter")
        sync.note_sync_gap(session, kube, "source_history_gap")
        session.commit()
        sync.materialize_ready_control(session, sync.select_ready_control_candidates(session)[0])
        session.commit()
        execute(session, batch.failed_receipt_id, "dead_letter")
        cp = session.scalar(select(SourceCheckpoint))
        assert cp.blocked_receipt_id == batch.failed_receipt_id
        delivery.skip_event(
            session,
            batch.failed_receipt_id,
            "skip-failed-control",
            None,
            "Close failed qualification",
        )
        session.commit()
        assert batch.drain_open and cp.blocked_receipt_id == received.receipt_id
        assert batch.failure_code == "source_history_gap" and not batch.completion_committed
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}


def test_state_sync_only_pending_watch_same_uid_preserves_history(kube, session_factory):
    with session_factory() as session:
        config = session.scalar(select(SubscriptionConfigVersion))
        config.config = {**config.config, "mode": "state_sync"}
        session.commit()
        ref, rid = ready_completed(session, kube, empty=True)
        execute(session, rid)
        first = sync.receive_watch_event(
            session, kube, object_envelope(kube, ref, "rv-1", watch_type="MODIFIED")
        )
        session.commit()
        old = session.get(EventReceipt, first.receipt_id)
        old_payload = old.envelope
        second = sync.receive_watch_event(
            session, kube, object_envelope(kube, ref, "rv-2", watch_type="MODIFIED", value=2)
        )
        session.commit()
        new = session.get(EventReceipt, second.receipt_id)
        assert old.state == "superseded" and old.envelope == old_payload
        assert old.superseded_by_id == new.id and new.coalesced_count == 1
        assert new.first_source_event_id == old.source_event_id
        assert new.first_received_at == old.first_received_at
        removed = sync.receive_watch_event(
            session, kube, object_envelope(kube, ref, "rv-3", watch_type="DELETED")
        )
        session.commit()
        assert new.state == "pending" and removed.receipt_id != new.id
        recreation = sync.receive_watch_event(
            session, kube, object_envelope(kube, ref, "rv-4", uid="new-uid", watch_type="ADDED")
        )
        session.commit()
        assert session.get(EventReceipt, removed.receipt_id).state == "pending"
        assert recreation.receipt_id != removed.receipt_id
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}


def test_control_uses_same_rate_and_prevents_ordinary_stealing_next_token(kube, session_factory):
    with session_factory() as session:
        config = session.scalar(select(SubscriptionConfigVersion))
        config.config = {**config.config, "limits": {"rate": 1}}
        session.commit()
        ref = sync.begin_sync_batch(session, kube, kube.identity_snapshot)
        session.commit()
        with pytest.raises(HTTPException):
            sync.receive_list_item(
                session, kube, ref, object_envelope(kube, ref), 0, "collection-rv"
            )
        session.rollback()
        ticket = sync.select_ready_control_candidates(session)[0]
        rows = delivery._quotas(session, kube.subscription_id)
        for quota in rows:
            quota.rate_window_at = database_now(session)
            quota.rate_count = 1 if quota.scope.startswith("sub:") else 50
        session.commit()
        assert not sync.materialize_ready_control(session, ticket).changed
        assert session.get(SyncBatch, ref.batch_id).reserved_control_count == 3
        for quota in rows:
            quota.rate_window_at = database_now(session) - timedelta(seconds=2)
        session.commit()
        assert sync.materialize_ready_control(session, ticket).changed
        session.commit()
        assert all(quota.rate_count == 1 for quota in rows)
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}


def test_stale_owner_cannot_advance_page_bookmark_or_gap(kube, session_factory):
    with session_factory() as session:
        ref = begin(session, kube)
        lease = session.get(SubscriptionLease, kube.subscription_id)
        lease.generation += 1
        session.commit()
        for mutate in (
            lambda: sync.finish_list_page(
                session, kube, ref, 0, "rv", [], expected_item_count=0, last_page=True
            ),
            lambda: sync.advance_bookmark(session, kube, "rv", 1),
            lambda: sync.note_sync_gap(session, kube, "source_history_gap"),
            lambda: sync.mark_sync_backpressure(session, kube),
        ):
            with pytest.raises(HTTPException):
                mutate()
            session.rollback()
        assert not session.get(SyncBatch, ref.batch_id).read_complete
        sync.maintain_batch(session, ref.batch_id)
        session.commit()
        assert session.get(SyncBatch, ref.batch_id).failure_code == "source_list_interrupted"


def test_abandon_rejects_actual_success_even_when_projection_is_stale(kube, session_factory):
    with session_factory() as session:
        ref, rid = ready_completed(session, kube, empty=True)
        execution = delivery.dispatch_delivery(session, latest(session, rid).id)
        session.commit()
        execution.status = "succeeded"
        session.commit()
        stop(session, kube)
        args = abandon_args(session, kube, ref, rid)
        with pytest.raises(HTTPException):
            sync.abandon_completion(session, kube.subscription_id, **args)
        session.rollback()
        assert not session.get(SyncBatch, ref.batch_id).completion_abandoned
        assert session.scalar(select(func.count()).select_from(DeliveryDisposition)) == 0


def test_quota_rebuild_includes_reserve_and_abandon_audit_and_gc_refs(kube, session_factory):
    with session_factory() as session:
        ref, rid = ready_completed(session, kube)
        execute(session, rid, "dead_letter")
        stop(session, kube)
        sync.abandon_completion(
            session, kube.subscription_id, **abandon_args(session, kube, ref, rid)
        )
        session.commit()
        quota = session.get(SubscriptionQuota, f"sub:{kube.subscription_id}")
        expected = delivery.audit_quota(session, kube.subscription_id)["expected"]
        quota.history_count = 0
        quota.reserved_control_count = 1
        quota.reserved_control_bytes = sync.CONTROL_BYTES
        session.commit()
        repaired = delivery.audit_quota(session, kube.subscription_id, repair=True)
        session.commit()
        assert repaired["expected"] == expected
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}
        batch = session.get(SyncBatch, ref.batch_id)
        activation = session.get(SubscriptionActivation, kube.activation_id)
        activation.created_at = database_now(session) - timedelta(days=40)
        sub = session.get(EventSubscription, kube.subscription_id)
        sub.activation_id = None
        session.commit()
        delivery.cleanup_history(session, kube.subscription_id)
        session.commit()
        assert session.get(SubscriptionActivation, activation.id) is not None
        assert session.get(SyncBatch, batch.id).completion_committed


def test_full_capacity_history_failure_leaves_control_reserve_and_bounded_failure(
    kube, session_factory
):
    with session_factory() as session:
        config = session.scalar(select(SubscriptionConfigVersion))
        config.config = {**config.config, "limits": {"history_count": 4}}
        session.commit()
        ref = begin(session, kube)
        one = sync.receive_list_item(
            session, kube, ref, object_envelope(kube, ref), 0, "collection-rv"
        )
        session.commit()
        with pytest.raises(HTTPException) as caught:
            sync.receive_list_item(
                session, kube, ref, object_envelope(kube, ref, uid="uid-2"), 0, "collection-rv"
            )
        assert caught.value.detail["code"] == "subscription_sync_history_capacity"
        session.rollback()
        sync.note_sync_gap(session, kube, "source_list_history_capacity")
        session.commit()
        sync.materialize_ready_control(session, sync.select_ready_control_candidates(session)[0])
        session.commit()
        batch = session.get(SyncBatch, ref.batch_id)
        assert batch.item_count == 1 and batch.failed_receipt_id is not None
        assert session.get(EventReceipt, one.receipt_id).state == "pending"
        # Unexecuted started is superseded on invalidation, so failed may close
        # qualification; the accepted ordinary item remains real responsibility.
        execute(session, batch.failed_receipt_id)
        assert batch.drain_open and delivery.has_unfinished(session, kube.subscription_id)
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}


def test_batch_and_abandon_audit_thirty_day_gc_release_once(kube, session_factory):
    with session_factory() as session:
        ref, rid = ready_completed(session, kube)
        execute(session, rid, "dead_letter")
        stop(session, kube)
        sync.abandon_completion(
            session, kube.subscription_id, **abandon_args(session, kube, ref, rid)
        )
        session.commit()
        old = database_now(session) - timedelta(days=40)
        for receipt in session.scalars(select(EventReceipt)):
            receipt.terminal_at = old
        for audit in session.scalars(select(DeliveryDisposition)):
            audit.created_at = old
        batch = session.get(SyncBatch, ref.batch_id)
        batch.finished_at = old
        activation = session.get(SubscriptionActivation, kube.activation_id)
        activation.created_at = old
        session.get(EventSubscription, kube.subscription_id).activation_id = None
        session.commit()
        cleaned = delivery.cleanup_history(session, kube.subscription_id)
        session.commit()
        assert cleaned["metadata_deleted"] == 3
        assert session.get(SyncBatch, ref.batch_id) is None
        assert session.scalar(select(func.count()).select_from(DeliveryDisposition)) == 0
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}
        assert delivery.audit_global_quota(session)["differences"] == {}
        delivery.cleanup_history(session, kube.subscription_id)
        session.commit()
        assert session.get(SubscriptionActivation, kube.activation_id) is None
        quota = session.get(SubscriptionQuota, f"sub:{kube.subscription_id}")
        assert quota.history_count == quota.history_bytes == quota.dedupe_count == 0


@pytest.mark.parametrize("operation", ["gap", "abandon"])
def test_t2_winning_adapter_lock_fences_late_invalidation_and_abandon(
    kube, session_factory, monkeypatch, operation
):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError

    from dlr.control.services import reliable_execution

    with session_factory() as session:
        ref, rid = ready_completed(session, kube, empty=True)
        if operation == "abandon":
            execute(session, rid, "dead_letter")
            delivery.replay_event(session, rid, "race-replay", None, "Retry failed completion")
            session.commit()
            stop(session, kube)
        args = abandon_args(session, kube, ref, rid)
        did = latest(session, rid).id
    entered, release, challenger_started = threading.Event(), threading.Event(), threading.Event()
    actual_accept = reliable_execution.accept_execution

    def gated(*args, **kwargs):
        execution = actual_accept(*args, **kwargs)
        entered.set()
        assert release.wait(10)
        return execution

    monkeypatch.setattr(reliable_execution, "accept_execution", gated)

    def t2():
        with session_factory.begin() as session:
            return delivery.dispatch_delivery(session, did).id

    def challenge():
        challenger_started.set()
        try:
            with session_factory.begin() as session:
                if operation == "gap":
                    sync.note_sync_gap(session, kube, "source_history_gap")
                else:
                    sync.abandon_completion(session, kube.subscription_id, **args)
            return "committed"
        except HTTPException:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(t2)
        assert entered.wait(10)
        challenger = pool.submit(challenge)
        assert challenger_started.wait(10)
        with pytest.raises(TimeoutError):
            challenger.result(timeout=0.1)
        release.set()
        execution_id = winner.result(timeout=10)
        assert challenger.result(timeout=10) == ("committed" if operation == "gap" else "conflict")
    with session_factory() as session:
        batch = session.get(SyncBatch, ref.batch_id)
        assert batch.completion_committed and not batch.completion_abandoned
        assert latest(session, rid).execution_id == execution_id
        assert batch.failed_receipt_id is None
        if operation == "gap":
            assert batch.pending_gap["after_completion_committed"]
        else:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(DeliveryDisposition)
                    .where(DeliveryDisposition.action == "abandon_completion")
                )
                == 0
            )
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}


def test_successfully_closed_completed_rejects_new_replay_but_prior_reads_back(
    kube, session_factory, monkeypatch
):
    with session_factory() as session:
        ref, rid = ready_completed(session, kube, empty=True)
        execute(session, rid, "dead_letter")
        replay = delivery.replay_event(
            session,
            rid,
            "approved-retry",
            None,
            "Retry unconfirmed completion",
            expected_config_version=1,
        )
        session.commit()
        execute(session, rid)
        assert session.get(SyncBatch, ref.batch_id).state == "completed"
        stop(session, kube)
        before = session.scalar(select(func.count()).select_from(EventDelivery))
        with pytest.raises(HTTPException) as caught:
            delivery.replay_event(
                session,
                rid,
                "new-closed-replay",
                None,
                "Use a new full batch",
                expected_config_version=1,
            )
        assert caught.value.detail["code"] == "subscription_sync_completion_conflict"
        session.rollback()
        sub = session.get(EventSubscription, kube.subscription_id)
        sub.config_version = 2
        session.commit()
        monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
        assert (
            delivery.replay_event(
                session,
                rid,
                "approved-retry",
                None,
                "Retry unconfirmed completion",
                expected_config_version=1,
            ).id
            == replay.id
        )
        assert session.scalar(select(func.count()).select_from(EventDelivery)) == before
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}


def test_skipped_list_member_never_regains_completion_qualification_after_replay(
    kube, session_factory
):
    with session_factory() as session:
        ref = begin(session, kube)
        batch = session.get(SyncBatch, ref.batch_id)
        execute(session, batch.started_receipt_id)
        received = sync.receive_list_item(
            session, kube, ref, object_envelope(kube, ref), 0, "collection-rv"
        )
        session.commit()
        sync.finish_list_page(
            session,
            kube,
            ref,
            0,
            "collection-rv",
            [received.receipt_id],
            expected_item_count=1,
            last_page=True,
        )
        session.commit()
        delivery.skip_event(
            session, received.receipt_id, "skip-member", None, "Explicit unprocessed member"
        )
        session.commit()
        assert batch.failure_code == "source_member_skipped" and batch.ready_kind == "sync.failed"
        sync.materialize_ready_control(session, sync.select_ready_control_candidates(session)[0])
        session.commit()
        execute(session, batch.failed_receipt_id)
        stop(session, kube)
        replay = delivery.replay_event(
            session,
            received.receipt_id,
            "historical-member-repair",
            None,
            "Repair business without cleanup qualification",
        )
        session.commit()
        execute(session, received.receipt_id)
        assert replay.state == "succeeded"
        assert batch.failure_code == "source_member_skipped"
        assert batch.completed_receipt_id is None and not batch.completion_committed
        assert batch.ready_kind is None
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}


def test_state_sync_keeps_executed_watch_pending_replay_separate(kube, session_factory):
    with session_factory() as session:
        config = session.scalar(select(SubscriptionConfigVersion))
        config.config = {**config.config, "mode": "state_sync"}
        session.commit()
        ref, completed_id = ready_completed(session, kube, empty=True)
        execute(session, completed_id)
        accepted = sync.receive_watch_event(
            session, kube, object_envelope(kube, ref, "watch-1", watch_type="MODIFIED")
        )
        session.commit()
        original = execute(session, accepted.receipt_id, "dead_letter")
        replay = delivery.replay_event(
            session, accepted.receipt_id, "explicit-watch-retry", None, "Repair executed watch"
        )
        session.commit()
        old = session.get(EventReceipt, accepted.receipt_id)
        original_envelope = old.envelope
        successor = sync.receive_watch_event(
            session, kube, object_envelope(kube, ref, "watch-2", watch_type="MODIFIED", value=2)
        )
        session.commit()
        new = session.get(EventReceipt, successor.receipt_id)
        assert old.state == "pending" and replay.state == "pending"
        assert replay.execution_id is None and old.superseded_by_id is None
        assert old.envelope == original_envelope and original.status == "dead_letter"
        assert new.id != old.id and new.coalesced_count == 0
        assert latest(session, new.id).state == "pending"
        quota = session.get(SubscriptionQuota, f"sub:{kube.subscription_id}")
        assert quota.pending_count == 2
        assert quota.pending_bytes == old.payload_bytes + new.payload_bytes
        assert quota.history_count == 5  # started/completed/original/replay/new
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}


@pytest.mark.parametrize("materialized", [False, True])
def test_started_latest_success_cannot_be_replayed_between_ready_and_t2(
    kube, session_factory, materialized
):
    with session_factory() as session:
        ref = begin(session, kube)
        batch = session.get(SyncBatch, ref.batch_id)
        execute(session, batch.started_receipt_id)
        sync.finish_list_page(
            session, kube, ref, 0, "collection-rv", [], expected_item_count=0, last_page=True
        )
        sync.maintain_batch(session, ref.batch_id)
        session.commit()
        assert batch.ready_kind == "sync.completed"
        if materialized:
            sync.materialize_ready_control(
                session, sync.select_ready_control_candidates(session)[0]
            )
            session.commit()
        before = session.scalar(select(func.count()).select_from(EventDelivery))
        with pytest.raises(HTTPException) as caught:
            delivery.replay_event(
                session,
                batch.started_receipt_id,
                "late-started-replay",
                None,
                "Cannot reopen started during completion",
            )
        assert caught.value.detail["code"] == "subscription_draining"
        session.rollback()
        assert session.scalar(select(func.count()).select_from(EventDelivery)) == before
        if not materialized:
            sync.materialize_ready_control(
                session, sync.select_ready_control_candidates(session)[0]
            )
            session.commit()
        assert (
            delivery.dispatch_delivery(session, latest(session, batch.completed_receipt_id).id)
            is not None
        )
        session.rollback()
