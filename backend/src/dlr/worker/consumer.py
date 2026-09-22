"""RabbitMQ Worker Consumer with one receive credit per execution slot."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pika

from dlr.control.schemas.reliable_runtime import V3TaskPayload
from dlr.worker import executor, sandbox, workspace
from dlr.worker.cache import CacheError
from dlr.worker.cache_lifecycle import CacheLifecycleStore, CacheUse, cache_key
from dlr.worker.cache_replacement import ReplacementAuthority
from dlr.worker.cache_replacement import activate as activate_replacement
from dlr.worker.client import ClientError, ControlClient, ControlUnavailableError

logger = logging.getLogger("dlr.worker.consumer")


def _client_error_code(error: ClientError) -> str:
    """Extract only a bounded machine code from a Control error response."""
    try:
        body = json.loads(error.body)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return "unknown"
    detail = body.get("detail") if isinstance(body, Mapping) else None
    code = detail.get("code") if isinstance(detail, Mapping) else None
    return code if isinstance(code, str) and code else "unknown"


def _is_successful_attempt_action(response: object, *, attempt_id: int, reason: str) -> bool:
    """Accept only the exact Control acknowledgement for one action."""
    return (
        isinstance(response, Mapping)
        and response.get("decision") == "ACK_NOOP"
        and response.get("reason") == reason
        and response.get("attempt_id") == attempt_id
        and response.get("cancel_requested") is False
    )


def _is_cancel_requested_action(response: object, *, attempt_id: int) -> bool:
    """Recognize Control's cancellation acknowledgement without losing ownership."""
    return (
        isinstance(response, Mapping)
        and response.get("decision") == "ACK_NOOP"
        and response.get("reason") == "cancel_requested"
        and response.get("attempt_id") == attempt_id
        and response.get("cancel_requested") is True
    )


@dataclass(frozen=True)
class ConsumerConfig:
    """Small immutable subset of WorkerConfig needed by the Consumer."""

    worker_id: int
    queue: str
    execution_slots: int
    runtime_root: Path
    attempt_journal_root: Path
    attempt_reconnect_max_seconds: float = 30.0
    claim_handshake_timeout_seconds: float = 30.0


@dataclass
class SlotTicket:
    """One reserved execution slot from consume registration through exit."""

    slot_id: int
    connection_epoch: int
    consumer_tag: str
    phase: str = "receiving"
    delivery_tag: int | None = None
    body: bytes | None = None
    deadline_at: float | None = None
    deadline_handle: Any | None = None
    work_submitted: bool = False
    completion_pending: bool = False
    disposition_sent: bool = False
    released: bool = False


@dataclass
class _ConnectionEpoch:
    number: int
    connection: Any | None = None
    channel: Any | None = None
    faulted: bool = False
    stopped: bool = False
    setup_deadline: Any | None = None
    abort_deadline: Any | None = None
    successful_claim: bool = False
    cancel_dispatcher: Callable[[Any], None] | None = None
    cancel_waiter: Callable[[Any], None] | None = None
    abort_initiated: bool = False
    transport_terminated: bool = False
    abort_failed: bool = False


@dataclass
class _PreparedExecution:
    payload: V3TaskPayload
    reservation: sandbox.ResourceReservation | None


@dataclass
class _PreparationResult:
    prepared: _PreparedExecution | None = None
    disposition: str | None = None


class V3Consumer:
    """Consume one fixed Worker queue and hand all state changes to Control."""

    def __init__(
        self,
        config: ConsumerConfig,
        client: ControlClient,
        *,
        runtime_settings: Any,
        connection_parameters: pika.ConnectionParameters | None = None,
        connection_factory: Callable[..., Any] = pika.SelectConnection,
        runner: Callable[..., dict[str, Any]] = executor.run,
    ) -> None:
        if config.execution_slots < 1:
            raise ValueError("execution_slots must be positive")
        self._config = config
        self._client = client
        self._connection_factory = connection_factory
        self._connection_parameters = connection_parameters
        self._runtime_settings = runtime_settings
        self._runner = runner
        self._stop = threading.Event()
        self._state_lock = threading.RLock()
        self._epoch_counter = 0
        self._ticket_counter = 0
        self._active_epoch: _ConnectionEpoch | None = None
        self._tickets: dict[int, SlotTicket] = {}
        sandbox_config = getattr(runtime_settings, "sandbox_config", None)
        if sandbox_config is None:
            raise sandbox.SandboxError("sandbox_linux_target_required")
        envelope = getattr(runtime_settings, "resource_envelope", None)
        if envelope is None:
            envelope = sandbox.read_verified_resource_envelope(sandbox_config)
        self._resource_budget = sandbox.ResourceBudget.from_verified_envelope(
            sandbox_config,
            config.execution_slots,
            envelope,
        )
        self._pool = ThreadPoolExecutor(
            max_workers=config.execution_slots,
            thread_name_prefix="dlr-attempt",
        )

    def request_stop(self) -> None:
        self._stop.set()
        with self._state_lock:
            epoch = self._active_epoch
            connection = epoch.connection if epoch is not None else None
        if epoch is None or connection is None:
            return
        try:
            connection.ioloop.add_callback_threadsafe(
                lambda: self._fault_epoch(epoch, "worker_stopping")
            )
        except Exception:
            logger.debug("consumer stop scheduling failed", exc_info=True)

    def run(self) -> None:
        """Run one SelectConnection I/O loop at a time with capped reconnect."""
        backoff = 1.0
        try:
            while not self._stop.is_set():
                epoch: _ConnectionEpoch | None = None
                try:
                    epoch = self._open_epoch()
                    connection = epoch.connection
                    assert connection is not None
                    connection.ioloop.start()
                except (pika.exceptions.AMQPError, OSError, TimeoutError, RuntimeError):
                    logger.warning(
                        "Consumer transport unavailable; reconnecting with bounded backoff"
                    )
                except Exception:
                    logger.exception("Consumer stopped on an unexpected transport error")
                finally:
                    if epoch is None:
                        with self._state_lock:
                            epoch = self._active_epoch
                    if epoch is not None:
                        self._close_epoch_after_loop(epoch)
                        if epoch.successful_claim:
                            backoff = 1.0
                if not self._stop.is_set():
                    self._stop.wait(min(backoff, self._config.attempt_reconnect_max_seconds))
                    backoff = min(backoff * 2, self._config.attempt_reconnect_max_seconds)
        finally:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def _open_epoch(self) -> _ConnectionEpoch:
        with self._state_lock:
            self._epoch_counter += 1
            epoch = _ConnectionEpoch(self._epoch_counter)
            self._active_epoch = epoch
        connection = self._connection_factory(
            parameters=self._connection_parameters,
            on_open_callback=lambda value: self._on_connection_open(epoch, value),
            on_open_error_callback=lambda value, error: self._on_connection_open_error(
                epoch, value, error
            ),
            on_close_callback=lambda value, error: self._on_connection_closed(epoch, value, error),
        )
        epoch.connection = connection
        ioloop = getattr(connection, "ioloop", None)
        required_loop = (
            "call_later",
            "remove_timeout",
            "add_callback_threadsafe",
            "stop",
            "close",
        )
        if ioloop is None or any(
            not callable(getattr(ioloop, name, None)) for name in required_loop
        ):
            raise RuntimeError("consumer_ioloop_capability_unavailable")
        if not callable(getattr(connection, "_terminate_stream", None)) or not callable(
            getattr(connection, "_adapter_disconnect_stream", None)
        ):
            raise RuntimeError("consumer_transport_abort_unavailable")
        return epoch

    def _on_connection_open(self, epoch: _ConnectionEpoch, connection: Any) -> None:
        if not self._epoch_is_current(epoch):
            return
        try:
            epoch.setup_deadline = connection.ioloop.call_later(
                self._config.claim_handshake_timeout_seconds,
                lambda: self._fault_epoch(epoch, "channel_registration_timeout"),
            )
            connection.channel(
                on_open_callback=lambda channel: self._on_channel_open(epoch, channel)
            )
        except Exception:
            self._fault_epoch(epoch, "channel_registration_failed")

    def _on_connection_open_error(
        self, epoch: _ConnectionEpoch, connection: Any, _error: Exception
    ) -> None:
        if epoch.connection is connection:
            epoch.faulted = True
            epoch.abort_initiated = True
            epoch.transport_terminated = True
            self._release_completed_tickets_io(epoch)
            self._stop_ioloop(epoch)

    def _on_connection_closed(
        self, epoch: _ConnectionEpoch, connection: Any, _error: Exception
    ) -> None:
        if epoch.connection is connection:
            epoch.faulted = True
            epoch.abort_initiated = True
            epoch.transport_terminated = True
            self._release_completed_tickets_io(epoch)
            self._stop_ioloop(epoch)

    def _on_channel_open(self, epoch: _ConnectionEpoch, channel: Any) -> None:
        if not self._epoch_is_current(epoch):
            return
        epoch.channel = channel
        try:
            epoch.cancel_dispatcher = lambda frame: self._on_cancel_ok_frame(epoch, frame)
            epoch.cancel_waiter = lambda _frame: None
            # Pika 1.3.2 registers basic_cancel's caller callback without its
            # consumer-tag filter. Keep one persistent public dispatcher for
            # routing, while the no-op caller callback only selects nowait=False.
            channel.add_callback(
                epoch.cancel_dispatcher,
                [pika.spec.Basic.CancelOk],
                one_shot=False,
            )
            channel.add_on_close_callback(
                lambda value, error: self._on_channel_closed(epoch, value, error)
            )
            channel.add_on_cancel_callback(lambda frame: self._on_broker_cancel(epoch, frame))
            channel.basic_qos(
                prefetch_count=1,
                global_qos=False,
                callback=lambda _frame: self._on_qos_ok(epoch),
            )
        except Exception:
            self._fault_epoch(epoch, "consumer_qos_failed")

    def _on_channel_closed(self, epoch: _ConnectionEpoch, _channel: Any, _error: Exception) -> None:
        if not self._epoch_is_current(epoch):
            return
        connection = epoch.connection
        if connection is not None and bool(getattr(connection, "is_closed", False)):
            # Pika 1.3.2 marks the Connection CLOSED before it meta-closes its
            # Channels, then invokes the Connection close callback.  The
            # transport has already exited at this point, so trying to abort it
            # again would call through a cleared ``_transport`` and incorrectly
            # turn an ordinary reconnect into a fail-closed Worker stop.
            epoch.faulted = True
            return
        self._fault_epoch(epoch, "broker_channel_closed")

    def _on_qos_ok(self, epoch: _ConnectionEpoch) -> None:
        if not self._epoch_is_current(epoch):
            return
        self._remove_timer(epoch, epoch.setup_deadline)
        epoch.setup_deadline = None
        self._release_completed_tickets_io(epoch)
        self._replenish_consumers(epoch)

    def _replenish_consumers(self, epoch: _ConnectionEpoch) -> None:
        if not self._epoch_is_current(epoch) or self._stop.is_set() or epoch.channel is None:
            return
        with self._state_lock:
            occupied = {slot_id for slot_id, ticket in self._tickets.items() if not ticket.released}
            free_slots = [
                slot_id
                for slot_id in range(self._config.execution_slots)
                if slot_id not in occupied
            ]
        connection = epoch.connection
        assert connection is not None
        for slot_id in free_slots:
            if not self._epoch_is_current(epoch):
                return
            with self._state_lock:
                self._ticket_counter += 1
                ticket_number = self._ticket_counter
            tag = f"dlr-worker-{self._config.worker_id}-e{epoch.number}-s{slot_id}-t{ticket_number}"
            ticket = SlotTicket(slot_id, epoch.number, tag)
            with self._state_lock:
                if slot_id in self._tickets and not self._tickets[slot_id].released:
                    continue
                self._tickets[slot_id] = ticket
            try:
                ticket.deadline_handle = connection.ioloop.call_later(
                    self._config.claim_handshake_timeout_seconds,
                    lambda value=ticket: self._registration_timeout(epoch, value),
                )
                returned_tag = epoch.channel.basic_consume(
                    queue=self._config.queue,
                    on_message_callback=(
                        lambda channel, method, properties, body, value=ticket: self._on_delivery(
                            epoch, value, channel, method, properties, body
                        )
                    ),
                    auto_ack=False,
                    consumer_tag=tag,
                    callback=lambda frame, value=ticket: self._on_consume_ok(epoch, value, frame),
                )
                if returned_tag != tag:
                    self._fault_epoch(epoch, "consumer_tag_mismatch")
                    return
            except Exception:
                self._fault_epoch(epoch, "consumer_registration_failed")
                return

    def _registration_timeout(self, epoch: _ConnectionEpoch, ticket: SlotTicket) -> None:
        if self._ticket_matches(ticket, epoch, "receiving"):
            self._fault_epoch(epoch, "consumer_registration_timeout")

    def _on_consume_ok(self, epoch: _ConnectionEpoch, ticket: SlotTicket, frame: Any) -> None:
        tag = getattr(getattr(frame, "method", None), "consumer_tag", None)
        if not self._ticket_matches(ticket, epoch, "receiving"):
            return
        if tag != ticket.consumer_tag:
            self._fault_epoch(epoch, "consumer_registration_mismatch")
            return
        self._remove_ticket_timer(epoch, ticket)

    def _on_delivery(
        self,
        epoch: _ConnectionEpoch,
        ticket: SlotTicket,
        _channel: Any,
        method: Any,
        _properties: Any,
        body: bytes,
    ) -> None:
        delivery_tag = int(method.delivery_tag)
        consumer_tag = str(method.consumer_tag)
        with self._state_lock:
            valid = (
                self._epoch_is_current_locked(epoch)
                and self._tickets.get(ticket.slot_id) is ticket
                and ticket.phase == "receiving"
                and ticket.delivery_tag is None
                and consumer_tag == ticket.consumer_tag
            )
            if valid:
                ticket.phase = "cancelling"
                ticket.delivery_tag = delivery_tag
                ticket.body = body
                ticket.deadline_at = time.monotonic() + self._config.claim_handshake_timeout_seconds
        if not valid:
            self._fault_epoch(epoch, "unexpected_consumer_delivery")
            return
        connection = epoch.connection
        channel = epoch.channel
        assert connection is not None and channel is not None
        try:
            ticket.deadline_handle = connection.ioloop.call_later(
                self._config.claim_handshake_timeout_seconds,
                lambda: self._handshake_timeout(epoch, ticket),
            )
            channel.basic_cancel(
                ticket.consumer_tag,
                callback=epoch.cancel_waiter,
            )
        except Exception:
            self._fault_epoch(epoch, "consumer_cancel_failed")

    def _on_cancel_ok_frame(self, epoch: _ConnectionEpoch, frame: Any) -> None:
        tag = getattr(getattr(frame, "method", None), "consumer_tag", None)
        with self._state_lock:
            ticket = next(
                (
                    value
                    for value in self._tickets.values()
                    if value.connection_epoch == epoch.number
                    and value.consumer_tag == tag
                    and not value.released
                ),
                None,
            )
        if ticket is None:
            return
        self._on_cancel_ok(epoch, ticket, frame)

    def _on_cancel_ok(self, epoch: _ConnectionEpoch, ticket: SlotTicket, frame: Any) -> None:
        tag = getattr(getattr(frame, "method", None), "consumer_tag", None)
        with self._state_lock:
            valid = (
                tag == ticket.consumer_tag
                and self._epoch_is_current_locked(epoch)
                and self._tickets.get(ticket.slot_id) is ticket
                and ticket.phase == "cancelling"
                and not ticket.work_submitted
            )
            if valid:
                ticket.phase = "working"
                ticket.work_submitted = True
                body = ticket.body
                deadline_at = ticket.deadline_at
        if not valid:
            return
        assert body is not None and deadline_at is not None
        try:
            future = self._pool.submit(self._process_ticket, ticket, body, deadline_at)
            future.add_done_callback(
                lambda value: self._complete_ticket(ticket, failed=self._future_failed(value))
            )
        except RuntimeError:
            with self._state_lock:
                ticket.phase = "cancelling"
                ticket.work_submitted = False
            self._fault_epoch(epoch, "consumer_work_submit_failed")

    def _on_broker_cancel(self, epoch: _ConnectionEpoch, frame: Any) -> None:
        tag = getattr(getattr(frame, "method", None), "consumer_tag", None)
        with self._state_lock:
            matches = any(
                ticket.connection_epoch == epoch.number
                and ticket.consumer_tag == tag
                and not ticket.released
                for ticket in self._tickets.values()
            )
        if matches and self._epoch_is_current(epoch):
            self._fault_epoch(epoch, "broker_consumer_cancelled")

    def _handshake_timeout(self, epoch: _ConnectionEpoch, ticket: SlotTicket) -> None:
        with self._state_lock:
            active = self._tickets.get(ticket.slot_id) is ticket and not ticket.released
        if active and self._epoch_is_current(epoch):
            self._fault_epoch(epoch, "claim_handshake_timeout")

    def _process_ticket(self, ticket: SlotTicket, body: bytes, deadline_at: float) -> None:
        reservation: sandbox.ResourceReservation | None = None
        try:
            decoded = self._decode_dispatch(body)
            if not self._ticket_can_continue(ticket, deadline_at):
                return
            try:
                decision = self._client.claim_v3(
                    self._config.worker_id,
                    decoded,
                    timeout_seconds=max(0.001, deadline_at - time.monotonic()),
                )
            except (ControlUnavailableError, ClientError) as error:
                if isinstance(error, ClientError):
                    logger.warning(
                        "Claim rejected by Control: status=%s code=%s",
                        error.status,
                        _client_error_code(error),
                    )
                self._request_fault(ticket, "control_claim_failed")
                return
            if not self._ticket_can_continue(ticket, deadline_at):
                return
            with self._state_lock:
                epoch = self._active_epoch
                if epoch is not None and epoch.number == ticket.connection_epoch:
                    epoch.successful_claim = True
            kind = decision.get("decision")
            if kind == "ACK_NOOP":
                self._send_disposition(ticket, deadline_at, "ack")
                return
            if kind == "REJECT_DLQ":
                self._send_disposition(ticket, deadline_at, "reject")
                return
            if kind == "PAUSE_CONSUMER":
                self._request_fault(ticket, "control_pause_consumer")
                return
            if kind == "DEFER":
                self._send_disposition(ticket, deadline_at, "defer")
                return
            if kind != "EXECUTE" or not isinstance(decision.get("payload"), Mapping):
                self._send_disposition(ticket, deadline_at, "reject")
                return
            prepared = self._prepare_execute(decision, ticket=ticket, deadline_at=deadline_at)
            if prepared.prepared is None:
                if prepared.disposition is not None:
                    self._send_disposition(ticket, deadline_at, prepared.disposition)
                elif self._ticket_can_continue(ticket, deadline_at):
                    self._request_fault(ticket, "attempt_prepare_failed")
                return
            reservation = prepared.prepared.reservation
            if not self._send_disposition(ticket, deadline_at, "ack"):
                return
            if not self._ticket_can_continue(ticket, deadline_at):
                return
            self._run_attempt(prepared.prepared.payload, reservation, release_reservation=False)
        finally:
            if reservation is not None and self._resource_budget is not None:
                self._resource_budget.release(reservation)

    @staticmethod
    def _decode_dispatch(body: bytes) -> Mapping[str, Any]:
        try:
            decoded: object = json.loads(body)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, Mapping) else {}

    def _prepare_execute(
        self,
        decision: Mapping[str, Any],
        *,
        ticket: SlotTicket | None = None,
        deadline_at: float | None = None,
    ) -> _PreparationResult:
        raw_payload = decision.get("payload")
        sandbox_config = getattr(self._runtime_settings, "sandbox_config", None)
        prevalidated_profile: sandbox.ResourceLimits | None = None
        if (
            sandbox_config is not None
            and isinstance(raw_payload, Mapping)
            and raw_payload.get("protocol_version", 3) == 3
        ):
            try:
                prevalidated_profile = sandbox.validate_resource_profile(
                    raw_payload.get("resource_profile"), sandbox_config
                )
                sandbox.validate_v3_payload_snapshots(raw_payload, prevalidated_profile)
            except sandbox.SandboxError as error:
                reported = self._report_prepare_failure(
                    decision,
                    error_code=error.code,
                    error_class="platform_transient",
                    timeout_seconds=self._remaining(deadline_at),
                )
                return _PreparationResult(disposition="ack" if reported else None)
        try:
            payload = V3TaskPayload.model_validate(raw_payload)
        except Exception:
            reported = self._report_prepare_failure(
                decision, timeout_seconds=self._remaining(deadline_at)
            )
            return _PreparationResult(disposition="ack" if reported else None)
        if sandbox_config is not None:
            try:
                profile = prevalidated_profile or sandbox.validate_resource_profile(
                    payload.resource_profile, sandbox_config
                )
                if prevalidated_profile is None:
                    sandbox.validate_v3_payload_snapshots(
                        cast(Mapping[str, Any], raw_payload), profile
                    )
            except sandbox.SandboxError as error:
                reported = self._report_prepare_failure(
                    decision,
                    error_code=error.code,
                    error_class="platform_transient",
                    timeout_seconds=self._remaining(deadline_at),
                )
                return _PreparationResult(disposition="ack" if reported else None)
        reservation: sandbox.ResourceReservation | None = None
        if self._resource_budget is not None:
            assert prevalidated_profile is not None
            reservation = self._resource_budget.try_reserve(prevalidated_profile)
            if reservation is None:
                reported = self._report_prepare_failure(
                    decision,
                    error_code="resource_capacity_unavailable",
                    error_class="platform_transient",
                    timeout_seconds=self._remaining(deadline_at),
                )
                return _PreparationResult(disposition="ack" if reported else None)
        try:
            planned_workspace = workspace.workspace_path(
                self._config.runtime_root,
                payload.execution_id,
                attempt_id=payload.attempt_id,
            )
            workspace.write_attempt_journal(
                self._config.attempt_journal_root,
                execution_id=payload.execution_id,
                attempt_id=payload.attempt_id,
                attempt_no=payload.attempt_no,
                fencing_token=payload.fencing_token,
                lease_expires_at=payload.lease_expires_at.isoformat(),
                workspace=planned_workspace,
                claim_token=payload.claim_token,
                cleanup_token=payload.cleanup_token,
            )
        except Exception:
            if reservation is not None and self._resource_budget is not None:
                self._resource_budget.release(reservation)
            reported = self._report_prepare_failure(
                decision, timeout_seconds=self._remaining(deadline_at)
            )
            return _PreparationResult(disposition="ack" if reported else None)
        if (
            ticket is not None
            and deadline_at is not None
            and not self._ticket_can_continue(ticket, deadline_at)
        ):
            if reservation is not None and self._resource_budget is not None:
                self._resource_budget.release(reservation)
            return _PreparationResult()
        return _PreparationResult(prepared=_PreparedExecution(payload, reservation))

    def _report_prepare_failure(
        self,
        decision: Mapping[str, Any],
        *,
        error_code: str = "attempt_prepare_failed",
        error_class: str = "platform_transient",
        timeout_seconds: float | None = None,
    ) -> bool:
        payload = decision.get("payload")
        if not isinstance(payload, Mapping):
            return False
        try:
            attempt_id = int(payload["attempt_id"])
            body = {
                "attempt_id": attempt_id,
                "fencing_token": int(payload["fencing_token"]),
                "claim_token": str(payload["claim_token"]),
                "error_code": error_code,
                "error_class": error_class,
            }
            self._client.prepare_failed_attempt(
                self._config.worker_id,
                attempt_id,
                body,
                timeout_seconds=timeout_seconds,
            )
            return True
        except (ControlUnavailableError, ClientError, KeyError, TypeError, ValueError):
            return False

    def _run_attempt(
        self,
        payload: V3TaskPayload,
        reservation: sandbox.ResourceReservation | None = None,
        *,
        release_reservation: bool = True,
    ) -> None:
        try:
            try:
                start_response = self._client.start_attempt(
                    self._config.worker_id,
                    payload.attempt_id,
                    {
                        "attempt_id": payload.attempt_id,
                        "fencing_token": payload.fencing_token,
                        "claim_token": payload.claim_token,
                    },
                )
            except (ControlUnavailableError, ClientError):
                return
            if not (
                isinstance(start_response, Mapping)
                and start_response.get("decision") == "ACK_NOOP"
                and start_response.get("reason") in {"started", "already_started"}
            ):
                # Claim is durable, but only Control can authorize the
                # Adapter side effect at this boundary.  Keep the journal
                # and let lease recovery reconcile terminal/cancelled/lost
                # Attempts instead of running an unowned process.
                return
            renew_stop = threading.Event()
            ownership_lost = threading.Event()
            cancel_requested = threading.Event()

            def renew_loop() -> None:
                while not renew_stop.wait(payload.renew_seconds):
                    if self._stop.is_set():
                        ownership_lost.set()
                        return
                    try:
                        response = self._client.renew_attempt(
                            self._config.worker_id,
                            payload.attempt_id,
                            {
                                "attempt_id": payload.attempt_id,
                                "fencing_token": payload.fencing_token,
                                "claim_token": payload.claim_token,
                            },
                        )
                        if not _is_successful_attempt_action(
                            response,
                            attempt_id=payload.attempt_id,
                            reason="renewed",
                        ):
                            if _is_cancel_requested_action(response, attempt_id=payload.attempt_id):
                                cancel_requested.set()
                                return
                            ownership_lost.set()
                            return
                    except (ControlUnavailableError, ClientError):
                        ownership_lost.set()
                        return

            renew_thread = threading.Thread(
                target=renew_loop, name="dlr-attempt-renew", daemon=True
            )
            renew_thread.start()

            def progress(stdout_chunk: str, stderr_chunk: str) -> bool:
                if self._stop.is_set():
                    # Stop payloads within Docker's grace period. Leave the
                    # durable Attempt to lease/fencing recovery; a service
                    # shutdown is not a user-requested business cancellation.
                    ownership_lost.set()
                    return True
                if cancel_requested.is_set():
                    return True
                if ownership_lost.is_set():
                    return True
                try:
                    result = self._client.progress_attempt(
                        self._config.worker_id,
                        payload.attempt_id,
                        {
                            "attempt_id": payload.attempt_id,
                            "fencing_token": payload.fencing_token,
                            "claim_token": payload.claim_token,
                            "stdout_chunk": stdout_chunk,
                            "stderr_chunk": stderr_chunk,
                        },
                    )
                    if not _is_successful_attempt_action(
                        result,
                        attempt_id=payload.attempt_id,
                        reason="progressed",
                    ):
                        if _is_cancel_requested_action(result, attempt_id=payload.attempt_id):
                            cancel_requested.set()
                            return True
                        ownership_lost.set()
                        return True
                    return False
                except (ControlUnavailableError, ClientError):
                    ownership_lost.set()
                    return True

            def download(descriptor: Mapping[str, Any], destination: Any) -> int:
                return self._client.download_input_artifact(
                    self._config.worker_id,
                    payload.execution_id,
                    int(descriptor["id"]),
                    claim_token=payload.claim_token,
                    destination=destination,
                )

            def download_builtin(descriptor: Mapping[str, Any], destination: Any) -> int:
                return self._client.download_builtin_package(
                    self._config.worker_id,
                    payload.execution_id,
                    int(descriptor["id"]),
                    claim_token=payload.claim_token,
                    destination=destination,
                )

            cache_use: CacheUse | None = None
            result: dict[str, Any] | None = None
            try:
                try:
                    cache_use = CacheLifecycleStore.for_runtime(
                        self._config.runtime_root
                    ).begin_use(
                        cache_key(payload.adapter_id, payload.version_id),
                        worker_id=self._config.worker_id,
                        execution_id=payload.execution_id,
                        attempt_id=payload.attempt_id,
                        fencing_token=payload.fencing_token,
                    )
                    cache_use.__enter__()
                except CacheError:
                    cache_use = None
                    result = {
                        "status": "failed",
                        "error_code": "cache_use_write_failed",
                        "error_class": "platform_transient",
                        "error": "Worker execution failed",
                        # No dependency, Workspace or Sandbox side effect has
                        # started. The terminal Result durably records that
                        # fact before the Attempt journal follows its normal
                        # removal path.
                        "workspace_cleanup_status": "completed",
                    }
                else:
                    try:
                        cleanup_root = (
                            getattr(
                                self._runtime_settings,
                                "workspace_cleanup_journal_root",
                                None,
                            )
                            or self._config.runtime_root / "cleanup-journal"
                        )
                        authority = ReplacementAuthority(
                            client=self._client,
                            worker_id=self._config.worker_id,
                            payload=payload.model_dump(mode="json"),
                            attempt_journal_root=self._config.attempt_journal_root,
                            cleanup_journal_root=cleanup_root,
                        )
                        with activate_replacement(authority):
                            result = self._runner(
                                payload.model_dump(mode="json"),
                                self._runtime_settings,
                                progress_callback=progress,
                                input_downloader=download,
                                **(
                                    {"builtin_downloader": download_builtin}
                                    if payload.builtin_package_snapshot is not None
                                    else {}
                                ),
                            )
                    except Exception:
                        result = {
                            "status": "failed",
                            "error_code": "worker_internal_error",
                            "error_class": "platform_transient",
                            "error": "Worker execution failed",
                        }
            finally:
                renew_stop.set()
                if cache_use is not None:
                    cleanup_summary = result.get("cleanup_summary") if result is not None else None
                    sandbox_summary = (
                        cleanup_summary.get("sandbox")
                        if isinstance(cleanup_summary, Mapping)
                        else None
                    )
                    cleanup_completed = bool(
                        isinstance(sandbox_summary, Mapping)
                        and sandbox_summary.get("status") == "completed"
                    )
                    try:
                        cache_use.release(cleanup_completed=cleanup_completed)
                    except CacheError:
                        logger.warning(
                            "cache use record could not be cleared for attempt %s",
                            payload.attempt_id,
                        )
            assert result is not None
            if ownership_lost.is_set():
                return
            if cancel_requested.is_set() and result.get("status") != "cancelled":
                result = {
                    **result,
                    "status": "cancelled",
                    "error": "execution cancelled",
                    "error_code": "execution_cancelled",
                    "error_class": "cancelled",
                }
            status = result.get("status")
            if status == "timeout":
                status = "timed_out"
            cleanup_status = result.get("workspace_cleanup_status")
            report = {
                "attempt_id": payload.attempt_id,
                "fencing_token": payload.fencing_token,
                "claim_token": payload.claim_token,
                **result,
                "status": status,
            }
            report.setdefault("error_class", "business_error" if status != "succeeded" else None)
            try:
                self._client.result_attempt(self._config.worker_id, payload.attempt_id, report)
            except (ControlUnavailableError, ClientError):
                return
            if cleanup_status == "completed":
                cleanup_root = getattr(
                    self._runtime_settings, "workspace_cleanup_journal_root", None
                )
                if cleanup_root is None:
                    cleanup_root = self._config.runtime_root / "cleanup-journal"
                try:
                    self._client.report_cleanup_receipt(
                        self._config.worker_id,
                        payload.execution_id,
                        cleanup_token=payload.cleanup_token,
                    )
                except (ControlUnavailableError, ClientError):
                    # The terminal Attempt is durable, but the cleanup
                    # journal remains the restart/recovery hand-off until
                    # Control accepts the independent receipt.
                    workspace.remove_attempt_journal(
                        self._config.attempt_journal_root,
                        payload.attempt_id,
                    )
                    return
                workspace.remove_cleanup_journal(
                    cleanup_root,
                    payload.execution_id,
                    attempt_id=payload.attempt_id,
                )
            workspace.remove_attempt_journal(
                self._config.attempt_journal_root,
                payload.attempt_id,
            )
        finally:
            if (
                release_reservation
                and reservation is not None
                and self._resource_budget is not None
            ):
                self._resource_budget.release(reservation)

    def _send_disposition(
        self,
        ticket: SlotTicket,
        deadline_at: float,
        disposition: str,
    ) -> bool:
        """Send one ACK/NACK in the original I/O loop and wait for send receipt."""

        receipt = threading.Event()
        outcome = {"sent": False}
        with self._state_lock:
            epoch = self._active_epoch
            connection = epoch.connection if epoch is not None else None
        if (
            epoch is None
            or connection is None
            or epoch.number != ticket.connection_epoch
            or not self._ticket_can_continue(ticket, deadline_at)
        ):
            return False

        def send() -> None:
            if not self._ticket_can_continue(ticket, deadline_at) or epoch.channel is None:
                receipt.set()
                return
            try:
                assert ticket.delivery_tag is not None
                if disposition == "ack":
                    epoch.channel.basic_ack(delivery_tag=ticket.delivery_tag)
                elif disposition == "defer":
                    epoch.channel.basic_nack(delivery_tag=ticket.delivery_tag, requeue=True)
                else:
                    epoch.channel.basic_nack(delivery_tag=ticket.delivery_tag, requeue=False)
                self._remove_ticket_timer(epoch, ticket)
                with self._state_lock:
                    ticket.disposition_sent = True
                outcome["sent"] = True
            except Exception:
                self._fault_epoch(epoch, "delivery_disposition_failed")
            finally:
                receipt.set()

        try:
            connection.ioloop.add_callback_threadsafe(send)
        except Exception:
            self._fail_closed_from_worker(epoch, "delivery_disposition_schedule_failed")
            return False
        remaining = max(0.0, deadline_at - time.monotonic())
        if not receipt.wait(remaining):
            self._request_fault(ticket, "claim_handshake_timeout")
            return False
        return outcome["sent"]

    def _ticket_can_continue(self, ticket: SlotTicket, deadline_at: float) -> bool:
        with self._state_lock:
            epoch = self._active_epoch
            return (
                time.monotonic() < deadline_at
                and not self._stop.is_set()
                and epoch is not None
                and epoch.number == ticket.connection_epoch
                and not epoch.faulted
                and self._tickets.get(ticket.slot_id) is ticket
                and ticket.phase == "working"
                and not ticket.released
            )

    def _request_fault(self, ticket: SlotTicket, reason: str) -> None:
        with self._state_lock:
            epoch = self._active_epoch
            connection = epoch.connection if epoch is not None else None
        if epoch is None or connection is None or epoch.number != ticket.connection_epoch:
            return
        try:
            connection.ioloop.add_callback_threadsafe(lambda: self._fault_epoch(epoch, reason))
        except Exception:
            self._fail_closed_from_worker(epoch, "consumer_fault_schedule_failed")

    @staticmethod
    def _future_failed(future: Future[None]) -> bool:
        if future.cancelled():
            return True
        try:
            return future.exception() is not None
        except Exception:
            return True

    def _complete_ticket(self, ticket: SlotTicket, *, failed: bool) -> None:
        with self._state_lock:
            ticket.completion_pending = True
            epoch = self._active_epoch
            connection = epoch.connection if epoch is not None else None
        if epoch is None or connection is None:
            return

        def complete() -> None:
            if epoch.number == ticket.connection_epoch and (failed or not ticket.disposition_sent):
                self._fault_epoch(epoch, "delivery_work_unsettled")
            self._release_completed_tickets_io(epoch)

        try:
            connection.ioloop.add_callback_threadsafe(complete)
        except Exception:
            self._fail_closed_from_worker(epoch, "ticket_completion_schedule_failed")

    def _fail_closed_from_worker(self, epoch: _ConnectionEpoch, reason: str) -> None:
        """Invalidate capacity without touching Pika when IO scheduling is unavailable."""

        with self._state_lock:
            if self._active_epoch is not epoch:
                return
            epoch.faulted = True
            epoch.abort_failed = True
            self._stop.set()
        logger.error(
            "Consumer I/O callback unavailable; stopping without slot reuse: code=%s epoch=%s",
            reason,
            epoch.number,
        )

    def _release_completed_tickets_io(self, epoch: _ConnectionEpoch) -> None:
        completed: list[SlotTicket] = []
        with self._state_lock:
            for ticket in self._tickets.values():
                transport_released = (
                    ticket.connection_epoch != epoch.number or epoch.transport_terminated
                )
                if (
                    ticket.completion_pending
                    and not ticket.released
                    and (ticket.disposition_sent or transport_released)
                ):
                    ticket.phase = "released"
                    ticket.released = True
                    completed.append(ticket)
            for ticket in completed:
                self._tickets.pop(ticket.slot_id, None)
        for ticket in completed:
            if ticket.connection_epoch == epoch.number:
                self._remove_ticket_timer(epoch, ticket)
        if completed:
            self._replenish_consumers(epoch)

    def _fault_epoch(self, epoch: _ConnectionEpoch, reason: str) -> None:
        """Run only in the I/O thread: invalidate the epoch and abort its stream."""

        if epoch.transport_terminated or epoch.abort_initiated:
            return
        epoch.faulted = True
        logger.warning("Consumer epoch faulted: code=%s epoch=%s", reason, epoch.number)
        connection = epoch.connection
        if connection is None:
            epoch.abort_failed = True
            self._stop.set()
            return
        try:
            connection._terminate_stream(TimeoutError(reason))
            epoch.abort_initiated = True
        except Exception:
            logger.debug("consumer transport abort failed", exc_info=True)
            try:
                connection._adapter_disconnect_stream()
                epoch.abort_initiated = True
            except Exception:
                logger.exception(
                    "Consumer transport could not be aborted; stopping without reconnect"
                )
                epoch.abort_failed = True
                self._stop.set()
                self._stop_ioloop(epoch)
                return
        try:
            epoch.abort_deadline = connection.ioloop.call_later(
                min(1.0, self._config.claim_handshake_timeout_seconds),
                lambda: self._stop_ioloop(epoch),
            )
        except Exception:
            self._stop_ioloop(epoch)

    def _stop_ioloop(self, epoch: _ConnectionEpoch) -> None:
        if epoch.stopped or epoch.connection is None:
            return
        epoch.stopped = True
        try:
            epoch.connection.ioloop.stop()
        except Exception:
            logger.debug("consumer I/O loop stop failed", exc_info=True)

    def _finalize_epoch(self, epoch: _ConnectionEpoch) -> None:
        """After the old loop exits, release only work the Broker still owns."""

        if not epoch.transport_terminated:
            epoch.abort_failed = True
            self._stop.set()
            return
        with self._state_lock:
            for slot_id, ticket in list(self._tickets.items()):
                if ticket.connection_epoch != epoch.number:
                    continue
                ticket.deadline_handle = None
                releasable_work = ticket.phase == "working" and ticket.completion_pending
                if ticket.phase in {"receiving", "cancelling"} or releasable_work:
                    ticket.phase = "released"
                    ticket.released = True
                    self._tickets.pop(slot_id, None)
            if self._active_epoch is epoch:
                self._active_epoch = None

    def _close_epoch_after_loop(self, epoch: _ConnectionEpoch) -> None:
        """Isolate an old transport before releasing Broker-owned tickets."""

        connection = epoch.connection
        if not (epoch.transport_terminated or epoch.abort_initiated):
            self._fault_epoch(epoch, "consumer_ioloop_exited")
        if epoch.abort_initiated and not epoch.transport_terminated and not epoch.abort_failed:
            try:
                # Pika's transport.abort() deactivates socket polling and
                # schedules connection_lost. Drive that callback before
                # closing the loop or releasing Broker-owned tickets.
                epoch.stopped = False
                epoch.abort_deadline = connection.ioloop.call_later(  # type: ignore[union-attr]
                    min(1.0, self._config.claim_handshake_timeout_seconds),
                    lambda: self._stop_ioloop(epoch),
                )
                connection.ioloop.start()  # type: ignore[union-attr]
            except Exception:
                logger.exception(
                    "Consumer transport termination did not complete; stopping without reconnect"
                )
                epoch.abort_failed = True
                self._stop.set()
        self._finalize_epoch(epoch)
        if connection is None or epoch.abort_failed:
            return
        try:
            connection.ioloop.close()
        except Exception:
            logger.exception("Consumer I/O loop close failed; stopping without reconnect")
            self._stop.set()

    def _remove_ticket_timer(self, epoch: _ConnectionEpoch, ticket: SlotTicket) -> None:
        self._remove_timer(epoch, ticket.deadline_handle)
        ticket.deadline_handle = None

    @staticmethod
    def _remove_timer(epoch: _ConnectionEpoch, handle: Any | None) -> None:
        if handle is None or epoch.connection is None:
            return
        try:
            epoch.connection.ioloop.remove_timeout(handle)
        except Exception:
            return

    def _ticket_matches(self, ticket: SlotTicket, epoch: _ConnectionEpoch, phase: str) -> bool:
        with self._state_lock:
            return (
                self._epoch_is_current_locked(epoch)
                and self._tickets.get(ticket.slot_id) is ticket
                and ticket.phase == phase
                and not ticket.released
            )

    def _epoch_is_current(self, epoch: _ConnectionEpoch) -> bool:
        with self._state_lock:
            return self._epoch_is_current_locked(epoch)

    def _epoch_is_current_locked(self, epoch: _ConnectionEpoch) -> bool:
        return self._active_epoch is epoch and not epoch.faulted

    @staticmethod
    def _remaining(deadline_at: float | None) -> float | None:
        if deadline_at is None:
            return None
        return max(0.001, deadline_at - time.monotonic())
