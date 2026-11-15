"""Standalone fixed v4 Worker with a conservative local recovery gate."""

from __future__ import annotations

import argparse
import logging
import math
import os
import signal
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn
from uuid import UUID, uuid4

from dlr.common.worker_protocol_v4 import INSTANCE_HEARTBEAT_SECONDS, validate_control_url
from dlr.worker import (
    broker_config_v4,
    cgroup_namespace,
    journal_anchor_v4,
    lease_session_v4,
    runtime_credential_v4,
    startup_v4_gate,
)

logger = logging.getLogger("dlr.worker.v4")


@dataclass(frozen=True)
class WorkerConfig:
    base_url: str
    worker_id: int
    worker_identity_id: UUID
    credential_directory: Path
    proof_trust_file: Path
    runtime_root: Path
    journal_anchor: Path
    cleanup_journal_root: Path
    attempt_journal_root: Path
    heartbeat_seconds: float = float(INSTANCE_HEARTBEAT_SECONDS)
    request_timeout_seconds: float = 5.0
    broker_config_file: Path | None = None
    claim_child_address_space_bytes: int = 256 * 1024 * 1024
    claim_wire_budget_bytes: int = 16 * 1024 * 1024
    result_wire_budget_bytes: int = 16 * 1024 * 1024
    receive_memory_bytes: int = 384 * 1024 * 1024
    recovery_operation: UUID | None = None


def run(config: WorkerConfig, stop: threading.Event) -> str:
    """Maintain one identity and, when configured, consume its fixed queue."""
    if (
        not sys.platform.startswith("linux")
        or type(config) is not WorkerConfig
        or not isinstance(stop, threading.Event)
        or type(config.worker_id) is not int
        or not 0 < config.worker_id < 2**63
        or type(config.worker_identity_id) is not UUID
        or config.worker_identity_id.int == 0
        or not all(
            isinstance(path, Path) and path.is_absolute()
            for path in (
                config.credential_directory,
                config.proof_trust_file,
                config.runtime_root,
                config.journal_anchor,
                config.cleanup_journal_root,
                config.attempt_journal_root,
            )
        )
        or isinstance(config.heartbeat_seconds, bool)
        or not isinstance(config.heartbeat_seconds, (int, float))
        or not math.isfinite(config.heartbeat_seconds)
        or not 0 < config.heartbeat_seconds <= INSTANCE_HEARTBEAT_SECONDS
        or isinstance(config.request_timeout_seconds, bool)
        or not isinstance(config.request_timeout_seconds, (int, float))
        or not math.isfinite(config.request_timeout_seconds)
        or not 0 < config.request_timeout_seconds <= 5
        or (
            config.broker_config_file is not None
            and (
                not isinstance(config.broker_config_file, Path)
                or not config.broker_config_file.is_absolute()
            )
        )
        or type(config.claim_child_address_space_bytes) is not int
        or not 128 * 1024 * 1024 <= config.claim_child_address_space_bytes < 2**63
        or type(config.claim_wire_budget_bytes) is not int
        or not 4096 <= config.claim_wire_budget_bytes <= config.claim_child_address_space_bytes
        or type(config.result_wire_budget_bytes) is not int
        or not 4096 <= config.result_wire_budget_bytes <= config.claim_child_address_space_bytes
        or type(config.receive_memory_bytes) is not int
        or not config.claim_child_address_space_bytes + 64 * 1024 * 1024
        <= config.receive_memory_bytes
        < 2**63
        or (
            config.recovery_operation is not None
            and (
                type(config.recovery_operation) is not UUID
                or config.recovery_operation.int == 0
                or config.broker_config_file is None
            )
        )
    ):
        return "configuration_invalid"
    phase = "configuration"
    recovery_lease = None
    try:
        validate_control_url(config.base_url)
        sandbox_config = None
        if config.broker_config_file is not None:
            # The cgroup bootstrap must run as PID 1 before starting threads.
            from dlr.worker import sandbox

            phase = "namespace"
            sandbox_config = cgroup_namespace.prepare(sandbox.SandboxConfig.from_environment())
            # cgroup bootstrap proves this is the container-owned PID 1.
            # Docker volume mounts can still be slave-propagated; make this
            # process's mount namespace private before creating any workspace.
            sandbox._mount(None, "/", None, sandbox.MS_REC | sandbox.MS_PRIVATE, None)
        phase = "journal_roots"
        with (
            journal_anchor_v4.prepare_roots(
                config.journal_anchor,
                runtime_root=config.runtime_root,
                journal_roots=(config.cleanup_journal_root, config.attempt_journal_root),
            ) as anchor,
            cgroup_namespace.lock_roots_v4(
                config.runtime_root,
                [config.journal_anchor, config.cleanup_journal_root, config.attempt_journal_root],
            ) as root_lock,
        ):
            anchor.bind(root_lock)

            def verify_roots() -> None:
                with root_lock.borrow(
                    config.runtime_root,
                    [
                        config.journal_anchor,
                        config.cleanup_journal_root,
                        config.attempt_journal_root,
                    ],
                ):
                    anchor.verify()

            with root_lock.borrow(
                config.runtime_root,
                [config.journal_anchor, config.cleanup_journal_root, config.attempt_journal_root],
            ):
                anchor.verify()
                for entry in root_lock._entries:
                    os.fsync(entry.lock_fd)
                    os.fsync(entry.directory_fd)
                if not anchor.only_journal_roots(
                    (config.cleanup_journal_root, config.attempt_journal_root)
                ):
                    return "recovery_required"
            observation = startup_v4_gate.inspect_local_startup(
                runtime_root=config.runtime_root,
                cleanup_journal_root=config.cleanup_journal_root,
                attempt_journal_root=config.attempt_journal_root,
                root_lock=root_lock,
            )
            if config.recovery_operation is not None and observation.status == "local_empty":
                # An empty/replacement volume cannot establish that the named
                # recovery operation retired its original frozen records.
                return "recovery_required"
            if observation.status != "local_empty":
                if config.recovery_operation is None or sandbox_config is None:
                    return "recovery_required"
                from dlr.worker import recovery_checkpoint_v4, recovery_execution_v4, sandbox

                phase = "recovery_scan"
                if sandbox_config.namespace_identity is None:
                    return "recovery_required"
                state = recovery_execution_v4.scan(
                    lock=root_lock,
                    runtime=config.runtime_root,
                    attempts=config.attempt_journal_root,
                    worker_id=config.worker_id,
                    identity_id=config.worker_identity_id,
                    boot_id=sandbox_config.namespace_identity.boot_id,
                )
                if not recovery_checkpoint_v4.fresh_process(
                    state.prepared["process"], recovery_checkpoint_v4.process_identity()
                ):
                    return "recovery_required"
                # Management stays inside this newly verified hard envelope;
                # both old frozen charges remain in state. No new ResourceBudget
                # exists until retirement and an empty-root check complete.
                sandbox.read_verified_resource_envelope(sandbox_config)
                credential = runtime_credential_v4.load_runtime_credential(
                    config.credential_directory,
                    worker_id=config.worker_id,
                    worker_identity_id=config.worker_identity_id,
                )
                recovery_lease = lease_session_v4.WorkerLeaseSession.from_trusted_file(
                    trust_file=config.proof_trust_file,
                    base_url=config.base_url,
                    worker_id=config.worker_id,
                    worker_identity_id=config.worker_identity_id,
                    credential=credential.secret,
                    credential_record_id=credential.credential_record_id,
                    instance_id=uuid4(),
                    timeout_seconds=config.request_timeout_seconds,
                )
                phase = "recovery_operation"
                lookup = recovery_execution_v4.read_operation(
                    recovery_lease, state, config.recovery_operation
                )
                phase = "recovery_registration"
                recovery_lease.register(stop=stop)
                phase = "recovery_execution"
                recovery_execution_v4.recover(
                    lease=recovery_lease,
                    state=state,
                    lookup=lookup,
                    lock=root_lock,
                    runtime=config.runtime_root,
                    attempts=config.attempt_journal_root,
                    stop=stop,
                )
                verify_roots()
                if (
                    startup_v4_gate.inspect_local_startup(
                        runtime_root=config.runtime_root,
                        cleanup_journal_root=config.cleanup_journal_root,
                        attempt_journal_root=config.attempt_journal_root,
                        root_lock=root_lock,
                    ).status
                    != "local_empty"
                ):
                    return "recovery_required"
            if stop.is_set():
                return "stopped"
            flags = None
            budget = receive_limits = None
            if sandbox_config is not None:
                from dlr.control.schemas.worker import REQUIRED_ISOLATION_CAPABILITIES
                from dlr.worker import workspace, workspace_fs_v4

                phase = "broker_configuration"
                if config.broker_config_file is None:
                    raise ValueError("Broker configuration unavailable")
                broker = broker_config_v4.BrokerConfigV4.from_private_file(
                    config.broker_config_file,
                    worker_id=config.worker_id,
                    worker_identity_id=config.worker_identity_id,
                )
                broker.connection_parameters()
                workspace.ensure_private_directory(config.runtime_root / "workspaces")
                if not workspace_fs_v4._mounts_safe(
                    config.runtime_root, config.runtime_root / "workspaces" / "preflight"
                ):
                    return "workspace_mount_unavailable"
                recovery_root = config.cleanup_journal_root / "sandbox-recovery"
                workspace.ensure_private_directory(recovery_root)
                envelope = sandbox.read_verified_resource_envelope(sandbox_config)
                budget = sandbox.ResourceBudget.from_verified_envelope(sandbox_config, 1, envelope)
                receive_limits = sandbox.ResourceLimits(
                    cpu_cores=0.25,
                    memory_bytes=config.receive_memory_bytes,
                    pids=32,
                    tmp_bytes=16 * 1024 * 1024,
                    nofile=256,
                    execution_timeout_seconds=30,
                )
                maximum = sandbox.ResourceLimits(
                    cpu_cores=sandbox_config.cpu_cores,
                    memory_bytes=sandbox_config.memory_bytes,
                    pids=sandbox_config.pids,
                    tmp_bytes=sandbox_config.tmp_bytes,
                    nofile=sandbox_config.nofile,
                    execution_timeout_seconds=sandbox_config.execution_timeout_seconds,
                )
                receive = budget.try_reserve(receive_limits)
                task = budget.try_reserve(maximum) if receive is not None else None
                if receive is None or task is None:
                    return "capacity_unavailable"
                budget.release(task)
                budget.release(receive)
                phase = "sandbox_preflight"
                result = sandbox.run_preflight(
                    sandbox_config, recovery_root=recovery_root, runtime_root=config.runtime_root
                )
                capabilities, details = result.get("capabilities"), result.get("details")
                if not isinstance(capabilities, dict) or not isinstance(details, dict):
                    return "preflight_unavailable"
                flags = {
                    key: (
                        True
                        if key == "resource_envelope_verified"
                        else capabilities.get(key) is True
                    )
                    for key in REQUIRED_ISOLATION_CAPABILITIES
                }
                if details.get("status") != "passed" or not all(flags.values()):
                    return "preflight_unavailable"
                if (
                    startup_v4_gate.inspect_local_startup(
                        runtime_root=config.runtime_root,
                        cleanup_journal_root=config.cleanup_journal_root,
                        attempt_journal_root=config.attempt_journal_root,
                        root_lock=root_lock,
                    ).status
                    != "local_empty"
                ):
                    return "recovery_required"
            phase = "credential"
            if recovery_lease is not None:
                lease = recovery_lease
                lease.heartbeat(stop=stop)
            else:
                credential = runtime_credential_v4.load_runtime_credential(
                    config.credential_directory,
                    worker_id=config.worker_id,
                    worker_identity_id=config.worker_identity_id,
                )
                lease = lease_session_v4.WorkerLeaseSession.from_trusted_file(
                    trust_file=config.proof_trust_file,
                    base_url=config.base_url,
                    worker_id=config.worker_id,
                    worker_identity_id=config.worker_identity_id,
                    credential=credential.secret,
                    credential_record_id=credential.credential_record_id,
                    instance_id=uuid4(),
                    timeout_seconds=config.request_timeout_seconds,
                )
            consumer = None
            consumer_thread = None
            try:
                phase = "registration"
                verify_roots()
                # A retry uses the same process identity and original local
                # deadline. An uncertain registration is never replaced.
                while not stop.is_set():
                    try:
                        if recovery_lease is None:
                            lease.register(stop=stop)
                        break
                    except lease_session_v4.WorkerLeaseError:
                        if lease._state == "dead":
                            return "registration_unconfirmed"
                        if stop.wait(1):
                            return "registration_unconfirmed"
                else:
                    return "stopped"
                verify_roots()
                if sandbox_config is not None:
                    from dlr.worker.consumer_v4 import FixedWorkerConsumer

                    assert flags is not None and budget is not None and receive_limits is not None
                    assert config.broker_config_file is not None
                    phase = "readiness"
                    lease.report_ready(flags)
                    consumer = FixedWorkerConsumer(
                        broker_file=config.broker_config_file,
                        lease=lease,
                        root_lock=root_lock,
                        runtime_root=config.runtime_root,
                        attempt_journal_root=config.attempt_journal_root,
                        sandbox_config=sandbox_config,
                        budget=budget,
                        receive_limits=receive_limits,
                        child_address_space_bytes=config.claim_child_address_space_bytes,
                        wire_budget=config.claim_wire_budget_bytes,
                        result_wire_budget=config.result_wire_budget_bytes,
                        stop=stop,
                    )
                    consumer_thread = threading.Thread(
                        target=consumer.run, name="dlr-v4-broker", daemon=True
                    )
                    consumer_thread.start()
                    phase = "consumer"
                next_heartbeat = time.monotonic() + config.heartbeat_seconds
                while not stop.wait(max(0.0, next_heartbeat - time.monotonic())):
                    started = time.monotonic()
                    phase = "heartbeat_roots"
                    verify_roots()
                    phase = "heartbeat_credential"
                    current = runtime_credential_v4.load_runtime_credential(
                        config.credential_directory,
                        worker_id=config.worker_id,
                        worker_identity_id=config.worker_identity_id,
                    )
                    lease.replace_runtime_credential(current)
                    phase = "heartbeat_exchange"
                    lease.heartbeat(stop=stop)
                    phase = "heartbeat_roots"
                    verify_roots()
                    next_heartbeat = started + config.heartbeat_seconds
                verify_roots()
                if consumer_thread is not None:
                    consumer_thread.join(timeout=16.0)
                    if consumer_thread.is_alive() or (
                        consumer is not None and consumer.failed.is_set()
                    ):
                        return "execution_unconfirmed"
                phase = "retirement"
                lease.retire()
                return "stopped"
            finally:
                if consumer_thread is not None and consumer_thread.is_alive():
                    stop.set()
                    consumer_thread.join(timeout=6.0)
                lease.close()
    except broker_config_v4.BrokerConfigurationError:
        return "broker_configuration_invalid"
    except Exception as error:  # noqa: BLE001 - no exception text, paths or secrets in logs
        logger.warning("fixed Worker unavailable: phase=%s error=%s", phase, type(error).__name__)
        return "worker_unavailable"
    finally:
        if recovery_lease is not None:
            recovery_lease.close()


class _InvalidArguments(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        raise _InvalidArguments


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description="Run a fixed v4 Worker")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--worker-id", type=int, required=True)
    parser.add_argument("--worker-identity-id", type=UUID, required=True)
    parser.add_argument("--credential-directory", type=Path, required=True)
    parser.add_argument("--proof-trust-file", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--journal-anchor", type=Path, required=True)
    parser.add_argument("--cleanup-journal-root", type=Path, required=True)
    parser.add_argument("--attempt-journal-root", type=Path, required=True)
    parser.add_argument(
        "--heartbeat-seconds", type=float, default=float(INSTANCE_HEARTBEAT_SECONDS)
    )
    parser.add_argument("--request-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--broker-config-file", type=Path)
    parser.add_argument("--claim-child-address-space-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--claim-wire-budget-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--result-wire-budget-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--receive-memory-bytes", type=int, default=384 * 1024 * 1024)
    parser.add_argument(
        "--recovery-operation",
        type=UUID,
        default=os.environ.get("DLR_WORKER_RECOVERY_OPERATION") or None,
    )
    try:
        args = parser.parse_args(argv)
        config = WorkerConfig(**vars(args))
    except Exception:  # noqa: BLE001 - invalid arguments may contain a secret
        print("v4 Worker configuration invalid", file=sys.stderr)
        return 2
    stop = threading.Event()

    def request_stop(_signal: int, _frame: object) -> None:
        stop.set()

    previous = {
        number: signal.signal(number, request_stop) for number in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        result = run(config, stop)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    print(f"v4 Worker: {result}")
    return 0 if result == "stopped" else 1


if __name__ == "__main__":
    raise SystemExit(main())
