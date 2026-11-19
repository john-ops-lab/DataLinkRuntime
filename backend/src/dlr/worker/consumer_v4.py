"""Single-slot fixed-Worker RabbitMQ consumer with explicit settlement."""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Literal

import pika

from dlr.common.dispatch_protocol import deserialize_dispatch_message
from dlr.common.worker_languages_v4 import valid_capabilities
from dlr.common.worker_protocol_v4 import WORKER_FORCE_STOP_CODE
from dlr.worker import (
    broker_config_v4,
    cgroup_namespace,
    claim_preparation_v4,
    cleanup_execution_v4,
    execution_supervision_v4,
    recovery_checkpoint_v4,
    result_exchange_v4,
    sandbox,
)
from dlr.worker.lease_session_v4 import WorkerLeaseSession

_Outcome = Literal["ack", "reject", "retry", "unknown"]
_MAX_DISPATCH_BYTES = 16_384
logger = logging.getLogger("dlr.worker.v4.consumer")
_WAIT_REASONS = frozenset(
    {
        "worker_not_ready",
        "worker_recovery_unverified",
        "worker_task_capability_unavailable",
        "claim_response_budget_exceeded",
        "claim_snapshot_changed",
        "execution_not_queued",
        "cancel_requested",
        "dispatch_generation_not_active",
        "claim_configuration_invalid",
        "claim_responsibility_unresolved",
        "claim_receive_capacity_unavailable",
        "claim_output_capacity_unavailable",
        "claim_task_capacity_unavailable",
        "claim_preparation_unavailable",
    }
)


def _unknown(stage: str) -> _Outcome:
    logger.warning("fixed Worker delivery is unsettled: stage=%s", stage)
    return "unknown"


class FixedWorkerConsumer:
    """Hold one delivery until its exact result and cleanup are settled."""

    def __init__(
        self,
        *,
        broker_file: Path,
        lease: WorkerLeaseSession,
        root_lock: cgroup_namespace.RootLockHandle,
        runtime_root: Path,
        attempt_journal_root: Path,
        sandbox_config: sandbox.SandboxConfig,
        budget: sandbox.ResourceBudget,
        receive_limits: sandbox.ResourceLimits,
        child_address_space_bytes: int,
        wire_budget: int,
        result_wire_budget: int,
        stop: threading.Event,
        capabilities: list[str] | None = None,
        business_credentials: bool = False,
        managed_files: bool = False,
        builtin_wheel: bool = False,
        check_capacity: bool = False,
    ) -> None:
        self._broker_file, self._lease = broker_file, lease
        self._root_lock, self._runtime_root = root_lock, runtime_root
        self._journal_root, self._config, self._budget = (
            attempt_journal_root,
            sandbox_config,
            budget,
        )
        self._receive_limits = receive_limits
        self._child_address_space_bytes = child_address_space_bytes
        self._wire_budget, self._result_wire_budget = wire_budget, result_wire_budget
        self._stop = stop
        languages = ["python"] if capabilities is None else capabilities
        if not valid_capabilities(languages):
            raise ValueError("Worker capabilities unavailable")
        self._languages = tuple(languages)
        self.failed = threading.Event()
        self.idle = threading.Event()
        self._preparer = claim_preparation_v4.ClaimPreparer(
            business_credentials=business_credentials,
            managed_files=managed_files,
            builtin_wheel=builtin_wheel,
            check_capacity=check_capacity,
        )

    def _execute(self, body: bytes) -> _Outcome:
        stage = "dispatch"
        try:
            if type(body) is not bytes or not 0 < len(body) <= _MAX_DISPATCH_BYTES:
                return "reject"
            try:
                message = deserialize_dispatch_message(body)
                if message.target_worker_id != self._lease._worker_id:
                    return "reject"
                if message.language not in self._languages:
                    return "retry"
            except Exception:
                return "reject"
            stage = "claim"
            result = self._preparer.prepare(
                lease=self._lease,
                dispatch=body,
                root_lock=self._root_lock,
                runtime_root=self._runtime_root,
                attempt_journal_root=self._journal_root,
                sandbox_config=self._config,
                budget=self._budget,
                receive_limits=self._receive_limits,
                child_address_space_bytes=self._child_address_space_bytes,
                wire_budget=self._wire_budget,
                result_wire_budget=self._result_wire_budget,
                timeout_seconds=5.0,
                stop=self._stop,
            )
            if result.state == "decision":
                if result.decision == "ACK_NOOP":
                    return "ack"
                if result.decision == "REJECT_DLQ":
                    return "reject"
                if result.decision in {"DEFER", "PAUSE_CONSUMER"}:
                    logger.warning(
                        "fixed Worker Claim deferred: reason=%s",
                        result.reason if result.reason in _WAIT_REASONS else "claim_deferred",
                    )
                    return "retry"
                return _unknown(stage)
            if result.state == "blocked":
                logger.warning(
                    "fixed Worker Claim blocked: reason=%s",
                    result.reason
                    if result.reason in _WAIT_REASONS
                    else "claim_preparation_unavailable",
                )
                if self._preparer._poisoned or self._preparer._active is not None:
                    return _unknown(stage)
                return "retry"
            if result.state != "claimed" or result.claimed is None:
                return _unknown(stage)
            stage = "staging"
            staged = self._preparer.stage_execution(
                result.claimed,
                root_lock=self._root_lock,
                runtime_root=self._runtime_root,
                attempt_journal_root=self._journal_root,
                sandbox_config=self._config,
                budget=self._budget,
                stop=self._stop,
            )
            if staged is None:
                return _unknown(stage)
            stage = "supervision"
            owner = execution_supervision_v4.start_execution(staged, stop=self._stop)
            if owner is None:
                return _unknown(stage)
            while not self._stop.is_set():
                observed = owner.observe(timeout=1.0)
                if observed.state == "pending":
                    continue
                if observed.state != "observed":
                    return _unknown(stage)
                if observed.stop_reason == WORKER_FORCE_STOP_CODE:
                    stage = "stopped_checkpoint"
                    if not owner.stop_watchdog():
                        return _unknown(stage)
                    recovery_checkpoint_v4.persist_force_stopped(owner)
                stage = "result"
                snapshot = owner.build_result()
                if snapshot is None or not result_exchange_v4.submit_result(
                    owner,
                    snapshot,
                    root_lock=self._root_lock,
                    runtime_root=self._runtime_root,
                    attempt_journal_root=self._journal_root,
                ):
                    return _unknown(stage)
                stage = "cleanup"
                if not cleanup_execution_v4.settle_execution(self._preparer, owner, snapshot):
                    return _unknown(stage)
                return "ack"
            return _unknown(stage)
        except BaseException as error:
            # No unobserved outcome may become an ACK or a new capacity slot.
            logger.warning(
                "fixed Worker delivery failed: stage=%s error=%s", stage, type(error).__name__
            )
            return "unknown"

    def run(self) -> None:
        """Keep AMQP heartbeats separate from the bounded execution thread."""
        backoff = 1.0
        while not self._stop.is_set():
            connection = None
            active: threading.Thread | None = None
            outcomes: queue.SimpleQueue[_Outcome] = queue.SimpleQueue()
            delivery_tag: int | None = None
            try:
                self.idle.clear()
                config = broker_config_v4.BrokerConfigV4.from_private_file(
                    self._broker_file,
                    worker_id=self._lease._worker_id,
                    worker_identity_id=self._lease._identity_id,
                )
                connection = pika.BlockingConnection(config.connection_parameters())
                channel = connection.channel()
                channel.basic_qos(prefetch_count=1)

                def deliver(
                    _channel: object,
                    method: object,
                    _properties: object,
                    body: bytes,
                    result_queue: queue.SimpleQueue[_Outcome] = outcomes,
                ) -> None:
                    nonlocal active, delivery_tag
                    self.idle.clear()
                    tag = getattr(method, "delivery_tag", None)
                    if active is not None or type(tag) is not int or tag <= 0:
                        self.failed.set()
                        self._stop.set()
                        return
                    delivery_tag = tag

                    def work() -> None:
                        result_queue.put(self._execute(body))

                    active = threading.Thread(target=work, name="dlr-v4-attempt", daemon=True)
                    active.start()

                channel.basic_consume(
                    queue=config.queue, on_message_callback=deliver, auto_ack=False
                )
                while not self._stop.is_set():
                    connection.process_data_events(time_limit=0.25)
                    try:
                        outcome = outcomes.get_nowait()
                    except queue.Empty:
                        if active is None:
                            self.idle.set()
                        continue
                    if active is None or delivery_tag is None:
                        raise ValueError("delivery state unavailable")
                    active.join(timeout=1.0)
                    if active.is_alive():
                        raise ValueError("execution thread unavailable")
                    tag = delivery_tag
                    active = None
                    delivery_tag = None
                    if outcome == "ack":
                        channel.basic_ack(delivery_tag=tag)
                        # The synchronous response on this channel follows
                        # processing the preceding ACK at the Broker.
                        channel.basic_qos(prefetch_count=1)
                        backoff = 1.0
                    elif outcome == "reject":
                        channel.basic_nack(delivery_tag=tag, requeue=False)
                    elif outcome == "retry":
                        channel.basic_nack(delivery_tag=tag, requeue=True)
                        break
                    else:
                        self.failed.set()
                        self._stop.set()
                        break
            except Exception as error:
                logger.warning("fixed Worker Broker unavailable: error=%s", type(error).__name__)
                if active is not None:
                    self.failed.set()
                    self._stop.set()
            finally:
                self.idle.clear()
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        if active is not None:
                            self.failed.set()
                            self._stop.set()
                if active is not None:
                    self.failed.set()
                    self._stop.set()
                    active.join(timeout=5.0)
            if not self._stop.is_set():
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
