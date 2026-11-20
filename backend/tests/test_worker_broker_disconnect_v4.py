"""An AMQP disconnect cannot interrupt a provable in-flight execution."""

import ssl
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pika
import pytest

from dlr.worker import broker_config_v4, consumer_v4


@pytest.mark.parametrize(
    "value,expected", [(None, 30), ("false", 30), ("true", 10), ("invalid", None)]
)
def test_standalone_broker_heartbeat_is_opt_in(monkeypatch, tmp_path, value, expected):
    if value is None:
        monkeypatch.delenv("DLR_RABBITMQ_HA_ENABLED", raising=False)
    else:
        monkeypatch.setenv("DLR_RABBITMQ_HA_ENABLED", value)
    tls = ssl.create_default_context()
    monkeypatch.setattr(ssl, "create_default_context", lambda *, cafile: tls)
    config = broker_config_v4.BrokerConfigV4(
        1, "broker.example", 5671, "test", "worker", "test", tmp_path / "ca.pem"
    )
    if expected is None:
        with pytest.raises(broker_config_v4.BrokerConfigurationError):
            config.connection_parameters()
    else:
        parameters = config.connection_parameters()
        assert parameters.heartbeat == expected
        assert (
            parameters.ssl_options.context is tls
            and parameters.ssl_options.server_hostname == config.host
        )


@pytest.mark.parametrize("outcome", ["ack", "unknown"])
def test_reconnect_only_after_resolving_the_active_delivery(monkeypatch, tmp_path, outcome):
    stop, staging, disconnected = Event(), Event(), Event()
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

    claim = object()

    def prepare(**kwargs):
        assert kwargs["dispatch"] == b"same delivery"
        if disconnected.is_set():
            calls.append(("redelivery_claim", stop.is_set()))
            return SimpleNamespace(state="decision", decision="ACK_NOOP")
        return SimpleNamespace(state="claimed", claimed=claim)

    def stage(claimed, **_kwargs):
        assert claimed is claim
        staging.set()
        assert disconnected.wait(timeout=2)
        calls.append(("staging_resumed", stop.is_set()))
        return claim if outcome == "ack" else None

    monkeypatch.setattr(
        consumer_v4,
        "deserialize_dispatch_message",
        lambda _: SimpleNamespace(target_worker_id=1, language="python"),
    )
    monkeypatch.setattr(consumer._preparer, "prepare", prepare)
    monkeypatch.setattr(consumer._preparer, "stage_execution", stage)
    owner = SimpleNamespace(
        observe=lambda **_kwargs: SimpleNamespace(state="observed", stop_reason=None),
        build_result=lambda: object(),
    )
    monkeypatch.setattr(
        consumer_v4.execution_supervision_v4, "start_execution", lambda *_args, **_kwargs: owner
    )
    monkeypatch.setattr(
        consumer_v4.result_exchange_v4, "submit_result", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(consumer_v4.cleanup_execution_v4, "settle_execution", lambda *_args: True)

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
                assert staging.wait(timeout=2)
                disconnected.set()
                raise pika.exceptions.ConnectionClosedByBroker(320, "shutdown")

        def basic_ack(self, *, delivery_tag):
            calls.append(("broker_ack", delivery_tag))
            stop.set()

        def close(self):
            pass

    sessions = iter((Connection(1), Connection(2)))
    monkeypatch.setattr(consumer_v4.pika, "BlockingConnection", lambda _: next(sessions))
    consumer.run()

    assert calls[0] == ("staging_resumed", False)
    if outcome == "ack":
        assert calls[1:] == [("redelivery_claim", False), ("broker_ack", 2)]
        assert not consumer.failed.is_set()
    else:
        assert len(calls) == 1
        assert consumer.failed.is_set()
