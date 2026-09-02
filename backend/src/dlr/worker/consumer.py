"""RabbitMQ Worker v3 Consumer with bounded delivery and ACK handling.

The consumer is intentionally isolated from the legacy long-poll loop.  It
uses one Pika connection thread, a finite local execution pool and a matching
Semaphore.  A Control transport/authentication failure pauses the channel and
lets the Broker re-deliver after a bounded reconnect backoff; it never creates
an immediate ``nack(requeue=True)`` loop.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, cast

import pika

from dlr.control.schemas.reliable_runtime import V3TaskPayload
from dlr.worker import executor, sandbox, workspace
from dlr.worker.client import ClientError, ControlClient, ControlUnavailableError

logger = logging.getLogger("dlr.worker.consumer")


def _is_successful_attempt_action(response: object, *, attempt_id: int, reason: str) -> bool:
    """Accept only the exact Control acknowledgement for one action."""
    return (
        isinstance(response, Mapping)
        and response.get("decision") == "ACK_NOOP"
        and response.get("reason") == reason
        and response.get("attempt_id") == attempt_id
        and response.get("cancel_requested") is False
    )


@dataclass(frozen=True)
class ConsumerConfig:
    """Small immutable subset of WorkerConfig needed by the v3 Consumer."""

    worker_id: int
    queue: str
    execution_slots: int
    runtime_root: Path
    attempt_journal_root: Path
    attempt_reconnect_max_seconds: float = 30.0


class V3Consumer:
    """Consume one fixed Worker queue and hand all state changes to Control."""

    def __init__(
        self,
        config: ConsumerConfig,
        client: ControlClient,
        *,
        connection_factory: Callable[[], pika.BlockingConnection],
        runtime_settings: Any,
        runner: Callable[..., dict[str, Any]] = executor.run,
    ) -> None:
        if config.execution_slots < 1:
            raise ValueError("execution_slots must be positive")
        self._config = config
        self._client = client
        self._connection_factory = connection_factory
        self._runtime_settings = runtime_settings
        self._runner = runner
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._slots = threading.BoundedSemaphore(config.execution_slots)
        self._pool = ThreadPoolExecutor(
            max_workers=config.execution_slots,
            thread_name_prefix="dlr-v3-attempt",
        )

    def request_stop(self) -> None:
        self._stop.set()
        self._pause.set()

    def run(self) -> None:
        """Reconnect with capped backoff while preserving unacked delivery."""
        backoff = 1.0
        try:
            while not self._stop.is_set():
                self._pause.clear()
                connection: pika.BlockingConnection | None = None
                try:
                    connection = self._connection_factory()
                    channel = connection.channel()
                    channel.basic_qos(
                        prefetch_count=self._config.execution_slots,
                        global_qos=False,
                    )
                    channel.basic_consume(
                        queue=self._config.queue,
                        on_message_callback=partial(self._on_delivery, connection, channel),
                        auto_ack=False,
                    )
                    backoff = 1.0
                    while not self._stop.is_set() and not self._pause.is_set():
                        connection.process_data_events(time_limit=1.0)
                except (ControlUnavailableError, ClientError) as error:
                    logger.warning(
                        "v3 Consumer paused by Control boundary: %s", type(error).__name__
                    )
                    self._pause.set()
                except (pika.exceptions.AMQPError, OSError, TimeoutError):
                    logger.warning(
                        "v3 Consumer transport unavailable; reconnecting with bounded backoff"
                    )
                    self._pause.set()
                except Exception:
                    logger.exception("v3 Consumer stopped on an unexpected transport error")
                    self._pause.set()
                finally:
                    if connection is not None:
                        try:
                            if connection.is_open:
                                connection.close()
                        except Exception:
                            logger.debug("v3 Consumer connection close failed", exc_info=True)
                if not self._stop.is_set():
                    self._stop.wait(min(backoff, self._config.attempt_reconnect_max_seconds))
                    backoff = min(backoff * 2, self._config.attempt_reconnect_max_seconds)
        finally:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def _on_delivery(
        self,
        connection: pika.BlockingConnection,
        channel: Any,
        _method: Any,
        _properties: Any,
        body: bytes,
    ) -> None:
        delivery_tag = int(_method.delivery_tag)
        if not self._slots.acquire(blocking=False):
            # Prefetch should make this unreachable. Closing the channel keeps
            # the delivery unacked and triggers a bounded broker redelivery.
            self._request_pause(connection, channel)
            return
        self._pool.submit(self._handle_delivery, connection, channel, delivery_tag, body)

    def _handle_delivery(
        self,
        connection: pika.BlockingConnection,
        channel: Any,
        delivery_tag: int,
        body: bytes,
    ) -> None:
        keep_slot = False
        try:
            try:
                decoded: object = json.loads(body)
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
                decoded = {}
            if not isinstance(decoded, Mapping):
                decoded = {}
            try:
                decision = self._client.claim_v3(self._config.worker_id, decoded)
            except (ControlUnavailableError, ClientError):
                self._request_pause(connection, channel)
                return
            kind = decision.get("decision")
            if kind == "ACK_NOOP":
                self._ack(connection, channel, delivery_tag)
                return
            if kind == "REJECT_DLQ":
                self._nack(connection, channel, delivery_tag)
                return
            if kind == "PAUSE_CONSUMER":
                self._request_pause(connection, channel)
                return
            if kind == "DEFER":
                self._defer(connection, channel, delivery_tag)
                return
            if kind != "EXECUTE" or not isinstance(decision.get("payload"), Mapping):
                self._nack(connection, channel, delivery_tag)
                return
            keep_slot = self._prepare_execute(connection, channel, delivery_tag, decision)
        finally:
            if not keep_slot:
                self._slots.release()

    def _prepare_execute(
        self,
        connection: pika.BlockingConnection,
        channel: Any,
        delivery_tag: int,
        decision: Mapping[str, Any],
    ) -> bool:
        raw_payload = decision.get("payload")
        try:
            payload = V3TaskPayload.model_validate(raw_payload)
        except Exception:
            if self._report_prepare_failure(decision):
                self._ack(connection, channel, delivery_tag)
            else:
                # A malformed payload is terminal only after Control has
                # recorded the prepare failure.  Keep the delivery unacked
                # when that report is unavailable so recovery can reconcile
                # the Claim instead of losing the dispatch.
                self._request_pause(connection, channel)
            return False
        sandbox_config = getattr(self._runtime_settings, "sandbox_config", None)
        if sandbox_config is not None:
            try:
                profile = sandbox.validate_resource_profile(
                    payload.resource_profile, sandbox_config
                )
                sandbox.validate_v3_payload_snapshots(cast(Mapping[str, Any], raw_payload), profile)
            except sandbox.SandboxError as error:
                if self._report_prepare_failure(
                    decision,
                    error_code=error.code,
                    error_class="platform_transient",
                ):
                    self._ack(connection, channel, delivery_tag)
                else:
                    self._request_pause(connection, channel)
                return False
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
            if self._report_prepare_failure(decision):
                self._ack(connection, channel, delivery_tag)
            else:
                self._request_pause(connection, channel)
            return False
        try:
            self._pool.submit(self._run_attempt, payload)
        except RuntimeError:
            # A concurrent stop can close the pool after the journal has been
            # made durable. Leave the Attempt to Control lease recovery; do
            # not ACK a delivery while pretending the local run was queued.
            self._request_pause(connection, channel)
            return False
        self._ack(connection, channel, delivery_tag)
        return True

    def _report_prepare_failure(
        self,
        decision: Mapping[str, Any],
        *,
        error_code: str = "attempt_prepare_failed",
        error_class: str = "platform_transient",
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
            self._client.prepare_failed_attempt(self._config.worker_id, attempt_id, body)
            return True
        except (ControlUnavailableError, ClientError, KeyError, TypeError, ValueError):
            return False

    def _run_attempt(self, payload: V3TaskPayload) -> None:
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

            def renew_loop() -> None:
                while not renew_stop.wait(payload.renew_seconds):
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
                            ownership_lost.set()
                            return
                    except (ControlUnavailableError, ClientError):
                        ownership_lost.set()
                        return

            renew_thread = threading.Thread(target=renew_loop, name="dlr-v3-renew", daemon=True)
            renew_thread.start()

            def progress(stdout_chunk: str, stderr_chunk: str) -> bool:
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

            try:
                result = self._runner(
                    payload.model_dump(mode="json"),
                    self._runtime_settings,
                    progress_callback=progress,
                    input_downloader=download,
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
            if ownership_lost.is_set():
                return
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
            self._slots.release()

    def _defer(
        self,
        connection: pika.BlockingConnection,
        channel: Any,
        delivery_tag: int,
    ) -> None:
        """Return the same delivery and let the Quorum Queue delay it.

        RabbitMQ 4.3 applies ``x-delayed-retry-type`` to a native returned
        delivery.  AMQP 0-9-1 ``basic.nack(requeue=True)`` therefore preserves
        the message identity and avoids the publish+ACK window that could
        otherwise create a duplicate.  The queue's bounded linear delay is
        authoritative; the Control ``retry_after_seconds`` value remains a
        bounded decision fact for observability, not an ``x-delay`` header.
        """
        try:
            connection.add_callback_threadsafe(
                partial(channel.basic_nack, delivery_tag=delivery_tag, requeue=True)
            )
        except Exception:
            self._request_pause(connection, channel)
            logger.debug("v3 native DEFER scheduling failed", exc_info=True)

    @staticmethod
    def _ack(connection: Any, channel: Any, delivery_tag: int) -> None:
        try:
            connection.add_callback_threadsafe(
                partial(channel.basic_ack, delivery_tag=delivery_tag)
            )
        except Exception:
            logger.debug("v3 ACK scheduling failed", exc_info=True)

    @staticmethod
    def _nack(connection: Any, channel: Any, delivery_tag: int) -> None:
        try:
            connection.add_callback_threadsafe(
                partial(channel.basic_nack, delivery_tag=delivery_tag, requeue=False)
            )
        except Exception:
            logger.debug("v3 DLQ disposition scheduling failed", exc_info=True)

    def _request_pause(self, connection: Any, channel: Any) -> None:
        self._pause.set()
        try:
            connection.add_callback_threadsafe(channel.stop_consuming)
        except Exception:
            logger.debug("v3 consumer pause scheduling failed", exc_info=True)
