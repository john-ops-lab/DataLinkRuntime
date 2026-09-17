"""Deterministic protocol tests for the one-consumer-per-slot Worker transport."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pika
import pytest
from pika import frame as pika_frame
from pika import spec as pika_spec
from pika.adapters.select_connection import IOLoop as PikaIOLoop
from pika.callback import CallbackManager
from pika.channel import Channel

from dlr.worker import workspace
from dlr.worker.client import ClientError, ControlClient, ControlUnavailableError
from dlr.worker.consumer import ConsumerConfig, SlotTicket, V3Consumer, _ConnectionEpoch
from worker_runtime_support import unit_resource_envelope, unit_sandbox_config


@dataclass
class _Timer:
    callback: Callable[[], None]
    cancelled: bool = False


class _IOLoop:
    def __init__(self) -> None:
        self.callbacks: deque[Callable[[], None]] = deque()
        self.timers: list[_Timer] = []
        self.stopped = False
        self.closed = False
        self.start_error: Exception | None = None
        self.reject_callbacks = False

    def call_later(self, _delay: float, callback: Callable[[], None]) -> _Timer:
        timer = _Timer(callback)
        self.timers.append(timer)
        return timer

    @staticmethod
    def remove_timeout(timer: _Timer) -> None:
        timer.cancelled = True

    def add_callback_threadsafe(self, callback: Callable[[], None]) -> None:
        if self.reject_callbacks:
            raise RuntimeError("callback scheduling failed")
        self.callbacks.append(callback)

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def start(self) -> None:
        if self.start_error is not None:
            error = self.start_error
            self.start_error = None
            raise error
        self.drain()

    def drain(self) -> None:
        while self.callbacks:
            self.callbacks.popleft()()

    @staticmethod
    def fire(timer: _Timer) -> None:
        if not timer.cancelled:
            timer.callback()


class _Channel:
    def __init__(self) -> None:
        self.qos: dict[str, Any] | None = None
        self.qos_callback: Callable[[Any], None] | None = None
        self.close_callback: Callable[[Any, Exception], None] | None = None
        self.cancel_callback: Callable[[Any], None] | None = None
        self.cancel_ok_dispatcher: Callable[[Any], None] | None = None
        self.consumers: dict[str, tuple[Callable[..., None], Callable[[Any], None]]] = {}
        self.consume_calls = 0
        self.cancel_ok: dict[str, Callable[[Any], None]] = {}
        self.acks: list[int] = []
        self.nacks: list[tuple[int, bool]] = []

    def add_on_close_callback(self, callback: Callable[[Any, Exception], None]) -> None:
        self.close_callback = callback

    def add_on_cancel_callback(self, callback: Callable[[Any], None]) -> None:
        self.cancel_callback = callback

    def add_callback(
        self,
        callback: Callable[[Any], None],
        replies: list[Any],
        *,
        one_shot: bool,
    ) -> None:
        assert replies == [pika_spec.Basic.CancelOk]
        assert one_shot is False
        self.cancel_ok_dispatcher = callback

    def basic_qos(self, **kwargs: Any) -> None:
        self.qos = {key: value for key, value in kwargs.items() if key != "callback"}
        self.qos_callback = kwargs["callback"]

    def basic_consume(self, **kwargs: Any) -> str:
        self.consume_calls += 1
        tag = str(kwargs["consumer_tag"])
        self.consumers[tag] = (kwargs["on_message_callback"], kwargs["callback"])
        return tag

    def basic_cancel(self, tag: str, callback: Callable[[Any], None]) -> None:
        self.cancel_ok[tag] = callback

    def basic_ack(self, *, delivery_tag: int) -> None:
        self.acks.append(delivery_tag)

    def basic_nack(self, *, delivery_tag: int, requeue: bool) -> None:
        self.nacks.append((delivery_tag, requeue))

    def qos_ok(self) -> None:
        assert self.qos_callback is not None
        self.qos_callback(SimpleNamespace())

    def consume_ok(self, tag: str) -> None:
        self.consumers[tag][1](_frame(tag))

    def deliver(self, tag: str, delivery_tag: int, body: bytes) -> None:
        self.consumers[tag][0](
            self,
            SimpleNamespace(consumer_tag=tag, delivery_tag=delivery_tag),
            SimpleNamespace(),
            body,
        )

    def cancelled(self, tag: str) -> None:
        assert self.cancel_ok_dispatcher is not None
        self.cancel_ok_dispatcher(_frame(tag))
        self.cancel_ok[tag](_frame(tag))

    def broker_cancel(self, tag: str) -> None:
        assert self.cancel_callback is not None
        self.cancel_callback(_frame(tag))


class _Connection:
    def __init__(self, callbacks: Mapping[str, Any]) -> None:
        self.ioloop = _IOLoop()
        self.channel_instance = _Channel()
        self.callbacks = callbacks
        self.abort_errors: list[Exception] = []
        self.adapter_disconnects = 0
        self.terminate_error: Exception | None = None
        self.adapter_error: Exception | None = None

    def channel(self, *, on_open_callback: Callable[[Any], None]) -> None:
        on_open_callback(self.channel_instance)

    def _terminate_stream(self, error: Exception) -> None:
        if self.terminate_error is not None:
            raise self.terminate_error
        self.abort_errors.append(error)
        self.ioloop.callbacks.append(lambda: self.callbacks["on_close_callback"](self, error))

    def _adapter_disconnect_stream(self) -> None:
        self.adapter_disconnects += 1
        if self.adapter_error is not None:
            raise self.adapter_error
        self.ioloop.callbacks.append(
            lambda: self.callbacks["on_close_callback"](self, RuntimeError("adapter disconnect"))
        )

    def opened(self) -> None:
        self.callbacks["on_open_callback"](self)


class _Factory:
    def __init__(self) -> None:
        self.connections: list[_Connection] = []

    def __call__(self, **kwargs: Any) -> _Connection:
        connection = _Connection(kwargs)
        self.connections.append(connection)
        return connection


class _PikaTransport:
    def __init__(self) -> None:
        self.abort_count = 0

    def abort(self) -> None:
        self.abort_count += 1


def _actual_pika_connection(
    consumer: V3Consumer,
    epoch: _ConnectionEpoch,
) -> tuple[pika.SelectConnection, Channel, _PikaTransport]:
    connection = pika.SelectConnection(
        parameters=pika.ConnectionParameters(),
        on_open_callback=lambda _connection: None,
        on_open_error_callback=lambda value, error: consumer._on_connection_open_error(
            epoch, value, error
        ),
        on_close_callback=lambda value, error: consumer._on_connection_closed(epoch, value, error),
        custom_ioloop=PikaIOLoop(),
        internal_connection_workflow=False,
    )
    connection._set_connection_state(connection.CONNECTION_OPEN)
    connection._opened = True
    channel = connection._create_channel(1, lambda _channel: None)
    channel._set_state(channel.OPEN)
    connection._channels[1] = channel
    channel.add_on_close_callback(
        lambda value, error: consumer._on_channel_closed(epoch, value, error)
    )
    transport = _PikaTransport()
    connection._transport = transport
    epoch.connection = connection
    epoch.channel = channel
    consumer._epoch_counter = max(consumer._epoch_counter, epoch.number)
    consumer._active_epoch = epoch
    return connection, channel, transport


class _RecordingStop:
    def __init__(self, *, stop_after_waits: int) -> None:
        self.waits: list[float] = []
        self._set = False
        self._stop_after_waits = stop_after_waits

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, delay: float) -> bool:
        self.waits.append(delay)
        if len(self.waits) >= self._stop_after_waits:
            self._set = True
        return self._set


class _Client:
    def __init__(self, decision: Mapping[str, Any]) -> None:
        self.decision = dict(decision)
        self.claims: list[tuple[Mapping[str, Any], float]] = []
        self.claim_entered = threading.Event()
        self.claim_release = threading.Event()
        self.block_claim = False
        self.events: list[str] = []

    def claim_v3(
        self,
        _worker_id: int,
        dispatch: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.events.append("claim")
        self.claims.append((dispatch, timeout_seconds))
        self.claim_entered.set()
        if self.block_claim:
            assert self.claim_release.wait(10)
        return dict(self.decision)

    def start_attempt(
        self, _worker_id: int, _attempt_id: int, _body: Mapping[str, Any]
    ) -> dict[str, Any]:
        self.events.append("start")
        return {"decision": "ACK_NOOP", "reason": "started"}

    def result_attempt(
        self, _worker_id: int, _attempt_id: int, _body: Mapping[str, Any]
    ) -> dict[str, Any]:
        self.events.append("result")
        return {"decision": "ACK_NOOP"}


def _frame(tag: str) -> Any:
    return SimpleNamespace(method=SimpleNamespace(consumer_tag=tag))


def _payload() -> dict[str, Any]:
    return {
        "execution_id": 13,
        "attempt_id": 41,
        "attempt_no": 1,
        "fencing_token": 7,
        "lease_expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        "lease_seconds": 30,
        "renew_seconds": 5,
        "claim_token": "claim-token",
        "cleanup_token": "cleanup-token",
        "adapter_id": 19,
        "version_id": 23,
        "language": "python",
        "code": "print('ok')",
        "requirements": "",
        "runtime_config": {},
        "input": None,
        "execution_timeout_seconds": 10,
        "recovery_grace_seconds_snapshot": 60,
        "workspace_cleanup_attempt_timeout_seconds_snapshot": 1,
        "workspace_cleanup_total_timeout_seconds_snapshot": 2,
        "input_source_type": "none",
        "input_snapshot": {"source_type": "none"},
        "resource_profile": {
            "schema_version": 1,
            "resource_class": "small",
            "backend": "cgroup_v2",
            "cpu_cores": 1.0,
            "memory_bytes": 16 * 1024 * 1024,
            "pids": 16,
            "tmp_bytes": 1024 * 1024,
            "nofile": 64,
            "execution_timeout_seconds": 10,
            "claim_timeout_seconds": 30,
            "recovery_grace_seconds": 60,
            "workspace_cleanup_attempt_timeout_seconds": 1,
            "workspace_cleanup_total_timeout_seconds": 2,
            "stream_max_bytes": 1,
            "output_max_bytes": 1,
            "output_preview_max_bytes": 1,
        },
    }


def _consumer(
    tmp_path: Path,
    client: _Client,
    factory: _Factory,
    *,
    slots: int = 1,
    runner: Callable[..., dict[str, Any]] | None = None,
    connection_parameters: pika.ConnectionParameters | None = None,
) -> V3Consumer:
    return V3Consumer(
        ConsumerConfig(
            worker_id=7,
            queue="dlr.worker.7.q",
            execution_slots=slots,
            runtime_root=tmp_path / "runtime",
            attempt_journal_root=tmp_path / "journal",
            claim_handshake_timeout_seconds=2,
        ),
        client,  # type: ignore[arg-type]
        connection_factory=factory,
        connection_parameters=connection_parameters,
        runtime_settings=SimpleNamespace(
            sandbox_config=unit_sandbox_config(), resource_envelope=unit_resource_envelope()
        ),
        runner=runner or (lambda *_args, **_kwargs: {"status": "succeeded"}),
    )


def _start_epoch(consumer: V3Consumer, factory: _Factory) -> tuple[Any, _Connection, _Channel]:
    epoch = consumer._open_epoch()
    connection = factory.connections[-1]
    connection.opened()
    channel = connection.channel_instance
    channel.qos_ok()
    for tag in list(channel.consumers):
        channel.consume_ok(tag)
    return epoch, connection, channel


def _drain_until(connection: _Connection, predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not predicate():
        connection.ioloop.drain()
        assert time.monotonic() < deadline
        time.sleep(0.001)


def test_per_slot_consumers_bound_credit_and_replenish_after_receipt(tmp_path: Path) -> None:
    client = _Client({"decision": "ACK_NOOP"})
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory, slots=2)
    try:
        _epoch, connection, channel = _start_epoch(consumer, factory)
        assert channel.qos == {"prefetch_count": 1, "global_qos": False}
        assert len(channel.consumers) == 2
        assert {ticket.phase for ticket in consumer._tickets.values()} == {"receiving"}

        first_tag = next(iter(channel.consumers))
        channel.deliver(first_tag, 11, json.dumps({"message_id": "one"}).encode())
        assert consumer._tickets[0].phase == "cancelling"
        assert client.claims == []
        channel.cancelled(first_tag)
        assert client.claim_entered.wait(10)
        _drain_until(connection, lambda: channel.acks == [11])
        _drain_until(connection, lambda: channel.consume_calls == 3)
        assert len(consumer._tickets) == 2
        assert sum(not ticket.released for ticket in consumer._tickets.values()) == 2
        assert consumer._pool._max_workers == 2
    finally:
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_pika_132_shared_cancel_dispatcher_routes_each_tag_once(tmp_path: Path) -> None:
    client = _Client({"decision": "ACK_NOOP"})
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory, slots=2)
    try:
        epoch, connection, channel = _start_epoch(consumer, factory)
        tags = list(channel.consumers)
        tickets = list(consumer._tickets.values())
        channel.deliver(tags[0], 121, b"{}")
        channel.deliver(tags[1], 122, b"{}")
        assert channel.cancel_ok[tags[0]] is channel.cancel_ok[tags[1]]
        assert channel.cancel_ok_dispatcher is not None

        pika_connection = SimpleNamespace(callbacks=CallbackManager())
        pika_channel = Channel(pika_connection, 1, lambda _channel: None)
        pika_channel._state = pika_channel.OPEN
        pika_channel._consumers = {tag: lambda *_args: None for tag in tags}
        pika_channel._send_method = lambda _method: None  # type: ignore[method-assign]

        pika_channel.add_callback(
            channel.cancel_ok_dispatcher,
            [pika_spec.Basic.CancelOk],
            one_shot=False,
        )
        waiter = channel.cancel_ok[tags[0]]
        pika_channel.basic_cancel(tags[0], callback=waiter)
        pika_channel.basic_cancel(tags[1], callback=waiter)
        for tag in tags:
            pika_channel.callbacks.process(
                1,
                pika_spec.Basic.CancelOk,
                pika_channel,
                pika_frame.Method(1, pika_spec.Basic.CancelOk(tag)),
            )

        deadline = time.monotonic() + 10
        while len(client.claims) != 2:
            assert time.monotonic() < deadline
            connection.ioloop.drain()
            time.sleep(0.001)
        _drain_until(connection, lambda: len(channel.acks) == 2)
        assert sorted(channel.acks) == [121, 122]
        assert all(ticket.work_submitted for ticket in tickets)
        assert not epoch.faulted
    finally:
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_select_connection_receives_exact_real_pika_parameters(tmp_path: Path) -> None:
    parameters = pika.ConnectionParameters(
        host="127.0.0.1",
        port=5679,
        virtual_host="capacity-unit",
        heartbeat=17,
        socket_timeout=3,
        stack_timeout=5,
    )
    client = _Client({"decision": "ACK_NOOP"})
    factory = _Factory()
    consumer = _consumer(
        tmp_path,
        client,
        factory,
        connection_parameters=parameters,
    )
    try:
        epoch = consumer._open_epoch()
        assert factory.connections[0].callbacks["parameters"] is parameters
        consumer._fault_epoch(epoch, "test_cleanup")
        factory.connections[0].ioloop.drain()
        consumer._finalize_epoch(epoch)
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_claim_and_prepare_failure_use_remaining_rpc_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ControlClient("http://control.invalid", "test-token", timeout_seconds=60)
    observed: list[float | None] = []

    def request(
        _method: str,
        _path: str,
        _payload: Any = None,
        timeout: float | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, bytes]:
        del headers
        observed.append(timeout)
        return 200, b"{}"

    monkeypatch.setattr(client, "_request", request)

    assert client.claim_v3(7, {}, timeout_seconds=0.25) == {}
    assert client.prepare_failed_attempt(7, 41, {}, timeout_seconds=0.125) == {}
    assert observed == [0.25, 0.125]


def test_execute_orders_cancel_claim_journal_ack_receipt_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _Client({"decision": "EXECUTE", "payload": _payload()})
    factory = _Factory()
    events = client.events
    original_journal = workspace.write_attempt_journal

    def journal(*args: Any, **kwargs: Any) -> Any:
        events.append("journal")
        return original_journal(*args, **kwargs)

    monkeypatch.setattr(workspace, "write_attempt_journal", journal)

    def runner(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        events.append("runner")
        return {"status": "succeeded"}

    consumer = _consumer(tmp_path, client, factory, runner=runner)
    try:
        _epoch, connection, channel = _start_epoch(consumer, factory)
        tag = next(iter(channel.consumers))
        original_ack = channel.basic_ack

        def ack(*, delivery_tag: int) -> None:
            events.append("ack-send")
            original_ack(delivery_tag=delivery_tag)

        monkeypatch.setattr(channel, "basic_ack", ack)
        channel.deliver(tag, 21, b'{"message_id":"execute"}')
        channel.cancelled(tag)
        _drain_until(connection, lambda: "result" in events)
        assert events[:5] == ["claim", "journal", "ack-send", "start", "runner"]
        assert channel.acks == [21]
    finally:
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize(
    ("decision", "expected_ack", "expected_nack"),
    [
        ({"decision": "ACK_NOOP"}, [31], []),
        ({"decision": "REJECT_DLQ"}, [], [(31, False)]),
        ({"decision": "DEFER"}, [], [(31, True)]),
        ({"decision": "unexpected"}, [], [(31, False)]),
    ],
)
def test_non_execute_decisions_settle_before_ticket_release(
    tmp_path: Path,
    decision: Mapping[str, Any],
    expected_ack: list[int],
    expected_nack: list[tuple[int, bool]],
) -> None:
    client = _Client(decision)
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory)
    try:
        _epoch, connection, channel = _start_epoch(consumer, factory)
        tag = next(iter(channel.consumers))
        old_ticket = consumer._tickets[0]
        channel.deliver(tag, 31, b"{}")
        channel.cancelled(tag)
        _drain_until(connection, lambda: old_ticket.released)
        assert channel.acks == expected_ack
        assert channel.nacks == expected_nack
        assert old_ticket.phase == "released"
    finally:
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize(
    "failure",
    [
        ControlUnavailableError("control unavailable"),
        ClientError(401, '{"detail":{"code":"worker_token_invalid"}}'),
    ],
)
def test_control_or_auth_failure_aborts_epoch_without_ack_or_hot_nack(
    tmp_path: Path, failure: Exception
) -> None:
    class _FailingClient(_Client):
        def claim_v3(
            self,
            _worker_id: int,
            _dispatch: Mapping[str, Any],
            *,
            timeout_seconds: float,
        ) -> dict[str, Any]:
            self.claim_entered.set()
            raise failure

    client = _FailingClient({"decision": "ACK_NOOP"})
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory)
    try:
        epoch, connection, channel = _start_epoch(consumer, factory)
        tag = next(iter(channel.consumers))
        ticket = consumer._tickets[0]
        channel.deliver(tag, 81, b"{}")
        channel.cancelled(tag)
        assert client.claim_entered.wait(10)
        _drain_until(connection, lambda: epoch.transport_terminated)
        _drain_until(connection, lambda: ticket.released)
        assert epoch.faulted
        assert channel.acks == []
        assert channel.nacks == []
    finally:
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("failure", ["malformed_json", "runtime_error"])
def test_unexpected_claim_failure_faults_before_unsettled_ticket_release(
    tmp_path: Path, failure: str
) -> None:
    if failure == "malformed_json":
        client: Any = ControlClient("http://control.invalid", "test-token")
        client._request = lambda *_args, **_kwargs: (200, b"not-json")
    else:

        class _UnexpectedClient(_Client):
            def claim_v3(
                self,
                _worker_id: int,
                _dispatch: Mapping[str, Any],
                *,
                timeout_seconds: float,
            ) -> dict[str, Any]:
                raise RuntimeError("unexpected claim failure")

        client = _UnexpectedClient({"decision": "ACK_NOOP"})

    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory)
    try:
        epoch, connection, channel = _start_epoch(consumer, factory)
        tag = next(iter(channel.consumers))
        ticket = consumer._tickets[0]
        channel.deliver(tag, 131, b"{}")
        channel.cancelled(tag)
        deadline = time.monotonic() + 10
        while not ticket.completion_pending:
            assert time.monotonic() < deadline
            time.sleep(0.001)
        connection.ioloop.callbacks.popleft()()

        assert epoch.faulted
        assert not ticket.released
        assert not epoch.transport_terminated
        assert channel.acks == []
        assert channel.nacks == []
        assert channel.consume_calls == 1

        connection.ioloop.drain()
        assert epoch.transport_terminated
        assert ticket.released
        assert channel.consume_calls == 1
    finally:
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_expired_claim_return_faults_even_before_io_deadline_callback(tmp_path: Path) -> None:
    client = _Client({"decision": "ACK_NOOP"})
    client.block_claim = True
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory)
    consumer._config = ConsumerConfig(
        **{
            **consumer._config.__dict__,
            "claim_handshake_timeout_seconds": 0.01,
        }
    )
    try:
        epoch, connection, channel = _start_epoch(consumer, factory)
        tag = next(iter(channel.consumers))
        ticket = consumer._tickets[0]
        channel.deliver(tag, 141, b"{}")
        channel.cancelled(tag)
        assert client.claim_entered.wait(10)
        time.sleep(0.02)
        client.claim_release.set()
        deadline = time.monotonic() + 10
        while not ticket.completion_pending:
            assert time.monotonic() < deadline
            time.sleep(0.001)

        # The fake IO deadline has deliberately not fired. Future completion
        # must still fault instead of cancelling the timer and replenishing.
        assert ticket.deadline_handle is not None
        connection.ioloop.callbacks.popleft()()
        assert epoch.faulted
        assert not ticket.released
        assert not epoch.transport_terminated
        assert channel.acks == []
        assert channel.nacks == []
        assert channel.consume_calls == 1
        connection.ioloop.drain()
        assert epoch.transport_terminated
        assert ticket.released
    finally:
        client.claim_release.set()
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_running_ticket_is_not_replenished_until_future_really_finishes(
    tmp_path: Path,
) -> None:
    client = _Client({"decision": "EXECUTE", "payload": _payload()})
    factory = _Factory()
    runner_entered = threading.Event()
    runner_release = threading.Event()

    def runner(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        runner_entered.set()
        assert runner_release.wait(10)
        return {"status": "succeeded"}

    consumer = _consumer(tmp_path, client, factory, runner=runner)
    try:
        _epoch, connection, channel = _start_epoch(consumer, factory)
        tag = next(iter(channel.consumers))
        ticket = consumer._tickets[0]
        channel.deliver(tag, 91, b"{}")
        channel.cancelled(tag)
        _drain_until(connection, runner_entered.is_set)
        assert channel.acks == [91]
        assert ticket.phase == "working"
        assert not ticket.released
        assert channel.consume_calls == 1

        runner_release.set()
        _drain_until(connection, lambda: ticket.released)
        assert channel.consume_calls == 2
    finally:
        runner_release.set()
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_deadline_aborts_transport_and_holds_ticket_until_slow_claim_exits(tmp_path: Path) -> None:
    client = _Client({"decision": "ACK_NOOP"})
    client.block_claim = True
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory)
    try:
        epoch, connection, channel = _start_epoch(consumer, factory)
        tag = next(iter(channel.consumers))
        ticket = consumer._tickets[0]
        channel.deliver(tag, 41, b"{}")
        channel.cancelled(tag)
        assert client.claim_entered.wait(10)
        assert ticket.deadline_handle is not None
        connection.ioloop.fire(ticket.deadline_handle)
        assert epoch.faulted
        assert connection.abort_errors
        assert not ticket.released
        assert channel.acks == []

        client.claim_release.set()
        _drain_until(connection, lambda: ticket.released)
        assert channel.acks == []
    finally:
        client.claim_release.set()
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_old_epoch_slow_failure_releases_without_faulting_new_epoch(tmp_path: Path) -> None:
    client = _Client({"decision": "ACK_NOOP"})
    client.block_claim = True
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory)
    try:
        old_epoch, old_connection, old_channel = _start_epoch(consumer, factory)
        old_tag = next(iter(old_channel.consumers))
        old_ticket = consumer._tickets[0]
        old_channel.deliver(old_tag, 151, b"{}")
        old_channel.cancelled(old_tag)
        assert client.claim_entered.wait(10)
        assert old_ticket.deadline_handle is not None
        old_connection.ioloop.fire(old_ticket.deadline_handle)
        old_connection.ioloop.drain()
        assert old_epoch.transport_terminated
        consumer._finalize_epoch(old_epoch)
        assert not old_ticket.released

        new_epoch, new_connection, _new_channel = _start_epoch(consumer, factory)
        assert not new_epoch.faulted
        client.claim_release.set()
        _drain_until(new_connection, lambda: old_ticket.released)

        assert not new_epoch.faulted
        assert new_connection.abort_errors == []
        assert old_channel.acks == []
        assert old_ticket.deadline_handle is None
    finally:
        client.claim_release.set()
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_slow_journal_cannot_ack_start_or_reuse_ticket_after_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _Client({"decision": "EXECUTE", "payload": _payload()})
    factory = _Factory()
    journal_entered = threading.Event()
    journal_release = threading.Event()

    def slow_journal(*_args: Any, **_kwargs: Any) -> None:
        journal_entered.set()
        assert journal_release.wait(10)

    monkeypatch.setattr(workspace, "write_attempt_journal", slow_journal)
    consumer = _consumer(tmp_path, client, factory)
    try:
        epoch, connection, channel = _start_epoch(consumer, factory)
        tag = next(iter(channel.consumers))
        ticket = consumer._tickets[0]
        channel.deliver(tag, 71, b"{}")
        channel.cancelled(tag)
        assert journal_entered.wait(10)
        assert ticket.deadline_handle is not None
        connection.ioloop.fire(ticket.deadline_handle)
        connection.ioloop.drain()
        assert epoch.transport_terminated
        assert not ticket.released
        assert channel.acks == []
        assert "start" not in client.events

        journal_release.set()
        _drain_until(connection, lambda: ticket.released)
        assert channel.acks == []
        assert "start" not in client.events
    finally:
        journal_release.set()
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("deadline", ["qos", "consume_ok"])
def test_registration_deadlines_abort_without_claim(tmp_path: Path, deadline: str) -> None:
    client = _Client({"decision": "ACK_NOOP"})
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory)
    try:
        epoch = consumer._open_epoch()
        connection = factory.connections[0]
        connection.opened()
        channel = connection.channel_instance
        if deadline == "qos":
            assert epoch.setup_deadline is not None
            connection.ioloop.fire(epoch.setup_deadline)
        else:
            channel.qos_ok()
            ticket = consumer._tickets[0]
            assert ticket.deadline_handle is not None
            connection.ioloop.fire(ticket.deadline_handle)
        assert epoch.faulted
        assert connection.abort_errors
        assert client.claims == []
    finally:
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_idle_broker_cancel_aborts_and_old_epoch_event_cannot_close_new_epoch(
    tmp_path: Path,
) -> None:
    client = _Client({"decision": "ACK_NOOP"})
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory)
    try:
        old_epoch, old_connection, old_channel = _start_epoch(consumer, factory)
        old_tag = next(iter(old_channel.consumers))
        old_channel.broker_cancel(old_tag)
        assert old_epoch.faulted
        assert old_connection.abort_errors
        old_connection.ioloop.drain()
        consumer._finalize_epoch(old_epoch)

        new_epoch, new_connection, _new_channel = _start_epoch(consumer, factory)
        consumer._on_broker_cancel(old_epoch, _frame(old_tag))
        assert not new_epoch.faulted
        assert new_connection.abort_errors == []
    finally:
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("fault", ["broker_cancel", "channel_close", "shutdown"])
def test_fault_interleaving_after_cancel_ok_keeps_work_ticket_until_exit(
    tmp_path: Path, fault: str
) -> None:
    client = _Client({"decision": "ACK_NOOP"})
    client.block_claim = True
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory)
    try:
        epoch, connection, channel = _start_epoch(consumer, factory)
        tag = next(iter(channel.consumers))
        ticket = consumer._tickets[0]
        channel.deliver(tag, 111, b"{}")
        channel.cancelled(tag)
        channel.cancelled(tag)
        assert client.claim_entered.wait(10)
        if fault == "broker_cancel":
            channel.broker_cancel(tag)
        elif fault == "channel_close":
            assert channel.close_callback is not None
            channel.close_callback(channel, RuntimeError("channel closed"))
        else:
            consumer.request_stop()
            connection.ioloop.drain()

        assert epoch.faulted
        assert not ticket.released
        assert channel.acks == []
        client.claim_release.set()
        _drain_until(connection, lambda: ticket.released)
        assert len(client.claims) == 1
        assert channel.acks == []
    finally:
        client.claim_release.set()
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_ticket_nonce_and_late_consume_ok_cannot_fault_reused_slot(tmp_path: Path) -> None:
    client = _Client({"decision": "ACK_NOOP"})
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory)
    try:
        epoch, connection, channel = _start_epoch(consumer, factory)
        old_tag = next(iter(channel.consumers))
        old_ticket = consumer._tickets[0]
        old_consume_ok = channel.consumers[old_tag][1]
        channel.deliver(old_tag, 61, b"{}")
        channel.cancelled(old_tag)
        _drain_until(connection, lambda: old_ticket.released)
        new_ticket = consumer._tickets[0]
        assert new_ticket.consumer_tag != old_tag

        old_consume_ok(_frame(old_tag))
        consumer._on_broker_cancel(epoch, _frame(old_tag))
        assert not epoch.faulted
        assert connection.abort_errors == []
        assert consumer._tickets[0] is new_ticket
    finally:
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_continuous_pre_claim_failures_use_capped_exponential_backoff(tmp_path: Path) -> None:
    client = _Client({"decision": "ACK_NOOP"})
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory)
    stop = _RecordingStop(stop_after_waits=5)
    consumer._stop = stop  # type: ignore[assignment]
    consumer._config = ConsumerConfig(
        **{
            **consumer._config.__dict__,
            "attempt_reconnect_max_seconds": 4,
        }
    )

    consumer.run()

    assert stop.waits == [1.0, 2.0, 4, 4, 4]
    assert len(factory.connections) == 5
    assert all(connection.abort_errors for connection in factory.connections)
    assert all(connection.ioloop.closed for connection in factory.connections)


def test_start_exception_aborts_and_closes_before_reconnect(tmp_path: Path) -> None:
    client = _Client({"decision": "ACK_NOOP"})
    factory = _Factory()

    class _StartErrorFactory(_Factory):
        def __call__(self, **kwargs: Any) -> _Connection:
            connection = super().__call__(**kwargs)
            connection.ioloop.start_error = RuntimeError("start failed")
            return connection

    factory = _StartErrorFactory()
    consumer = _consumer(tmp_path, client, factory)
    stop = _RecordingStop(stop_after_waits=1)
    consumer._stop = stop  # type: ignore[assignment]

    consumer.run()

    connection = factory.connections[0]
    assert connection.abort_errors
    assert connection.ioloop.closed
    assert consumer._active_epoch is None


def test_cleanup_restart_rearms_stop_after_first_abort_timer_wins(tmp_path: Path) -> None:
    client = _Client({"decision": "ACK_NOOP"})
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory)
    try:
        epoch, connection, _channel = _start_epoch(consumer, factory)
        consumer._fault_epoch(epoch, "test_fault")
        assert epoch.abort_deadline is not None

        # Model the abort deadline stopping the first start() before Pika's
        # queued connection_lost callback gets CPU time.
        connection.ioloop.fire(epoch.abort_deadline)
        assert epoch.stopped
        assert not epoch.transport_terminated

        consumer._close_epoch_after_loop(epoch)

        assert epoch.transport_terminated
        assert epoch.stopped
        assert connection.ioloop.closed
        assert consumer._active_epoch is None
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_ack_schedule_failure_fails_closed_without_start_or_slot_reuse(
    tmp_path: Path,
) -> None:
    client = _Client({"decision": "ACK_NOOP"})
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory)
    try:
        epoch, connection, channel = _start_epoch(consumer, factory)
        tag = next(iter(channel.consumers))
        ticket = consumer._tickets[0]
        channel.deliver(tag, 101, b"{}")
        connection.ioloop.reject_callbacks = True
        channel.cancelled(tag)
        assert client.claim_entered.wait(10)
        deadline = time.monotonic() + 10
        while not ticket.completion_pending:
            assert time.monotonic() < deadline
            time.sleep(0.001)

        assert epoch.faulted
        assert epoch.abort_failed
        assert consumer._stop.is_set()
        assert not ticket.released
        assert channel.acks == []
        assert channel.nacks == []
        assert channel.consume_calls == 1
        assert "start" not in client.events
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_abort_fallback_must_succeed_or_consumer_fails_closed(tmp_path: Path) -> None:
    client = _Client({"decision": "ACK_NOOP"})
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory)
    try:
        epoch, connection, _channel = _start_epoch(consumer, factory)
        connection.terminate_error = RuntimeError("terminate failed")
        consumer._fault_epoch(epoch, "test_fault")
        assert epoch.abort_initiated
        assert connection.adapter_disconnects == 1
        connection.ioloop.drain()
        consumer._finalize_epoch(epoch)
        assert consumer._active_epoch is None

        next_epoch, next_connection, _channel = _start_epoch(consumer, factory)
        ticket = consumer._tickets[0]
        next_connection.terminate_error = RuntimeError("terminate failed")
        next_connection.adapter_error = RuntimeError("adapter failed")
        consumer._fault_epoch(next_epoch, "test_fatal_fault")
        consumer._finalize_epoch(next_epoch)
        assert next_epoch.abort_failed
        assert consumer._stop.is_set()
        assert not ticket.released
        assert consumer._active_epoch is next_epoch
    finally:
        consumer._pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize("close_kind", ["eof", "connection_close"])
def test_actual_pika_connection_teardown_does_not_reabort_closed_transport(
    tmp_path: Path, close_kind: str
) -> None:
    client = _Client({"decision": "ACK_NOOP"})
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory)
    connection: pika.SelectConnection | None = None
    try:
        epoch = _ConnectionEpoch(number=1)
        connection, channel, transport = _actual_pika_connection(consumer, epoch)
        ticket = SlotTicket(
            slot_id=0,
            connection_epoch=epoch.number,
            consumer_tag="actual-pika-ticket",
            phase="working",
        )
        consumer._tickets[0] = ticket

        if close_kind == "eof":
            connection._got_eof = True
        else:
            connection._on_connection_close_from_broker(
                SimpleNamespace(
                    method=SimpleNamespace(reply_code=320, reply_text="connection forced")
                )
            )
            assert transport.abort_count == 1

        connection._proto_connection_lost(None)

        assert connection.is_closed
        assert epoch.faulted
        assert epoch.abort_initiated
        assert epoch.transport_terminated
        assert not epoch.abort_failed
        assert not consumer._stop.is_set()
        assert transport.abort_count == (0 if close_kind == "eof" else 1)
        assert not ticket.released

        consumer._finalize_epoch(epoch)
        assert consumer._active_epoch is None
        assert not ticket.released

        ticket.completion_pending = True
        new_epoch, new_connection, _new_channel = _start_epoch(consumer, factory)
        assert ticket.released
        consumer._on_channel_closed(epoch, channel, RuntimeError("late old channel close"))
        assert not new_epoch.faulted
        assert new_connection.abort_errors == []
    finally:
        if connection is not None:
            connection.ioloop.close()
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_actual_pika_channel_only_close_still_aborts_open_connection(tmp_path: Path) -> None:
    client = _Client({"decision": "ACK_NOOP"})
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory)
    connection: pika.SelectConnection | None = None
    try:
        epoch = _ConnectionEpoch(number=1)
        connection, channel, transport = _actual_pika_connection(consumer, epoch)

        channel._on_close_meta(pika.exceptions.ChannelClosedByBroker(406, "channel fault"))

        assert connection.is_open
        assert epoch.faulted
        assert epoch.abort_initiated
        assert not epoch.transport_terminated
        assert not epoch.abort_failed
        assert not consumer._stop.is_set()
        assert transport.abort_count == 1

        connection._proto_connection_lost(None)
        assert epoch.transport_terminated
        assert not epoch.abort_failed
        assert not consumer._stop.is_set()
    finally:
        if connection is not None:
            connection.ioloop.close()
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)


def test_cancel_ok_deadline_never_claims_and_releases_once_after_epoch_exit(tmp_path: Path) -> None:
    client = _Client({"decision": "ACK_NOOP"})
    factory = _Factory()
    consumer = _consumer(tmp_path, client, factory)
    try:
        epoch, connection, channel = _start_epoch(consumer, factory)
        tag = next(iter(channel.consumers))
        ticket = consumer._tickets[0]
        channel.deliver(tag, 51, b"{}")
        assert ticket.deadline_handle is not None
        connection.ioloop.fire(ticket.deadline_handle)
        assert connection.abort_errors
        assert client.claims == []
        assert not ticket.released
        connection.ioloop.drain()
        consumer._finalize_epoch(epoch)
        assert ticket.released
        consumer._finalize_epoch(epoch)
        assert ticket.phase == "released"
    finally:
        consumer.request_stop()
        consumer._pool.shutdown(wait=True, cancel_futures=True)
