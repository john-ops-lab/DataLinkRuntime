"""DLR Worker Agent (M2).

Outbound-only agent: registers with the Control Node, sends heartbeats,
long-polls for tasks and executes each one in a fresh subprocess inside a
version-scoped venv. The Worker never opens an inbound port.

When the Control Node is unavailable the agent keeps registering /
heartbeating / claiming with simple capped backoff instead of crashing.
"""

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from math import isfinite
from pathlib import Path
from typing import Any

from dlr.common.platform_logging import configure_platform_logging
from dlr.worker import executor, i18n
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
DEFAULT_PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOL_VERSIONS = frozenset({1, 2, 3})
ISOLATION_CAPABILITY_KEYS = (
    "cgroup_v2",
    "mount_namespace",
    "pid_namespace",
    "memory_hard_limit",
    "pids_hard_limit",
    "tmpfs_hard_limit",
    "bounded_output",
    "preflight_passed",
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
        try:
            self.protocol_version = int(
                os.environ.get("DLR_WORKER_PROTOCOL_VERSION", str(DEFAULT_PROTOCOL_VERSION))
            )
        except ValueError as error:
            raise ValueError("DLR_WORKER_PROTOCOL_VERSION must be 1, 2 or 3") from error
        if self.protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise ValueError("DLR_WORKER_PROTOCOL_VERSION must be 1, 2 or 3")
        self.heartbeat_seconds = float(os.environ.get("DLR_WORKER_HEARTBEAT_SECONDS", "10"))
        self.claim_wait_seconds = int(os.environ.get("DLR_WORKER_CLAIM_WAIT_SECONDS", "20"))
        self.max_concurrency = int(os.environ.get("DLR_WORKER_MAX_CONCURRENCY", "4"))
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
        self.execution_slots = max(
            1, int(os.environ.get("DLR_WORKER_EXECUTION_SLOTS", str(self.max_concurrency)))
        )
        self.attempt_journal_root = Path(
            os.environ.get("DLR_ATTEMPT_JOURNAL_ROOT", str(self.runtime_root / "attempt-journal"))
        )
        self.isolation_capabilities = self._read_isolation_capabilities()

    def _read_isolation_capabilities(self) -> dict[str, bool]:
        """Report observed capabilities; Batch 2 never infers sandbox PASS."""
        raw = os.environ.get("DLR_WORKER_ISOLATION_CAPABILITIES_JSON")
        if raw:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = {}
            if isinstance(value, dict) and all(
                isinstance(key, str) and isinstance(flag, bool) for key, flag in value.items()
            ):
                return {key: value[key] for key in value if key in ISOLATION_CAPABILITY_KEYS}
        return {key: False for key in ISOLATION_CAPABILITY_KEYS}

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
        )


class Agent:
    def __init__(self, config: WorkerConfig, client: ControlClient) -> None:
        self._config = config
        self._client = client
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._in_flight = 0
        self._in_flight_execution_ids: set[int] = set()
        # (adapter_id, version_id) -> number of Executions currently running
        # against that version. Reference counting prevents a concurrent
        # cleanup from deleting a venv while a second Execution is still
        # using it (a plain set would discard the version as soon as the
        # first Execution finishes).
        self._active_versions: dict[tuple[int, int], int] = {}
        self._registration_info: dict[str, Any] = {}
        self._consumer: V3Consumer | None = None

    def request_stop(self) -> None:
        self._stop.set()
        if self._consumer is not None:
            self._consumer.request_stop()

    def _mark_execution_in_flight(self, execution_id: int) -> None:
        with self._state_lock:
            self._in_flight_execution_ids.add(execution_id)

    def _unmark_execution_in_flight(self, execution_id: int) -> None:
        with self._state_lock:
            self._in_flight_execution_ids.discard(execution_id)

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
        try:
            if self._config.protocol_version >= 3:
                if not self._registration_info.get("rabbitmq_execution_v3", False):
                    logger.warning(
                        "Worker v3 isolation preflight is not passed; RabbitMQ "
                        "Consumer remains paused"
                    )
                    self._stop.wait()
                else:
                    self._run_v3_consumer(worker_id)
            else:
                self._claim_loop(worker_id)
        finally:
            ready_file.unlink(missing_ok=True)
            self._graceful_offline(worker_id)
            logger.info("worker agent stopped")

    def _register(self) -> int | None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                capabilities = self._config.capabilities()
                if not capabilities:
                    raise RuntimeError("no supported Runtime is installed")
                try:
                    info = self._client.register(
                        self._config.name,
                        capabilities,
                        protocol_version=self._config.protocol_version,
                        isolation_capabilities=self._config.isolation_capabilities,
                    )
                except TypeError as error:
                    # Keep the v1/v2 in-process client seam usable during a
                    # rolling deployment. A real ControlClient accepts the
                    # matrix; only an older injected client may not.
                    if "isolation_capabilities" not in str(error):
                        raise
                    info = self._client.register(
                        self._config.name,
                        capabilities,
                        protocol_version=self._config.protocol_version,
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

    def _run_v3_consumer(self, worker_id: int) -> None:
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

    # --- claim / execute --------------------------------------------------------

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

        # Keep the snapshot and task-start marker under one lock.  A task
        # thread that has not yet established its journal waits here, while
        # already-running tasks remain protected for the whole scan.
        with self._state_lock:
            counts = workspace_manager.recover_cleanup_journals(
                self._config.workspace_cleanup_journal_root,
                self._config.runtime_root,
                report_cleanup=report_cleanup,
                scan_timeout_seconds=workspace_manager.RECOVERY_SCAN_TIMEOUT_SECONDS,
                retry_backoff_seconds=workspace_manager.RECOVERY_RETRY_BACKOFF_SECONDS,
                in_flight_execution_ids=frozenset(self._in_flight_execution_ids),
            )
        if counts["retained"] or counts["deferred"]:
            logger.info(
                "workspace cleanup scan inspected %s; completed %s, deferred %s, retained %s",
                counts["inspected"],
                counts["completed"],
                counts["deferred"],
                counts["retained"],
            )

    def _claim_loop(self, worker_id: int) -> None:
        backoff = 1.0
        next_cleanup_scan = 0.0
        with ThreadPoolExecutor(max_workers=self._config.max_concurrency) as pool:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_cleanup_scan:
                    self._recover_cleanup_journals(worker_id)
                    next_cleanup_scan = now + max(
                        1.0, self._config.workspace_cleanup_interval_seconds
                    )
                with self._state_lock:
                    saturated = self._in_flight >= self._config.max_concurrency
                if saturated:
                    self._stop.wait(1.0)
                    continue
                try:
                    task = self._client.claim(worker_id, self._config.claim_wait_seconds)
                    backoff = 1.0
                except ControlUnavailableError as error:
                    logger.warning(
                        "control unavailable during claim (%s); retrying in %.0fs",
                        error,
                        backoff,
                    )
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                    continue
                except ClientError as error:
                    logger.error(
                        "claim rejected by control with status %s; retrying in %.0fs",
                        error.status,
                        backoff,
                    )
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                    continue
                if task is None:
                    continue
                self._track_start(task)
                try:
                    future = pool.submit(self._execute_task, worker_id, task)
                except BaseException:
                    self._track_end(task)
                    raise
                future.add_done_callback(partial(self._task_done, task))

    def _task_done(self, task: dict[str, Any], _future: Future[None]) -> None:
        self._track_end(task)

    def _track_start(self, task: dict[str, Any]) -> None:
        with self._state_lock:
            self._in_flight += 1
            if task.get("kind") == "adapter_cleanup":
                return
            key = (int(task["adapter_id"]), int(task["version_id"]))
            self._active_versions[key] = self._active_versions.get(key, 0) + 1

    def _track_end(self, task: dict[str, Any]) -> None:
        with self._state_lock:
            self._in_flight -= 1
            if task.get("kind") == "adapter_cleanup":
                return
            key = (int(task["adapter_id"]), int(task["version_id"]))
            count = self._active_versions.get(key, 0)
            if count <= 1:
                self._active_versions.pop(key, None)
            else:
                self._active_versions[key] = count - 1

    def _execute_task(self, worker_id: int, task: dict[str, Any]) -> None:
        if task.get("kind") == "adapter_cleanup":
            self._execute_cleanup_task(worker_id, task)
            return
        execution_id = int(task["execution_id"])
        self._mark_execution_in_flight(execution_id)
        try:
            self._execute_execution_task(worker_id, task)
        finally:
            self._unmark_execution_in_flight(execution_id)

    def _execute_execution_task(self, worker_id: int, task: dict[str, Any]) -> None:
        execution_id = int(task["execution_id"])
        logger.info(
            "executing execution %s (adapter %s version %s)",
            execution_id,
            task["adapter_id"],
            task["version_id"],
        )
        claim_token = task.get("claim_token")

        def progress_callback(stdout_chunk: str, stderr_chunk: str) -> bool:
            # Best effort: the executor swallows any exception raised here.
            # Control answers the cancel flag on every upload (M3.2), and
            # empty uploads double as the cancel poll.
            if claim_token is None:
                return self._client.report_progress(
                    worker_id, execution_id, stdout_chunk, stderr_chunk
                )
            return self._client.report_progress(
                worker_id,
                execution_id,
                stdout_chunk,
                stderr_chunk,
                claim_token=claim_token,
            )

        input_downloader = None
        if claim_token is not None:

            def download_input(
                input_file: Mapping[str, Any], destination: workspace_manager.WritableBinary
            ) -> int:
                return self._client.download_input_artifact(
                    worker_id,
                    execution_id,
                    int(input_file["id"]),
                    claim_token=claim_token,
                    destination=destination,
                )

            input_downloader = download_input

        try:
            if input_downloader is None:
                result = executor.run(
                    task,
                    self._config.runtime_settings(),
                    progress_callback=progress_callback,
                )
            else:
                result = executor.run(
                    task,
                    self._config.runtime_settings(),
                    progress_callback=progress_callback,
                    input_downloader=input_downloader,
                )
        except Exception:  # noqa: BLE001 - a worker must survive any task
            logger.error("unexpected executor failure for execution %s", execution_id)
            result = {
                "status": "failed",
                "error": i18n.text(
                    i18n.resolve_locale(task.get("locale")),
                    "runtime.worker_internal_error",
                ),
                "error_code": "worker_internal_error",
                "stdout": "",
                "stdout_truncated": False,
                "stderr": "",
                "stderr_truncated": False,
            }
            try:
                if int(task.get("protocol_version") or 1) >= 2:
                    result.update(
                        {
                            "workspace_cleanup_status": "deferred",
                            "workspace_cleanup_error_code": "workspace_cleanup_failed",
                        }
                    )
            except (TypeError, ValueError):
                pass
        report_accepted = self._report_with_retry(
            worker_id,
            execution_id,
            result,
            claim_token=claim_token,
        )
        if (
            claim_token is not None
            and report_accepted
            and result.get("workspace_cleanup_status") == "completed"
            and not workspace_manager.remove_cleanup_journal(
                self._config.workspace_cleanup_journal_root, execution_id
            )
        ):
            logger.warning("cleanup journal removal deferred for execution %s", execution_id)
        self._cleanup_version_environments(task)

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

    def _report_with_retry(
        self,
        worker_id: int,
        execution_id: int,
        result: dict[str, Any],
        *,
        claim_token: str | None = None,
    ) -> bool:
        """Limited transport-level retries; not a business re-run."""
        delay = 2.0
        for attempt in range(1, REPORT_ATTEMPTS + 1):
            try:
                if claim_token is None:
                    self._client.report_result(worker_id, execution_id, result)
                else:
                    self._client.report_result(
                        worker_id,
                        execution_id,
                        result,
                        claim_token=claim_token,
                    )
                return True
            except ControlUnavailableError as error:
                logger.warning("report attempt %s/%s failed: %s", attempt, REPORT_ATTEMPTS, error)
            except ClientError as error:
                logger.error(
                    "result report rejected by control with status %s",
                    error.status,
                )
                return False
            self._stop.wait(delay)
            delay *= 2
        logger.error(
            "gave up reporting execution %s after %s attempts", execution_id, REPORT_ATTEMPTS
        )
        return False

    def _cleanup_version_environments(self, task: dict[str, Any]) -> None:
        adapter_id = int(task["adapter_id"])
        keep = {int(task["version_id"])}
        if task.get("latest_version_id") is not None:
            keep.add(int(task["latest_version_id"]))
        with self._state_lock:
            # Any version with an active count > 0 must be kept.
            for (active_adapter_id, active_version_id), count in self._active_versions.items():
                if active_adapter_id == adapter_id and count > 0:
                    keep.add(active_version_id)
        try:
            venv_manager.cleanup_stale_venvs(self._config.runtime_root, adapter_id, keep)
        except Exception:  # noqa: BLE001 - cleanup must never affect outcomes
            logger.warning("version environment cleanup failed for adapter %s", adapter_id)


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
