"""Runtime lifecycle against isolated PostgreSQL; source SDKs have their own protocol tests."""

import asyncio
import socket
import threading
from contextlib import contextmanager

import httpx
import uvicorn
from fastapi import HTTPException
from sqlalchemy import func, select

from conftest import ADMIN_TOKEN
from dlr.common.config import settings
from dlr.control.models import Credential, Execution
from dlr.control.models.subscription import (
    EventSubscription,
    SourceCheckpoint,
    SubscriptionActivation,
    SubscriptionConfigVersion,
    SubscriptionLease,
)
from dlr.control.services import subscription_management as management
from dlr.control.services import subscription_runtime as runtime
from dlr.control.services.secrets import encrypt_fields
from dlr.control.services.subscription_source import SourceFailure
from test_issue219_management import action, activate, setup_subscription


class RecordingSource:
    def __init__(self, *, failure=None):
        self.contexts = []
        self.values = []
        self.started = threading.Event()
        self.thread = None
        self.failure = failure
        self.diagnostics = 0

    def run(self, context):
        self.thread = threading.current_thread()
        self.contexts.append(context)
        self.values.append(dict(context.credential_fields))
        self.started.set()
        if self.failure:
            raise self.failure
        try:
            context.connected()
        except HTTPException:
            raise SourceFailure("subscription_owner_stale", "backoff") from None
        assert context.stop_event.wait(5), "test failed to stop source owner"

    def test_connection(self, source_config, credential_fields):
        self.diagnostics += 1
        return {
            "cluster_id": source_config["cluster_id"],
            "topic_count": len(source_config["topics"]),
            "group_available": True,
            "sdk_object": object(),
            "password": "never-return-this-field",
        }

    def join(self):
        assert self.started.wait(3)
        self.thread.join(5)
        assert not self.thread.is_alive()


def test_connection_uses_implemented_registry_without_enabling(
    api_client, monkeypatch, session_factory
):
    monkeypatch.setattr(management, "AVAILABLE_SOURCES", frozenset())
    _, _, sub = setup_subscription(api_client, monkeypatch)
    source = RecordingSource()
    manager = runtime.SubscriptionRuntimeManager(session_factory, {"kafka": source})
    monkeypatch.setattr(runtime, "_runtime", manager)
    response = action(api_client, sub, "test-connection")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "source_type": "kafka",
        "cluster_id": "test-cluster",
        "topic_count": 1,
        "group_available": True,
    }
    assert source.diagnostics == 1 and manager.active_count == 0
    assert api_client.get("/api/event-subscriptions/capabilities").json()["available_sources"] == []
    assert action(api_client, sub, "enable").status_code == 409
    with session_factory() as session:
        assert session.get(EventSubscription, sub["id"]).desired_state == "disabled"
        assert session.scalar(select(func.count()).select_from(SourceCheckpoint)) == 0
        assert session.scalar(select(func.count()).select_from(Execution)) == 0


def test_connection_preserves_acl_expected_version_and_feature_gate(
    api_client, monkeypatch, session_factory
):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    source = RecordingSource()
    monkeypatch.setattr(
        runtime, "_runtime", runtime.SubscriptionRuntimeManager(session_factory, {"kafka": source})
    )
    assert (
        api_client.post(
            f"/api/event-subscriptions/{sub['id']}/test-connection",
            json={"expected_config_version": 99},
        ).json()["detail"]["code"]
        == "subscription_config_conflict"
    )
    monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
    assert (
        action(api_client, sub, "test-connection").json()["detail"]["code"]
        == "source_not_available"
    )
    assert source.diagnostics == 0


def test_runtime_uses_activation_configuration_and_current_source_secret(
    api_client, monkeypatch, session_factory
):
    with session_factory.begin() as session:
        credential = Credential(
            name="source-runtime-credential",
            type="password",
            ciphertext=encrypt_fields({"username": "source-reader", "password": "original"}),
        )
        session.add(credential)
        session.flush()
        credential_id = credential.id
    _, _, sub = setup_subscription(api_client, monkeypatch, credential_id=credential_id)
    sub = activate(api_client, monkeypatch, sub)
    with session_factory.begin() as session:
        session.get(Credential, credential_id).ciphertext = encrypt_fields(
            {"username": "source-reader", "password": "rotated"}
        )
        old_config = session.scalar(
            select(SubscriptionConfigVersion).where(
                SubscriptionConfigVersion.subscription_id == sub["id"]
            )
        )
        # Model a later config head without changing the already-fixed activation.
        session.add(
            SubscriptionConfigVersion(
                subscription_id=sub["id"],
                version=2,
                source_epoch="later-epoch",
                config={**old_config.config, "topics": ["later-topic"]},
            )
        )
        session.get(EventSubscription, sub["id"]).config_version = 2
    source = RecordingSource()
    manager = runtime.SubscriptionRuntimeManager(session_factory, {"kafka": source})
    assert manager.start_once(sub["id"])
    assert source.started.wait(3)
    try:
        context = source.contexts[0]
        assert context.config_version == 1 and context.source_epoch == sub["source_epoch"]
        assert context.source_config["topics"] == ["events"]
        assert source.values == [{"username": "source-reader", "password": "rotated"}]
        assert "rotated" not in repr(context)
        with session_factory() as session:
            assert (
                session.get(SubscriptionActivation, context.activation_id).config_version_id
                == old_config.id
            )
    finally:
        manager.stop_single(sub["id"])
        source.join()
    assert context.credential_fields == {} and manager.active_count == 0


def test_feature_off_fences_native_owner_without_changing_desired(
    api_client, monkeypatch, session_factory
):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    sub = activate(api_client, monkeypatch, sub)
    source = RecordingSource()
    manager = runtime.SubscriptionRuntimeManager(session_factory, {"kafka": source})
    assert manager.start_once(sub["id"])
    assert source.started.wait(3)
    original_generation = source.contexts[0].generation
    monkeypatch.setattr(settings, "event_subscriptions_enabled", False)
    manager.reconcile_once()
    source.join()
    assert manager.active_count == 0 and not manager.start_once(sub["id"])
    with session_factory() as session:
        lease = session.get(SubscriptionLease, sub["id"])
        state = session.get(EventSubscription, sub["id"])
        assert lease.generation > original_generation and not lease.owner_id
        assert state.desired_state == "enabled" and state.connection_state == "stopped"


def test_authentication_failure_blocks_until_explicit_resume(
    api_client, monkeypatch, session_factory
):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    sub = activate(api_client, monkeypatch, sub)
    source = RecordingSource(failure=SourceFailure("source_authentication_failed"))
    manager = runtime.SubscriptionRuntimeManager(session_factory, {"kafka": source})
    assert manager.start_once(sub["id"])
    source.join()
    manager.reconcile_once()
    assert len(source.contexts) == 1
    with session_factory() as session:
        state = session.get(EventSubscription, sub["id"])
        assert state.connection_state == "blocked"
        assert state.last_error_code == "source_authentication_failed"
        assert state.activation_id == sub["activation_id"]
    assert action(api_client, sub, "resume").status_code == 200
    source.failure = None
    source.started.clear()
    assert manager.start_once(sub["id"])
    manager.stop_single(sub["id"])
    source.join()
    assert manager.active_count == 0


def test_connection_failures_use_bounded_code_without_secrets(
    api_client, monkeypatch, session_factory
):
    _, _, sub = setup_subscription(api_client, monkeypatch)

    class BrokenDiagnostic(RecordingSource):
        def test_connection(self, source_config, credential_fields):
            raise SourceFailure("PRIVATE-token=value / broker.example.test")

    manager = runtime.SubscriptionRuntimeManager(session_factory, {"kafka": BrokenDiagnostic()})
    monkeypatch.setattr(runtime, "_runtime", manager)
    response = action(api_client, sub, "test-connection")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_error"
    assert "PRIVATE" not in response.text and "broker.example" not in response.text
    assert manager.active_count == 0
    asyncio.run(manager.aclose(timeout=0.1))


def test_start_guard_commit_exit_failure_returns_only_owned_permit(
    api_client, monkeypatch, session_factory
):
    _, _, sub = setup_subscription(api_client, monkeypatch)

    class CommitExitFailure:
        @contextmanager
        def begin(self):
            with session_factory.begin() as session:
                yield session
            raise RuntimeError("injected transaction exit failure")

    source = RecordingSource()
    manager = runtime.SubscriptionRuntimeManager(session_factory, {"kafka": source})
    monkeypatch.setattr(manager, "session_factory", CommitExitFailure())
    # The disabled guard releases its claim before the transaction's __exit__
    # fails. The outer error path must not release that same claim a second time.
    assert manager.start_once(sub["id"]) is False
    assert manager.active_count == 0 and source.contexts == []
    claimed = []
    try:
        for ident in range(1000, 1016):
            assert manager._claim(ident)
            claimed.append(ident)
        assert not manager._claim(1016)
        assert manager.active_count == 16
    finally:
        for ident in claimed:
            manager._release_start(ident)
    assert manager.active_count == 0


def test_connection_database_preparation_does_not_block_control_loop(
    api_client, monkeypatch, session_factory
):
    _, _, sub = setup_subscription(api_client, monkeypatch)
    manager = runtime.SubscriptionRuntimeManager(session_factory, {"kafka": RecordingSource()})
    monkeypatch.setattr(runtime, "_runtime", manager)
    entered, release = threading.Event(), threading.Event()
    preparation_threads = []
    original_config = management.current_config

    def blocked_config(session, subscription):
        preparation_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5), "test failed to release blocked database preparation"
        return original_config(session, subscription)

    monkeypatch.setattr(management, "current_config", blocked_config)

    async def exercise():
        loop_thread = threading.get_ident()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        server = uvicorn.Server(
            uvicorn.Config(api_client.app, lifespan="off", log_level="critical", ws="none")
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        pending = None
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.001)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                trust_env=False,
                timeout=3,
            ) as client:
                pending = asyncio.create_task(
                    client.post(
                        f"/api/event-subscriptions/{sub['id']}/test-connection",
                        json={"expected_config_version": 1},
                    )
                )
                async with asyncio.timeout(2):
                    while not entered.is_set():
                        await asyncio.sleep(0.001)
                assert not pending.done() and len(preparation_threads) == 1
                assert preparation_threads[0] != loop_thread
                health = await asyncio.wait_for(client.get("/api/health"), timeout=1)
                assert health.status_code in {200, 503}
                assert health.json()["service"] == "dlr-control"
                assert not pending.done()
                release.set()
                response = await pending
                assert response.status_code == 200, response.text
        finally:
            release.set()
            if pending is not None and not pending.done():
                await asyncio.wait_for(pending, timeout=5)
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=5)
            listener.close()

    asyncio.run(exercise())
