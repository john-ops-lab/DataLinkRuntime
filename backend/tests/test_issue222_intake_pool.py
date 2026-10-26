"""Real intake exhaustion cannot consume the management connection reserve."""

import asyncio
import threading
from contextlib import ExitStack
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError

from dlr.control.services.ingress_database import close_intake_database, intake_sessions
from dlr.control.services.subscription_runtime import SubscriptionRuntimeManager


def test_shared_intake_pool_is_finite_and_management_remains_available(session_factory):
    factory = intake_sessions(session_factory.kw["bind"].url)
    manager = SubscriptionRuntimeManager(session_factory, {}, intake_session_factory=factory)
    assert manager.intake_session_factory is factory
    assert manager.session_factory is session_factory
    assert intake_sessions(session_factory.kw["bind"].url) is factory
    try:
        with ExitStack() as stack:
            for _ in range(8):
                session = stack.enter_context(factory())
                assert session.scalar(text("SELECT 1")) == 1
            pool = factory.kw["bind"].pool
            assert close_intake_database(native_closed=False) is False
            assert intake_sessions(session_factory.kw["bind"].url) is factory
            assert manager.intake_session_factory is factory
            assert factory.kw["bind"].pool is pool
            assert pool.checkedout() == 8
            with session_factory() as management:
                assert management.scalar(text("SELECT 1")) == 1
            with factory() as denied, pytest.raises(TimeoutError):
                denied.scalar(text("SELECT 1"))
        with factory() as resumed:
            assert resumed.scalar(text("SELECT 1")) == 1
    finally:
        assert close_intake_database(native_closed=True) is True


def test_http_batch_hint_never_authorizes_stopped_or_single_owners(session_factory):
    import threading

    manager = SubscriptionRuntimeManager(session_factory, {})
    stop = threading.Event()
    context = SimpleNamespace(
        public_id="opaque-route", source_config={"mode": "business_batch"}, stop_event=stop
    )
    owner = SimpleNamespace(context=context, native_closed=False)
    manager._owners[1] = owner
    assert manager.http_batch_hint("opaque-route")
    assert not manager.http_batch_hint("other-route")
    context.source_config = {"mode": "per_event"}
    assert not manager.http_batch_hint("opaque-route")
    context.source_config = {"mode": "business_batch"}
    stop.set()
    assert not manager.http_batch_hint("opaque-route")
    stop.clear()
    owner.native_closed = True
    assert not manager.http_batch_hint("opaque-route")


def _fake_intake_pool(monkeypatch):
    from dlr.control.services import ingress_database

    disposals = []
    engine = SimpleNamespace(pool=object())

    def dispose():
        disposals.append(ingress_database._lock.locked())

    def factory():
        return None

    engine.dispose = dispose
    monkeypatch.setattr(ingress_database, "_engine", engine)
    monkeypatch.setattr(ingress_database, "_factory", factory)
    return ingress_database, engine, factory, disposals


@pytest.mark.parametrize("receipt", [False, None, "unknown", 1])
def test_pool_retains_identity_without_explicit_native_success(monkeypatch, receipt):
    service, engine, factory, disposals = _fake_intake_pool(monkeypatch)
    pool = engine.pool
    if receipt is None:
        result = service.close_intake_database()
    else:
        result = service.close_intake_database(native_closed=receipt)
    assert result is False
    assert service._engine is engine and service._factory is factory
    assert engine.pool is pool and not disposals
    assert service.intake_sessions("never-used-dsn") is factory


@pytest.mark.parametrize("kind", ["owner", "probe", "starting", "supervisor", "unknown_owner"])
def test_runtime_close_receipt_waits_for_real_native_ownership(monkeypatch, kind):
    service, engine, factory, disposals = _fake_intake_pool(monkeypatch)
    manager = SubscriptionRuntimeManager(lambda: None, {}, intake_session_factory=factory)
    monkeypatch.setattr(manager, "stop_all", lambda: None)
    release, entered = threading.Event(), threading.Event()
    owner = SimpleNamespace(native_closed=False, close_unconfirmed=kind == "unknown_owner")
    if kind in {"owner", "unknown_owner"}:
        manager._owners[1] = owner
    elif kind == "probe":
        manager._probes.add(1)
    elif kind == "starting":
        manager._starting.add(1)

    def native():
        entered.set()
        if kind == "unknown_owner":
            return
        release.wait()
        with manager._lock:
            if kind == "owner":
                owner.native_closed = True
                manager._owners.pop(1)
            elif kind == "probe":
                manager._probes.remove(1)
            elif kind == "starting":
                manager._starting.remove(1)

    thread = threading.Thread(target=native, name="intake-close-test")
    owner.thread = thread
    if kind == "supervisor":
        manager._supervisor = thread
    thread.start()
    assert entered.wait(1)
    if kind == "unknown_owner":
        thread.join(1)
        assert not thread.is_alive()
    try:
        receipt = asyncio.run(manager.aclose(timeout=0.03))
        assert receipt is False
        assert service.close_intake_database(native_closed=receipt) is False
        assert service._engine is engine and service._factory is factory
        assert service.intake_sessions("never-used-dsn") is manager.intake_session_factory
        assert not disposals
        if kind != "unknown_owner":
            release.set()
            thread.join(1)
            assert not thread.is_alive()
            receipt = asyncio.run(manager.aclose(timeout=0.3))
            assert receipt is True
            assert service.close_intake_database(native_closed=receipt) is True
            assert disposals == [True]
            assert service._engine is None and service._factory is None
    finally:
        release.set()
        thread.join(1)
        assert not thread.is_alive()
        if manager._supervisor is not None:
            manager._supervisor.join(1)
            assert not manager._supervisor.is_alive()


@pytest.mark.parametrize("native_receipt", [False, None, True])
def test_lifespan_waits_for_http_future_before_intake_pool_close(monkeypatch, native_receipt):
    from dlr.common.config import settings
    from dlr.control import app as app_module
    from dlr.control.api import ingress
    from dlr.control.services.subscription_source import ApplicationBufferBudget

    service, engine, factory, disposals = _fake_intake_pool(monkeypatch)
    release, entered, shutdown_entered = (threading.Event() for _ in range(3))
    events = []
    native_thread = None
    if native_receipt is not True:
        native_entered = threading.Event()

        def native_owner():
            native_entered.set()
            release.wait()
            events.append("native_actual_finished")

        native_thread = threading.Thread(target=native_owner, name="late-native-close", daemon=True)
        native_thread.start()
        assert native_entered.wait(1)
    resources = ingress.IngressResources(
        buffer=ApplicationBufferBudget(), session_factory=lambda: None
    )
    assert resources.work.acquire()

    def native_job():
        entered.set()
        try:
            release.wait()
            if native_thread is not None:
                native_thread.join(1)
                assert not native_thread.is_alive()
        finally:
            resources.work.release()
            events.append("http_job_finished")

    future = resources.executor.submit(native_job)
    assert entered.wait(1)
    monkeypatch.setattr(ingress, "_resources", resources)
    shutdown = ingress.shutdown_ingress_resources

    def observed_shutdown():
        events.append("http_shutdown_enter")
        shutdown_entered.set()
        shutdown()
        assert future.done()
        events.append("http_shutdown_finished")

    monkeypatch.setattr(ingress, "shutdown_ingress_resources", observed_shutdown)
    dispose = engine.dispose

    def observed_dispose():
        assert future.done() and resources.work.used == 0
        events.append("pool_dispose")
        dispose()

    engine.dispose = observed_dispose

    class Runtime:
        def start(self):
            pass

        async def aclose(self):
            if native_thread is not None:
                assert native_thread.is_alive()
            events.append("native_close_receipt")
            return native_receipt

    async def background():
        await asyncio.Event().wait()

    monkeypatch.setattr(app_module, "get_runtime", Runtime)
    monkeypatch.setattr(settings, "rabbitmq_url", None)
    for name in (
        "_demo_bootstrap_loop",
        "attachment_gc_loop",
        "scheduler_loop",
        "retention_loop",
        "artifact_gc_loop",
        "orphan_audit_loop",
        "admission_reconciler_loop",
        "attempt_reconciler_loop",
        "infrastructure_dlq_loop",
        "subscription_maintenance_loop",
        "subscription_quota_audit_loop",
    ):
        monkeypatch.setattr(app_module, name, background)

    async def exercise():
        context = app_module.lifespan(SimpleNamespace())
        await context.__aenter__()
        closing = asyncio.create_task(context.__aexit__(None, None, None))
        try:
            assert await asyncio.to_thread(shutdown_entered.wait, 1)
            assert not future.done() and resources.work.used == 1
            assert not closing.done() and not disposals
            assert service._engine is engine and service._factory is factory
        finally:
            release.set()
            await asyncio.wait_for(closing, 2)
        assert future.done() and resources.work.used == 0

    try:
        asyncio.run(exercise())
        assert events.index("native_close_receipt") < events.index("http_shutdown_finished")
        if native_receipt is True:
            assert events.index("http_shutdown_finished") < events.index("pool_dispose")
            assert disposals == [True]
            assert service._engine is None and service._factory is None
        else:
            assert events.index("native_close_receipt") < events.index("native_actual_finished")
            assert events.index("native_actual_finished") < events.index("http_shutdown_finished")
            assert not disposals and "pool_dispose" not in events
            assert service._engine is engine and service._factory is factory
    finally:
        release.set()
        resources.executor.shutdown(wait=True)
        if native_thread is not None:
            native_thread.join(1)
            assert not native_thread.is_alive()
