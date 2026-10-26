"""Real intake exhaustion cannot consume the management connection reserve."""

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
            with session_factory() as management:
                assert management.scalar(text("SELECT 1")) == 1
            with factory() as denied, pytest.raises(TimeoutError):
                denied.scalar(text("SELECT 1"))
        with factory() as resumed:
            assert resumed.scalar(text("SELECT 1")) == 1
    finally:
        close_intake_database()


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
