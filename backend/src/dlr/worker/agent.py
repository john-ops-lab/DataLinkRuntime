"""Outbound-only Worker with a reliable message Consumer and resource isolation.

Outbound-only agent: registers with the Control Node, sends heartbeats,
consumes persistent dispatches and executes each Attempt inside its Sandbox.
The Worker never opens an inbound port.

When the Control Node is unavailable the agent keeps registering /
heartbeating with capped backoff instead of crashing.
"""

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
from collections.abc import Mapping
from math import isfinite
from pathlib import Path
from typing import Any

from dlr.common.platform_logging import configure_platform_logging
from dlr.worker import executor, sandbox
from dlr.worker import venv as venv_manager
from dlr.worker import workspace as workspace_manager
from dlr.worker.client import ClientError, ControlClient, ControlUnavailableError
from dlr.worker.consumer import ConsumerConfig, V3Consumer

logger = logging.getLogger("dlr.worker")

READY_FILE_ENV = "DLR_WORKER_READY_FILE"
DEFAULT_READY_FILE = "/tmp/dlr-worker.ready"

MAX_BACKOFF_SECONDS = 30.0
REPORT_ATTEMPTS = 3
MIN_JAVA_MAJOR_VERSION = 21
PROTOCOL_VERSION = 3
ISOLATION_CAPABILITY_KEYS = (
    "cgroup_v2",
    "cgroup_namespace_private",
    "mount_namespace",
    "pid_namespace",
    "memory_hard_limit",
    "pids_hard_limit",
    "tmpfs_hard_limit",
    "bounded_output",
    "preflight_passed",
    "resource_envelope_verified",
    "cpu_hard_limit",
    "swap_hard_limit",
    "nofile_hard_limit",
    "no_new_privileges",
    "cgroup_kill",
    "adapter_control_plane_hidden",
    "adapter_mount_blocked",
    "sandbox_cleanup",
)


def _runtime_major_version(command: str) -> int | None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed Runtime version probe
            [command, "-version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    output = f"{completed.stdout}\n{completed.stderr}"
    prefix = "javac" if command == "javac" else "version"
    match = re.search(rf"\b{prefix}\s+\"?(\d+)", output)
    return int(match.group(1)) if match is not None else None


def _supports_java_runtime() -> bool:
    if any(shutil.which(command) is None for command in ("java", "javac", "mvn")):
        return False
    return all(
        (_runtime_major_version(command) or 0) >= MIN_JAVA_MAJOR_VERSION
        for command in ("java", "javac")
    )


class WorkerConfig:
    """Worker settings read from environment variables (spec §8)."""

    def __init__(self) -> None:
        self.control_url = os.environ.get("DLR_CONTROL_URL", "http://control:8000")
        self.token = os.environ.get("DLR_WORKER_TOKEN", "")
        self.name = os.environ.get("DLR_WORKER_NAME", "worker-1")
        configured_protocol = os.environ.get("DLR_WORKER_PROTOCOL_VERSION")
        if configured_protocol is not None and configured_protocol != str(PROTOCOL_VERSION):
            raise ValueError("Worker protocol is fixed; remove DLR_WORKER_PROTOCOL_VERSION")
        self.protocol_version = PROTOCOL_VERSION
        self.heartbeat_seconds = float(os.environ.get("DLR_WORKER_HEARTBEAT_SECONDS", "10"))
        self.runtime_root = Path(os.environ.get("DLR_RUNTIME_ROOT", "/var/lib/dlr/runtime"))
        self.workspace_cleanup_journal_root = Path(
            os.environ.get(
                "DLR_WORKSPACE_CLEANUP_JOURNAL_ROOT",
                str(self.runtime_root / "cleanup-journal"),
            )
        )
        try:
            cleanup_interval = float(os.environ.get("DLR_WORKSPACE_CLEANUP_INTERVAL_SECONDS", "30"))
        except ValueError:
            cleanup_interval = 30.0
        self.workspace_cleanup_interval_seconds = (
            min(cleanup_interval, 86_400.0)
            if isfinite(cleanup_interval) and cleanup_interval > 0
            else 30.0
        )
        self.execution_timeout_seconds = int(os.environ.get("DLR_EXECUTION_TIMEOUT_SECONDS", "300"))
        self.dep_install_timeout_seconds = int(
            os.environ.get("DLR_DEP_INSTALL_TIMEOUT_SECONDS", "300")
        )
        self.pypi_index_url = os.environ.get("DLR_PYPI_INDEX_URL") or None
        self.npm_registry_url = os.environ.get("DLR_NPM_REGISTRY_URL") or None
        self.maven_repository_url = os.environ.get("DLR_MAVEN_REPOSITORY_URL") or None
        self.rabbitmq_url = os.environ.get("DLR_RABBITMQ_URL") or None
        self.execution_slots = max(1, int(os.environ.get("DLR_WORKER_EXECUTION_SLOTS", "2")))
        self.attempt_journal_root = Path(
            os.environ.get("DLR_ATTEMPT_JOURNAL_ROOT", str(self.runtime_root / "attempt-journal"))
        )
        self.sandbox_config = sandbox.SandboxConfig.from_environment()
        self.isolation_capabilities = {key: False for key in ISOLATION_CAPABILITY_KEYS}
        self._verified_resource_envelope: sandbox.ResourceEnvelope | None = None
        self._preflight_completed = False

    def capabilities(self) -> list[str]:
        capabilities: list[str] = []
        if shutil.which("python") and shutil.which("uv"):
            capabilities.append("python")
        if shutil.which("node") and shutil.which("npm"):
            capabilities.append("javascript")
        if _supports_java_runtime():
            capabilities.append("java")
        return capabilities

    def runtime_settings(self) -> executor.RuntimeSettings:
        return executor.RuntimeSettings(
            runtime_root=self.runtime_root,
            execution_timeout_seconds=self.execution_timeout_seconds,
            dep_install_timeout_seconds=self.dep_install_timeout_seconds,
            pypi_index_url=self.pypi_index_url,
            npm_registry_url=self.npm_registry_url,
            maven_repository_url=self.maven_repository_url,
            workspace_cleanup_journal_root=self.workspace_cleanup_journal_root,
            sandbox_config=self.sandbox_config,
            resource_envelope=self._verified_resource_envelope,
        )

    def run_preflight(self) -> None:
        """Run the real isolation probe once before registration is attempted."""
        if self._preflight_completed:
            return
        self._preflight_completed = True
        # Do not retain environment-provided or stale capability claims while
        # the real probe is running.  An absent/malformed receipt is a failed
        # startup gate, never an invitation to register v3 as ready.
        self.isolation_capabilities = {key: False for key in ISOLATION_CAPABILITY_KEYS}
        self._verified_resource_envelope = None
        try:
            # Payload runtimes live below this root, so it must be traversable
            # but not listable or writable by the payload identity.
            workspace_manager.ensure_runtime_root(self.runtime_root)
            for private_root in (
                self.runtime_root / workspace_manager.WORKSPACES_DIRNAME,
                self.workspace_cleanup_journal_root,
                self.workspace_cleanup_journal_root / "sandbox-recovery",
                self.attempt_journal_root,
            ):
                workspace_manager.ensure_private_directory(private_root)
            # The finite deployment envelope is part of v3 eligibility, not a
            # late Consumer construction check.  Validate it before running
            # the disposable probe and before the Agent submits any v3
            # capability matrix to Control.
            envelope = sandbox.read_verified_resource_envelope(self.sandbox_config)
            sandbox.ResourceBudget.from_verified_envelope(
                self.sandbox_config,
                slots=self.execution_slots,
                envelope=envelope,
            )
            logger.info(
                "pre-registration finite resource envelope gate passed; starting sandbox preflight"
            )
        except Exception as error:  # noqa: BLE001 - startup gate must fail closed
            error_code = getattr(error, "code", type(error).__name__)
            logger.warning(
                "resource envelope verification failed (%s); RabbitMQ execution remains disabled",
                error_code,
            )
            return
        try:
            result = sandbox.run_preflight(
                self.sandbox_config,
                recovery_root=self.workspace_cleanup_journal_root / "sandbox-recovery",
                runtime_root=self.runtime_root,
            )
            capabilities = result.get("capabilities")
            details = result.get("details")
            if (
                isinstance(capabilities, dict)
                and isinstance(details, Mapping)
                and details.get("status") == "passed"
            ):
                self.isolation_capabilities = {
                    key: capabilities.get(key) is True for key in ISOLATION_CAPABILITY_KEYS
                }
                self.isolation_capabilities["resource_envelope_verified"] = True
                self._verified_resource_envelope = envelope
            logger.info(
                "sandbox preflight %s; rabbitmq execution gate=%s",
                result.get("details", {}).get("status", "failed"),
                self.isolation_capabilities.get("preflight_passed", False),
            )
        except Exception:  # noqa: BLE001 - startup gate must fail closed
            self.isolation_capabilities = {key: False for key in ISOLATION_CAPABILITY_KEYS}
            logger.warning("sandbox preflight failed; RabbitMQ execution remains disabled")


class Agent:
    def __init__(self, config: WorkerConfig, client: ControlClient) -> None:
        self._config = config
        self._client = client
        self._stop = threading.Event()
        self._registration_info: dict[str, Any] = {}
        self._consumer: V3Consumer | None = None

    def request_stop(self) -> None:
        self._stop.set()
        if self._consumer is not None:
            self._consumer.request_stop()

    # --- lifecycle ------------------------------------------------------------

    def run(self) -> None:
        ready_file = Path(os.environ.get(READY_FILE_ENV, DEFAULT_READY_FILE))
        worker_id = self._register()
        if worker_id is None:  # stop requested before registration succeeded
            return
        self._recover_cleanup_journals(worker_id)
        ready_file.write_text(str(os.getpid()), encoding="utf-8")
        logger.info("worker '%s' registered with id %s", self._config.name, worker_id)

        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, args=(worker_id,), name="dlr-heartbeat", daemon=True
        )
        heartbeat_thread.start()
        cleanup_thread = threading.Thread(
            target=self._cleanup_loop, args=(worker_id,), name="dlr-cleanup", daemon=True
        )
        cleanup_thread.start()
        try:
            if not self._registration_info.get("rabbitmq_execution_v3", False) or not all(
                self._config.isolation_capabilities.get(key, False)
                for key in ISOLATION_CAPABILITY_KEYS
            ):
                logger.warning("Worker isolation preflight failed; execution remains paused")
                self._stop.wait()
            else:
                self._run_consumer(worker_id)
        finally:
            self.request_stop()
            cleanup_thread.join(timeout=min(self._config.workspace_cleanup_interval_seconds, 5))
            ready_file.unlink(missing_ok=True)
            self._graceful_offline(worker_id)
            logger.info("worker agent stopped")

    def _register(self) -> int | None:
        self._config.run_preflight()
        backoff = 1.0
        while not self._stop.is_set():
            try:
                capabilities = self._config.capabilities()
                if not capabilities:
                    raise RuntimeError("no supported Runtime is installed")
                info = self._client.register(
                    self._config.name,
                    capabilities,
                    protocol_version=PROTOCOL_VERSION,
                    isolation_capabilities=self._config.isolation_capabilities,
                )
                self._registration_info = info
                return int(info["id"])
            except ControlUnavailableError as error:
                logger.warning(
                    "control unavailable during register (%s); retrying in %.0fs",
                    error,
                    backoff,
                )
            except ClientError as error:
                logger.error(
                    "registration rejected by control with status %s; retrying in %.0fs",
                    error.status,
                    backoff,
                )
            self._stop.wait(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        return None

    def _run_consumer(self, worker_id: int) -> None:
        from dlr.control.services import rabbitmq

        self._consumer = V3Consumer(
            ConsumerConfig(
                worker_id=worker_id,
                queue=rabbitmq.topology_names(worker_id).queue,
                execution_slots=self._config.execution_slots,
                runtime_root=self._config.runtime_root,
                attempt_journal_root=self._config.attempt_journal_root,
            ),
            self._client,
            connection_factory=rabbitmq.connect,
            runtime_settings=self._config.runtime_settings(),
        )
        self._consumer.run()

    def _heartbeat_loop(self, worker_id: int) -> None:
        while not self._stop.wait(self._config.heartbeat_seconds):
            try:
                self._client.heartbeat(
                    worker_id, isolation_capabilities=self._config.isolation_capabilities
                )
            except ControlUnavailableError:
                logger.debug("heartbeat skipped: control unavailable")
            except ClientError:
                logger.warning("heartbeat rejected by control")

    def _graceful_offline(self, worker_id: int) -> None:
        try:
            self._client.mark_offline(worker_id)
            logger.info("marked worker offline (best-effort)")
        except Exception:  # noqa: BLE001 - graceful shutdown must not raise
            logger.debug("offline notification failed")

    # --- startup recovery and adapter cleanup ----------------------------------

    def _recover_cleanup_journals(self, worker_id: int) -> None:
        """Recover owned Workspace journals without deleting unknown paths."""

        def report_cleanup(execution_id: int, cleanup_token: str) -> bool:
            try:
                self._client.report_cleanup_receipt(
                    worker_id,
                    execution_id,
                    cleanup_token=cleanup_token,
                )
                return True
            except ControlUnavailableError:
                logger.warning(
                    "cleanup receipt deferred for execution %s: control unavailable",
                    execution_id,
                )
            except ClientError as error:
                # Do not log the response body: a malformed peer must not turn
                # an opaque credential or storage fact into a Worker log.
                logger.warning(
                    "cleanup receipt rejected for execution %s with status %s",
                    execution_id,
                    error.status,
                )
            return False

        # Recovery runs before the Consumer starts; no local Attempt is active.
        sandbox_counts = sandbox.recover(
            self._config.sandbox_config,
            self._config.workspace_cleanup_journal_root / "sandbox-recovery",
            runtime_root=self._config.runtime_root,
        )
        counts = workspace_manager.recover_cleanup_journals(
            self._config.workspace_cleanup_journal_root,
            self._config.runtime_root,
            report_cleanup=report_cleanup,
            scan_timeout_seconds=workspace_manager.RECOVERY_SCAN_TIMEOUT_SECONDS,
            retry_backoff_seconds=workspace_manager.RECOVERY_RETRY_BACKOFF_SECONDS,
        )
        if sandbox_counts["inspected"] or sandbox_counts["retained"]:
            logger.info(
                "sandbox recovery scan inspected %s; completed %s, retained %s",
                sandbox_counts["inspected"],
                sandbox_counts["completed"],
                sandbox_counts["retained"],
            )
        if counts["retained"] or counts["deferred"]:
            logger.info(
                "workspace cleanup scan inspected %s; completed %s, deferred %s, retained %s",
                counts["inspected"],
                counts["completed"],
                counts["deferred"],
                counts["retained"],
            )

    def _cleanup_loop(self, worker_id: int) -> None:
        """Poll only filesystem cleanup requests; executions use the Consumer."""
        delay = max(1.0, self._config.workspace_cleanup_interval_seconds)
        while not self._stop.is_set():
            try:
                task = self._client.claim_cleanup(worker_id)
                if task is not None:
                    self._execute_cleanup_task(worker_id, task)
            except ControlUnavailableError:
                logger.debug("adapter cleanup deferred: control unavailable")
            except ClientError as error:
                logger.warning("adapter cleanup rejected: status=%s", error.status)
            self._stop.wait(delay)

    def _execute_cleanup_task(self, worker_id: int, task: dict[str, Any]) -> None:
        cleanup_id = int(task["cleanup_id"])
        adapter_id = int(task["adapter_id"])
        try:
            venv_manager.cleanup_adapter_environment(self._config.runtime_root, adapter_id)
        except Exception:  # noqa: BLE001 - cleanup result is retried by Control
            logger.warning(
                "adapter environment cleanup failed for adapter %s",
                adapter_id,
            )
            self._report_cleanup_with_retry(worker_id, cleanup_id, success=False)
            return
        self._report_cleanup_with_retry(worker_id, cleanup_id, success=True)

    def _report_cleanup_with_retry(self, worker_id: int, cleanup_id: int, *, success: bool) -> None:
        """Bounded transport retries; never send filesystem error text."""
        delay = 2.0
        for attempt in range(1, REPORT_ATTEMPTS + 1):
            try:
                self._client.report_cleanup(worker_id, cleanup_id, success=success)
                return
            except ControlUnavailableError as error:
                logger.warning(
                    "cleanup report attempt %s/%s failed: %s",
                    attempt,
                    REPORT_ATTEMPTS,
                    error,
                )
            except ClientError as error:
                logger.error("cleanup report rejected by control with status %s", error.status)
                return
            self._stop.wait(delay)
            delay *= 2
        logger.error("gave up reporting cleanup %s after %s attempts", cleanup_id, REPORT_ATTEMPTS)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    configure_platform_logging("worker")
    config = WorkerConfig()
    if not config.token:
        raise SystemExit("DLR_WORKER_TOKEN is not configured; refusing to start")

    client = ControlClient(config.control_url, config.token)
    agent = Agent(config, client)

    def handle_signal(signum: int, _frame: object) -> None:
        logger.info("received signal %s, shutting down", signum)
        agent.request_stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    agent.run()


if __name__ == "__main__":
    main()
