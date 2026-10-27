"""Real thread/Future coverage for HTTP ready ownership; no DB or transport."""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from dlr.control.api import ingress
from dlr.control.services.http_ingress_group import (
    MAX_PAYLOAD_BYTES,
    HttpInput,
    estimated_input_bytes,
)
from dlr.control.services.subscription_source import ApplicationBufferBudget
from test_http_ingress_limits import request
from test_issue222_http_groups import group_world as group_world
from test_issue222_http_groups import http_world as http_world
from test_issue222_http_groups import world as world


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0)


def entry(pool, key, releases, body=b"{}"):
    assert pool.requests.acquire(blocking=False)
    assert pool.buffer.reserve(ingress.BODY_RESERVATION)

    def release():
        releases.append(key)
        pool.buffer.release(ingress.BODY_RESERVATION)
        pool.requests.release()

    future = asyncio.get_running_loop().create_future()
    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return HttpInput(None, key, body, release, future)


def pool():
    return ingress.IngressResources(buffer=ApplicationBufferBudget(), session_factory=lambda: None)


def test_route_fifo_other_route_parallel_and_cancelled_waiters_keep_permits(monkeypatch):
    async def run():
        current = pool()
        releases, calls = [], []
        started, unblock = threading.Event(), threading.Event()

        def native(_pool, route, items):
            calls.append((route, [item.key for item in items]))
            if items[0].key == "first":
                started.set()
                assert unblock.wait(5)
            return [(202, {"key": item.key}) for item in items]

        monkeypatch.setattr(ingress, "_run_group_sync", native)
        first, other, second, third, idle = [
            entry(current, key, releases) for key in ("first", "other", "second", "third", "idle")
        ]
        current.dispatch("a", [first, other])
        try:
            await until(started.is_set)
            current.dispatch("a", [second])
            current.dispatch("a", [third])
            current.dispatch("b", [idle])
            assert (await idle.future)[0] == 202
            assert calls == [("a", ["first", "other"]), ("b", ["idle"])]
            first.future.cancel()
            second.future.cancel()
            assert current.buffer.used_bytes == 4 * ingress.BODY_RESERVATION
            assert current.work.used == 1 and releases == ["idle"]
            unblock.set()
            assert await asyncio.gather(other.future, third.future) == [
                (202, {"key": "other"}),
                (202, {"key": "third"}),
            ]
            await until(lambda: current.work.used == current.buffer.used_bytes == 0)
            assert calls == [
                ("a", ["first", "other"]),
                ("b", ["idle"]),
                ("a", ["second", "third"]),
            ]
            assert sorted(releases) == ["first", "idle", "other", "second", "third"]
            assert not current.ready._routes and current.requests._value == 32
        finally:
            unblock.set()
            await asyncio.to_thread(current.close)

    asyncio.run(run())


@pytest.mark.parametrize("counts", [(8, 8), (9, 11)])
def test_ready_groups_repack_fifo_and_keep_partial_tail_deadline(monkeypatch, counts):
    async def run():
        current = pool()
        releases, calls = [], []
        started, unblock = threading.Event(), threading.Event()
        held = 8
        for _ in range(held):
            assert current.work.acquire()
        values = [entry(current, str(i), releases) for i in range(sum(counts))]
        identities = [(id(value), id(value.body), id(value.future)) for value in values]

        def native(_pool, _route, items):
            calls.append([item.key for item in items])
            for item in items:
                assert (id(item), id(item.body), id(item.future)) == identities[int(item.key)]
            if len(calls) == 1:
                started.set()
                assert unblock.wait(5)
            return [(202, {"key": item.key}) for item in items]

        monkeypatch.setattr(ingress, "_run_group_sync", native)
        current.dispatch("route", values[: counts[0]])
        current.dispatch("route", values[counts[0] :])
        donor = current.ready._routes["route"].ready[1]
        deadline = donor.deadline
        current.work.release()
        held -= 1
        try:
            await until(started.is_set)
            assert calls == [[str(i) for i in range(16)]]
            assert current.work.used == held + 1 and not releases
            queued = current.ready._routes["route"].ready
            if sum(counts) == 20:
                assert list(queued) == [donor]
                assert donor.deadline == deadline and donor.state == "ready"
                assert donor.items == values[16:]
            else:
                assert not queued and donor.state == "done" and not donor.items
            unblock.set()
            assert await asyncio.gather(*(v.future for v in values)) == [
                (202, {"key": str(i)}) for i in range(sum(counts))
            ]
            await until(lambda: current.work.used == held)
            assert calls == [[str(i) for i in range(16)]] + (
                [[str(i) for i in range(16, 20)]] if sum(counts) == 20 else []
            )
            assert len(releases) == len(set(releases)) == sum(counts)
        finally:
            unblock.set()
            await asyncio.to_thread(current.close)
            for _ in range(held):
                current.work.release()
        assert not current.ready._routes and current.requests._value == 32
        assert current.buffer.parent.used_bytes == current.work.used == 0

    asyncio.run(run())


@pytest.mark.parametrize("extra", [0, 1])
def test_ready_repack_byte_limit_never_skips_a_nonfitting_head(monkeypatch, extra):
    async def run():
        current = pool()
        releases, calls = [], []
        started, unblock = threading.Event(), threading.Event()
        held = 8
        for _ in range(held):
            assert current.work.acquire()
        sizes = [65530 + extra, 65536, 65536, 5462, 2]
        values = [entry(current, str(i), releases, b"x" * size) for i, size in enumerate(sizes)]
        assert sum(estimated_input_bytes(n) for n in sizes[:4]) == MAX_PAYLOAD_BYTES + extra

        def native(_pool, _route, items):
            calls.append([item.key for item in items])
            assert sum(estimated_input_bytes(len(item.body)) for item in items) <= MAX_PAYLOAD_BYTES
            if len(calls) == 1:
                started.set()
                assert unblock.wait(5)
            return [(202, {"key": item.key}) for item in items]

        monkeypatch.setattr(ingress, "_run_group_sync", native)
        current.dispatch("route", values[:2])
        current.dispatch("route", values[2:4])
        current.dispatch("route", values[4:])
        donor = current.ready._routes["route"].ready[1]
        deadline = donor.deadline
        current.work.release()
        held -= 1
        try:
            await until(started.is_set)
            prefix = 4 if extra == 0 else 3
            assert calls == [[str(i) for i in range(prefix)]]
            if extra:
                assert donor.items == [values[3]] and donor.deadline == deadline
                assert (
                    sum(estimated_input_bytes(n) for n in sizes[:3] + sizes[4:]) < MAX_PAYLOAD_BYTES
                )
            unblock.set()
            await asyncio.gather(*(value.future for value in values))
            await until(lambda: current.work.used == held)
            assert calls == [[str(i) for i in range(prefix)], [str(i) for i in range(prefix, 5)]]
            assert len(releases) == len(set(releases)) == 5
        finally:
            unblock.set()
            await asyncio.to_thread(current.close)
            for _ in range(held):
                current.work.release()
        assert current.work.used == current.buffer.parent.used_bytes == 0
        assert current.requests._value == 32 and not current.ready._routes

    asyncio.run(run())


def test_ready_repack_stops_before_expired_donor_and_later_members(monkeypatch):
    async def run():
        current = pool()
        releases, calls = [], []
        started, unblock = threading.Event(), threading.Event()
        held = 8
        for _ in range(held):
            assert current.work.acquire()
        values = [entry(current, key, releases) for key in ("first", "expired", "last")]

        def native(_pool, _route, items):
            calls.append([item.key for item in items])
            if len(calls) == 1:
                started.set()
                assert unblock.wait(5)
            return [(202, {"key": item.key}) for item in items]

        monkeypatch.setattr(ingress, "_run_group_sync", native)
        for value in values:
            current.dispatch("route", [value])
        current.ready._routes["route"].ready[1].deadline = asyncio.get_running_loop().time() - 1
        current.work.release()
        held -= 1
        try:
            await until(started.is_set)
            with pytest.raises(HTTPException) as error:
                await values[1].future
            assert error.value.status_code == 429
            assert calls == [["first"]] and releases == ["expired"]
            assert not values[2].future.done()
            unblock.set()
            assert await asyncio.gather(values[0].future, values[2].future) == [
                (202, {"key": "first"}),
                (202, {"key": "last"}),
            ]
            await until(lambda: current.work.used == held)
            assert calls == [["first"], ["last"]]
        finally:
            unblock.set()
            await asyncio.to_thread(current.close)
            for _ in range(held):
                current.work.release()
        assert sorted(releases) == ["expired", "first", "last"]
        assert current.work.used == current.buffer.parent.used_bytes == 0

    asyncio.run(run())


def test_repacked_prefix_keeps_permits_while_cancelled_tail_closes(monkeypatch):
    async def run():
        current = pool()
        releases, calls = [], []
        started, unblock = threading.Event(), threading.Event()
        held = 8
        for _ in range(held):
            assert current.work.acquire()
        values = [entry(current, str(i), releases) for i in range(20)]

        def native(_pool, _route, items):
            calls.append([item.key for item in items])
            started.set()
            assert unblock.wait(5)
            return [(202, {"key": item.key}) for item in items]

        monkeypatch.setattr(ingress, "_run_group_sync", native)
        current.dispatch("route", values[:9])
        current.dispatch("route", values[9:])
        current.work.release()
        held -= 1
        try:
            await until(started.is_set)
            assert calls == [[str(i) for i in range(16)]]
            values[16].future.cancel()
            current.begin_close()
            tail = await asyncio.gather(*(v.future for v in values[16:]), return_exceptions=True)
            assert isinstance(tail[0], asyncio.CancelledError)
            assert all(
                isinstance(value, HTTPException) and value.status_code == 503 for value in tail[1:]
            )
            assert sorted(releases) == [str(i) for i in range(16, 20)]
            assert current.buffer.used_bytes == 16 * ingress.BODY_RESERVATION
            assert current.requests._value == 16 and current.work.used == held + 1
            assert all(not value.future.done() for value in values[:16])
            unblock.set()
            assert await asyncio.gather(*(v.future for v in values[:16])) == [
                (202, {"key": str(i)}) for i in range(16)
            ]
            await until(lambda: current.work.used == held)
        finally:
            unblock.set()
            await asyncio.to_thread(current.close)
            for _ in range(held):
                current.work.release()
        assert len(releases) == len(set(releases)) == 20
        assert current.requests._value == 32 and not current.ready._routes
        assert current.work.used == current.buffer.parent.used_bytes == 0

    asyncio.run(run())


@pytest.mark.parametrize("submit_rejected", [False, True])
def test_repacked_prefix_fast_future_or_submit_error_releases_each_member_once(
    monkeypatch, submit_rejected
):
    from concurrent.futures import Future

    async def run():
        current = pool()
        releases, calls = [], []
        held = 8
        for _ in range(held):
            assert current.work.acquire()
        values = [entry(current, str(i), releases) for i in range(20)]

        def submit(_function, _pool, _route, items):
            calls.append([item.key for item in items])
            assert current.work.used == held + 1
            if submit_rejected:
                raise RuntimeError("executor closed")
            result = Future()
            result.set_result([(202, {"key": item.key}) for item in items])
            return result

        monkeypatch.setattr(current.executor, "submit", submit)
        current.dispatch("route", values[:9])
        current.dispatch("route", values[9:])
        current.work.release()
        held -= 1
        try:
            results = await asyncio.gather(*(v.future for v in values), return_exceptions=True)
            if submit_rejected:
                assert all(
                    isinstance(value, HTTPException) and value.status_code == 503
                    for value in results
                )
            else:
                assert results == [(202, {"key": str(i)}) for i in range(20)]
            assert calls == [[str(i) for i in range(16)], [str(i) for i in range(16, 20)]]
            assert len(releases) == len(set(releases)) == 20
            assert current.work.used == held and not current.ready._routes
        finally:
            await asyncio.to_thread(current.close)
            for _ in range(held):
                current.work.release()
        assert current.requests._value == 32 and current.buffer.parent.used_bytes == 0

    asyncio.run(run())


@pytest.mark.parametrize("status", [False, True])
def test_real_single_or_get_release_pumps_ready_without_new_post(monkeypatch, status):
    async def run():
        current = pool()
        blocks = [threading.Event() for _ in range(8)]
        started, releases, tasks = set(), [], []

        def native(_pool, _route, _auth, _body, key, receipt_id):
            index = receipt_id - 1 if status else int(key)
            started.add(index)
            assert blocks[index].wait(5)
            return 200, {"receipt_id": index + 1}

        monkeypatch.setattr(ingress, "resources", lambda: current)
        monkeypatch.setattr(ingress, "_run_sync", native)
        monkeypatch.setattr(ingress, "_run_group_sync", lambda *a: [(202, {})])
        from dlr.control.services import subscription_runtime

        monkeypatch.setattr(
            subscription_runtime,
            "get_runtime",
            lambda: SimpleNamespace(http_batch_hint=lambda _: False),
        )
        try:
            for index in range(8):
                tasks.append(
                    asyncio.create_task(
                        ingress._request(
                            "legacy",
                            request([b"{}"], [(b"content-type", b"application/json")]),
                            "Bearer test",
                            str(index),
                            receipt_id=index + 1 if status else None,
                        )
                    )
                )
            await until(lambda: len(started) == 8)
            queued = entry(current, "batch", releases)
            current.dispatch("batch", [queued])
            await asyncio.sleep(0)
            assert not queued.future.done() and current.work.used == 8
            blocks[0].set()
            assert (await queued.future)[0] == 202
            assert releases == ["batch"] and current.work.used <= 8
        finally:
            for block in blocks:
                block.set()
            await asyncio.gather(*tasks)
            await asyncio.to_thread(current.close)
        assert current.work.used == current.buffer.parent.used_bytes == 0
        assert not current.ready._routes and current.requests._value == 32

    asyncio.run(run())


def test_expired_ready_rejected_before_next_claim_and_close_is_reentrant(monkeypatch):
    async def run():
        current = pool()
        releases, calls = [], []
        for _ in range(8):
            assert current.work.acquire()
        monkeypatch.setattr(ingress, "_run_group_sync", lambda *a: calls.append(1) or [(202, {})])
        expired, fresh = entry(current, "expired", releases), entry(current, "fresh", releases)
        current.dispatch("route", [expired])
        current.dispatch("route", [fresh])
        current.ready._routes["route"].ready[0].deadline = asyncio.get_running_loop().time() - 1
        # Refusal completion is deliberately reentrant: no fresh claim may
        # already have happened before it changes the pool to closing.
        original_finish = current.ready._finish

        def finish(group, result, claimed):
            if group.items[0].key == "expired":
                current.begin_close()
            original_finish(group, result, claimed)

        current.ready._finish = finish
        current.work.release()
        try:
            with pytest.raises(HTTPException) as error:
                await expired.future
            assert error.value.status_code == 429
            with pytest.raises(HTTPException) as error:
                await fresh.future
            assert error.value.status_code == 503
            assert calls == [] and sorted(releases) == ["expired", "fresh"]
        finally:
            await asyncio.to_thread(current.close)
            for _ in range(7):
                current.work.release()
        assert not current.ready._routes and current.buffer.parent.used_bytes == 0

    asyncio.run(run())


def test_close_during_submit_and_fast_future_keep_owner_until_registered_callback(monkeypatch):
    async def run():
        current = pool()
        started, unblock, closing = threading.Event(), threading.Event(), threading.Event()
        releases, closers = [], []
        real_submit = current.executor.submit

        def native(_pool, _route, items):
            started.set()
            assert unblock.wait(5)
            return [(202, {}) for _ in items]

        def submit(*args, **kwargs):
            future = real_submit(*args, **kwargs)
            assert started.wait(5)

            def close():
                closing.set()
                current.close()

            closer = threading.Thread(target=close)
            closers.append(closer)
            closer.start()
            assert closing.wait(5)
            assert current.ready._lock.acquire(timeout=1)
            current.ready._lock.release()
            assert current.work.used == 1 and not releases
            unblock.set()
            future.result(timeout=5)
            assert current.work.used == 1 and not releases
            return future

        monkeypatch.setattr(ingress, "_run_group_sync", native)
        monkeypatch.setattr(current.executor, "submit", submit)
        values = [entry(current, key, releases) for key in ("one", "two")]
        for value in values:
            current.dispatch("route", [value])
        assert await asyncio.gather(*(value.future for value in values)) == [(202, {}), (202, {})]
        for closer in closers:
            await asyncio.to_thread(closer.join, 5)
            assert not closer.is_alive()
        assert releases == ["one", "two"] and not current.ready._routes
        assert current.work.used == current.buffer.parent.used_bytes == 0
        assert current.requests._value == 32

    asyncio.run(run())


def test_submit_closed_executor_and_to_thread_close_flush_once():
    async def run():
        current = pool()
        releases = []
        current.executor.shutdown(wait=True)
        failed = entry(current, "failed", releases)
        current.dispatch("route", [failed])
        with pytest.raises(HTTPException) as error:
            await failed.future
        assert error.value.status_code == 503 and releases == ["failed"]
        pending = entry(current, "collecting", releases)
        current.collector.add("route", pending)
        await asyncio.to_thread(current.close)
        with pytest.raises(HTTPException) as error:
            await pending.future
        assert error.value.status_code == 503
        current.close()
        assert sorted(releases) == ["collecting", "failed"]
        assert current.work.used == current.buffer.parent.used_bytes == 0
        assert current.requests._value == 32 and not current.ready._routes
        assert not current.collector.pending and current.ready._timer is None

    asyncio.run(run())


def test_existing_body_request_limits_bound_ready_routes_and_one_expiry_timer(monkeypatch):
    async def run():
        current = pool()
        releases = []
        started, unblock = threading.Event(), threading.Event()

        def native(*args):
            started.set()
            assert unblock.wait(5)
            return [(202, {})]

        monkeypatch.setattr(ingress, "_run_group_sync", native)
        first = entry(current, "active", releases)
        current.dispatch("route", [first])
        try:
            await until(started.is_set)
            waiting = [entry(current, str(index), releases) for index in range(24)]
            for value in waiting:
                current.dispatch("route", [value])
            # Isolate state/timer bounds from wall-clock expiry, covered above.
            for group in current.ready._routes["route"].ready:
                group.deadline = asyncio.get_running_loop().time() + 60
            await asyncio.sleep(0)
            timer = current.ready._timer
            assert timer is not None and len(current.ready._routes) == 1
            for _ in range(100):
                current.ready.wake()
            await asyncio.sleep(0)
            assert current.ready._timer is timer
            assert current.buffer.used_bytes == 25 * ingress.BODY_RESERVATION
            assert current.buffer.used_bytes <= 8 * 1024**2
            assert not current.buffer.reserve(ingress.BODY_RESERVATION)
            assert current.requests._value == 7 and current.work.used == 1
            current.begin_close()
            results = await asyncio.gather(*(v.future for v in waiting), return_exceptions=True)
            assert all(isinstance(r, HTTPException) and r.status_code == 503 for r in results)
            assert current.buffer.used_bytes == ingress.BODY_RESERVATION
            assert current.ready._timer is None
            unblock.set()
            assert (await first.future)[0] == 202
            assert len(releases) == len(set(releases)) == 25
        finally:
            unblock.set()
            await asyncio.to_thread(current.close)
        assert not current.ready._routes and current.requests._value == 32
        assert current.buffer.parent.used_bytes == current.work.used == 0

    asyncio.run(run())


def test_submitted_future_not_started_still_owns_route_and_bytes_on_close(monkeypatch):
    async def run():
        from concurrent.futures import ThreadPoolExecutor

        current = pool()
        current.executor.shutdown(wait=True)
        # A real executor with a controlled occupied worker exposes the legal
        # submit-return/native-not-started interval; production still has8.
        current.executor = ThreadPoolExecutor(max_workers=1)
        busy, release = threading.Event(), threading.Event()
        releases = []

        def blocker():
            busy.set()
            assert release.wait(5)

        occupied = current.executor.submit(blocker)
        assert busy.wait(5)
        monkeypatch.setattr(ingress, "_run_group_sync", lambda *args: [(202, {})])
        value = entry(current, "submitted", releases)
        current.dispatch("route", [value])
        try:
            await until(lambda: current.work.used == 1)
            active = current.ready._routes["route"].active
            assert active is not None and active.state == "running"
            active.deadline = asyncio.get_running_loop().time() - 1
            current.begin_close()
            assert current.work.used == 1 and not value.future.done()
            assert current.buffer.used_bytes == ingress.BODY_RESERVATION and not releases
            release.set()
            assert (await value.future)[0] == 202
            occupied.result(timeout=5)
        finally:
            release.set()
            await asyncio.to_thread(current.close)
        assert releases == ["submitted"] and not current.ready._routes
        assert current.work.used == current.buffer.parent.used_bytes == 0

    asyncio.run(run())


def test_real_group_responses_and_ledger_through_ready_queue(
    group_world, session_factory, monkeypatch
):
    from sqlalchemy import func, select

    from dlr.control.models import EventDelivery, EventReceipt
    from dlr.control.services import subscription_delivery, subscription_runtime

    async def run():
        current = ingress.IngressResources(
            buffer=ApplicationBufferBudget(), session_factory=session_factory
        )
        monkeypatch.setattr(ingress, "resources", lambda: current)
        monkeypatch.setattr(
            subscription_runtime,
            "get_runtime",
            lambda: SimpleNamespace(http_batch_hint=lambda _: True),
        )

        async def send(key, body=b"{}", token="source-token"):
            return await ingress._request(
                group_world["public_id"],
                request([body], [(b"content-type", b"application/json")]),
                "Bearer " + token,
                key,
            )

        def status(value):
            return value.status_code

        try:
            values = await asyncio.gather(
                send("wrong", token="wrong"),
                send("broken", b"{"),
                send("same"),
                send("same"),
                return_exceptions=True,
            )
            assert [status(value) for value in values] == [401, 422, 202, 200]
            assert values[2].body == values[3].body
            values = await asyncio.gather(send("same", b"[]"), send("new"), return_exceptions=True)
            assert [status(value) for value in values] == [409, 202]
            await until(lambda: current.work.used == current.buffer.used_bytes == 0)
            assert current.requests._value == 32 and not current.ready._routes
        finally:
            await asyncio.to_thread(current.close)

    asyncio.run(run())
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 2
        assert session.scalar(select(func.count()).select_from(EventDelivery)) == 2
        assert subscription_delivery.audit_quota(session, group_world["sub"])["differences"] == {}
        assert subscription_delivery.audit_global_quota(session)["differences"] == {}


@pytest.mark.parametrize("commit_mode", ["normal", "before", "after"])
def test_two_ready_groups_commit_one_native_unit_with_independent_responses(
    group_world, session_factory, monkeypatch, commit_mode
):
    import hashlib
    from copy import deepcopy

    from sqlalchemy import select
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session, sessionmaker

    from dlr.control.models import EventDelivery, EventReceipt, SubscriptionQuota
    from dlr.control.services import subscription_delivery

    def snapshot():
        with session_factory() as session:
            facts = {
                "receipts": {
                    row.source_event_id: {
                        "id": row.id,
                        "raw": row.source_raw_body,
                        "content_hash": row.content_hash,
                        "envelope": deepcopy(row.envelope),
                    }
                    for row in session.scalars(select(EventReceipt))
                },
                "deliveries": [
                    (row.id, row.receipt_id, row.ordinal, row.state)
                    for row in session.scalars(select(EventDelivery).order_by(EventDelivery.id))
                ],
                "quotas": {
                    scope: (
                        {key: getattr(quota, key) for key in subscription_delivery.QUOTA_FACT_KEYS}
                        if (quota := session.get(SubscriptionQuota, scope)) is not None
                        else None
                    )
                    for scope in (f"sub:{group_world['sub']}", "global")
                },
            }
            assert (
                subscription_delivery.audit_quota(session, group_world["sub"])["differences"] == {}
            )
            assert subscription_delivery.audit_global_quota(session)["differences"] == {}
            return facts

    commit_calls = []

    class BrokenCommit(Session):
        def commit(self):
            commit_calls.append(commit_mode)
            if commit_mode == "after":
                super().commit()
            raise OperationalError(None, None, RuntimeError("commit connection lost"))

    async def run():
        current = ingress.IngressResources(
            buffer=ApplicationBufferBudget(),
            session_factory=(
                session_factory
                if commit_mode == "normal"
                else sessionmaker(
                    bind=session_factory.kw["bind"],
                    class_=BrokenCommit,
                    expire_on_commit=False,
                )
            ),
        )
        releases, calls = [], []
        held = 8
        for _ in range(held):
            assert current.work.acquire()
        specs = [
            ("wrong", b"{}", "wrong"),
            ("broken", b"{", "source-token"),
            ("same", b'{"value":1}', "source-token"),
            ("same", b'{"value":1}', "source-token"),
            ("same", b"[]", "source-token"),
            ("new", b'{"value":2}', "source-token"),
        ]
        actual_native = ingress._run_group_sync

        def native(pool, public_id, items):
            calls.append([(id(value), id(value.future), id(value.body)) for value in items])
            return actual_native(pool, public_id, items)

        monkeypatch.setattr(ingress, "_run_group_sync", native)

        async def receive(round_number):
            nonlocal held
            values = []
            for index, (key, body, token) in enumerate(specs):
                value = entry(current, f"{round_number}:{index}", releases, body)
                value.key, value.authorization = key, "Bearer " + token
                values.append(value)
            identities = [(id(value), id(value.future), id(value.body)) for value in values]
            before_calls = len(calls)
            assert current.work.used == 8
            current.dispatch(group_world["public_id"], values[:3])
            current.dispatch(group_world["public_id"], values[3:])
            route = current.ready._routes[group_world["public_id"]]
            assert len(route.ready) == 2 and route.active is None
            current.work.release()
            held -= 1
            results = await asyncio.gather(*(v.future for v in values), return_exceptions=True)
            assert calls[before_calls:] == [identities]
            await until(lambda: current.work.used == held)
            assert len(releases) == len(set(releases)) == 6 * (round_number + 1)
            assert current.requests._value == 32 and not current.ready._routes
            assert current.buffer.used_bytes == 0
            return results

        def statuses(results):
            return [
                value.status_code if isinstance(value, HTTPException) else value[0]
                for value in results
            ]

        before = snapshot()
        assert before["receipts"] == {} and before["deliveries"] == []
        try:
            results = await receive(0)
            committed = snapshot()
            if commit_mode == "normal":
                assert statuses(results) == [401, 422, 202, 200, 409, 202]
                assert results[2][1] == results[3][1] and commit_calls == []
            else:
                assert statuses(results) == [503] * 6
                assert all(value.detail["code"] == "ingress_unavailable" for value in results)
                assert commit_calls == [commit_mode]
                if commit_mode == "before":
                    assert committed == before
                else:
                    assert set(committed["receipts"]) == {"same", "new"}
                    assert len(committed["deliveries"]) == 2
                current.session_factory = session_factory
                assert current.work.acquire()
                held += 1
                retry = await receive(1)
                assert statuses(retry) == (
                    [401, 422, 200, 200, 409, 200]
                    if commit_mode == "after"
                    else [401, 422, 202, 200, 409, 202]
                )
                assert retry[2][1] == retry[3][1]
                assert commit_calls == [commit_mode]
                if commit_mode == "after":
                    # Exclude duplicate/last-seen timestamps; retain the
                    # committed identities, original bytes/Envelope and bill.
                    assert snapshot() == committed
        finally:
            await asyncio.to_thread(current.close)
            for _ in range(held):
                current.work.release()
        assert current.work.used == current.buffer.parent.used_bytes == 0

    asyncio.run(run())
    with session_factory() as session:
        receipts = {row.source_event_id: row for row in session.scalars(select(EventReceipt))}
        assert set(receipts) == {"same", "new"}
        assert len(list(session.scalars(select(EventDelivery)))) == 2
        for key, body in (("same", b'{"value":1}'), ("new", b'{"value":2}')):
            receipt = receipts[key]
            assert receipt.source_raw_body == body
            assert receipt.content_hash == hashlib.sha256(body).hexdigest()
            assert receipt.envelope["event"]["id"] == key and receipt.state == "pending"
        for scope in (f"sub:{group_world['sub']}", "global"):
            quota = session.get(SubscriptionQuota, scope)
            assert (quota.pending_count, quota.history_count, quota.dedupe_count) == (2, 2, 2)
        assert subscription_delivery.audit_quota(session, group_world["sub"])["differences"] == {}
        assert subscription_delivery.audit_global_quota(session)["differences"] == {}
