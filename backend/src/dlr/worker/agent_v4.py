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
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn
from uuid import UUID, uuid4

from dlr.common.worker_languages_v4 import (
    COMPILED_LANGUAGES,
    mode_capabilities,
    valid_language_mode,
)
from dlr.common.worker_protocol_v4 import INSTANCE_HEARTBEAT_SECONDS, validate_control_url
from dlr.worker import (
    broker_config_v4,
    cgroup_namespace,
    journal_anchor_v4,
    lease_session_v4,
    runtime_credential_v4,
    startup_v4_gate,
)
from dlr.worker.cache import CacheError
from dlr.worker.client import ControlUnavailableError

if TYPE_CHECKING:
    from dlr.worker.cache_maintenance_v4 import CacheMaintenance

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
    language_mode: str = "python"
    business_credentials_mode: str = "disabled"
    input_mode: str = "disabled"
    builtin_wheel_mode: str = "disabled"
    cache_root: Path | None = None


def run(config: WorkerConfig, stop: threading.Event) -> str:
    """Maintain one identity and, when configured, consume its fixed queue."""
    if (
        not sys.platform.startswith("linux")
        or type(config) is not WorkerConfig
        or not isinstance(stop, threading.Event)
        or type(config.worker_id) is not int
        or not valid_language_mode(config.language_mode)
        or type(config.business_credentials_mode) is not str
        or config.business_credentials_mode not in {"disabled", "bindings"}
        or type(config.input_mode) is not str
        or config.input_mode not in {"disabled", "managed_files"}
        or type(config.builtin_wheel_mode) is not str
        or config.builtin_wheel_mode not in {"disabled", "pure_python"}
        or (config.builtin_wheel_mode == "pure_python" and config.input_mode != "managed_files")
        or (
            config.cache_root is not None
            and (not isinstance(config.cache_root, Path) or not config.cache_root.is_absolute())
        )
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
                    allow_files=config.input_mode == "managed_files",
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
                    sandbox_config,
                    recovery_root=recovery_root,
                    runtime_root=config.runtime_root,
                    managed_files=config.input_mode == "managed_files",
                    **(
                        {"builtin_wheel": True}
                        if config.builtin_wheel_mode == "pure_python"
                        else {}
                    ),
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
                    config.input_mode == "managed_files"
                    and capabilities.get("managed_files_v1") is not True
                ):
                    return "preflight_unavailable"
                if (
                    config.builtin_wheel_mode == "pure_python"
                    and capabilities.get("builtin_wheel_v1") is not True
                ):
                    return "preflight_unavailable"
                languages = mode_capabilities(config.language_mode)
                if "javascript" in languages:
                    from dlr.worker import node_runtime_v4

                    if not node_runtime_v4.run_preflight(
                        sandbox_config,
                        recovery_root=recovery_root,
                        runtime_root=config.runtime_root,
                    ):
                        return "preflight_unavailable"
                for language in languages:
                    if language not in COMPILED_LANGUAGES:
                        continue
                    from dlr.worker import compiled_runtime_v4

                    if not compiled_runtime_v4.run_preflight(
                        language,
                        sandbox_config,
                        recovery_root=recovery_root,
                        runtime_root=config.runtime_root,
                    ):
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
            cache = None
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
                    from dlr.worker.cache_maintenance_v4 import CacheMaintenance
                    from dlr.worker.consumer_v4 import FixedWorkerConsumer

                    assert flags is not None and budget is not None and receive_limits is not None
                    assert config.broker_config_file is not None
                    if config.cache_root is not None:
                        phase = "cache_owner"
                        protected = (
                            config.runtime_root,
                            config.journal_anchor,
                            config.credential_directory,
                        )
                        if any(
                            path == config.cache_root
                            or path in config.cache_root.parents
                            or config.cache_root in path.parents
                            for path in protected
                        ):
                            raise ValueError("cache volume unavailable")
                        cache = CacheMaintenance(config.cache_root, lease, stop)
                    phase = "readiness"
                    lease.report_ready(
                        flags,
                        capabilities=mode_capabilities(config.language_mode),
                        business_credentials=config.business_credentials_mode == "bindings",
                        managed_files=config.input_mode == "managed_files",
                        builtin_wheel=config.builtin_wheel_mode == "pure_python",
                        adapter_cleanup=cache is None,
                        cache_governance=cache is not None,
                    )
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
                        capabilities=mode_capabilities(config.language_mode),
                        business_credentials=config.business_credentials_mode == "bindings",
                        managed_files=config.input_mode == "managed_files",
                        builtin_wheel=config.builtin_wheel_mode == "pure_python",
                    )
                    consumer_thread = threading.Thread(
                        target=consumer.run, name="dlr-v4-broker", daemon=True
                    )
                    consumer_thread.start()
                    phase = "consumer"
                next_heartbeat = time.monotonic() + config.heartbeat_seconds
                next_cleanup = next_heartbeat
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
                    if consumer is not None and started >= next_cleanup and not stop.is_set():
                        next_cleanup = started + 15.0
                        if cache is not None:
                            phase = "cache_maintenance"
                            with _maintenance_observation(phase):
                                cache.poll()
                        if consumer.idle.is_set():
                            phase = "adapter_cleanup"
                            with _maintenance_observation(phase):
                                _settle_adapter_cleanup(config, root_lock, lease, stop, cache)
                            phase = "history_retirement"
                            with _maintenance_observation(phase):
                                _retire_local_history(config, root_lock, lease, stop)
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
                if cache is not None:
                    cache.close()
    except broker_config_v4.BrokerConfigurationError:
        return "broker_configuration_invalid"
    except Exception as error:  # noqa: BLE001 - no exception text, paths or secrets in logs
        logger.warning("fixed Worker unavailable: phase=%s error=%s", phase, type(error).__name__)
        return "worker_unavailable"
    finally:
        if recovery_lease is not None:
            recovery_lease.close()


@contextmanager
def _maintenance_observation(phase: str) -> Iterator[None]:
    try:
        yield
    except (lease_session_v4.WorkerLeaseError, CacheError, ControlUnavailableError):
        # Keep the unresolved responsibility charged while the other
        # independent maintenance paths retain their own authority checks.
        logger.warning("fixed Worker maintenance observation unavailable: phase=%s", phase)


def _local_storage_empty(config: WorkerConfig, lock: cgroup_namespace.RootLockHandle) -> bool:
    return (
        startup_v4_gate.inspect_local_startup(
            runtime_root=config.runtime_root,
            cleanup_journal_root=config.cleanup_journal_root,
            attempt_journal_root=config.attempt_journal_root,
            root_lock=lock,
        ).status
        == "local_empty"
    )


def _retire_local_history(
    config: WorkerConfig,
    lock: cgroup_namespace.RootLockHandle,
    lease: lease_session_v4.WorkerLeaseSession,
    stop: threading.Event,
) -> None:
    # Capture already committed receipts first. An unrelated later Claim
    # cannot create new journal records for any of these terminal Attempts.
    route = "history-retirement/"
    result = lease._body(lease._data_exchange(route + "observe", {}, stop), {"items"})
    items = result["items"]
    if not isinstance(items, list) or len(items) > 8:
        raise lease_session_v4.WorkerLeaseError("history retirement unavailable")
    if items and _local_storage_empty(config, lock):
        observed = lease._body(lease._data_exchange(route + "confirm", result, stop), {"items"})
        if observed != result:
            raise lease_session_v4.WorkerLeaseError("history retirement observation unavailable")


def _settle_adapter_cleanup(
    config: WorkerConfig,
    lock: cgroup_namespace.RootLockHandle,
    lease: lease_session_v4.WorkerLeaseSession,
    stop: threading.Event,
    cache: CacheMaintenance | None = None,
) -> None:
    # A normal run may be holding files or Attempt records. Only settle from
    # a bounded, independently verified quiet state of the original roots.
    if not _local_storage_empty(config, lock):
        return
    # Control has already fenced the deleted Adapter. A different Adapter may
    # prepare concurrently, so never hold the root mutex over network IO.
    offered = lease._body(lease._data_exchange("adapter-cleanup/claim", {}, stop), {"claim"})
    claim = offered["claim"]
    if claim is None:
        return
    if (
        not isinstance(claim, dict)
        or set(claim) != {"cleanup_id", "adapter_id", "claim_attempt"}
        or any(type(value) is not int or not 0 < value < 2**63 for value in claim.values())
    ):
        raise lease_session_v4.WorkerLeaseError("adapter cleanup observation unavailable")
    if cache is not None and not cache.cleanup_adapter(claim):
        return
    response = lease._body(
        lease._data_exchange("adapter-cleanup/complete", claim, stop), {"completed"}
    )
    if response["completed"] != claim:
        raise lease_session_v4.WorkerLeaseError("adapter cleanup observation unavailable")


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
    parser.add_argument(
        "--language-mode", default=os.environ.get("DLR_WORKER_LANGUAGE_MODE", "python")
    )
    parser.add_argument("--input-mode", default=os.environ.get("DLR_WORKER_INPUT_MODE", "disabled"))
    parser.add_argument(
        "--builtin-wheel-mode",
        default=os.environ.get("DLR_WORKER_BUILTIN_WHEEL_MODE", "disabled"),
    )
    parser.add_argument(
        "--cache-root", type=Path, default=os.environ.get("DLR_WORKER_CACHE_ROOT") or None
    )
    parser.add_argument("--claim-child-address-space-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--claim-wire-budget-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--result-wire-budget-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument(
        "--business-credentials-mode",
        default=os.environ.get("DLR_WORKER_BUSINESS_CREDENTIALS_MODE", "disabled"),
    )
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
