"""Real Pika recv/decoder/assembler with a byte socket and no network."""

import errno
import ssl
from collections import deque
from threading import Event
from types import SimpleNamespace

import pika
import pytest
from pika import callback, frame, spec
from pika.adapters.select_connection import SelectConnection, _Timer
from pika.adapters.utils.io_services_utils import _AsyncPlaintextTransport, _AsyncSSLTransport

from dlr.control.services import subscription_queue as queue
from dlr.control.services import subscription_rabbitmq as rabbit
from dlr.control.services.event_envelope import validate_envelope
from dlr.control.services.subscription_rabbit_budget import NativeBudget, NativeReader
from dlr.control.services.subscription_source import ApplicationBufferBudget, SourceFailure


class ByteSocket:
    def __init__(self, data, *, tls=False):
        self.data = bytearray(data)
        self.reads = 0
        self.bytes_read = 0
        self.tls = tls

    def fileno(self):
        return 42

    def recv(self, maximum):
        if not self.data:
            if self.tls:
                raise ssl.SSLWantReadError(ssl.SSL_ERROR_WANT_READ, "fixture")
            raise BlockingIOError(errno.EWOULDBLOCK, "fixture")
        self.reads += 1
        value = bytes(self.data[:maximum])
        del self.data[:maximum]
        self.bytes_read += len(value)
        return value

    def pending(self):
        return len(self.data)


def wire_message(tag, body, metadata=""):
    return b"".join(
        [
            frame.Method(1, spec.Basic.Deliver("fixture", tag, False, "", "events")).marshal(),
            frame.Header(
                1,
                len(body),
                pika.BasicProperties(message_id=f"message-{tag}", headers={"metadata": metadata}),
            ).marshal(),
            *[
                frame.Body(1, body[offset : offset + 65536]).marshal()
                for offset in range(0, len(body), 65536)
            ],
        ]
    )


class ReadLoop:
    def __init__(self, clock, sock):
        self.clock, self.sock = clock, sock
        self.reader = None
        self.timer = _Timer()
        self.callbacks = deque()
        self.polls = 0

    def set_reader(self, fd, function):
        assert fd == 42
        self.reader = function

    def remove_reader(self, fd):
        assert fd == 42
        self.reader = None

    def remove_writer(self, fd):
        assert fd == 42

    def add_callback_threadsafe(self, function):
        self.callbacks.append(function)

    def call_later(self, delay, function):
        return self.timer.call_later(delay, function)

    def remove_timeout(self, handle):
        self.timer.remove_timeout(handle)

    def poll(self):
        self.polls += 1
        if self.reader is not None and self.sock.data:
            self.clock[0] += 0.00001
            self.reader()
        else:
            self.clock[0] += 0.201

    def process_timeouts(self):
        for _ in range(len(self.callbacks)):
            self.callbacks.popleft()()
        self.timer.process_timeouts()


class Stream:
    def __init__(self, monkeypatch, messages, *, parent=None, tls=False):
        self.clock = [0.0]
        monkeypatch.setattr(pika.compat, "time_now", lambda: self.clock[0])
        monkeypatch.setattr(rabbit.time, "monotonic", lambda: self.clock[0])
        self.sock = ByteSocket(
            b"".join(
                wire_message(index, body, metadata)
                for index, (body, metadata) in enumerate(messages, 1)
            ),
            tls=tls,
        )
        self.loop = ReadLoop(self.clock, self.sock)
        self.connection = SelectConnection.__new__(SelectConnection)
        self.connection._frame_buffer = b""
        self.connection._channels = {}
        self.connection.callbacks = callback.CallbackManager()
        self.connection.frames_received = 0
        self.connection.bytes_received = 0
        self.connection._heartbeat_checker = None
        self.connection._nbio = SimpleNamespace(get_native_ioloop=lambda: self.loop)
        self.channel = pika.channel.Channel.__new__(pika.channel.Channel)
        self.channel.channel_number = 1
        self.channel._state = pika.channel.Channel.OPEN
        self.channel._content_assembler = pika.channel.ContentFrameAssembler()
        self.channel._on_unexpected_frame = lambda value: pytest.fail("Unexpected frame")
        self.connection._channels[1] = self.channel
        kind = _AsyncSSLTransport if tls else _AsyncPlaintextTransport
        sdk = kind.__new__(kind)
        sdk._state = sdk._STATE_ACTIVE
        sdk._sock, sdk._nbio = self.sock, self.loop
        sdk._protocol = SimpleNamespace(data_received=self.connection._on_data_available)
        if tls:
            sdk._ssl_readable_action = sdk._consume
            sdk._ssl_writable_action = None
            sdk._tx_buffers = deque()
        self.connection._transport = sdk
        self.loop.set_reader(42, sdk._on_socket_readable)
        self.parent = parent or ApplicationBufferBudget()
        self.scope = self.parent.scope()
        self.native = NativeBudget(self.scope, self.channel)
        assert NativeBudget.compatible(self.connection, self.channel, require_reader=True)
        self.native.install(self.connection, protect_head=True)
        self.source = rabbit.RabbitMQTransport.__new__(rabbit.RabbitMQTransport)
        self.source.micro_t1, self.source._native = True, self.native
        self.source._raw_ready = deque()
        self.source._pending_failure = self.source.failed_message = None
        self.source._consumer_started = True
        self.source._native_cleaned = False
        self.source.log_boundary = queue.SourcePikaLogBoundary()
        self.source.config = {
            "source_type": "rabbitmq",
            "mode": "business_batch",
            "cluster_id": "fixture",
            "vhost": "/",
            "queue": "events",
        }
        self.sent, self.acked, self.aborted = [], [], []
        self.source.channel = SimpleNamespace(_impl=self.channel, _pending_events=deque())
        self.source.connection = SimpleNamespace(
            _impl=self.connection,
            is_closed=False,
            _closed_result=SimpleNamespace(ready=False, reset=lambda: None),
            _cleanup=lambda: self.loop.timer.close(),
        )
        self.channel._on_deliver = lambda method, header, body: self.source._capture(
            self.channel, method.method, header.properties, body
        )
        self.channel.basic_ack = lambda **kwargs: self.sent.append(kwargs["delivery_tag"])

        def abort(error):
            self.aborted.append(type(error).__name__)
            self.source.connection.is_closed = True

        sdk._initiate_abort = abort
        self.connection._terminate_stream = abort

        def flush(**kwargs):
            assert kwargs == {"time_limit": 0}
            assert self.native.reader.paused
            before = self.sock.reads
            sdk._on_socket_readable()  # a stale ready notification consumes zero
            assert self.sock.reads == before
            self.acked.extend(self.sent)
            self.sent.clear()

        self.source.connection.process_data_events = flush
        self.context = SimpleNamespace(
            subscription_id=1,
            config_version=1,
            source_epoch="epoch-1",
            source_config=self.source.config,
            stop_event=Event(),
            connected=lambda: None,
        )

    def prepare(self, message):
        return queue._prepare_micro(self.context, message)

    def ack(self, prepared):
        tags = [value.message.handle for value in prepared]
        for value in prepared:
            value.value = value.message = None
            value.token.end_processing()
        prepared.clear()
        self.source.ack_handles(tags)

    def close(self):
        self.source.connection.is_closed = True
        self.source.close()


def drive_stream(monkeypatch, messages, custody=None, *, lose_first_ack=False, parent=None):
    """Actual owner loop and SDK; only T1 commit and Broker ACK are simulated.

    The fake Broker exposes at most 16 unconfirmed deliveries, introducing
    another delivery only after the owner's ACK flush. This is not a DB or
    business-success oracle: custody stores only stable hashes for redelivery.
    """
    custody = {} if custody is None else custody
    stream = Stream(monkeypatch, messages[:16], parent=parent)
    pending = deque(enumerate(messages[16:], 17))
    groups = []
    monkeypatch.setattr(
        queue, "_capacity_decision", lambda context: queue._CapacityDecision(True, 16)
    )

    def receive(context, prepared):
        assert 1 <= len(prepared) <= 16
        assert sum(len(item.message.body) + item.size for item in prepared) <= 1024**2
        assert all(item.error is None for item in prepared)
        tags = []
        for item in prepared:
            identity = item.value["event"]["id"]
            stable, _ = validate_envelope(item.value)
            if identity in custody:
                assert custody[identity] == stable
            custody[identity] = stable
            tags.append(item.message.handle)
        groups.append(tags)
        return len(prepared), None, None

    def no_diagnosis(*args):
        pytest.fail("Native byte/ACK backpressure must not create a poison diagnosis")

    monkeypatch.setattr(queue, "_receive_micro", receive)
    monkeypatch.setattr(queue, "record_fault", no_diagnosis)
    original_flush = stream.source.connection.process_data_events

    def flush(**kwargs):
        if lose_first_ack:
            raise TimeoutError("simulated ACK result unavailable")
        old = len(stream.acked)
        before = stream.sock.reads
        original_flush(**kwargs)
        for _ in range(len(stream.acked) - old):
            if pending:
                tag, (body, metadata) = pending.popleft()
                stream.sock.data.extend(wire_message(tag, body, metadata))
        # Even new data available during ACK cannot be read until the next
        # head has its own reserved workspace and the native loop resumes.
        assert stream.native.reader.paused
        stream.connection._transport._on_socket_readable()
        assert stream.sock.reads == before
        if len(stream.acked) == len(messages):
            stream.context.stop_event.set()

    stream.source.connection.process_data_events = flush
    runner = queue.QueueSourceRunner()
    runner.open_context = lambda context: stream.source
    if lose_first_ack:
        with pytest.raises(SourceFailure, match="source_unavailable"):
            runner.run(stream.context)
    else:
        runner.run(stream.context)
        assert stream.acked == list(range(1, len(messages) + 1))
        assert not stream.sock.data and not stream.connection._frame_buffer
        assert not pending
    assert stream.scope.used_bytes == 0
    assert stream.scope.limit_bytes == 2 * 1024**2
    assert stream.parent.peak_bytes <= 64 * 1024**2
    assert stream.aborted == ["StreamLostError"]  # normal dedicated stream close
    return stream, custody, groups


def test_first_ready_stops_current_consume_before_prefetch_until_head_workspace(monkeypatch):
    stream = Stream(monkeypatch, [(b"x" * 2048, "")] * 16)
    try:
        first = stream.source.poll()
        assert first.handle == 1 and len(stream.native.tokens) == 1
        assert len(stream.connection._frame_buffer) < 4096
        assert stream.native.reader.paused and stream.loop.reader is None
        assert stream.sock.reads == 1
        decoded = stream.connection.frames_received
        stream.connection._on_data_available(b"")
        assert stream.connection.frames_received == decoded
        assert stream.native.tokens[1].working == 0
        assert stream.source.poll_bounded(0) is None
        value = stream.prepare(first)
        assert value is not None and value.error is None and stream.native.head_prepared
        prepared = [value]
        first = value = None
        for _ in range(15):
            message = stream.source.poll_bounded(0.005)
            assert message is not None
            value = stream.prepare(message)
            assert value is not None and value.error is None
            prepared.append(value)
        message = value = None
        assert len(prepared) == 16 and len(stream.native.tokens) == 16
        stream.ack(prepared)
        assert stream.acked == list(range(1, 17))
        assert stream.parent.peak_bytes <= 2 * 1024**2
    finally:
        stream.close()
    assert stream.scope.used_bytes == 0


@pytest.mark.parametrize(
    "body_size,metadata",
    [(180000, "x" * 8000), (140000, "🚀" + "x" * 62996)],
    ids=["near_inline", "wide_metadata"],
)
def test_large_legal_head_can_prepare_then_budget_pause_keeps_connection_for_prefix_ack(
    monkeypatch, body_size, metadata
):
    stream, custody, groups = drive_stream(monkeypatch, [(b"x" * body_size, metadata)] * 16)
    assert len(custody) == 16
    assert [tag for group in groups for tag in group] == list(range(1, 17))
    assert stream.source._pending_failure is None and stream.native.fault is None
    assert stream.parent.peak_bytes <= 2 * 1024**2


def test_next_windows_wait_through_ack_flush_then_resume_only_the_new_head(monkeypatch):
    stream, custody, groups = drive_stream(monkeypatch, [(b"small" * 100, "")] * 48)
    assert len(custody) == 48 and len(groups) >= 3
    assert all(len(group) <= 16 for group in groups)
    assert stream.parent.peak_bytes <= 2 * 1024**2


def test_zero_body_header_completion_still_pauses_before_the_next_delivery(monkeypatch):
    stream, custody, groups = drive_stream(monkeypatch, [(b"", "")] * 32)
    assert len(custody) == 32
    assert [tag for group in groups for tag in group] == list(range(1, 33))
    assert stream.parent.peak_bytes <= 2 * 1024**2


def test_header_reservation_soft_pause_can_ack_prefix_and_finish_same_connection(monkeypatch):
    stream = Stream(monkeypatch, [(b"x" * 180000, "x" * 8000)] * 2)
    pressure = stream.parent.scope(64 * 1024**2)
    prepared = []
    try:
        message = stream.source.poll()
        value = stream.prepare(message)
        assert value is not None and value.error is None
        prepared.append(value)
        message = value = None
        # There is space for the normal frame workspace and Method, but not
        # the next large Header's raw/join reservation. No limit is changed.
        assert pressure.reserve(stream.parent.limit_bytes - stream.parent.used_bytes - 384 * 1024)
        assert stream.source.poll_bounded(0.005) is None
        assert stream.native.soft_blocked and stream.native.current.tag == 2
        assert stream.native.current.raw_size is None
        assert stream.native.fault is None and stream.source._pending_failure is None
        assert not stream.aborted
        stream.ack(prepared)
        assert stream.acked == [1]
        message = stream.source.poll()
        assert message is not None and message.handle == 2
        value = stream.prepare(message)
        assert value is not None and value.error is None
        prepared.append(value)
        message = value = None
        stream.ack(prepared)
        assert stream.acked == [1, 2] and not stream.aborted
        assert stream.parent.peak_bytes <= 64 * 1024**2
    finally:
        for value in prepared:
            value.value = value.message = None
            value.token.end_processing()
        prepared.clear()
        stream.close()
        pressure.release_all()


def test_successful_micro_runner_propagates_unknown_close_without_releasing_scope(monkeypatch):
    stream = Stream(monkeypatch, [(b"body", "")])
    runner = queue.QueueSourceRunner()
    runner.open_context = lambda context: stream.source
    runner._run_micro = lambda context, transport: None
    monkeypatch.setattr(
        queue, "_capacity_decision", lambda context: queue._CapacityDecision(True, 16)
    )
    before = stream.scope.used_bytes
    close = stream.source.close

    def unknown_close():
        raise SourceFailure("source_close_unconfirmed", closed=False)

    monkeypatch.setattr(stream.source, "close", unknown_close)
    try:
        with pytest.raises(SourceFailure) as error:
            runner.run(stream.context)
        assert error.value.code == "source_close_unconfirmed" and not error.value.closed
        assert before > 0 and stream.scope.used_bytes == stream.parent.used_bytes == before
        assert not stream.source.connection.is_closed
    finally:
        monkeypatch.setattr(stream.source, "close", close)
        stream.close()
    assert stream.scope.used_bytes == stream.parent.used_bytes == 0


def test_ssl_stale_read_and_writer_reregistration_cannot_recv_while_paused(monkeypatch):
    stream = Stream(monkeypatch, [(b"body" * 512, "")] * 2, tls=True)
    prepared = None
    try:
        message = stream.source.poll()
        assert message is not None and message.handle == 1
        assert stream.native.reader.paused
        reads = stream.sock.reads
        sdk = stream.connection._transport
        sdk._on_socket_readable()  # queued SSL pending-plaintext callback
        assert stream.sock.reads == reads
        sdk._ssl_readable_action = None
        sdk._ssl_writable_action = sdk._produce
        sdk._produce()  # upstream may register readability after a write
        assert stream.loop.reader is not None
        stream.loop.reader()
        assert stream.sock.reads == reads
        prepared = stream.prepare(message)
        assert prepared is not None and prepared.error is None
        message = None
        # After workspace ownership transfers, the same real SSL reader can
        # progress. No actual TLS connection/socket was created by this test.
        next_message = stream.source.poll_bounded(0.005)
        assert next_message is not None and next_message.handle == 2
        assert stream.sock.reads > reads
        next_message = None
    finally:
        if prepared is not None:
            prepared.value = prepared.message = None
            prepared.token.end_processing()
        stream.close()


def test_mixed_legal_messages_and_ack_loss_redelivery_make_continuous_progress(monkeypatch):
    messages = [
        (b"x" * 180000, "x" * 8000),
        (b"y" * 140000, "🚀" + "x" * 62996),
        (b"medium" * 4000, "ordinary"),
        (b"small" * 20, ""),
    ] * 12
    first, custody, groups = drive_stream(monkeypatch, messages, lose_first_ack=True)
    assert not first.acked and custody and groups
    unique_before = len(custody)
    second, custody, groups = drive_stream(monkeypatch, messages, custody)
    assert unique_before <= len(custody) == 48
    assert [tag for group in groups for tag in group] == list(range(1, 49))
    assert first.parent.peak_bytes <= second.scope.limit_bytes
    assert second.parent.peak_bytes <= second.scope.limit_bytes


def test_global_parent_reservations_remain_held_while_the_owner_progresses(monkeypatch):
    parent = ApplicationBufferBudget()
    other = parent.scope(64 * 1024**2)
    reserved = 62 * 1024**2
    assert other.reserve(reserved)
    try:
        stream, custody, _ = drive_stream(
            monkeypatch, [(b"large" * 20000, "metadata")] * 16, parent=parent
        )
        assert len(custody) == 16 and stream.scope.used_bytes == 0
        assert other.used_bytes == parent.used_bytes == reserved
        assert parent.peak_bytes <= parent.limit_bytes
    finally:
        other.release_all()
    assert parent.used_bytes == 0


def test_a_seventeenth_unconfirmed_delivery_never_becomes_a_native_credit(monkeypatch):
    stream = Stream(monkeypatch, [(b"body", "")] * 17)
    prepared = []
    try:
        message = stream.source.poll()
        value = stream.prepare(message)
        assert value is not None and value.error is None
        prepared.append(value)
        message = value = None
        for _ in range(15):
            message = stream.source.poll_bounded(0.005)
            assert message is not None
            value = stream.prepare(message)
            assert value is not None and value.error is None
            prepared.append(value)
        message = value = None
        with pytest.raises(SourceFailure) as error:
            stream.source.poll_bounded(0.005)
        assert error.value.code == "source_buffer_capacity"
        assert error.value.disposition == "backoff"
        assert len(stream.native.tokens) == 16 and 17 not in stream.native.tokens
        assert stream.parent.peak_bytes <= 2 * 1024**2
    finally:
        for value in prepared:
            value.message = value.value = None
            value.token.end_processing()
        prepared.clear()
        stream.close()


def test_reader_guard_unavailable_is_incompatible_and_unknown_close_keeps_budget(monkeypatch):
    stream = Stream(monkeypatch, [(b"body", "")])
    message = stream.source.poll()
    assert message.body == b"body"
    before = stream.scope.used_bytes
    stream.connection._terminate_stream = lambda error: None
    stream.source.connection.process_data_events = lambda **kwargs: None
    clock = iter([0, 3])
    monkeypatch.setattr(rabbit.time, "monotonic", lambda: next(clock))
    with pytest.raises(SourceFailure) as error:
        stream.source._close()
    assert not error.value.closed and stream.scope.used_bytes == before
    assert stream.native.reader.paused
    stream.connection._transport = SimpleNamespace()
    assert not NativeReader.compatible(stream.connection)
    message = None
    stream.close()
    assert stream.scope.used_bytes == 0
