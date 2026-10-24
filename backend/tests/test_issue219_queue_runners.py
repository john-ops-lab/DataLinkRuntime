"""Focused queue custody/failure windows, plus isolated opt-in real RabbitMQ."""

import json
import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pika
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from dlr.control.models import EventReceipt, EventSubscription, SourceCheckpoint, SubscriptionLease
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services import subscription_queue as queue
from dlr.control.services.subscription_rabbitmq import (
    RabbitMQTransport,
    guard_content,
    typed_property,
)
from dlr.control.services.subscription_source import SourceFailure
from test_issue219_kafka import context as kafka_context

context = kafka_context


@pytest.fixture
def rabbit_context(context):
    config = {
        "source_type": "rabbitmq",
        "cluster_id": "fixture",
        "credential_id": 1,
        "server": "amqp://broker.invalid:5672",
        "vhost": "/",
        "queue": "events",
    }
    with context.session_factory.begin() as session:
        session.get(EventSubscription, context.subscription_id).source_type = "rabbitmq"
    return replace(context, source_config=config)


def message(identity="one", body=b"\x00\xff", handle=1):
    return queue.Message(
        identity,
        body,
        {
            "cluster_id": "fixture",
            "vhost": "/",
            "queue": "events",
            "exchange": "",
            "routing_key": "events",
            "properties": {},
        },
        handle,
    )


def counts(context):
    with context.session_factory() as session:
        return session.scalar(select(func.count()).select_from(EventReceipt))


def test_queue_rollback_then_redelivery_and_postcommit_duplicate(rabbit_context, monkeypatch):
    original = delivery.receive_event

    def failed(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("database lost before commit")

    monkeypatch.setattr(delivery, "receive_event", failed)
    with pytest.raises(RuntimeError):
        queue.take_custody(rabbit_context, message())
    assert counts(rabbit_context) == 0 and rabbit_context.buffer.used_bytes == 0
    monkeypatch.setattr(delivery, "receive_event", original)
    assert queue.take_custody(rabbit_context, message())
    assert queue.take_custody(rabbit_context, message(handle=99))
    assert counts(rabbit_context) == 1
    with pytest.raises(HTTPException) as error:
        queue.take_custody(rabbit_context, message(body=b"changed"))
    assert error.value.detail["code"] == "event_identity_conflict"


def test_stop_and_stale_owner_never_take_custody(rabbit_context):
    rabbit_context.stop_event.set()
    assert not queue.take_custody(rabbit_context, message())
    rabbit_context.stop_event.clear()
    with rabbit_context.session_factory.begin() as session:
        session.get(SubscriptionLease, rabbit_context.subscription_id).generation += 1
    with pytest.raises(HTTPException):
        queue.take_custody(rabbit_context, message())
    assert counts(rabbit_context) == 0


def test_ack_follows_committed_t1_and_ack_loss_does_not_erase_custody(rabbit_context):
    class Transport:
        closed = False

        def poll(self):
            return message()

        def ack(self, value):
            assert counts(rabbit_context) == 1
            raise TimeoutError("sensitive upstream details")

        def close(self):
            self.closed = True

    transport = Transport()
    runner = queue.QueueSourceRunner()
    runner.open = lambda *a, **k: transport
    with pytest.raises(SourceFailure, match="source_unavailable"):
        runner.run(rabbit_context)
    assert transport.closed and counts(rabbit_context) == 1


def test_invalid_id_and_capacity_close_without_ack(rabbit_context, monkeypatch):
    class Transport:
        acked = False
        closed = False

        def poll(self):
            return message(identity="")

        def ack(self, value):
            self.acked = True

        def close(self):
            self.closed = True

    transport = Transport()
    runner = queue.QueueSourceRunner()
    runner.open = lambda *a, **k: transport
    with pytest.raises(SourceFailure, match="event_content_invalid"):
        runner.run(rabbit_context)
    assert not transport.acked and transport.closed and counts(rabbit_context) == 0
    with rabbit_context.session_factory() as session:
        checkpoint = session.scalar(select(SourceCheckpoint))
        assert checkpoint.blocked_unaccepted["code"] == "event_content_invalid"
    monkeypatch.setattr(queue, "capacity_ready", lambda _: False)
    runner.open = lambda *a, **k: pytest.fail("capacity must prevent connection")
    with pytest.raises(SourceFailure, match="subscription_capacity_exceeded"):
        runner.run(rabbit_context)


def test_unconfirmed_close_retains_native_budget(rabbit_context):
    class Transport:
        def poll(self):
            rabbit_context.stop_event.set()
            return None

        def close(self):
            raise RuntimeError("unclosed")

    runner = queue.QueueSourceRunner()
    runner.open = lambda *a, **k: Transport()
    with pytest.raises(SourceFailure) as error:
        runner.run(rabbit_context)
    assert error.value.closed is False


def test_pika_guard_rejects_before_assembler_and_typed_properties():
    calls = []
    channel = SimpleNamespace(_handle_content_frame=calls.append)
    guard_content(channel)
    header = pika.frame.Header(1, 512 * 1024 + 1, pika.BasicProperties())
    with pytest.raises(SourceFailure, match="event_payload_too_large"):
        channel._handle_content_frame(header)
    assert not calls
    assert typed_property(b"\x00\xff")["base64"] == "AP8="
    with pytest.raises(SourceFailure):
        typed_property([[[[[[[[[[[[[[0]]]]]]]]]]]]]])


@pytest.mark.skipif(not os.environ.get("DLR_QUEUE_FIXTURE_FILE"), reason="isolated broker opt-in")
def test_real_rabbitmq_passive_diagnostic_redelivery_qos_guard_and_close(rabbit_context):
    private = json.loads(Path(os.environ["DLR_QUEUE_FIXTURE_FILE"]).read_text())["rabbitmq"]
    credentials = {"username": private["username"], "password": private["password"]}
    config = {
        **rabbit_context.source_config,
        "server": f"amqp://{private['host']}:{private['port']}",
    }
    # Test owns this isolated queue; the product driver only passively checks it.
    producer = pika.BlockingConnection(
        pika.ConnectionParameters(
            host=private["host"],
            port=private["port"],
            credentials=pika.PlainCredentials(**credentials),
        )
    )
    channel = producer.channel()
    channel.queue_declare(queue=config["queue"], durable=True)
    channel.queue_purge(queue=config["queue"])
    body = b"\x00\xffsource-binary"
    channel.basic_publish(
        "",
        config["queue"],
        body,
        pika.BasicProperties(message_id="real-one", headers={"raw": b"\x00\xff"}),
    )
    diagnostic = RabbitMQTransport(config, credentials, diagnostic=True)
    assert diagnostic.diagnostic()["source_type"] == "rabbitmq"
    diagnostic.close()
    assert channel.queue_declare(queue=config["queue"], passive=True).method.message_count == 1
    first = RabbitMQTransport(config, credentials, diagnostic=False)
    item = first.poll()
    assert item.body == body
    started = time.monotonic()
    first.close()  # no custody, no ACK -> source redelivery
    assert time.monotonic() - started < 2.5
    second = RabbitMQTransport(config, credentials, diagnostic=False)
    redelivery = second.poll()
    assert redelivery.message_id == item.message_id
    assert queue.take_custody(replace(rabbit_context, source_config=config), redelivery)
    second.close()  # committed but ACK lost
    third = RabbitMQTransport(config, credentials, diagnostic=False)
    duplicate = third.poll()
    assert queue.take_custody(replace(rabbit_context, source_config=config), duplicate)
    third.ack(duplicate)
    third.close()
    assert counts(rabbit_context) == 1
    channel.basic_publish(
        "", config["queue"], b"x" * (512 * 1024 + 1), pika.BasicProperties(message_id="oversized")
    )
    oversized = RabbitMQTransport(config, credentials, diagnostic=False)
    try:
        with pytest.raises(SourceFailure, match="event_payload_too_large"):
            oversized.poll()
    finally:
        oversized.close()
    channel.queue_purge(queue=config["queue"])
    producer.close()
