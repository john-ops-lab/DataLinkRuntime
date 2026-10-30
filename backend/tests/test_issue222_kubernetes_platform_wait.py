"""Real PostgreSQL diagnostics for cancellable Kubernetes custody waits."""

from copy import deepcopy
from dataclasses import replace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from dlr.control.models import (
    EventDelivery,
    EventReceipt,
    EventSubscription,
    Execution,
    ExecutionOutbox,
    SourceCheckpoint,
    SubscriptionConfigVersion,
    SubscriptionLease,
    SubscriptionQuota,
    SyncBatch,
)
from dlr.control.services import ingress_capacity
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_kubernetes as kubernetes
from dlr.control.services import subscription_sync as sync
from dlr.control.services.subscription_source import SourceFailure
from test_issue219_delivery import world  # noqa: F401
from test_issue219_sync import begin, execute, object_envelope
from test_issue219_sync import kube as kube_fixture

kube = kube_fixture

CODES = ("ingress_capacity_unavailable", "ingress_disk_low", "ingress_disk_unavailable")


def responsibility_snapshot(session):
    result = {}
    for model in (
        EventReceipt,
        EventDelivery,
        Execution,
        ExecutionOutbox,
        SourceCheckpoint,
        SubscriptionLease,
        SubscriptionQuota,
        SyncBatch,
    ):
        table = model.__table__
        result[table.name] = deepcopy(
            [
                dict(row)
                for row in session.execute(select(table).order_by(*table.primary_key)).mappings()
            ]
        )
    return result


@pytest.mark.parametrize("code", CODES)
@pytest.mark.parametrize(
    "existing_code", [None, "subscription_capacity_exceeded", "event_content_invalid"]
)
def test_platform_wait_preserves_frozen_ready_responsibility(
    kube, monkeypatch, code, existing_code
):
    with kube.session_factory() as session:
        ref = begin(session, kube)
        batch = session.get(SyncBatch, ref.batch_id)
        execute(session, batch.started_receipt_id)
        sync.finish_list_page(
            session, kube, ref, 0, "collection-rv", [], expected_item_count=0, last_page=True
        )
        session.commit()
        sync.maintain_batch(session, ref.batch_id)
        session.commit()
        assert batch.ready_kind == "sync.completed" and batch.completed_receipt_id is None
        if existing_code:
            checkpoint = session.scalar(select(SourceCheckpoint))
            checkpoint.blocked_unaccepted = {
                "code": existing_code,
                "position": dict(checkpoint.position),
            }
            session.get(EventSubscription, kube.subscription_id).last_error_code = existing_code
            session.commit()
        before = responsibility_snapshot(session)

    def unavailable(*args, **kwargs):
        raise HTTPException(503, detail={"code": code})

    waits = []

    def stop_after_wait(timeout):
        waits.append(timeout)
        kube.stop_event.set()
        return True

    monkeypatch.setattr(sync, "receive_watch_event", unavailable)
    monkeypatch.setattr(kube.stop_event, "wait", stop_after_wait)
    with pytest.raises(SourceFailure) as stopped:
        kubernetes.KubernetesSourceRunner()._receive(
            kube, object_envelope(kube, watch_type="ADDED")
        )
    assert stopped.value.code == "source_stopped" and waits == [0.25]
    with kube.session_factory() as session:
        assert responsibility_snapshot(session) == before
        assert session.get(EventSubscription, kube.subscription_id).last_error_code == (
            existing_code or code
        )
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}
    assert kube.buffer.used_bytes == 0


@pytest.mark.parametrize("code", CODES)
@pytest.mark.parametrize("source_phase", ["list", "watch"])
def test_platform_wait_recovers_same_original_without_false_checkpoint(
    kube, monkeypatch, code, source_phase
):
    ref = None
    if source_phase == "list":
        with kube.session_factory() as session:
            ref = begin(session, kube)
            execute(session, session.get(SyncBatch, ref.batch_id).started_receipt_id)
    envelope = object_envelope(kube, ref, watch_type="LIST" if ref else "ADDED")
    with kube.session_factory() as session:
        before = responsibility_snapshot(session)
    target = (
        "require_active_profile"
        if code == "ingress_capacity_unavailable"
        else "require_disk_capacity"
    )
    original_guard = getattr(ingress_capacity, target)
    guard_calls = []

    def temporarily_unavailable(*args, **kwargs):
        guard_calls.append(True)
        if len(guard_calls) == 1:
            raise HTTPException(503, detail={"code": code})
        return original_guard(*args, **kwargs)

    waits = []

    def recover_after_wait(timeout):
        waits.append(timeout)
        with kube.session_factory() as session:
            # Failed custody rolled back, including any provisional checkpoint.
            assert responsibility_snapshot(session) == before
            assert session.get(EventSubscription, kube.subscription_id).last_error_code == code
        return False

    monkeypatch.setattr(ingress_capacity, target, temporarily_unavailable)
    monkeypatch.setattr(kube.stop_event, "wait", recover_after_wait)
    result = kubernetes.KubernetesSourceRunner()._receive(
        kube, envelope, ref, collection_rv="collection-rv"
    )
    assert not result.duplicate and waits == [0.25] and len(guard_calls) == 2
    with kube.session_factory() as session:
        receipt = session.get(EventReceipt, result.receipt_id)
        assert receipt.envelope == envelope and receipt.source_phase == source_phase
        assert session.get(EventSubscription, kube.subscription_id).last_error_code is None
        checkpoint = session.scalar(select(SourceCheckpoint))
        assert checkpoint.blocked_unaccepted is None
        assert checkpoint.next_sequence == receipt.sequence + 1
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}
    assert kube.buffer.used_bytes == 0


def test_true_quota_wait_keeps_original_capacity_marker_and_default_call(kube, monkeypatch):
    with kube.session_factory() as session:
        accepted = sync.receive_watch_event(
            session, kube, object_envelope(kube, watch_type="ADDED")
        )
        session.commit()
        config = session.scalar(select(SubscriptionConfigVersion))
        config.config = {**config.config, "limits": {"pending_count": 1}}
        session.commit()
        quota_before = responsibility_snapshot(session)["subscription_quotas"]

    waits = []

    def stop_after_wait(timeout):
        waits.append(timeout)
        kube.stop_event.set()
        return True

    monkeypatch.setattr(kube.stop_event, "wait", stop_after_wait)
    with pytest.raises(SourceFailure, match="source_stopped"):
        kubernetes.KubernetesSourceRunner()._receive(
            kube, object_envelope(kube, rv="next-rv", watch_type="MODIFIED")
        )
    assert waits == [0.25]
    with kube.session_factory() as session:
        # The old two-argument API remains the quota-only operation.
        sync.mark_sync_backpressure(session, kube)
        session.commit()
        checkpoint = session.scalar(select(SourceCheckpoint))
        assert checkpoint.blocked_unaccepted == {
            "code": "subscription_capacity_exceeded",
            "position": {"resource_version": "item-rv"},
        }
        assert session.get(SubscriptionQuota, f"sub:{kube.subscription_id}").blocked_reason == (
            "subscription_capacity_exceeded"
        )
        after = responsibility_snapshot(session)["subscription_quotas"]
        for rows in (quota_before, after):
            for row in rows:
                row.pop("blocked_reason")
        assert after == quota_before
        assert session.scalars(select(EventReceipt.id)).all() == [accepted.receipt_id]
        assert delivery.audit_quota(session, kube.subscription_id)["differences"] == {}


def test_platform_hint_is_fenced_and_other_codes_are_rejected(kube):
    with kube.session_factory() as session:
        before = responsibility_snapshot(session)
        with pytest.raises(HTTPException) as stale:
            sync.mark_sync_backpressure(
                session, replace(kube, generation=kube.generation + 1), "ingress_disk_low"
            )
        assert stale.value.detail["code"] == "subscription_owner_stale"
        session.rollback()
        assert responsibility_snapshot(session) == before
        assert session.get(EventSubscription, kube.subscription_id).last_error_code is None
        with pytest.raises(ValueError, match="Unsupported sync backpressure code"):
            sync.mark_sync_backpressure(session, kube, "arbitrary_platform_failure")


@pytest.mark.parametrize(
    "existing_code", ["subscription_capacity_exceeded", "event_content_invalid"]
)
def test_success_does_not_clear_other_existing_diagnosis(kube, existing_code):
    with kube.session_factory() as session:
        session.get(EventSubscription, kube.subscription_id).last_error_code = existing_code
        session.commit()
        sync.receive_watch_event(session, kube, object_envelope(kube, watch_type="ADDED"))
        session.commit()
        assert session.get(EventSubscription, kube.subscription_id).last_error_code == existing_code
