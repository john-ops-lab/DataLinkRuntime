"""DLR Worker Agent (M2).

Outbound-only agent: registers with the Control Node, sends heartbeats,
long-polls for tasks and executes each one in a fresh subprocess inside a
version-scoped venv. The Worker never opens an inbound port.

When the Control Node is unavailable the agent keeps registering /
heartbeating / claiming with simple capped backoff instead of crashing.
"""

import logging
import os
import signal
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

from dlr.worker import executor
from dlr.worker import venv as venv_manager
from dlr.worker.client import ClientError, ControlClient, ControlUnavailableError

logger = logging.getLogger("dlr.worker")

READY_FILE_ENV = "DLR_WORKER_READY_FILE"
DEFAULT_READY_FILE = "/tmp/dlr-worker.ready"

MAX_BACKOFF_SECONDS = 30.0
REPORT_ATTEMPTS = 3


class WorkerConfig:
    """Worker settings read from environment variables (spec §8)."""

    def __init__(self) -> None:
        self.control_url = os.environ.get("DLR_CONTROL_URL", "http://control:8000")
        self.token = os.environ.get("DLR_WORKER_TOKEN", "")
        self.name = os.environ.get("DLR_WORKER_NAME", "worker-1")
        self.heartbeat_seconds = float(os.environ.get("DLR_WORKER_HEARTBEAT_SECONDS", "10"))
        self.claim_wait_seconds = int(os.environ.get("DLR_WORKER_CLAIM_WAIT_SECONDS", "20"))
        self.max_concurrency = int(os.environ.get("DLR_WORKER_MAX_CONCURRENCY", "4"))
        self.runtime_root = Path(os.environ.get("DLR_RUNTIME_ROOT", "/var/lib/dlr/runtime"))
        self.execution_timeout_seconds = int(os.environ.get("DLR_EXECUTION_TIMEOUT_SECONDS", "300"))
        self.dep_install_timeout_seconds = int(
            os.environ.get("DLR_DEP_INSTALL_TIMEOUT_SECONDS", "300")
        )
        self.pypi_index_url = os.environ.get("DLR_PYPI_INDEX_URL") or None

    def runtime_settings(self) -> executor.RuntimeSettings:
        return executor.RuntimeSettings(
            runtime_root=self.runtime_root,
            execution_timeout_seconds=self.execution_timeout_seconds,
            dep_install_timeout_seconds=self.dep_install_timeout_seconds,
            pypi_index_url=self.pypi_index_url,
        )


class Agent:
    def __init__(self, config: WorkerConfig, client: ControlClient) -> None:
        self._config = config
        self._client = client
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._in_flight = 0
        # (adapter_id, version_id) -> number of Executions currently running
        # against that version. Reference counting prevents a concurrent
        # cleanup from deleting a venv while a second Execution is still
        # using it (a plain set would discard the version as soon as the
        # first Execution finishes).
        self._active_versions: dict[tuple[int, int], int] = {}

    def request_stop(self) -> None:
        self._stop.set()

    # --- lifecycle ------------------------------------------------------------

    def run(self) -> None:
        ready_file = Path(os.environ.get(READY_FILE_ENV, DEFAULT_READY_FILE))
        worker_id = self._register()
        if worker_id is None:  # stop requested before registration succeeded
            return
        ready_file.write_text(str(os.getpid()), encoding="utf-8")
        logger.info("worker '%s' registered with id %s", self._config.name, worker_id)

        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, args=(worker_id,), name="dlr-heartbeat", daemon=True
        )
        heartbeat_thread.start()
        try:
            self._claim_loop(worker_id)
        finally:
            ready_file.unlink(missing_ok=True)
            self._graceful_offline(worker_id)
            logger.info("worker agent stopped")

    def _register(self) -> int | None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                info = self._client.register(self._config.name, ["python"])
                return int(info["id"])
            except ControlUnavailableError as error:
                logger.warning(
                    "control unavailable during register (%s); retrying in %.0fs",
                    error,
                    backoff,
                )
            except ClientError as error:
                logger.error(
                    "registration rejected by control (%s); retrying in %.0fs", error, backoff
                )
            self._stop.wait(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        return None

    def _heartbeat_loop(self, worker_id: int) -> None:
        while not self._stop.wait(self._config.heartbeat_seconds):
            try:
                self._client.heartbeat(worker_id)
            except ControlUnavailableError:
                logger.debug("heartbeat skipped: control unavailable")
            except ClientError:
                logger.warning("heartbeat rejected by control", exc_info=True)

    def _graceful_offline(self, worker_id: int) -> None:
        try:
            self._client.mark_offline(worker_id)
            logger.info("marked worker offline (best-effort)")
        except Exception:  # noqa: BLE001 - graceful shutdown must not raise
            logger.debug("offline notification failed", exc_info=True)

    # --- claim / execute --------------------------------------------------------

    def _claim_loop(self, worker_id: int) -> None:
        backoff = 1.0
        with ThreadPoolExecutor(max_workers=self._config.max_concurrency) as pool:
            while not self._stop.is_set():
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
                        "claim rejected by control (%s); retrying in %.0fs", error, backoff
                    )
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                    continue
                if task is None:
                    continue
                self._track_start(task)
                future = pool.submit(self._execute_task, worker_id, task)
                future.add_done_callback(partial(self._task_done, task))

    def _task_done(self, task: dict[str, Any], _future: Future[None]) -> None:
        self._track_end(task)

    def _track_start(self, task: dict[str, Any]) -> None:
        with self._state_lock:
            self._in_flight += 1
            key = (int(task["adapter_id"]), int(task["version_id"]))
            self._active_versions[key] = self._active_versions.get(key, 0) + 1

    def _track_end(self, task: dict[str, Any]) -> None:
        with self._state_lock:
            self._in_flight -= 1
            key = (int(task["adapter_id"]), int(task["version_id"]))
            count = self._active_versions.get(key, 0)
            if count <= 1:
                self._active_versions.pop(key, None)
            else:
                self._active_versions[key] = count - 1

    def _execute_task(self, worker_id: int, task: dict[str, Any]) -> None:
        execution_id = int(task["execution_id"])
        logger.info(
            "executing execution %s (adapter %s version %s)",
            execution_id,
            task["adapter_id"],
            task["version_id"],
        )

        def progress_callback(stdout_chunk: str, stderr_chunk: str) -> None:
            # Best effort: the executor swallows any exception raised here,
            # and Control answers 204 no-op once the Execution is terminal.
            self._client.report_progress(worker_id, execution_id, stdout_chunk, stderr_chunk)

        try:
            result = executor.run(
                task, self._config.runtime_settings(), progress_callback=progress_callback
            )
        except Exception:  # noqa: BLE001 - a worker must survive any task
            logger.exception("unexpected executor failure for execution %s", execution_id)
            result = {
                "status": "failed",
                "error": "worker internal error while executing task",
                "stdout": "",
                "stderr": "",
            }
        self._report_with_retry(worker_id, execution_id, result)
        self._cleanup_venvs(task)

    def _report_with_retry(self, worker_id: int, execution_id: int, result: dict[str, Any]) -> None:
        """Limited transport-level retries; not a business re-run."""
        delay = 2.0
        for attempt in range(1, REPORT_ATTEMPTS + 1):
            try:
                self._client.report_result(worker_id, execution_id, result)
                return
            except ControlUnavailableError as error:
                logger.warning("report attempt %s/%s failed: %s", attempt, REPORT_ATTEMPTS, error)
            except ClientError as error:
                logger.error("result report rejected by control: %s", error)
                return
            self._stop.wait(delay)
            delay *= 2
        logger.error(
            "gave up reporting execution %s after %s attempts", execution_id, REPORT_ATTEMPTS
        )

    def _cleanup_venvs(self, task: dict[str, Any]) -> None:
        adapter_id = int(task["adapter_id"])
        keep = {int(task["version_id"])}
        for pointer in ("latest_version_id", "published_version_id"):
            if task.get(pointer) is not None:
                keep.add(int(task[pointer]))
        with self._state_lock:
            # Any version with an active count > 0 must be kept.
            for (active_adapter_id, active_version_id), count in self._active_versions.items():
                if active_adapter_id == adapter_id and count > 0:
                    keep.add(active_version_id)
        try:
            venv_manager.cleanup_stale_venvs(self._config.runtime_root, adapter_id, keep)
        except Exception:  # noqa: BLE001 - cleanup must never affect outcomes
            logger.warning("venv cleanup failed for adapter %s", adapter_id, exc_info=True)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
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
