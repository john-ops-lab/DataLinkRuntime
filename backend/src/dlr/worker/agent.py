"""Outbound-only Worker with a reliable message Consumer and resource isolation.

Outbound-only agent: registers with the Control Node, sends heartbeats,
consumes persistent dispatches and executes each Attempt inside its Sandbox.
The Worker never opens an inbound port.

When the Control Node is unavailable the agent keeps registering /
heartbeating with capped backoff instead of crashing.
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
from math import isfinite
from pathlib import Path
from typing import Any

from dlr.common.config import settings
from dlr.common.platform_logging import configure_platform_logging
from dlr.worker import cgroup_namespace, executor, sandbox
from dlr.worker import workspace as workspace_manager
from dlr.worker.cache import CacheError, VerifiedVersionCache
from dlr.worker.cache_deletion import CacheDeletionManager, DeletionEligibility
from dlr.worker.cache_lifecycle import CacheLifecycleStore, JournalProtection
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
        self.go_proxy_url = os.environ.get("DLR_GO_PROXY_URL") or None
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
        if shutil.which("node") and shutil.which("npm") and shutil.which("tsc"):
            try:
                checked = subprocess.run(
                    ["tsc", "--version"], capture_output=True, text=True, timeout=10, check=False
                )
                if checked.returncode == 0 and checked.stdout.strip() == "Version 5.8.3":
                    capabilities.append("typescript")
            except (OSError, subprocess.SubprocessError):
                pass
        if shutil.which("go"):
            try:
                checked = subprocess.run(
                    ["go", "version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                    env={
                        "GOTOOLCHAIN": "local",
                        "GOENV": "off",
                        "PATH": os.environ.get("PATH", ""),
                    },
                )
                if checked.returncode == 0 and checked.stdout.startswith("go version go1.27.1 "):
                    capabilities.append("go")
            except (OSError, subprocess.SubprocessError):
                pass
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
            go_proxy_url=self.go_proxy_url,
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
                self.isolation_capabilities["builtin_packages_v1"] = True
                self._verified_resource_envelope = envelope
            logger.info(
                "sandbox preflight %s; rabbitmq execution gate=%s",
                result.get("details", {}).get("status", "failed"),
                self.isolation_capabilities.get("preflight_passed", False),
            )
            if isinstance(details, Mapping):
                # This receipt contains only the disposable synthetic probe's
                # isolation checks, never Worker credentials or Adapter data.
                logger.info("sandbox preflight receipt: %s", json.dumps(dict(details)))
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
        self._startup_cleanup_journals: frozenset[str] = frozenset()
        self._sandbox_recovery_blocked = False
        self._cache_journal_protection: JournalProtection | None = None
        self._cache_deletion_manager: CacheDeletionManager | None = None
        self._active_cleanup_task: dict[str, Any] | None = None
        self._cleanup_entries: Any | None = None
        self._cleanup_retry_path: Path | None = None
        self._cleanup_retained = False

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
        # Cache ownership persists with the volume. A legacy non-empty root
        # needs explicit deployment confirmation before governance can bind
        # it; normal execution remains available while it is retained.
        lifecycle = CacheLifecycleStore.for_runtime(self._config.runtime_root)
        owner_bound = False
        try:
            lifecycle.bind_owner(worker_id)
            owner_bound = True
        except CacheError as error:
            logger.warning("cache governance disabled: %s", error.code)
        self._recover_cleanup_journals(worker_id)
        self._refresh_cache_journal_protection(worker_id, lifecycle)
        if owner_bound:
            self._cache_deletion_manager = CacheDeletionManager(
                VerifiedVersionCache(self._config.runtime_root / "version-cache"),
                lifecycle,
                self._client,
                worker_id=worker_id,
                journal_protected=lambda key: self._journal_protected(worker_id, lifecycle, key),
            )
            self._recover_cache_deletions()
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
        # This additive capability describes the cache protocol implementation,
        # independently of whether sandbox isolation was available at startup.
        self._config.isolation_capabilities["cache_governance_v1"] = True
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
                claim_handshake_timeout_seconds=settings.rabbitmq_claim_handshake_timeout_seconds,
            ),
            self._client,
            runtime_settings=self._config.runtime_settings(),
            connection_parameters=rabbitmq.connection_parameters(),
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

    def _refresh_cache_journal_protection(
        self, worker_id: int, lifecycle: CacheLifecycleStore
    ) -> None:
        resolver_unavailable = False

        def resolve(execution_id: int, attempt_id: int | None) -> str | None:
            nonlocal resolver_unavailable
            if resolver_unavailable:
                raise ControlUnavailableError("cache reference resolver unavailable")
            try:
                return self._client.resolve_cache_reference(worker_id, execution_id, attempt_id)
            except ControlUnavailableError:
                resolver_unavailable = True
                raise

        self._cache_journal_protection = lifecycle.scan_journal_protections(
            attempt_journal_root=self._config.attempt_journal_root,
            cleanup_journal_root=self._config.workspace_cleanup_journal_root,
            sandbox_recovery_root=(
                self._config.workspace_cleanup_journal_root / "sandbox-recovery"
            ),
            resolve=resolve,
        )

    def _journal_protected(self, worker_id: int, lifecycle: CacheLifecycleStore, key: str) -> bool:
        self._refresh_cache_journal_protection(worker_id, lifecycle)
        protection = self._cache_journal_protection
        return protection is None or protection.block_all or key in protection.protected_keys

    def _recover_cache_deletions(self) -> None:
        if self._cache_deletion_manager is None:
            return
        try:
            self._cache_deletion_manager.recover_round()
        except (CacheError, ControlUnavailableError):
            logger.debug("cache deletion recovery deferred")
        except ClientError as error:
            logger.warning("cache deletion recovery rejected: status=%s", error.status)

    def _recover_cleanup_journals(self, worker_id: int, *, startup: bool = True) -> None:
        """Recover owned Workspace journals without deleting unknown paths."""
        if not startup and (self._sandbox_recovery_blocked or not self._startup_cleanup_journals):
            return
        root = self._config.workspace_cleanup_journal_root
        if startup and root.is_dir():
            # Capture before the Consumer starts. Never scan the journals of
            # newly started live Attempts in a background recovery retry.
            self._startup_cleanup_journals = frozenset(
                path.name
                for path in root.iterdir()
                if workspace_manager.JOURNAL_NAME_PATTERN.fullmatch(path.name)
                or workspace_manager.ATTEMPT_CLEANUP_JOURNAL_NAME_PATTERN.fullmatch(path.name)
            )

        def report_cleanup(execution_id: int, cleanup_token: str) -> bool:
            try:
                self._client.report_cleanup_receipt(
                    worker_id,
                    execution_id,
                    cleanup_token=cleanup_token,
                )
                for name in self._startup_cleanup_journals:
                    match = workspace_manager.ATTEMPT_CLEANUP_JOURNAL_NAME_PATTERN.fullmatch(name)
                    if match is None or int(match.group(1)) != execution_id:
                        continue
                    attempt_id = int(match.group(2))
                    record = workspace_manager.load_attempt_journal(
                        self._config.attempt_journal_root, attempt_id
                    )
                    if (
                        record is not None
                        and record["execution_id"] == execution_id
                        and record["cleanup_token"] == cleanup_token
                        and Path(record["workspace_path"])
                        == workspace_manager.workspace_path(
                            self._config.runtime_root, execution_id, attempt_id=attempt_id
                        )
                    ):
                        workspace_manager.remove_attempt_journal(
                            self._config.attempt_journal_root, attempt_id
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

        # Kernel recovery runs only before the Consumer starts. Background
        # retries handle only the captured old filesystem/receipt journals.
        sandbox_counts = (
            sandbox.recover(
                self._config.sandbox_config,
                root / "sandbox-recovery",
                runtime_root=self._config.runtime_root,
            )
            if startup
            else {"inspected": 0, "completed": 0, "retained": 0}
        )
        if sandbox_counts["retained"]:
            # Do not report filesystem cleanup while kernel ownership is
            # unresolved, or consume new work beside an unverified residue.
            self._config.isolation_capabilities = {key: False for key in ISOLATION_CAPABILITY_KEYS}
            self._sandbox_recovery_blocked = True
            logger.warning(
                "sandbox recovery retained %s records; execution and workspace receipts paused",
                sandbox_counts["retained"],
            )
            return
        counts = workspace_manager.recover_cleanup_journals(
            self._config.workspace_cleanup_journal_root,
            self._config.runtime_root,
            report_cleanup=report_cleanup,
            scan_timeout_seconds=workspace_manager.RECOVERY_SCAN_TIMEOUT_SECONDS,
            retry_backoff_seconds=workspace_manager.RECOVERY_RETRY_BACKOFF_SECONDS,
            candidate_names=self._startup_cleanup_journals,
        )
        self._startup_cleanup_journals = frozenset(
            name for name in self._startup_cleanup_journals if (root / name).exists()
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
                self._recover_cleanup_journals(worker_id, startup=False)
                self._recover_cache_deletions()
                task = self._active_cleanup_task or self._client.claim_cleanup(worker_id)
                if task is not None:
                    self._active_cleanup_task = task
                    if self._execute_cleanup_task(worker_id, task):
                        self._active_cleanup_task = None
            except ControlUnavailableError:
                logger.debug("adapter cleanup deferred: control unavailable")
            except ClientError as error:
                logger.warning("adapter cleanup rejected: status=%s", error.status)
            self._stop.wait(delay)

    def _execute_cleanup_task(self, worker_id: int, task: dict[str, Any]) -> bool:
        cleanup_id = int(task["cleanup_id"])
        adapter_id = int(task["adapter_id"])
        claim_attempt = int(task["claim_attempt"])
        manager = self._cache_deletion_manager
        if manager is None:
            self._report_cleanup_with_retry(
                worker_id,
                cleanup_id,
                claim_attempt=claim_attempt,
                success=False,
                error_code="cache_cleanup_retained",
            )
            return True
        try:
            owner = manager.lifecycle.owner(worker_id)
            deadline = time.monotonic() + 5.0
            round_budget = manager.make_round_budget(
                max_nodes=100_000,
                max_bytes=256 * 1024 * 1024,
                max_scan_nodes=100_000,
                max_hash_bytes=256 * 1024 * 1024,
                deadline=deadline,
            )
            inspected = 0
            if self._cleanup_entries is None:
                self._cleanup_entries = os.scandir(manager.cache.entries)
                self._cleanup_retry_path = None
                self._cleanup_retained = False
            exhausted = False
            while inspected < 100 and time.monotonic() < deadline:
                retry_path = self._cleanup_retry_path
                retrying_entry = retry_path is not None
                if retry_path is not None:
                    entry_path = retry_path
                else:
                    try:
                        item = next(self._cleanup_entries)
                    except StopIteration:
                        exhausted = True
                        break
                    entry_path = Path(item.path)
                inspected += 1
                if entry_path.name.startswith("."):
                    self._cleanup_retained = True
                    self._cleanup_retry_path = None
                    continue
                try:
                    observed = manager.observed_identity_bounded(entry_path, budget=round_budget)
                except CacheError as error:
                    if error.code == "cache_scan_budget_exhausted":
                        if retrying_entry:
                            self._cleanup_retained = True
                            self._cleanup_retry_path = None
                            continue
                        self._cleanup_retry_path = entry_path
                        return False
                    raise
                if observed is None:
                    self._cleanup_retained = True
                    self._cleanup_retry_path = None
                    continue
                identity, digest = observed
                try:
                    version_id = int(identity["version_id"])
                    language = str(identity["language"])
                    source_sha256 = str(identity["source_sha256"])
                except (KeyError, TypeError, ValueError):
                    self._cleanup_retained = True
                    self._cleanup_retry_path = None
                    continue
                if (
                    identity.get("adapter_id") == adapter_id
                    and entry_path.name != f"{adapter_id}-{version_id}"
                ):
                    self._cleanup_retained = True
                    self._cleanup_retry_path = None
                    continue
                if identity.get("adapter_id") != adapter_id:
                    self._cleanup_retry_path = None
                    continue
                facts = manager.lifecycle.lifecycle(entry_path.name)
                try:
                    result = manager.begin(
                        DeletionEligibility(
                            adapter_id=adapter_id,
                            version_id=version_id,
                            identity=identity,
                            digest=digest,
                            last_used_before=float(facts["last_used_at"]),
                            cleanup_context={
                                "cleanup_id": cleanup_id,
                                "claim_attempt": claim_attempt,
                            },
                            observed_identity={
                                "store_id": owner["store_id"],
                                "language": language,
                                "source_sha256": source_sha256,
                                "digest": digest,
                            },
                        ),
                        max_seconds=max(0.001, deadline - time.monotonic()),
                        round_budget=round_budget,
                    )
                except CacheError as error:
                    if error.code in {
                        "cache_budget_exhausted",
                        "cache_scan_budget_exhausted",
                        "cache_lock_busy",
                    }:
                        if retrying_entry and error.code != "cache_lock_busy":
                            self._cleanup_retained = True
                            self._cleanup_retry_path = None
                            continue
                        self._cleanup_retry_path = entry_path
                        return False
                    if error.code in {
                        "cache_not_eligible",
                        "cache_entry_in_use",
                        "cache_journal_protected",
                        "cache_identity_unverified",
                    }:
                        self._cleanup_retained = True
                        self._cleanup_retry_path = None
                        continue
                    raise
                except ClientError as error:
                    try:
                        detail = json.loads(error.body).get("detail", {})
                        code = detail.get("code") if isinstance(detail, dict) else None
                    except (AttributeError, TypeError, ValueError):
                        code = None
                    if error.status == 409 and code in {
                        "cache_reference_active",
                        "cache_reclamation_in_progress",
                    }:
                        self._cleanup_retained = True
                        self._cleanup_retry_path = None
                        continue
                    raise
                self._cleanup_retry_path = None
                if result.status == "failed":
                    self._cleanup_retained = True
            if not exhausted:
                return False
            self._cleanup_entries.close()
            self._cleanup_entries = None
            pre_cache = self._config.runtime_root / "adapters" / str(adapter_id)
            if pre_cache.exists() or pre_cache.is_symlink():
                self._cleanup_retained = True
            cleanup_state = manager.cleanup_state(cleanup_id)
            if cleanup_state == "pending":
                return False
            if cleanup_state in {"failed", "unknown"}:
                self._cleanup_retained = True
        except (CacheError, OSError, ValueError, ClientError, ControlUnavailableError):
            logger.warning(
                "adapter environment cleanup failed for adapter %s",
                adapter_id,
            )
            self._report_cleanup_with_retry(
                worker_id,
                cleanup_id,
                claim_attempt=claim_attempt,
                success=False,
                error_code="cache_cleanup_failed",
            )
            if self._cleanup_entries is not None:
                self._cleanup_entries.close()
            self._cleanup_entries = None
            self._cleanup_retry_path = None
            self._cleanup_retained = False
            return True
        self._report_cleanup_with_retry(
            worker_id,
            cleanup_id,
            claim_attempt=claim_attempt,
            success=not self._cleanup_retained,
            error_code="cache_cleanup_retained" if self._cleanup_retained else None,
        )
        self._cleanup_retained = False
        return True

    def _report_cleanup_with_retry(
        self,
        worker_id: int,
        cleanup_id: int,
        *,
        claim_attempt: int,
        success: bool,
        error_code: str | None = None,
    ) -> None:
        """Bounded transport retries; never send filesystem error text."""
        delay = 2.0
        for attempt in range(1, REPORT_ATTEMPTS + 1):
            try:
                self._client.report_cleanup(
                    worker_id,
                    cleanup_id,
                    success=success,
                    claim_attempt=claim_attempt,
                    error_code=error_code,
                )
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
    config = WorkerConfig()
    if not config.token:
        raise SystemExit("DLR_WORKER_TOKEN is not configured; refusing to start")
    # A SIGKILL does not run the prior Agent's finally block. Never let its
    # stale marker make Docker health checks accept an unfinished bootstrap.
    Path(os.environ.get(READY_FILE_ENV, DEFAULT_READY_FILE)).unlink(missing_ok=True)

    with cgroup_namespace.lock_roots(
        config.runtime_root,
        [config.workspace_cleanup_journal_root, config.attempt_journal_root],
    ):
        try:
            config.sandbox_config = cgroup_namespace.prepare(config.sandbox_config)
        except (sandbox.SandboxError, OSError) as error:
            cause = error.__cause__
            logger.error(
                "sandbox namespace bootstrap failed: code=%s errno=%s",
                getattr(error, "code", "sandbox_namespace_bootstrap_failed"),
                getattr(error, "errno", None) or getattr(cause, "errno", None),
            )
            raise SystemExit(1) from None
        configure_platform_logging("worker")

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
