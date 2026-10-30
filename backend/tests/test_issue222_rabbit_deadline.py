"""Actual Pika timer/blocking loops; no socket, Broker or database required."""

from collections import deque
from threading import Lock
from types import SimpleNamespace

import pika
import pytest
from pika.adapters.blocking_connection import BlockingChannel, BlockingConnection, _CallbackResult
from pika.adapters.select_connection import _Timer

from dlr.control.services.subscription_queue import Message, SourcePikaLogBoundary
from dlr.control.services.subscription_rabbitmq import RabbitMQTransport


class TimerLoop:
    """The timer is upstream Pika; only time and socket readiness are manual."""

    def __init__(self, clock):
        self.clock = clock
        self.timer = _Timer()
        self.polls = 0
        self.waits = []
        self.closed = False
        self.removed = []
        self.callbacks = deque()

    def call_later(self, delay, callback):
        self.waits.append(delay)
        return self.timer.call_later(delay, callback)

    def remove_timeout(self, handle):
        assert not self.closed, "Cancellation must not use the already-closed loop"
        self.removed.append(handle)
        self.timer.remove_timeout(handle)

    def poll(self):
        self.polls += 1
        assert self.polls <= 2, "Blocked flush outlived the native deadline"
        if self.polls == 1:
            self.clock[0] += 3
        else:
            assert self.callbacks, "Only asynchronous close can require another native turn"

    def process_timeouts(self):
        for _ in range(len(self.callbacks)):
            self.callbacks.popleft()()
        self.timer.process_timeouts()

    def close(self):
        self.closed = True
        self.timer.close()
        self.callbacks.clear()


@pytest.fixture
def owner(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(pika.compat, "time_now", lambda: clock[0])
    loop = TimerLoop(clock)
    aborts = []
    state = {"outbound": 1, "defer_close": False}
    result = _CallbackResult(lambda error: SimpleNamespace(error=error))
    connection = BlockingConnection.__new__(BlockingConnection)
    connection._ready_events = deque()
    connection._channels_pending_dispatch = set()
    connection._event_dispatch_suspend_depth = 0
    connection._closed_result = result
    connection._cleanup_mutex = Lock()
    native = SimpleNamespace(
        is_closed=False,
        ioloop=loop,
        _transport=object(),
        _get_write_buffer_size=lambda: state["outbound"],
    )

    def abort(error):
        aborts.append(error)
        # Match Pika: clear tx_buffers now, notify connection_lost on a
        # subsequent native callback turn rather than a user timer dispatch.
        state["outbound"] = 0

        def notify_closed():
            native.is_closed = True
            result.set_value_once(error)

        if state["defer_close"]:
            loop.callbacks.append(notify_closed)
        else:
            notify_closed()

    native._terminate_stream = abort

    # The blocking time_limit itself remains upstream code and uses the native
    # adapter timer. A callback handle has the same cancel() interface.
    def adapter_timer(delay, callback):
        handle = loop.call_later(delay, callback)

        def cancel():
            # Already-called/closed Pika handles are inert, including the
            # BlockingConnection time_limit context after its own cleanup.
            if handle.callback is not None:
                loop.remove_timeout(handle)

        return SimpleNamespace(cancel=cancel)

    native._adapter_call_later = adapter_timer
    native._adapter_remove_timeout = lambda handle: handle.cancel()
    connection._impl = native
    transport = RabbitMQTransport.__new__(RabbitMQTransport)
    transport.connection = connection
    boundary = SourcePikaLogBoundary()
    try:
        yield SimpleNamespace(
            clock=clock,
            loop=loop,
            aborts=aborts,
            connection=connection,
            native=native,
            transport=transport,
            state=state,
        )
    finally:
        boundary.close()
        loop.close()


@pytest.mark.parametrize("mode", ["per_event", "micro"])
@pytest.mark.parametrize("asynchronous_close", [False, True])
def test_native_deadline_interrupts_actual_blocked_ack_flush_before_user_timer_dispatch(
    owner, mode, asynchronous_close
):
    owner.state["defer_close"] = asynchronous_close
    sent, released = [], []
    implementation = SimpleNamespace(
        is_closed=False,
        basic_ack=lambda **kwargs: sent.append(kwargs["delivery_tag"]),
    )
    if mode == "per_event":
        channel = BlockingChannel.__new__(BlockingChannel)
        channel._impl = implementation
        channel._connection = owner.connection
        channel._closing_reason = None
        owner.transport.channel = channel
    else:
        owner.transport.channel = SimpleNamespace(_impl=implementation)
        owner.transport._native = SimpleNamespace(
            retiring=lambda tag: None,
            before_ack=lambda: None,
            resume_io=lambda **kwargs: None,
            released=lambda tag: released.append(tag),
            pause_io=lambda implementation: None,
        )
    with pytest.raises(pika.exceptions.StreamLostError):
        if mode == "per_event":
            owner.transport.ack(Message("fixture", b"fixture", {}, 1))
        else:
            owner.transport.ack_handles([1])
    assert len(owner.aborts) == 1 and owner.clock[0] == 3
    assert owner.loop.polls == (2 if asynchronous_close and mode == "per_event" else 1)
    assert owner.native._get_write_buffer_size() == 0
    assert sent == [1] and released == []
    assert owner.connection._ready_events == deque()
    if asynchronous_close and mode == "micro":
        assert not owner.native.is_closed and not owner.loop.closed
        assert len(owner.loop.callbacks) == 1  # actual close authority still pending
    else:
        assert owner.loop.closed and owner.loop.timer._timeout_heap is None


@pytest.mark.parametrize("exit_case", ["normal", "exception", "connection_closed", "loop_closed"])
def test_finally_cancels_on_success_failure_and_closed_owner_without_late_abort(owner, exit_case):
    try:
        with owner.transport.deadline():
            handle = owner.loop.timer._timeout_heap[0]
            if exit_case == "exception":
                raise ValueError("fixture")
            if exit_case == "connection_closed":
                owner.native.is_closed = True
            if exit_case == "loop_closed":
                owner.loop.close()
    except ValueError:
        assert exit_case == "exception"
    assert handle.callback is None
    owner.clock[0] = 30
    if not owner.loop.closed:
        owner.loop.process_timeouts()
    assert owner.aborts == []
    assert len(owner.loop.removed) == (0 if exit_case == "loop_closed" else 1)


def test_cancelled_previous_round_cannot_abort_new_round_on_same_owner(owner):
    with owner.transport.deadline():
        first = owner.loop.timer._timeout_heap[0]
    owner.clock[0] = 1
    with owner.transport.deadline():
        owner.clock[0] = 3
        owner.loop.process_timeouts()  # former round's deadline; new one is 4
        assert first.callback is None and owner.aborts == []
    owner.clock[0] = 30
    owner.loop.process_timeouts()
    assert owner.aborts == []


def test_timer_remains_owned_by_original_connection_if_caller_moves_on(owner):
    other_loop = TimerLoop(owner.clock)
    other_native = SimpleNamespace(is_closed=False, ioloop=other_loop)
    other_connection = SimpleNamespace(_impl=other_native, is_closed=False)
    other_calls = []
    other_handle = other_loop.call_later(3, lambda: other_calls.append("own timer"))
    try:
        with owner.transport.deadline():
            first = owner.loop.timer._timeout_heap[0]
            owner.transport.connection = other_connection
        assert first.callback is None and other_handle.callback is not None
        assert len(owner.loop.removed) == 1 and other_loop.removed == []
        owner.clock[0] = 3
        owner.loop.process_timeouts()
        other_loop.process_timeouts()
        assert owner.aborts == [] and other_calls == ["own timer"]
    finally:
        other_loop.close()


def test_deadline_does_not_abort_a_connection_already_closed_before_timer_fires(owner):
    with owner.transport.deadline():
        owner.native.is_closed = True
        owner.clock[0] = 3
        owner.loop.process_timeouts()
    assert owner.aborts == []
