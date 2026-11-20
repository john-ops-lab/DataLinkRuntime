"""An AMQP disconnect cannot interrupt a provable in-flight execution."""

from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pika
import pytest

from dlr.worker import consumer_v4


@pytest.mark.parametrize("outcome", ["ack", "unknown"])
def test_reconnect_only_after_resolving_the_active_delivery(monkeypatch, tmp_path, outcome):
    stop, supervised, disconnected, finished = Event(), Event(), Event(), Event()
    calls = []
    config = SimpleNamespace(queue="worker-queue", connection_parameters=lambda: None)
    monkeypatch.setattr(
        consumer_v4.broker_config_v4.BrokerConfigV4,
        "from_private_file",
        lambda *_args, **_kwargs: config,
    )
    consumer = consumer_v4.FixedWorkerConsumer(
        broker_file=tmp_path / "broker.json",
        lease=SimpleNamespace(_worker_id=1, _identity_id=uuid4()),
        root_lock=None,
        runtime_root=tmp_path,
        attempt_journal_root=tmp_path,
        sandbox_config=None,
        budget=None,
        receive_limits=None,
        child_address_space_bytes=1,
        wire_budget=1,
        result_wire_budget=1,
        stop=stop,
    )

    def execute(body, **kwargs):
        assert body == b"same delivery"
        if not finished.is_set():
            kwargs["supervised"].set()
            supervised.set()
            assert disconnected.wait(timeout=2)
            calls.append(("execution_completed", stop.is_set()))
            finished.set()
            return outcome
        calls.append(("redelivery_claim", stop.is_set()))
        return "ack"

    class Connection:
        def __init__(self, tag):
            self.tag = tag
            self.delivered = False

        def channel(self):
            return self

        def basic_qos(self, *, prefetch_count):
            assert prefetch_count == 1

        def basic_consume(self, *, queue, on_message_callback, auto_ack):
            assert queue == "worker-queue" and not auto_ack
            self.deliver = on_message_callback

        def process_data_events(self, *, time_limit):
            if self.delivered:
                return
            self.delivered = True
            self.deliver(self, SimpleNamespace(delivery_tag=self.tag), None, b"same delivery")
            if self.tag == 1:
                assert supervised.wait(timeout=2)
                disconnected.set()
                raise pika.exceptions.ConnectionClosedByBroker(320, "shutdown")

        def basic_ack(self, *, delivery_tag):
            calls.append(("broker_ack", delivery_tag))
            stop.set()

        def close(self):
            pass

    sessions = iter((Connection(1), Connection(2)))
    monkeypatch.setattr(consumer_v4.pika, "BlockingConnection", lambda _: next(sessions))
    monkeypatch.setattr(consumer, "_execute", execute)
    consumer.run()

    assert calls[0] == ("execution_completed", False)
    if outcome == "ack":
        assert calls[1:] == [("redelivery_claim", False), ("broker_ack", 2)]
        assert not consumer.failed.is_set()
    else:
        assert len(calls) == 1
        assert consumer.failed.is_set()
