"""Issue 223 recovery boundaries with real PostgreSQL transactions and public APIs.

Native queue I/O is controlled in the close-handshake case; ownership, receipts,
the runtime manager, API authorization and all batch mutations remain real.
"""

import asyncio
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.common.config import settings
from dlr.control.models import (
    AdapterBatchItem,
    EventDelivery,
    EventReceipt,
    EventSubscription,
    Execution,
    SubscriptionLease,
    User,
)
from dlr.control.services import adapter_batch
from dlr.control.services.accounts import CSRF_COOKIE_NAME, bootstrap_default_admin
from dlr.control.services.subscription_queue import Message
from dlr.control.services.subscription_rocketmq import RocketMQSourceRunner
from dlr.control.services.subscription_runtime import SubscriptionRuntimeManager
from runtime_api_support import (
    claim_execution,
    mark_broker_ready,
    ready_registration,
    report_attempt,
)

CODE = "def handle(context, input):\n    return input\n"
PREFIX = "/__dlr_account"


def ready_adapter(client: TestClient, name: str) -> dict[str, Any]:
    registered = client.post(
        "/api/workers/register",
        json=ready_registration("batch-recovery-worker", ["python"]),
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
    )
    assert registered.status_code == 200, registered.text
    mark_broker_ready()
    created = client.post(
        "/api/adapters",
        json={"name": name, "language": "python", "adapter_type": "task"},
    )
    assert created.status_code == 201, created.text
    adapter_id = created.json()["id"]
    patched = client.patch(
        f"/api/adapters/{adapter_id}",
        json={"runtime_worker_id": registered.json()["id"]},
    )
    assert patched.status_code == 200, patched.text
    saved = client.post(f"/api/adapters/{adapter_id}/versions", json={"code": CODE})
    assert saved.status_code == 201, saved.text
    return client.get(f"/api/adapters/{adapter_id}").json()


def plan(
    client: TestClient, adapters: list[dict[str, Any]], action: str = "start"
) -> dict[str, Any]:
    response = client.post(
        "/api/adapter-batches",
        json={
            "id": str(uuid.uuid4()),
            "action": action,
            "targets": [
                {"adapter_id": row["id"], "expected_token": row["batch_token"]} for row in adapters
            ],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def run(client: TestClient, batch: dict[str, Any]) -> dict[str, Any]:
    response = client.post(f"/api/adapter-batches/{batch['id']}/execute")
    assert response.status_code == 200, response.text
    return response.json()


def execution_count(factory: sessionmaker[Session], adapter_id: int) -> int:
    with factory() as session:
        return (
            session.scalar(
                select(func.count())
                .select_from(Execution)
                .where(Execution.adapter_id == adapter_id)
            )
            or 0
        )


def test_committed_receipt_survives_lost_commit_acknowledgement(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ready_adapter(api_client, "lost-commit-ack")
    remaining = ready_adapter(api_client, "after-lost-commit-ack")
    batch = plan(api_client, [adapter, remaining])
    batch_id = uuid.UUID(batch["id"])
    real_commit = Session.commit
    lost = False

    def commit_then_disconnect(session: Session) -> None:
        nonlocal lost
        committing_action = any(
            isinstance(row, AdapterBatchItem)
            and row.batch_id == batch_id
            and row.status == "accepted"
            for row in session.dirty
        )
        real_commit(session)
        if committing_action and not lost:
            lost = True
            raise ConnectionError("simulated loss of the committed response")

    monkeypatch.setattr(Session, "commit", commit_then_disconnect)
    with pytest.raises(ConnectionError, match="committed response"):
        api_client.post(f"/api/adapter-batches/{batch['id']}/execute")
    assert lost
    monkeypatch.setattr(Session, "commit", real_commit)

    observed = api_client.get(f"/api/adapter-batches/{batch['id']}")
    assert observed.status_code == 200, observed.text
    original = observed.json()["items"][0]
    assert original["status"] == "accepted"
    assert original["error_code"] is None
    assert not original["retryable"]
    assert len(original["execution_ids"]) == 1
    assert observed.json()["items"][1]["status"] == "pending"
    assert execution_count(session_factory, adapter["id"]) == 1
    assert execution_count(session_factory, remaining["id"]) == 0

    # A fresh HTTP request models a new page/process after the lost response.
    repeated = run(api_client, batch)["items"][0]
    assert repeated["execution_ids"] == original["execution_ids"]
    assert execution_count(session_factory, adapter["id"]) == 1
    assert execution_count(session_factory, remaining["id"]) == 1
    retry = api_client.post(
        f"/api/adapter-batches/{batch['id']}/retry", json={"adapter_ids": [adapter["id"]]}
    )
    assert retry.status_code == 409
    assert execution_count(session_factory, adapter["id"]) == 1


def account_write(client: TestClient, method: str, path: str, **kwargs: Any) -> Any:
    csrf = client.get(f"{PREFIX}/api/auth/account/csrf")
    assert csrf.status_code == 200, csrf.text
    return client.request(
        method,
        PREFIX + path,
        headers={"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)},
        **kwargs,
    )


def login_admin(client: TestClient) -> tuple[TestClient, int]:
    created = client.post(
        "/api/users",
        json={"username": "batch-operator", "password": "batch-operator-123", "role": "admin"},
    )
    assert created.status_code == 201, created.text
    account = TestClient(client.app)
    logged_in = account_write(
        account,
        "POST",
        "/api/auth/account/login",
        json={"username": "batch-operator", "password": "batch-operator-123"},
    )
    assert logged_in.status_code == 200, logged_in.text
    if logged_in.json()["principal"]["must_change_password"]:
        changed = account_write(
            account,
            "POST",
            "/api/auth/account/change-password",
            json={"current_password": "batch-operator-123", "new_password": "batch-operator-456"},
        )
        assert changed.status_code == 200, changed.text
        logged_in = account_write(
            account,
            "POST",
            "/api/auth/account/login",
            json={"username": "batch-operator", "password": "batch-operator-456"},
        )
        assert logged_in.status_code == 200, logged_in.text
    return account, created.json()["id"]


@pytest.mark.parametrize(
    "change", [{"role": "user"}, {"enabled": False}], ids=["demote", "disable"]
)
def test_account_change_between_items_rechecks_current_authority(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, Any],
) -> None:
    with session_factory() as session:
        bootstrap_default_admin(session)
    first = ready_adapter(api_client, "authority-first")
    second = ready_adapter(api_client, "authority-second")
    account, user_id = login_admin(api_client)
    request = {
        "id": str(uuid.uuid4()),
        "action": "start",
        "targets": [
            {"adapter_id": row["id"], "expected_token": row["batch_token"]}
            for row in [first, second]
        ],
    }
    prepared = account_write(account, "POST", "/api/adapter-batches", json=request)
    assert prepared.status_code == 200, prepared.text
    batch_id = uuid.UUID(request["id"])
    real_commit = Session.commit
    changed = False

    def revoke_after_first_commit(session: Session) -> None:
        nonlocal changed
        first_completed = any(
            isinstance(row, AdapterBatchItem)
            and row.batch_id == batch_id
            and row.adapter_id == first["id"]
            and row.status == "accepted"
            for row in session.dirty
        )
        real_commit(session)
        if first_completed and not changed:
            changed = True
            response = api_client.patch(f"/api/users/{user_id}", json=change)
            assert response.status_code == 200, response.text

    monkeypatch.setattr(Session, "commit", revoke_after_first_commit)
    response = account_write(account, "POST", f"/api/adapter-batches/{batch_id}/execute")
    assert response.status_code == 200, response.text
    assert changed
    assert execution_count(session_factory, first["id"]) == 1
    assert execution_count(session_factory, second["id"]) == 0
    with session_factory() as session:
        original = session.get(AdapterBatchItem, (batch_id, first["id"]))
        denied = session.get(AdapterBatchItem, (batch_id, second["id"]))
        assert original is not None and original.status == "accepted"
        assert denied is not None and denied.status == "failed"
        assert denied.error_code in {"adapter_not_found", "batch_account_unavailable"}
        user = session.get(User, user_id)
        assert user is not None
        assert user.role == change.get("role", "admin")
        assert user.enabled == change.get("enabled", True)
    # The response must not expose receipts for system-owned objects after
    # an account has lost its administrator visibility.
    for item in response.json()["items"]:
        assert item["adapter_name"] is None
        assert item["execution_ids"] == []


@pytest.mark.parametrize("change", ["input", "version", "worker", "mode"])
def test_saved_configuration_change_rejects_original_plan(
    api_client: TestClient, session_factory: sessionmaker[Session], change: str
) -> None:
    adapter = ready_adapter(api_client, "config-" + change)
    batch = plan(api_client, [adapter])
    path = f"/api/adapters/{adapter['id']}"
    if change == "input":
        current = api_client.get(path + "/input-config").json()
        response = api_client.put(
            path + "/input-config",
            json={
                "expected_revision": current["revision"],
                "source_type": "json",
                "json_value": {"v": 2},
            },
        )
    elif change == "version":
        response = api_client.post(path + "/versions", json={"code": CODE + "\n# changed\n"})
    elif change == "worker":
        worker = api_client.post(
            "/api/workers/register",
            json=ready_registration("different-worker", ["python"]),
            headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        )
        assert worker.status_code == 200, worker.text
        response = api_client.patch(path, json={"runtime_worker_id": worker.json()["id"]})
    else:
        response = api_client.patch(path, json={"run_mode": "schedule"})
    assert response.status_code in {200, 201}, response.text
    current = api_client.get(path).json()
    assert current["batch_token"] != adapter["batch_token"]
    result = run(api_client, batch)["items"][0]
    assert result["status"] == "failed"
    assert result["error_code"] == "batch_config_conflict"
    assert not result["retryable"]
    assert execution_count(session_factory, adapter["id"]) == 0
    assert api_client.get(path).json()["batch_token"] == current["batch_token"]
    assert run(api_client, batch)["items"][0]["status"] == "failed"


@pytest.mark.parametrize(
    "original_completed", [False, True], ids=["cancel-queued", "already-finished"]
)
def test_stop_never_cancels_execution_created_after_preparation(
    api_client: TestClient, original_completed: bool
) -> None:
    adapter = ready_adapter(api_client, "frozen-cancel")
    created = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert created.status_code == 202, created.text
    original_id = created.json()["id"]
    batch = plan(api_client, [adapter], "stop")
    assert batch["items"][0]["execution_ids"] == [original_id]
    if original_completed:
        claimed = claim_execution(api_client, adapter["runtime_worker_id"])
        assert claimed.status_code == 200, claimed.text
        finished = report_attempt(
            api_client, adapter["runtime_worker_id"], original_id, {"status": "succeeded"}
        )
        assert finished.status_code == 200, finished.text
    later = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert later.status_code == 202, later.text
    later_id = later.json()["id"]
    result = run(api_client, batch)["items"][0]
    assert result["execution_ids"] == [original_id]
    assert result["executions"][0]["status"] == ("succeeded" if original_completed else "cancelled")
    latest = api_client.get(f"/api/executions/{later_id}").json()
    assert latest["status"] == "queued" and not latest["cancel_requested"]
    assert run(api_client, batch)["items"][0]["execution_ids"] == [original_id]
    assert api_client.get(f"/api/executions/{later_id}").json()["status"] == "queued"


def test_concurrent_execute_is_bounded_and_query_stays_readable(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ready_adapter(api_client, "concurrent-batch")
    batch = plan(api_client, [adapter])
    entered, release = threading.Event(), threading.Event()
    real_apply = adapter_batch._apply

    def held_apply(*args: Any, **kwargs: Any) -> None:
        entered.set()
        assert release.wait(10), "Test did not release the first coordinator"
        real_apply(*args, **kwargs)

    monkeypatch.setattr(adapter_batch, "_apply", held_apply)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(api_client.post, f"/api/adapter-batches/{batch['id']}/execute")
        try:
            assert entered.wait(5)
            second = api_client.post(f"/api/adapter-batches/{batch['id']}/execute")
            assert second.status_code == 409, second.text
            assert second.json()["detail"]["code"] == "batch_operation_busy"
            observed = api_client.get(f"/api/adapter-batches/{batch['id']}")
            assert observed.status_code == 200, observed.text
            assert observed.json()["items"][0]["status"] == "pending"
            assert execution_count(session_factory, adapter["id"]) == 0
        finally:
            release.set()
        completed = first.result(timeout=10)
    assert completed.status_code == 200, completed.text
    assert execution_count(session_factory, adapter["id"]) == 1
    assert (
        run(api_client, batch)["items"][0]["execution_ids"]
        == completed.json()["items"][0]["execution_ids"]
    )


class CloseControlledTransport:
    def __init__(self) -> None:
        self.polling = threading.Event()
        self.deliver = threading.Event()
        self.closing = threading.Event()
        self.allow_close = threading.Event()
        self.acked = threading.Event()
        self.message: Message | None = None
        self.closed = False
        self.close_fails = False

    def poll(self) -> Message | None:
        self.polling.set()
        assert self.deliver.wait(10)
        self.deliver.clear()
        message, self.message = self.message, None
        return message

    def ack(self, message: Message) -> None:
        self.acked.set()

    def close(self) -> None:
        self.closing.set()
        assert self.allow_close.wait(10)
        if self.close_fails:
            raise RuntimeError("controlled native close failure")
        self.closed = True


def eventually(predicate: Any) -> None:
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "Native close acknowledgement did not converge"
        time.sleep(0.01)


@pytest.mark.parametrize(
    ("with_backlog", "close_fails"),
    [(False, False), (True, False), (False, True)],
    ids=["new-activation", "preserve-backlog", "close-unconfirmed"],
)
def test_subscription_stop_waits_for_native_close_and_preserves_scope(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    with_backlog: bool,
    close_fails: bool,
) -> None:
    monkeypatch.setattr(settings, "event_subscriptions_enabled", True)
    adapter = ready_adapter(api_client, "native-stop")
    created = api_client.post(
        "/api/event-subscriptions",
        json={
            "adapter_id": adapter["id"],
            "name": "batch-close-source",
            "config": {
                "source_type": "rocketmq",
                "cluster_id": "batch-fixture",
                "server": "grpc://broker.invalid:8081",
                "topic": "events",
                "group_id": "batch-fixture",
            },
        },
    )
    assert created.status_code == 201, created.text
    subscription_id = created.json()["id"]
    enabled = api_client.post(
        f"/api/event-subscriptions/{subscription_id}/enable", json={"expected_config_version": 1}
    )
    assert enabled.status_code == 200, enabled.text
    original_activation = enabled.json()["activation_id"]
    transport = CloseControlledTransport()
    transport.close_fails = close_fails
    runner = RocketMQSourceRunner()
    monkeypatch.setattr(runner, "open", lambda *args, **kwargs: transport)
    manager = SubscriptionRuntimeManager(session_factory, {"rocketmq": runner})
    try:
        assert manager.start_once(subscription_id)
        assert transport.polling.wait(5)
        if with_backlog:
            transport.message = Message(
                "accepted-before-stop",
                b"business-data",
                {
                    "cluster_id": "batch-fixture",
                    "topic": "events",
                    "group_id": "batch-fixture",
                    "tag": "",
                    "keys": [],
                    "properties": {},
                },
                None,
            )
            transport.deliver.set()
            assert transport.acked.wait(5)
        adapter = api_client.get(f"/api/adapters/{adapter['id']}").json()
        batch = plan(api_client, [adapter], "stop")
        unexecuted_stop = plan(api_client, [adapter], "stop")
        stopped = run(api_client, batch)["items"][0]
        assert stopped["status"] == "stopping"
        manager.reconcile_once()
        transport.deliver.set()
        assert transport.closing.wait(5)
        assert not transport.closed
        waiting = api_client.get(f"/api/adapter-batches/{batch['id']}").json()["items"][0]
        assert waiting["status"] == "stopping"
        with session_factory() as session:
            lease = session.get(SubscriptionLease, subscription_id)
            assert lease is not None and lease.owner_id and lease.closing_generation is not None
        transport.allow_close.set()
        if close_fails:

            def blocked_by_close_failure() -> bool:
                with session_factory() as session:
                    source = session.get(EventSubscription, subscription_id)
                    return (
                        source is not None and source.last_error_code == "source_close_unconfirmed"
                    )

            eventually(blocked_by_close_failure)
            uncertain = api_client.get(f"/api/adapter-batches/{batch['id']}").json()["items"][0]
            assert uncertain["status"] == "unknown"
            assert uncertain["error_code"] == "source_close_unconfirmed"
            assert not uncertain["retryable"]
            with session_factory() as session:
                lease = session.get(SubscriptionLease, subscription_id)
                assert lease is not None and lease.owner_id and lease.closing_generation is not None
            retry = api_client.post(
                f"/api/adapter-batches/{batch['id']}/retry",
                json={"adapter_ids": [adapter["id"]]},
            )
            assert retry.status_code == 409
            # A separate stop prepared before the failure must not erase the
            # diagnostic or guess that the native resource has been reclaimed.
            again = run(api_client, unexecuted_stop)["items"][0]
            assert again["status"] == "unknown"
            assert again["error_code"] == "source_close_unconfirmed"
            return
        eventually(lambda: manager.active_count == 0)
        closed = api_client.get(f"/api/adapter-batches/{batch['id']}").json()["items"][0]
        assert transport.closed and closed["status"] == "succeeded"
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(EventReceipt)) == int(
                with_backlog
            )
            assert session.scalar(select(func.count()).select_from(EventDelivery)) == int(
                with_backlog
            )
            if with_backlog:
                delivery = session.scalar(select(EventDelivery))
                assert delivery is not None and delivery.state == "pending"
        if not with_backlog:
            reopened = api_client.post(
                f"/api/event-subscriptions/{subscription_id}/enable",
                json={"expected_config_version": 1},
            )
            assert reopened.status_code == 200, reopened.text
            assert reopened.json()["activation_id"] != original_activation
            # Replaying the old batch cannot disable the newly activated source.
            old = run(api_client, batch)["items"][0]
            assert old["status"] in {"succeeded", "unknown"}
            if old["status"] == "unknown":
                assert old["error_code"] == "batch_activation_changed"
            stale = run(api_client, unexecuted_stop)["items"][0]
            assert stale["status"] == "failed"
            assert stale["error_code"] == "batch_activation_changed"
            assert not stale["retryable"]
            with session_factory() as session:
                source = session.get(EventSubscription, subscription_id)
                assert source is not None and source.desired_state == "enabled"
                assert source.activation_id == reopened.json()["activation_id"]
    finally:
        manager.stop_all()
        transport.deliver.set()
        transport.allow_close.set()
        asyncio.run(manager.aclose(timeout=0.1 if close_fails else 5))
