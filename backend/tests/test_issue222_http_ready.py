"""Real thread/Future coverage for HTTP ready ownership; no DB or transport."""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from dlr.control.api import ingress
from dlr.control.services.http_ingress_group import HttpInput
from dlr.control.services.subscription_source import ApplicationBufferBudget
from test_http_ingress_limits import request
from test_issue222_http_groups import group_world as group_world
from test_issue222_http_groups import http_world as http_world
from test_issue222_http_groups import world as world


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0)


def entry(pool, key, releases):
    assert pool.requests.acquire(blocking=False)
    assert pool.buffer.reserve(ingress.BODY_RESERVATION)

    def release():
        releases.append(key)
        pool.buffer.release(ingress.BODY_RESERVATION)
        pool.requests.release()

    future = asyncio.get_running_loop().create_future()
    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return HttpInput(None, key, b"{}", release, future)


def pool():
    return ingress.IngressResources(buffer=ApplicationBufferBudget(), session_factory=lambda: None)


def test_route_fifo_other_route_parallel_and_cancelled_waiters_keep_permits(monkeypatch):
    async def run():
        current = pool()
        releases, calls = [], []
        started, unblock = threading.Event(), threading.Event()

        def native(_pool, route, items):
            calls.append((route, items[0].key))
            if items[0].key == "first":
                started.set()
                assert unblock.wait(5)
            return [(202, {}) for _ in items]

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
            assert calls == [("a", "first"), ("b", "idle")]
            first.future.cancel()
            second.future.cancel()
            assert current.buffer.used_bytes == 4 * ingress.BODY_RESERVATION
            assert current.work.used == 1 and releases == ["idle"]
            unblock.set()
            await asyncio.gather(other.future, third.future)
            await until(lambda: current.work.used == current.buffer.used_bytes == 0)
            assert calls == [("a", "first"), ("b", "idle"), ("a", "second"), ("a", "third")]
            assert sorted(releases) == ["first", "idle", "other", "second", "third"]
            assert not current.ready._routes and current.requests._value == 32
        finally:
            unblock.set()
            await asyncio.to_thread(current.close)

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

        monkeypatch.setattr(subscription_runtime, "get_runtime", lambda: SimpleNamespace(
            http_batch_hint=lambda _: False
        ))
        try:
            for index in range(8):
                tasks.append(asyncio.create_task(ingress._request(
                    "legacy", request([b"{}"], [(b"content-type", b"application/json")]),
                    "Bearer test", str(index), receipt_id=index + 1 if status else None,
                )))
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
        monkeypatch.setattr(ingress, "_run_group_sync", lambda *a: (calls.append(1) or [(202, {})]))
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

        def native(*args):
            started.set()
            assert unblock.wait(5)
            return [(202, {})]

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
        value = entry(current, "one", releases)
        current.dispatch("route", [value])
        assert (await value.future)[0] == 202
        for closer in closers:
            await asyncio.to_thread(closer.join, 5)
            assert not closer.is_alive()
        assert releases == ["one"] and not current.ready._routes
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
        monkeypatch.setattr(subscription_runtime, "get_runtime", lambda: SimpleNamespace(
            http_batch_hint=lambda _: True
        ))

        async def send(key, body=b"{}", token="source-token"):
            return await ingress._request(
                group_world["public_id"],
                request([body], [(b"content-type", b"application/json")]),
                "Bearer " + token, key,
            )

        def status(value):
            return value.status_code

        try:
            values = await asyncio.gather(
                send("wrong", token="wrong"), send("broken", b"{"),
                send("same"), send("same"), return_exceptions=True,
            )
            assert [status(value) for value in values] == [401, 422, 202, 200]
            assert values[2].body == values[3].body
            values = await asyncio.gather(
                send("same", b"[]"), send("new"), return_exceptions=True
            )
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
