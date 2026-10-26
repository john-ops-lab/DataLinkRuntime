"""Pure HTTP commit/wake wiring; no PostgreSQL or broker is contacted."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request

from dlr.control.api import ingress
from dlr.control.services import subscription_runtime, subscription_scheduler
from dlr.control.services.subscription_source import ApplicationBufferBudget


class RecordingSession:
    def __init__(self, events, failure=None):
        self.events = events
        self.failure = failure
        self.durable = False

    def __enter__(self):
        self.events.append("enter")
        return self

    def __exit__(self, *args):
        self.events.append("exit")

    def commit(self):
        self.events.append("commit_begin")
        assert "notify" not in self.events
        if self.failure == "before":
            raise SQLAlchemyError("synthetic commit did not complete")
        self.durable = True
        if self.failure == "after":
            raise SQLAlchemyError("synthetic connection lost after commit")
        self.events.append("commit_end")

    def rollback(self):
        self.events.append("rollback")


@pytest.fixture
def wiring(monkeypatch):
    events = []
    session = RecordingSession(events)

    def notify():
        assert session.durable
        assert events[-1] == "commit_end"
        events.append("notify")

    monkeypatch.setattr(subscription_scheduler, "notify_delivery_work", notify)
    return SimpleNamespace(events=events, session=session, session_factory=lambda: session)


def group_items(count):
    # The native helper only reads these fields; no asyncio waiter is required.
    return [
        SimpleNamespace(authorization="Bearer token", key=str(i), body=b"{}") for i in range(count)
    ]


@pytest.mark.parametrize("status", [202, 200])
def test_single_new_commit_wakes_after_success_and_duplicate_does_not(wiring, monkeypatch, status):
    expected = {"receipt_id": 1}
    monkeypatch.setattr(ingress.service, "receive", lambda *args: (status, expected))
    assert ingress._run_sync(wiring, "route", "Bearer token", b"{}", "one", None) == (
        status,
        expected,
    )
    assert wiring.events == [
        "enter",
        "commit_begin",
        "commit_end",
        *(["notify"] if status == 202 else []),
        "exit",
    ]


def test_mixed_group_wakes_once_for_all_new_items_after_one_commit(wiring, monkeypatch):
    rejected = HTTPException(409, {"code": "event_identity_conflict"})
    values = [
        (202, {"receipt_id": 1}),
        (200, {"receipt_id": 2}),
        rejected,
        (202, {"receipt_id": 3}),
    ]
    monkeypatch.setattr(ingress.service, "receive_group", lambda *args: values)
    result = ingress._run_group_sync(wiring, "route", group_items(4))
    assert result[:2] == values[:2] and result[3] == values[3]
    assert isinstance(result[2], HTTPException) and result[2].detail == rejected.detail
    assert wiring.events == ["enter", "commit_begin", "commit_end", "notify", "exit"]


@pytest.mark.parametrize("rejected", [False, True])
def test_group_without_new_custody_does_not_wake(wiring, monkeypatch, rejected):
    values = (
        [HTTPException(401, {"code": "unauthorized"})] if rejected else [(200, {"receipt_id": 1})]
    )
    monkeypatch.setattr(ingress.service, "receive_group", lambda *args: values)
    result = ingress._run_group_sync(wiring, "route", group_items(1))
    assert len(result) == 1
    assert wiring.events == ["enter", "commit_begin", "commit_end", "exit"]


def test_status_read_does_not_commit_or_wake(wiring, monkeypatch):
    monkeypatch.setattr(ingress.service, "receipt_status", lambda *args: {"receipt_id": 1})
    assert ingress._run_sync(wiring, "route", "Bearer token", None, None, 1) == (
        200,
        {"receipt_id": 1},
    )
    assert wiring.events == ["enter", "exit"]


@pytest.mark.parametrize("mode", ["single", "group"])
def test_rejected_transaction_does_not_wake_or_commit(wiring, monkeypatch, mode):
    def reject(*args):
        raise HTTPException(401, {"code": "unauthorized"})

    monkeypatch.setattr(ingress.service, "receive_group" if mode == "group" else "receive", reject)
    if mode == "group":
        result = ingress._run_group_sync(wiring, "route", group_items(1))
        assert isinstance(result[0], HTTPException) and result[0].status_code == 401
        assert wiring.events == ["enter", "rollback", "exit"]
    else:
        with pytest.raises(HTTPException) as raised:
            ingress._run_sync(wiring, "route", "Bearer token", b"{}", "one", None)
        assert raised.value.status_code == 401
        assert wiring.events == ["enter", "exit"]


@pytest.mark.parametrize("mode", ["single", "group"])
@pytest.mark.parametrize("failure", ["before", "after"])
def test_failed_or_unknown_commit_keeps_503_and_never_wakes(wiring, monkeypatch, mode, failure):
    wiring.session.failure = failure
    monkeypatch.setattr(ingress.service, "receive", lambda *args: (202, {"receipt_id": 1}))
    monkeypatch.setattr(ingress.service, "receive_group", lambda *args: [(202, {"receipt_id": 1})])
    with pytest.raises(HTTPException) as raised:
        if mode == "group":
            ingress._run_group_sync(wiring, "route", group_items(1))
        else:
            ingress._run_sync(wiring, "route", "Bearer token", b"{}", "one", None)
    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "ingress_unavailable"
    assert wiring.session.durable == (failure == "after")
    assert wiring.events == ["enter", "commit_begin", "rollback", "exit"]


@pytest.mark.parametrize("mode", ["single", "group"])
def test_hint_failure_preserves_known_response_and_real_native_budget_lifetime(monkeypatch, mode):
    events = []
    session = RecordingSession(events)
    pool = ingress.IngressResources(
        buffer=ApplicationBufferBudget(), session_factory=lambda: session
    )
    monkeypatch.setattr(ingress, "resources", lambda: pool)
    monkeypatch.setattr(
        subscription_runtime,
        "get_runtime",
        lambda: SimpleNamespace(http_batch_hint=lambda _: mode == "group"),
    )
    monkeypatch.setattr(ingress.service, "receive", lambda *args: (202, {"receipt_id": 1}))
    monkeypatch.setattr(ingress.service, "receive_group", lambda *args: [(202, {"receipt_id": 1})])

    def broken_hint():
        assert session.durable and events[-1] == "commit_end"
        assert pool.work.used == 1
        assert pool.buffer.used_bytes == pool.buffer.parent.used_bytes == ingress.BODY_RESERVATION
        events.append("notify")
        raise RuntimeError("synthetic hint failure after known commit")

    monkeypatch.setattr(subscription_scheduler, "notify_delivery_work", broken_hint)

    async def run():
        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/ingress/route",
                "headers": [(b"content-type", b"application/json")],
            },
            receive,
        )
        response = await ingress._request("route", request, "Bearer token", "one")
        assert response.status_code == 202 and response.body == b'{"receipt_id":1}'
        assert events == ["enter", "commit_begin", "commit_end", "notify", "exit"]
        assert pool.work.used == pool.buffer.used_bytes == pool.buffer.parent.used_bytes == 0
        for _ in range(32):
            assert pool.requests.acquire(blocking=False)
        assert not pool.requests.acquire(blocking=False)
        for _ in range(32):
            pool.requests.release()

    try:
        asyncio.run(run())
    finally:
        pool.close()
