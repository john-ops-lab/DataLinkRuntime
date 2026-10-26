"""Real Pika decoding/assembly, without a socket, before any payload allocation."""

import json
import os
import struct
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from types import SimpleNamespace

import pika
import pytest
from pika import frame, spec
from pika.adapters.select_connection import IOLoop, SelectConnection

from dlr.control.services import subscription_rabbit_budget as budget
from dlr.control.services import subscription_rabbitmq as source
from dlr.control.services.subscription_source import ApplicationBufferBudget, SourceFailure


class NativeWire:
    """Use the actual pinned decoder/assembler; only the socket is absent."""

    def __init__(self, *, owner_limit=2 * 1024**2, global_limit=64 * 1024**2):
        self.parent = ApplicationBufferBudget(global_limit)
        self.scope = self.parent.scope(owner_limit)
        self.connection = SelectConnection.__new__(SelectConnection)
        self.connection._frame_buffer = b""
        self.channel = pika.channel.Channel.__new__(pika.channel.Channel)
        self.channel.channel_number = 1
        self.channel._state = pika.channel.Channel.OPEN
        self.channel._content_assembler = pika.channel.ContentFrameAssembler()
        self.received = []
        self.channel._on_deliver = lambda *values: self.received.append(values)
        self.channel._on_unexpected_frame = lambda value: pytest.fail("Unexpected SDK frame")
        assert budget.NativeBudget.compatible(self.connection, self.channel)
        self.native = budget.NativeBudget(self.scope, self.channel)
        self.native.install(self.connection)

    def feed(self, value):
        self.connection._frame_buffer = value.marshal()
        consumed, decoded = self.connection._read_frame()
        assert consumed == len(self.connection._frame_buffer)
        self.connection._frame_buffer = b""
        self.channel._handle_content_frame(decoded)

    def method(self, tag):
        self.feed(frame.Method(1, spec.Basic.Deliver("consumer", tag, False, "", "events")))

    def header(self, size, **properties):
        self.feed(frame.Header(1, size, pika.BasicProperties(message_id="one", **properties)))

    def delivery(self, tag, body):
        self.method(tag)
        self.header(len(body))
        for offset in range(0, len(body), 65536):
            self.feed(frame.Body(1, body[offset : offset + 65536]))

    def close(self):
        self.received.clear()
        self.connection._frame_buffer = b""
        self.channel._content_assembler._reset()
        self.native.closed()


def test_checked_upstream_and_unknown_sdk_fallback(monkeypatch):
    wire = NativeWire()
    assert not budget.NativeBudget.compatible(wire.connection, SimpleNamespace())
    monkeypatch.setattr(pika, "__version__", "1.3.3")
    assert not budget.NativeBudget.compatible(wire.connection, wire.channel)
    monkeypatch.setattr(pika, "__version__", "1.3.2")
    monkeypatch.setattr(frame, "decode_frame", lambda data: (0, None))
    assert not budget.NativeBudget.compatible(wire.connection, wire.channel)
    wire.close()


@pytest.mark.parametrize("size", [512 * 1024 + 1, 196609])
def test_header_declared_impossible_payload_never_enters_sdk_decoder(monkeypatch, size):
    wire = NativeWire()
    wire.method(1)
    decoded = []
    original = frame.decode_frame

    def decode(value):
        decoded.append(value[0])
        return original(value)

    monkeypatch.setattr(frame, "decode_frame", decode)
    with pytest.raises(SourceFailure) as error:
        wire.header(size)
    assert error.value.code == "event_payload_too_large"
    assert decoded == []
    assert wire.channel._content_assembler._header_frame is None
    wire.close()
    assert wire.scope.used_bytes == wire.parent.used_bytes == 0


def test_metadata_is_reserved_before_properties_decode(monkeypatch):
    wire = NativeWire(owner_limit=400000)
    wire.method(1)
    decoded = []
    original = frame.decode_frame

    def decode(value):
        decoded.append(value[0])
        return original(value)

    monkeypatch.setattr(frame, "decode_frame", decode)
    with pytest.raises(SourceFailure) as error:
        wire.header(1, headers={"metadata": "x" * 40000})
    assert error.value.code == "source_buffer_capacity"
    assert error.value.disposition == "backoff"
    assert decoded == []
    assert wire.channel._content_assembler._header_frame is None
    wire.close()


def test_fragment_and_join_charge_precedes_allocation_and_real_finish_releases_scratch(
    monkeypatch,
):
    wire = NativeWire()
    wire.method(1)
    wire.header(180000)
    token = wire.native.tokens[1]
    assert token.reservation.amount == token.metadata + 360000
    expected = token.reservation.amount
    original = frame.decode_frame
    seen = []

    def decode(value):
        if value[0] == spec.FRAME_BODY:
            assert token.reservation.amount == expected + budget.FRAGMENT_OVERHEAD
            seen.append(len(value))
        return original(value)

    monkeypatch.setattr(frame, "decode_frame", decode)
    for fragment in [b"a" * 60000, b"b" * 60000, b"c" * 60000]:
        wire.feed(frame.Body(1, fragment))
        expected += budget.FRAGMENT_OVERHEAD
    assert len(seen) == 3 and len(wire.received) == 1
    assert wire.received[0][2] == b"a" * 60000 + b"b" * 60000 + b"c" * 60000
    assert token.assembled and token.fragments == 3
    assert token.reservation.amount == token.metadata + 180000
    assert wire.channel._content_assembler._body_fragments == []
    assert token.begin_processing()
    assert token.working > 0
    assert wire.parent.peak_bytes <= wire.scope.limit_bytes
    token.end_processing()
    wire.close()


def test_global_refusal_and_body_overrun_happen_before_decoder(monkeypatch):
    wire = NativeWire(global_limit=400000)
    wire.method(1)
    wire.header(4)
    other = wire.parent.scope()
    assert other.reserve(wire.parent.limit_bytes - wire.parent.used_bytes)
    called = []
    original = frame.decode_frame

    def decode(value):
        called.append(True)
        return original(value)

    monkeypatch.setattr(frame, "decode_frame", decode)
    with pytest.raises(SourceFailure, match="source_buffer_capacity"):
        wire.feed(frame.Body(1, b"four"))
    assert not called
    remaining = other.used_bytes
    wire.close()
    assert wire.parent.used_bytes == remaining
    other.release_all()
    assert wire.parent.used_bytes == 0


def test_overrun_and_oversized_wire_frame_are_rejected_before_decode(monkeypatch):
    wire = NativeWire()
    wire.method(1)
    wire.header(4)
    decoded = []
    monkeypatch.setattr(frame, "decode_frame", lambda data: decoded.append(True))
    with pytest.raises(SourceFailure, match="source_buffer_capacity"):
        wire.feed(frame.Body(1, b"oversize"))
    assert not decoded
    wire.connection._frame_buffer = struct.pack(">BHI", spec.FRAME_BODY, 1, budget.FRAME_MAX)
    with pytest.raises(SourceFailure, match="source_buffer_capacity"):
        wire.connection._read_frame()
    assert not decoded
    wire.close()


def test_current_and_next_window_share_budget_and_retiring_tags_are_not_live_credit():
    wire = NativeWire()
    for tag in range(1, 5):
        wire.delivery(tag, b"data")
    before = wire.scope.used_bytes
    wire.received.clear()  # owner has dropped real bodies before ACK handoff
    for tag in range(1, 5):
        wire.native.retiring(tag)
    assert wire.scope.used_bytes < before
    for tag in range(5, 9):
        wire.delivery(tag, b"next")
    assert len(wire.native.tokens) == 8
    assert sum(not token.retiring for token in wire.native.tokens.values()) == 4
    with pytest.raises(SourceFailure, match="source_buffer_capacity"):
        wire.method(9)
    assert wire.parent.peak_bytes <= wire.scope.limit_bytes
    for tag in range(1, 5):
        wire.native.released(tag)
    assert set(wire.native.tokens) == {5, 6, 7, 8}
    wire.close()
    assert wire.parent.used_bytes == 0


def test_processing_refusal_does_not_mutate_already_assembled_credit():
    wire = NativeWire(owner_limit=400000)
    wire.delivery(1, b"body")
    token = wire.native.tokens[1]
    extra = budget.Reservation(wire.scope)
    assert extra.resize(wire.scope.limit_bytes - wire.scope.used_bytes)
    before = token.reservation.amount
    assert not token.begin_processing()
    assert token.reservation.amount == before and token.working == 0
    extra.release()
    assert token.begin_processing()
    token.end_processing()
    wire.close()


def transport_shell(wire, *, closed):
    value = source.RabbitMQTransport.__new__(source.RabbitMQTransport)
    value._raw_ready = deque()
    value.failed_message = None
    value._native = wire.native
    value._native_cleaned = False
    value.channel = SimpleNamespace(_impl=wire.channel, _pending_events=deque())
    value.connection = SimpleNamespace(
        is_closed=closed,
        _impl=wire.connection,
        _closed_result=SimpleNamespace(reset=lambda: None),
        _cleanup=lambda: None,
    )
    return value


def test_unknown_native_close_keeps_credits_until_confirmed_close(monkeypatch):
    wire = NativeWire()
    wire.delivery(1, b"pending")
    wire.received.clear()
    value = transport_shell(wire, closed=False)
    value.connection._impl._terminate_stream = lambda error: None
    value.connection.process_data_events = lambda **kwargs: None
    clock = iter([0, 3])
    monkeypatch.setattr(source.time, "monotonic", lambda: next(clock))
    before = wire.scope.used_bytes
    with pytest.raises(SourceFailure) as error:
        value._close()
    assert error.value.code == "source_close_unconfirmed" and not error.value.closed
    assert wire.scope.used_bytes == before
    value.connection.is_closed = True
    value._close()
    assert wire.scope.used_bytes == wire.parent.used_bytes == 0


def test_native_capture_keeps_completed_prefix_before_later_header_failure():
    wire = NativeWire()
    value = transport_shell(wire, closed=False)
    value.config = {"cluster_id": "fixture", "vhost": "/", "queue": "events"}
    value.micro_t1 = True
    value._pending_failure = None
    value._consumer_started = True
    wire.channel._on_deliver = lambda method, header, body: value._capture(
        wire.channel, method.method, header.properties, body
    )
    wire.delivery(1, b"good")
    wire.method(2)
    with pytest.raises(SourceFailure):
        wire.header(512 * 1024 + 1)
    value._pending_failure = wire.native.fault
    first = value.poll_bounded(0)
    assert first.body == b"good" and first.handle == 1
    assert first.native_token.assembled
    with pytest.raises(SourceFailure, match="event_payload_too_large"):
        value.poll_bounded(0)
    first = None
    value.connection.is_closed = True
    value._close()
    assert wire.scope.used_bytes == 0


@pytest.mark.parametrize("body_size,metadata_size", [(1, 15000), (180000, 8000), (140000, 63000)])
def test_legal_large_metadata_single_message_preserves_original_envelope_limit(
    body_size, metadata_size
):
    from dlr.control.services import subscription_queue as queue

    wire = NativeWire()
    value = transport_shell(wire, closed=False)
    value.config = {"cluster_id": "fixture", "vhost": "/", "queue": "events"}
    value.micro_t1 = True
    value._pending_failure = None
    value._consumer_started = True
    wire.channel._on_deliver = lambda method, header, body: value._capture(
        wire.channel, method.method, header.properties, body
    )
    wire.method(1)
    wire.header(body_size, headers={"metadata": "🚀" + "x" * (metadata_size - 4)})
    for offset in range(0, body_size, 65536):
        wire.feed(frame.Body(1, b"x" * min(65536, body_size - offset)))
    message = value.poll_bounded(0)
    context = SimpleNamespace(
        subscription_id=1,
        config_version=1,
        source_epoch="epoch-1",
        source_config={"source_type": "rabbitmq", "cluster_id": "fixture"},
    )
    prepared = queue._prepare_micro(context, message)
    assert prepared is not None and prepared.error is None
    assert 0 < prepared.size <= 256 * 1024
    assert wire.scope.used_bytes <= 2 * 1024**2
    assert wire.parent.peak_bytes <= 2 * 1024**2
    prepared.value = prepared.message = None
    prepared.token.end_processing()
    prepared = message = None
    value.connection.is_closed = True
    value._close()
    assert wire.scope.used_bytes == 0


def test_property_scanner_matches_all_upstream_types_and_independent_property_flags():
    wire = NativeWire()
    wire.method(1)
    nested = {
        "string": "text",
        "bytes": b"\x00\xff",
        "integer": 2**63 - 1,
        "decimal": Decimal("0.25"),
        "time": datetime.now(UTC),
        "array": [None, True, {"nested": "value"}],
    }
    wire.header(
        1,
        content_type="application/octet-stream",
        content_encoding="binary",
        headers=nested,
        delivery_mode=2,
        priority=1,
        correlation_id="correlation",
        reply_to="reply",
        expiration="1000",
        timestamp=1,
        type="type",
        user_id="user",
        app_id="application",
        cluster_id="cluster",
    )
    wire.feed(frame.Body(1, b"x"))
    assert wire.native.tokens[1].assembled
    assert wire.received[0][1].properties.headers["integer"] == 2**63 - 1
    wire.close()


def test_property_preview_preserves_twelve_level_contract_and_4096_value_budget():
    from dlr.control.services.subscription_rabbit_metadata import json_shape

    for depth in (12, 13):
        wire = NativeWire()
        nested = None
        for _ in range(depth - 1):
            nested = {"child": nested}
        wire.method(1)
        if depth == 13:
            with pytest.raises(SourceFailure, match="source_properties_invalid"):
                wire.header(1, headers={"nested": nested})
        else:
            wire.header(1, headers={"nested": nested})
            wire.feed(frame.Body(1, b"x"))
            method, header, body = wire.received.pop()
            value = source.RabbitMQTransport.__new__(source.RabbitMQTransport)
            value.config = {"cluster_id": "fixture", "vhost": "/", "queue": "events"}
            message = value._message(method.method, header.properties, body, wire.native.tokens[1])
            assert json_shape(message.attributes).utf8 < 64 * 1024
        wire.close()
    for count in (4094, 4095):
        wire = NativeWire()
        wire.method(1)
        if count == 4095:
            with pytest.raises(SourceFailure, match="source_properties_invalid"):
                wire.header(1, headers={"items": [None] * count})
        else:
            wire.header(1, headers={"items": [None] * count})
            wire.feed(frame.Body(1, b"x"))
        wire.close()


@pytest.mark.parametrize(
    "value", [None, True, 42, 'x\n🚀é\\"', {"text": "中🚀", "list": [1, None]}]
)
def test_json_shape_tracks_actual_utf8_and_wide_serializer_storage(value):
    from dlr.control.services.subscription_rabbit_metadata import json_shape

    shape = json_shape(value)
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    ascii_json = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    assert (shape.utf8, shape.chars, shape.ascii) == (
        len(encoded.encode()),
        len(encoded),
        len(ascii_json),
    )
    assert shape.width * shape.chars + 80 >= encoded.__sizeof__()


class ManualLoop:
    def __init__(self, turns):
        self.turns = deque(turns)
        self.callback = None
        self.removed = []
        self.polls = 0
        self.waits = []

    def call_later(self, seconds, callback):
        assert 0 <= seconds <= 3
        self.waits.append(seconds)
        self.callback = callback
        return "native-timer"

    def remove_timeout(self, timer):
        self.removed.append(timer)

    def poll(self):
        self.polls += 1
        if self.turns:
            self.turns.popleft()()
        else:
            self.callback()

    def process_timeouts(self):
        pass


def polling_transport(wire, turns):
    value = transport_shell(wire, closed=False)
    value.config = {"cluster_id": "fixture", "vhost": "/", "queue": "events"}
    value.micro_t1 = True
    value._pending_failure = None
    value._consumer_started = True
    value.connection._closed_result.ready = False
    loop = ManualLoop(turns)
    wire.connection._nbio = SimpleNamespace(get_native_ioloop=lambda: loop)
    value.connection.process_data_events = lambda **kwargs: pytest.fail(
        "Collection must not wait for outbound flush"
    )
    wire.channel._on_deliver = lambda method, header, body: value._capture(
        wire.channel, method.method, header.properties, body
    )
    return value, loop


def test_first_poll_returns_on_complete_body_without_waiting_for_backpressured_output():
    wire = NativeWire()
    value, loop = polling_transport(wire, [lambda: wire.delivery(1, b"ready")])
    # Outbound state is deliberately nonempty. Native polling must ignore its
    # flush completion after a complete body arrives, including heartbeat I/O.
    wire.connection._get_write_buffer_size = lambda: 100000
    message = value.poll()
    assert message.body == b"ready" and loop.polls == 1
    assert loop.removed == ["native-timer"]
    assert value.poll_bounded(0) is None and loop.polls == 1
    message = None
    value.connection.is_closed = True
    value._close()


def test_collection_remaining_window_completes_partial_next_body_without_flush():
    wire = NativeWire()

    def initial_read():
        wire.delivery(1, b"first")
        wire.method(2)
        wire.header(4)
        wire.feed(frame.Body(1, b"ne"))

    value, loop = polling_transport(wire, [initial_read, lambda: wire.feed(frame.Body(1, b"xt"))])
    first = value.poll()
    assert first.body == b"first" and loop.polls == 1
    second = value.poll_bounded(0.003)
    assert second.body == b"next" and second.handle == 2
    assert loop.polls == 2 and loop.waits == [0.2, 0.003]
    assert loop.removed == ["native-timer"] * 2
    assert wire.parent.peak_bytes <= wire.scope.limit_bytes
    first = second = None
    value.connection.is_closed = True
    value._close()


def test_collection_ready_or_expired_or_faulted_never_waits_and_positive_wait_is_capped():
    wire = NativeWire()
    value, loop = polling_transport(wire, [])
    assert value.poll_bounded(0) is None
    assert value.poll_bounded(-1) is None
    assert loop.polls == 0 and loop.waits == []
    wire.delivery(1, b"ready")
    message = value.poll_bounded(0.004)
    assert message.body == b"ready" and loop.polls == 0 and loop.waits == []
    assert value.poll_bounded(1) is None
    assert loop.polls == 1 and loop.waits == [0.005]
    value._pending_failure = SourceFailure("source_buffer_capacity", "backoff")
    with pytest.raises(SourceFailure, match="source_buffer_capacity"):
        value.poll_bounded(0.003)
    assert loop.polls == 1 and loop.waits == [0.005]
    message = None
    value.connection.is_closed = True
    value._close()


def test_collection_deadline_stops_partial_body_without_ack_or_extra_read():
    wire = NativeWire()
    value, loop = polling_transport(wire, [])
    wire.method(1)
    wire.header(4)
    wire.feed(frame.Body(1, b"pa"))
    assert value.poll_bounded(0.002) is None
    assert loop.waits == [0.002] and loop.polls == 1
    assert wire.native.tokens[1].assembled is False
    assert wire.native.tokens[1].raw_size == 4
    assert value.poll_bounded(0) is None and loop.polls == 1
    value.connection.is_closed = True
    value._close()


def test_collection_next_header_fault_keeps_prior_message_and_safe_fault_code():
    wire = NativeWire()

    def bad_header():
        wire.method(2)
        wire.header(512 * 1024 + 1)

    value, loop = polling_transport(wire, [lambda: wire.delivery(1, b"prefix"), bad_header])
    first = value.poll()
    with pytest.raises(SourceFailure) as error:
        value.poll_bounded(0.004)
    assert first.body == b"prefix" and first.handle == 1
    assert error.value.code == "event_payload_too_large" and error.value.disposition == "blocked"
    assert loop.polls == 2 and loop.waits == [0.2, 0.004]
    first = None
    value.connection.is_closed = True
    value._close()


def test_partial_frames_and_empty_heartbeat_turns_have_a_native_timer():
    wire = NativeWire()

    def header():
        wire.method(1)
        wire.header(4)

    value, loop = polling_transport(
        wire, [lambda: None, header, lambda: wire.feed(frame.Body(1, b"done"))]
    )
    message = value.poll()
    assert message.body == b"done" and loop.polls == 3
    message = None
    assert value.poll() is None
    assert loop.polls == 4 and loop.removed == ["native-timer"] * 2
    value.connection.is_closed = True
    value._close()


def test_transport_caught_fault_and_channel_close_preserve_good_prefix_and_original_code():
    wire = NativeWire()

    def swallowed():
        wire.delivery(1, b"prefix")
        wire.method(2)
        try:
            wire.header(512 * 1024 + 1)
        except SourceFailure:
            # This is what the actual async socket reader does: abort later,
            # without propagating the original content error through poll().
            wire.channel._state = pika.channel.Channel.CLOSED

    value, loop = polling_transport(wire, [swallowed])
    message = value.poll()
    assert message.body == b"prefix" and loop.polls == 1
    with pytest.raises(SourceFailure) as error:
        value.poll_bounded(0)
    assert error.value.code == "event_payload_too_large"
    assert error.value.disposition == "blocked"
    message = None
    value.connection.is_closed = True
    value._close()


def test_broker_permission_channel_close_retains_existing_safe_configuration_classification():
    wire = NativeWire()

    def rejected():
        wire.channel._closing_reason = pika.exceptions.ChannelClosedByBroker(403, "private detail")
        wire.channel._state = pika.channel.Channel.CLOSED

    value, loop = polling_transport(wire, [rejected])
    with pytest.raises(SourceFailure) as error:
        value.poll()
    assert error.value.code == "source_queue_unavailable"
    assert error.value.disposition == "blocked"
    assert "private detail" not in str(error.value)
    assert loop.removed == ["native-timer"]
    value.connection.is_closed = True
    value._close()
    assert wire.channel._closing_reason is None and value.channel._closing_reason is None


def test_ack_flush_can_admit_next_window_while_retiring_only_identity_credits():
    wire = NativeWire()
    value, _loop = polling_transport(wire, [])
    for tag in range(1, 5):
        wire.delivery(tag, b"old")
    for _ in range(4):
        message = value.poll_bounded(0)
        assert message.native_token.assembled
    message = None
    acked = []
    wire.channel.basic_ack = lambda **kwargs: acked.append(kwargs["delivery_tag"])

    def flush(**kwargs):
        assert kwargs == {"time_limit": 0}
        assert all(wire.native.tokens[tag].retiring for tag in range(1, 5))
        assert all(wire.native.tokens[tag].reservation.amount == 4096 for tag in range(1, 5))
        for tag in range(5, 9):
            wire.delivery(tag, b"new")

    value.connection.process_data_events = flush
    value.connection.call_later = lambda *args: "ack-deadline"
    value.connection.remove_timeout = lambda timer: None
    value.ack_handles([1, 2, 3, 4])
    assert acked == [1, 2, 3, 4] and set(wire.native.tokens) == {5, 6, 7, 8}
    assert len(value._raw_ready) == 4 and wire.parent.peak_bytes <= 2 * 1024**2
    value.connection.is_closed = True
    value._close()
    assert wire.scope.used_bytes == 0


@pytest.mark.parametrize(
    "version,mode,credit",
    [("1.3.2", "business_batch", 4), ("unknown", "business_batch", 1), ("1.3.2", "per_event", 1)],
)
def test_transport_enables_four_credits_only_for_checked_opt_in_sdk(
    monkeypatch, version, mode, credit
):
    connection = SelectConnection.__new__(SelectConnection)
    connection._frame_buffer = b""
    constructor_loop = ManualLoop([])
    connection._nbio = SimpleNamespace(get_native_ioloop=lambda: constructor_loop)
    channel = pika.channel.Channel.__new__(pika.channel.Channel)
    channel.channel_number = 1
    channel._state = pika.channel.Channel.OPEN
    channel._content_assembler = pika.channel.ContentFrameAssembler()
    qos, consumers = [], []
    blocking_channel = SimpleNamespace(
        _impl=channel,
        _pending_events=deque(),
        queue_declare=lambda **kwargs: None,
        basic_qos=lambda **kwargs: qos.append(kwargs["prefetch_count"]),
        consume=lambda **kwargs: consumers.append(kwargs) or iter(()),
    )
    blocking = SimpleNamespace(
        _impl=connection,
        is_closed=False,
        _closed_result=SimpleNamespace(reset=lambda: None),
        channel=lambda: blocking_channel,
        call_later=lambda *args: 1,
        remove_timeout=lambda timer: None,
        process_data_events=lambda **kwargs: None,
        _cleanup=lambda: None,
    )
    connection._terminate_stream = lambda error: setattr(blocking, "is_closed", True)
    monkeypatch.setattr(pika, "__version__", version)
    monkeypatch.setattr(pika, "BlockingConnection", lambda parameters: blocking)
    scope = ApplicationBufferBudget().scope()
    value = source.RabbitMQTransport(
        {
            "source_type": "rabbitmq",
            "cluster_id": "fixture",
            "server": "amqp://broker.invalid:5672",
            "vhost": "/",
            "queue": "events",
            "credential_id": 1,
            "mode": mode,
        },
        {"username": "test", "password": "test"},
        diagnostic=False,
        budget=scope,
    )
    assert qos == [credit]
    assert value.micro_t1 == (credit == 4)
    assert len(consumers) == (0 if credit == 4 else 1)
    value.close()
    assert scope.used_bytes == 0


def loop_fds(loop):
    loop._poller.activate_poller()
    result = [loop._poller._r_interrupt.fileno(), loop._poller._w_interrupt.fileno()]
    selector = getattr(loop._poller, "_kqueue", None) or getattr(loop._poller, "_poll", None)
    if hasattr(selector, "fileno"):
        result.append(selector.fileno())
    return result


def fd_open(fd):
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


@pytest.mark.parametrize("closed_case", ["ordinary", "fault", "sdk_already_cleaned", "unknown"])
def test_real_ioloop_close_releases_only_own_fds_after_native_close_authority(
    closed_case, monkeypatch
):
    wire = NativeWire()
    wire.delivery(1, b"unacknowledged")
    wire.received.clear()
    loop, other_loop = IOLoop(), IOLoop()
    own_fds, other_fds = loop_fds(loop), loop_fds(other_loop)
    wire.connection._nbio = SimpleNamespace(get_native_ioloop=lambda: loop)
    wire.connection.connection_state = pika.connection.Connection.CONNECTION_CLOSED
    blocking = pika.BlockingConnection.__new__(pika.BlockingConnection)
    blocking._impl = wire.connection
    blocking._cleanup_mutex = Lock()
    blocking._ready_events = deque()
    blocking._closed_result = SimpleNamespace(reset=lambda: None)
    value = transport_shell(wire, closed=True)
    value.connection = blocking
    try:
        assert all(fd_open(fd) for fd in own_fds + other_fds)
        if closed_case == "fault":
            wire.native.fault = SourceFailure("event_payload_too_large")
            wire.channel._closing_reason = SourceFailure("event_payload_too_large")
        elif closed_case == "sdk_already_cleaned":
            blocking._cleanup()
        elif closed_case == "unknown":
            wire.connection.connection_state = pika.connection.Connection.CONNECTION_OPEN
            wire.connection._terminate_stream = lambda error: None
            clock = iter([0, 3])
            monkeypatch.setattr(source.time, "monotonic", lambda: next(clock))
            before = wire.scope.used_bytes
            with pytest.raises(SourceFailure) as error:
                value._close()
            assert not error.value.closed and error.value.code == "source_close_unconfirmed"
            assert wire.scope.used_bytes == before and not value._native_cleaned
            assert all(fd_open(fd) for fd in own_fds + other_fds)
            wire.connection.connection_state = pika.connection.Connection.CONNECTION_CLOSED
        value._close()
        assert not any(fd_open(fd) for fd in own_fds)
        assert all(fd_open(fd) for fd in other_fds)
        assert loop._timer._timeout_heap is None and wire.scope.used_bytes == 0
        assert value._native_cleaned
        value._close()  # repeated owner cleanup is idempotent
        assert all(fd_open(fd) for fd in other_fds)
    finally:
        blocking._cleanup()
        other_loop.close()
