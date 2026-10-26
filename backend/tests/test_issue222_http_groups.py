"""HTTP cohort isolation, real commit ambiguity and shared-waiter cancellation."""

import asyncio
import hashlib
import threading
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from dlr.common.config import settings
from dlr.control.api import ingress
from dlr.control.models import (
    AdapterVersion,
    EventDelivery,
    EventReceipt,
    SubscriptionConfigVersion,
    SubscriptionQuota,
)
from dlr.control.services import http_ingress as service
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services.http_ingress_group import HttpInput
from dlr.control.services.subscription_source import ApplicationBufferBudget
from test_http_ingress_limits import request
from test_issue222_http_ingress import http_world as foundation_http_world
from test_issue222_http_ingress import world as foundation_world

http_world = foundation_http_world
world = foundation_world


@pytest.fixture
def group_world(http_world, session_factory):
    with session_factory.begin() as session:
        config = session.get(SubscriptionConfigVersion, http_world["config"])
        config.config = config.config | {"mode": "business_batch"}
        revision = session.get(AdapterVersion, http_world["revision"])
        revision.runtime_config = {"event_batch_schema": "dlr.event.batch.v1"}
    return http_world


def item(key="same", body=b"{}", token="source-token"):
    return service.HttpReception("Bearer " + token, key, body)


def accept(session_factory, group_world, items):
    with session_factory.begin() as session:
        return service.receive_group(session, group_world["public_id"], items)


def test_group_auth_json_and_conflict_are_independent(group_world, session_factory):
    result = accept(
        session_factory,
        group_world,
        [
            item("invalid", token="wrong"),
            item("broken", b"{"),
            item(),
            item(),
        ],
    )
    assert [r.status_code if isinstance(r, HTTPException) else r[0] for r in result] == [
        401,
        422,
        202,
        200,
    ]
    assert result[2][1]["receipt_id"] == result[3][1]["receipt_id"]
    result = accept(session_factory, group_world, [item(body=b"[]"), item("new")])
    assert result[0].status_code == 409 and result[1][0] == 202
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 2
        assert delivery.audit_quota(session, group_world["sub"])["differences"] == {}


def test_group_disabled_platform_keeps_authenticated_duplicate_readback(
    group_world, session_factory, monkeypatch
):
    first = accept(session_factory, group_world, [item()])[0][1]["receipt_id"]
    monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
    result = accept(session_factory, group_world, [item(), item("new")])
    assert result[0][0] == 200 and result[0][1]["receipt_id"] == first
    assert result[1].status_code == 503


def test_non_ascii_bearer_is_401_without_rejecting_other_group_members(
    group_world, session_factory
):
    result = accept(
        session_factory,
        group_world,
        [item("bad", token="é"), item("a"), item("b"), item("c")],
    )
    assert result[0].status_code == 401
    assert [r[0] for r in result[1:]] == [202] * 3
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 3
        assert delivery.audit_quota(session, group_world["sub"])["differences"] == {}


@pytest.mark.parametrize("body", [b'"' + b"x" * 65534 + b'"', b" " * 65534 + b"[]"])
def test_four_maximum_http_bodies_fit_the_actual_unit_boundary(group_world, session_factory, body):
    result = accept(session_factory, group_world, [item(str(i), body) for i in range(4)])
    assert [r[0] for r in result] == [202] * 4


def test_stale_hint_does_not_change_per_event_mode(http_world, session_factory):
    with session_factory.begin() as session:
        assert service.receive_group(session, http_world["public_id"], [item()]) is None


@pytest.mark.parametrize("after_commit", [False, True])
def test_group_commit_loss_never_exposes_success_before_known_commit(
    group_world, session_factory, after_commit
):
    def snapshot(session):
        receipts = {
            row.source_event_id: {
                "id": row.id,
                "content_hash": row.content_hash,
                "raw": row.source_raw_body,
                "envelope": deepcopy(row.envelope),
            }
            for row in session.scalars(select(EventReceipt))
        }
        quotas = {}
        for scope in (f"sub:{group_world['sub']}", "global"):
            quota = session.get(SubscriptionQuota, scope)
            quotas[scope] = (
                {
                    key: getattr(quota, key)
                    for key in (*delivery.QUOTA_FACT_KEYS, "rate_count", "rate_window_at")
                }
                if quota is not None
                else None
            )
        return receipts, quotas

    class BrokenCommit(Session):
        def commit(self):
            if after_commit:
                super().commit()
            raise OperationalError(None, None, RuntimeError("commit connection lost"))

    pool = ingress.IngressResources(
        buffer=ApplicationBufferBudget(),
        session_factory=sessionmaker(
            bind=session_factory.kw["bind"], class_=BrokenCommit, expire_on_commit=False
        ),
    )
    try:
        bodies = [f'{{ "value": {i} }}\n'.encode() for i in range(4)]
        requests = [
            SimpleNamespace(authorization="Bearer source-token", key=str(i), body=bodies[i])
            for i in range(4)
        ]
        with session_factory() as session:
            before = snapshot(session)
        with pytest.raises(HTTPException) as error:
            ingress._run_group_sync(pool, group_world["public_id"], requests)
        assert error.value.status_code == 503
        assert error.value.detail["code"] == "ingress_unavailable"
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(EventReceipt)) == 4 * int(
                after_commit
            )
            assert session.scalar(select(func.count()).select_from(EventDelivery)) == 4 * int(
                after_commit
            )
            committed = snapshot(session)
            if not after_commit:
                assert committed == before
        retry = accept(session_factory, group_world, [item(str(i), bodies[i]) for i in range(4)])
        assert [r[0] for r in retry] == [200 if after_commit else 202] * 4
        retry_ids = {str(i): result[1]["receipt_id"] for i, result in enumerate(retry)}
        assert len(set(retry_ids.values())) == 4
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(EventReceipt)) == 4
            assert session.scalar(select(func.count()).select_from(EventDelivery)) == 4
            after = snapshot(session)
            assert {key: row["id"] for key, row in after[0].items()} == retry_ids
            for i, body in enumerate(bodies):
                row = after[0][str(i)]
                assert row["raw"] == body
                assert row["content_hash"] == hashlib.sha256(body).hexdigest()
                assert row["envelope"]["event"]["id"] == str(i)
                assert row["envelope"]["data"]["payload"] == {"value": i}
            if after_commit:
                assert after == committed  # Original IDs, frozen Envelopes and both quota rows.
            for scope in (f"sub:{group_world['sub']}", "global"):
                assert after[1][scope]["pending_count"] == 4
                assert after[1][scope]["history_count"] == 4
                assert after[1][scope]["dedupe_count"] == 4
            assert delivery.audit_quota(session, group_world["sub"])["differences"] == {}
            assert delivery.audit_global_quota(session)["differences"] == {}
    finally:
        pool.close()


def test_cancel_one_waiter_keeps_group_and_other_results(monkeypatch):
    async def run():
        parent = ApplicationBufferBudget()
        pool = ingress.IngressResources(buffer=parent, session_factory=lambda: None)
        started, unblock = threading.Event(), threading.Event()
        groups = []

        def blocked(_pool, _public, items):
            groups.append(len(items))
            started.set()
            assert unblock.wait(5)
            return [(202, {"receipt_id": i}) for i in range(len(items))]

        monkeypatch.setattr(ingress, "resources", lambda: pool)
        monkeypatch.setattr(ingress, "_run_group_sync", blocked)
        from dlr.control.services import subscription_runtime

        monkeypatch.setattr(
            subscription_runtime,
            "get_runtime",
            lambda: SimpleNamespace(http_batch_hint=lambda _: True),
        )
        tasks = [
            asyncio.create_task(
                ingress._request(
                    "route",
                    request([b"{}"], [(b"content-type", b"application/json")]),
                    "Bearer source-token",
                    str(i),
                )
            )
            for i in range(4)
        ]
        try:
            async with asyncio.timeout(5):
                while not started.is_set():
                    await asyncio.sleep(0.001)
                tasks[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[0]
                assert parent.used_bytes == 4 * ingress.BODY_RESERVATION
                assert pool.work.used == 1 and pool.requests._value == 28
                unblock.set()
                assert [r.status_code for r in await asyncio.gather(*tasks[1:])] == [202] * 3
                while parent.used_bytes:
                    await asyncio.sleep(0.001)
                assert groups == [4]
                assert pool.work.used == 0 and pool.requests._value == 32
        finally:
            unblock.set()
            pool.close()

    asyncio.run(run())


def test_sparse_group_timer_never_waits_in_native_thread(monkeypatch):
    async def run():
        parent = ApplicationBufferBudget()
        pool = ingress.IngressResources(buffer=parent, session_factory=lambda: None)
        started, unblock = threading.Event(), threading.Event()
        added, sizes = [], []
        original_add = pool.collector.add

        def observe_add(public_id, item):
            assert pool.work.used == 0 and not started.is_set()
            original_add(public_id, item)
            added.append(item.key)
            assert pool.work.used == 0 and not started.is_set()

        def blocked(_pool, _public, items):
            sizes.append(len(items))
            started.set()
            assert unblock.wait(5)
            return [(202, {"receipt_id": 1})]

        monkeypatch.setattr(ingress, "resources", lambda: pool)
        monkeypatch.setattr(ingress, "_run_group_sync", blocked)
        monkeypatch.setattr(pool.collector, "add", observe_add)
        from dlr.control.services import subscription_runtime

        monkeypatch.setattr(
            subscription_runtime,
            "get_runtime",
            lambda: SimpleNamespace(http_batch_hint=lambda _: True),
        )
        task = asyncio.create_task(
            ingress._request(
                "route",
                request([b"{}"], [(b"content-type", b"application/json")]),
                "Bearer source-token",
                "one",
            )
        )
        try:
            async with asyncio.timeout(5):
                while not started.is_set():
                    await asyncio.sleep(0.001)
                assert added == ["one"] and sizes == [1] and not pool.collector.pending
                assert pool.work.used == 1 and pool.requests._value == 31
                assert parent.used_bytes == ingress.BODY_RESERVATION and not task.done()
                unblock.set()
                assert (await task).status_code == 202
                while parent.used_bytes or pool.work.used:
                    await asyncio.sleep(0.001)
                assert pool.requests._value == 32
        finally:
            unblock.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            pool.close()

    asyncio.run(run())


def test_delayed_timer_cannot_admit_a_request_after_collection_deadline():
    async def run():
        import time

        from dlr.control.services.http_ingress_group import HttpCollector

        groups = []
        collector = HttpCollector(lambda _public, items: groups.append([i.key for i in items]))
        loop = asyncio.get_running_loop()
        collector.add("route", HttpInput(None, "first", b"{}", lambda: None, loop.create_future()))
        # Simulate an event-loop stall: the due timer has not run when the
        # next request arrives. It must start a new group rather than join it.
        time.sleep(0.02)
        collector.add("route", HttpInput(None, "later", b"{}", lambda: None, loop.create_future()))
        assert groups == [["first"]]
        collector.close()
        assert groups == [["first"], ["later"]]

    asyncio.run(run())
