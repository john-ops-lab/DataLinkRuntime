"""Deployment and disk guards never become message poison on existing sources."""

import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from dlr.control.models import EventReceipt, SourceCheckpoint
from dlr.control.services import subscription_kafka as kafka
from dlr.control.services import subscription_kubernetes as kubernetes
from dlr.control.services import subscription_queue as queue
from dlr.control.services.subscription_source import ApplicationBufferBudget, SourceFailure
from test_issue219_kafka import FakeTransport, record
from test_issue219_kafka import context as kafka_context

# Reuse the real PostgreSQL source fixture under its existing pytest name.
context = kafka_context

CODES = ("ingress_capacity_unavailable", "ingress_disk_low", "ingress_disk_unavailable")


@pytest.mark.parametrize("code", CODES)
@pytest.mark.parametrize(
    "classify", [kafka.classify_error, kubernetes.classify_error, queue.classify]
)
def test_platform_failure_is_retryable_source_state(classify, code):
    failure = classify(HTTPException(503, detail={"code": code}))
    assert failure.code == code
    assert failure.disposition == "backoff"


@pytest.mark.parametrize("code", CODES)
def test_kafka_platform_rejection_does_not_persist_unaccepted_barrier(
    context,
    monkeypatch,
    code,
):
    transport = FakeTransport(context, [record(0)])
    runner = kafka.KafkaSourceRunner(lambda *args: transport)

    def unavailable(*args, **kwargs):
        raise HTTPException(503, detail={"code": code})

    monkeypatch.setattr(runner, "_receive", unavailable)
    with pytest.raises(SourceFailure) as result:
        runner.run(context)
    assert result.value.code == code
    assert result.value.disposition == "backoff"
    assert transport.closed
    with context.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(EventReceipt)) == 0
        checkpoints = session.scalars(select(SourceCheckpoint)).all()
        assert checkpoints and all(item.blocked_unaccepted is None for item in checkpoints)
    # Only initial safe source-position synchronization is allowed; no event ACK.
    assert all(all(offset == 0 for offset in commit.values()) for commit in transport.commits)


@pytest.mark.parametrize("code", CODES)
def test_kubernetes_platform_wait_resumes_same_message_without_a_gap(monkeypatch, code):
    result = object()
    attempts = []
    pressured = []

    @contextmanager
    def transaction():
        yield object()

    def receive(session, context, envelope):
        attempts.append(envelope)
        if len(attempts) == 1:
            raise HTTPException(503, detail={"code": code})
        return result

    buffer = ApplicationBufferBudget()
    ctx = SimpleNamespace(
        buffer=buffer,
        stop_event=threading.Event(),
        session_factory=SimpleNamespace(begin=transaction),
    )
    envelope = {"same": "original"}
    monkeypatch.setattr(kubernetes, "validate_envelope", lambda value: ("fixture-hash", 8))
    monkeypatch.setattr(kubernetes.sync_service, "receive_watch_event", receive)
    monkeypatch.setattr(
        kubernetes.sync_service,
        "mark_sync_backpressure",
        lambda session, context: pressured.append(context),
    )
    runner = kubernetes.KubernetesSourceRunner()
    assert runner._receive(ctx, envelope) is result
    assert attempts == [envelope, envelope]
    assert pressured == [ctx]
    assert buffer.used_bytes == 0 and buffer.peak_bytes == 8
