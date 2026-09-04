"""Version-scoped virtual environments (M2 spec §9).

One independent venv per AdapterVersion, lazily built on first execution
with ``uv venv`` + ``uv pip install``. A ``.ready`` marker is written only
after dependencies are fully prepared; an incomplete directory is removed
and rebuilt. Within one Worker, concurrent first runs of the same Version
share a lightweight in-process lock.

M3.2 dependency strategy (identical for manual and triggered runs):
a ``.ready`` venv passes without any network; otherwise installation tries
the local ``uv`` cache offline first, falls back to the configured package
source index URL, and fails with an explicit operator-facing message when
neither is available.
"""

import base64
import hashlib
import json
import logging
import os
import re
import resource
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast
from urllib import parse as url_parse

from dlr.worker import cache, i18n
from dlr.worker.cache import CacheReservation, VerifiedVersionCache

logger = logging.getLogger("dlr.worker.venv")

DependencyLogCallback = Callable[[str], None]


@dataclass(frozen=True)
class DependencyExecutionContext:
    """The already-created Attempt boundary used by dependency preparation."""

    cgroup_path: Path
    tmpdir: Path
    nofile: int
    log_max_bytes: int
    reservation_check: Callable[[], None] | None = None
    reservation_lost: threading.Event | None = None

    def with_reservation(
        self,
        reservation_check: Callable[[], None],
        reservation_lost: threading.Event,
    ) -> "DependencyExecutionContext":
        """Bind dependency writes and subprocesses to one live reservation."""
        return replace(
            self,
            reservation_check=reservation_check,
            reservation_lost=reservation_lost,
        )


# Keep the original function as an explicit test seam.  Unit tests that
# replace ``subprocess.run`` still get their deterministic fake, while real
# dependency commands use the fixed-chunk Popen reader below.
_ORIGINAL_SUBPROCESS_RUN = subprocess.run
_DEPENDENCY_READ_CHUNK = 64 * 1024

_build_locks: dict[tuple[int, int], threading.Lock] = {}
_build_locks_guard = threading.Lock()
CACHE_RESERVATION_BYTES = 256 * 1024 * 1024
_CACHE_RESERVATION_BYTES = CACHE_RESERVATION_BYTES

_URI_USERINFO_PATTERN = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^\s/?#@]+@")


def _dependency_child(command: list[str], ready_fd: int, nofile: int) -> int:
    """Wait for parent-side cgroup placement, then apply limits and exec."""

    try:
        try:
            if os.read(ready_fd, 1) != b"1":
                return 125
        finally:
            os.close(ready_fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
        os.execvpe(command[0], command, os.environ)
    except BaseException:
        # Keep helper diagnostics stable and path-free; the parent maps a
        # non-zero exit to the existing dependency sandbox error contract.
        print("DLR_DEPENDENCY_HELPER_ERROR", flush=True)
        return 125


def _dependency_helper_command(command: list[str], ready_fd: int, nofile: int) -> list[str]:
    encoded = base64.urlsafe_b64encode(
        json.dumps(command, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return [
        sys.executable,
        "-m",
        "dlr.worker.venv",
        "--dependency-child",
        "--command",
        encoded,
        "--ready-fd",
        str(ready_fd),
        "--nofile",
        str(nofile),
    ]


# DLR-owned marker inserted between third-party install outputs.
OFFLINE_CACHE_FALLBACK_MARKER = (
    "[offline cache insufficient; retrying with the configured package source]"
)

# Environment variables the dependency subprocess is allowed to inherit.
# Everything else (platform tokens, database URL, runtime secrets) is
# explicitly excluded so third-party build code cannot read them.
_INHERITED_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TZ",
    "USER",
    # Proxy / certificate / index configuration may be needed by uv pip.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)


def _dependency_env() -> dict[str, str]:
    """Build a minimal environment for dependency installation.

    Only whitelisted keys are inherited; platform tokens, database URL and
    runtime secrets are never passed to the subprocess.
    """
    env: dict[str, str] = {}
    for key in _INHERITED_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


def package_index_secret_values(index_url: str | None) -> list[str]:
    """Return encoded and decoded userinfo values from a package index URL.

    The effective package-source URL may carry Basic Auth credentials. These
    values join the Worker's explicit redaction set so uv output cannot leak
    either the URL-encoded form or a decoded username/password.
    """
    if not index_url:
        return []
    try:
        parts = url_parse.urlsplit(index_url)
    except ValueError:
        return []
    if "@" not in parts.netloc:
        return []

    raw_userinfo = parts.netloc.rsplit("@", 1)[0]
    candidates = [raw_userinfo, url_parse.unquote(raw_userinfo)]
    for value in (parts.username, parts.password):
        if value:
            candidates.extend((value, url_parse.unquote(value)))
    return list(dict.fromkeys(value for value in candidates if value))


def _redact_sensitive(text: str, sensitive_values: Iterable[str] = ()) -> str:
    """Defensively redact credentials from dependency logs.

    Scans for explicit values plus common sensitive patterns (including URI
    userinfo) and replaces them with [REDACTED]. This is a safety net on top
    of the minimal environment; even if uv or a build script echoes a package
    source URL, it will not be persisted to Execution.stderr.
    """
    redacted = text
    # Known package-source and runtime values, longest first so a username
    # cannot partially rewrite its complete ``username:password`` userinfo.
    for value in sorted(set(sensitive_values), key=len, reverse=True):
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    # URI userinfo for any scheme, independent of the explicit value set.
    redacted = _URI_USERINFO_PATTERN.sub(r"\g<scheme>[REDACTED]@", redacted)
    # Bearer tokens and common token patterns.
    redacted = re.sub(
        r"Bearer\s+[A-Za-z0-9._-]+", "Bearer [REDACTED]", redacted, flags=re.IGNORECASE
    )
    redacted = re.sub(
        r"(?i)(token|secret|password|api_key)\s*[:=]\s*\S+", r"\1=[REDACTED]", redacted
    )
    # Database URLs.
    redacted = re.sub(
        r"postgresql\+psycopg://[^\s]+",
        "postgresql+psycopg://[REDACTED]",
        redacted,
        flags=re.IGNORECASE,
    )
    redacted = re.sub(
        r"postgresql://[^\s]+", "postgresql://[REDACTED]", redacted, flags=re.IGNORECASE
    )
    # DLR_SECRET_* values.
    for key, value in os.environ.items():
        if key.startswith("DLR_SECRET_") and value:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def redact_package_index_log(text: str, index_url: str | None) -> str:
    """Redact one dependency log using the effective package index URL."""
    return _redact_sensitive(text, package_index_secret_values(index_url))


def dependency_specs(requirements: str) -> list[str]:
    """Return package declaration lines suitable for user-facing status logs.

    The complete requirements file is still passed to uv unchanged. Pure pip
    option lines are intentionally omitted here: they configure the joint
    install but are not standalone packages that can truthfully be reported as
    installed.
    """
    return [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and not line.lstrip().startswith("-")
    ]


def dependency_failure_label(dependencies: Iterable[str], install_log: str) -> str | None:
    """Identify a declared package when an ecosystem tool names it in output."""
    declarations = list(dependencies)
    lowered_log = install_log.casefold()
    for dependency in declarations:
        if dependency.casefold() in lowered_log:
            return dependency
    return declarations[0] if len(declarations) == 1 else None


# --- actionable install-error classification (M5.5.8) --------------------------


def dependency_source_hint_code(log: str) -> str | None:
    """Classify a dependency-source failure without exposing raw tool text."""
    lowered = log.lower()
    if any(
        marker in lowered
        for marker in (
            "temporary failure in name resolution",
            "could not resolve host",
            "failed to resolve",
            "name or service not known",
            "nodename nor servname provided",
            "getaddrinfo failed",
            "dns lookup failed",
        )
    ):
        return "dns"
    if any(
        marker in lowered
        for marker in (
            "network is unreachable",
            "connection timed out",
            "connection refused",
            "failed to connect",
            "operation timed out",
            "connection reset by peer",
            "cannot connect",
            "could not connect",
            "no route to host",
            "host unreachable",
        )
    ):
        return "network"
    if any(
        marker in lowered
        for marker in (
            "certificate verify failed",
            "self-signed certificate",
            "tls handshake",
            "ssl error",
            "unable to get local issuer certificate",
        )
    ):
        return "tls"
    if any(
        marker in lowered
        for marker in (
            "401",
            "403",
            "unauthorized",
            "authentication failed",
            "authentication failure",
            "invalid username or password",
            "bad password",
            "e401",
            "e403",
            "credentials rejected",
        )
    ):
        return "auth"
    if any(
        marker in lowered
        for marker in (
            "no matching distribution found",
            "no matching version found",
            "could not find a version",
            "not found from versions",
            "could not find artifact",
            "404 not found - get",
            "not found on the registry",
            "package does not exist",
            "cannot find a version",
        )
    ):
        return "package"
    if any(
        marker in lowered
        for marker in (
            "invalid index url",
            "invalid url",
            "404 client error",
            "http error 404",
            "repository not found",
            "remote repository",
            "cannot access",
            "does not exist or is not a valid",
            "requested url returned error: 404",
        )
    ):
        return "source"
    return None


def classify_dependency_install_error(
    log: str, locale: i18n.WorkerLocale = i18n.DEFAULT_WORKER_LOCALE
) -> str | None:
    """Return a localized actionable hint for a known source failure."""
    return i18n.source_hint(locale, dependency_source_hint_code(log))


def _run_install_logged(
    command: list[str],
    timeout_seconds: int,
    *,
    context: DependencyExecutionContext | None = None,
) -> str:
    """Run a dependency-install command with an actionable error hint appended."""
    try:
        if context is None:
            return _run_logged(command, timeout_seconds)
        return _run_logged(command, timeout_seconds, context=context)
    except DependencyPreparationError as error:
        # Keep the exception message locale-neutral. The executor owns the
        # user-facing localized hint and the raw install log remains separate.
        raise DependencyPreparationError(
            error.args[0],
            error.install_log,
            dependency=error.dependency,
            hint_code=error.hint_code,
            no_source=error.no_source,
            error_code=error.error_code,
        ) from error


def _run_logged_in_context(
    command: list[str], timeout_seconds: int, context: DependencyExecutionContext | None
) -> str:
    if context is None:
        return _run_logged(command, timeout_seconds)
    return _run_logged(command, timeout_seconds, context=context)


def _run_install_logged_in_context(
    command: list[str], timeout_seconds: int, context: DependencyExecutionContext | None
) -> str:
    if context is None:
        return _run_install_logged(command, timeout_seconds)
    return _run_install_logged(command, timeout_seconds, context=context)


class DependencyPreparationError(Exception):
    """venv creation or dependency installation failed."""

    def __init__(
        self,
        message: str,
        install_log: str,
        dependency: str | None = None,
        hint_code: str | None = None,
        no_source: bool = False,
        error_code: str = "dependency_preparation_failed",
    ) -> None:
        super().__init__(message)
        self.install_log = install_log
        self.dependency = dependency
        self.hint_code = hint_code or dependency_source_hint_code(install_log)
        self.error_code = error_code
        # Stable machine marker: this failure is exactly "no dependency source
        # is configured". The executor replaces the English instruction with
        # the Execution-locale message without relying on the dependency label.
        self.no_source = no_source


def _lock_for(adapter_id: int, version_id: int) -> threading.Lock:
    with _build_locks_guard:
        lock = _build_locks.get((adapter_id, version_id))
        if lock is None:
            lock = threading.Lock()
            _build_locks[(adapter_id, version_id)] = lock
        return lock


def version_dir(runtime_root: Path, adapter_id: int, version_id: int) -> Path:
    return runtime_root / "version-cache" / "entries" / f"{adapter_id}-{version_id}"


def _cache_identity(
    adapter_id: int,
    version_id: int,
    language: str,
    source: str,
) -> dict[str, object]:
    return {
        "adapter_id": adapter_id,
        "version_id": version_id,
        "language": language,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
    }


@dataclass
class _VersionBuild:
    cache: VerifiedVersionCache
    staging: Path
    target: Path
    reservation: CacheReservation
    staging_root: Path | None = None
    _staging_cleanup_done: bool = field(default=False, init=False, repr=False)
    _lease_stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lease_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _lease_lost: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lease_error_code: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        interval = max(0.05, min(self.reservation.ttl_seconds / 3, 30.0))

        def renew_while_live() -> None:
            while not self._lease_stop.wait(interval):
                try:
                    self.reservation.renew()
                except cache.CacheError as error:
                    self._lease_error_code = error.code
                    self._lease_lost.set()
                    logger.warning("version cache reservation lease was lost (%s)", error.code)
                    return

        self._lease_thread = threading.Thread(
            target=renew_while_live,
            name="dlr-cache-reservation",
            daemon=True,
        )
        self._lease_thread.start()

    @property
    def lease_lost(self) -> threading.Event:
        """Expose the one-way lease-loss signal to dependency subprocesses."""
        return self._lease_lost

    def assert_live(self) -> None:
        """Check the reservation before every externally visible build step."""
        if self._lease_lost.is_set():
            raise cache.CacheError(self._lease_error_code or "cache_reservation_expired")
        try:
            self.reservation.assert_active()
        except cache.CacheError as error:
            self._lease_error_code = error.code
            self._lease_lost.set()
            raise

    def _stop_lease(self) -> None:
        self._lease_stop.set()
        if self._lease_thread is not None and self._lease_thread is not threading.current_thread():
            self._lease_thread.join(timeout=1.0)

    def _remove_tmpfs_staging(self) -> None:
        if self._staging_cleanup_done:
            return
        if self.staging_root is None:
            self.cache.remove_staging(self.staging)
            self._staging_cleanup_done = True
            return
        try:
            try:
                root = self.staging_root.resolve(strict=True)
            except FileNotFoundError:
                # ``finish`` may already have removed the staging directory
                # before a caller's error path invokes ``abort``.
                self._staging_cleanup_done = True
                return
            candidate = self.staging.resolve(strict=False)
            if (
                not root.is_dir()
                or candidate.parent != root
                or candidate.name.startswith(".")
                or candidate.is_symlink()
            ):
                raise cache.CacheError("cache_staging_invalid")
            if candidate.exists():
                shutil.rmtree(candidate)
            with suppress(OSError):
                root.rmdir()
            self._staging_cleanup_done = True
        except cache.CacheError:
            raise
        except OSError as error:
            raise cache.CacheError("cache_staging_cleanup_failed") from error

    def finish(self, identity: Mapping[str, object]) -> Path:
        primary_error: BaseException | None = None
        try:
            if self.staging_root is None:
                return self.cache.promote(
                    self.staging,
                    self.target,
                    identity=identity,
                    reservation=self.reservation,
                )
            return self.cache.promote_from_tmpfs(
                self.staging,
                self.target,
                identity=identity,
                reservation=self.reservation,
            )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            self._stop_lease()
            try:
                self._remove_tmpfs_staging()
            except cache.CacheError as error:
                if primary_error is None:
                    raise
                logger.warning(
                    "version cache staging cleanup failed after primary build error (%s)",
                    error.code,
                )

    def abort(self) -> None:
        self._stop_lease()
        try:
            self._remove_tmpfs_staging()
        except cache.CacheError as error:
            logger.warning("version cache staging cleanup failed during abort (%s)", error.code)
        finally:
            try:
                self.reservation.release()
            except cache.CacheError as error:
                logger.warning(
                    "version cache reservation release failed during abort (%s)",
                    error.code,
                )


def _begin_version_build(
    runtime_root: Path,
    adapter_id: int,
    version_id: int,
    *,
    identity: Mapping[str, object],
    dependency_context: DependencyExecutionContext | None = None,
) -> tuple[VerifiedVersionCache, Path, _VersionBuild | None]:
    version_cache = VerifiedVersionCache(runtime_root / "version-cache")
    target = version_dir(runtime_root, adapter_id, version_id)
    if version_cache.verify(target, identity):
        return version_cache, target, None
    if target.exists():
        version_cache.remove_entry(target)
    reservation = version_cache.reserve(_CACHE_RESERVATION_BYTES)
    staging_root: Path | None = None
    if dependency_context is None:
        staging = version_cache.staging_path(f"{adapter_id}-{version_id}", reservation.token)
    else:
        staging_root = dependency_context.tmpdir / "version-builds"
        try:
            staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if staging_root.is_symlink():
                raise OSError("dependency staging root must not be a symlink")
            staging = staging_root / f"{adapter_id}-{version_id}-{reservation.token}"
        except OSError as error:
            reservation.release()
            raise cache.CacheError("cache_staging_create_failed") from error
    try:
        staging.mkdir(mode=0o700)
    except OSError as error:
        reservation.release()
        raise cache.CacheError("cache_staging_create_failed") from error
    return (
        version_cache,
        target,
        _VersionBuild(
            version_cache,
            staging,
            target,
            reservation,
            staging_root,
        ),
    )


def venv_python(directory: Path) -> Path:
    return directory / ".venv" / "bin" / "python"


def _partial_log(error: subprocess.TimeoutExpired) -> str:
    """Best-effort decode of whatever output a timed-out command produced."""
    chunks: list[str] = []
    for chunk in (error.stdout, error.stderr):
        if isinstance(chunk, bytes):
            chunks.append(chunk.decode(errors="replace"))
        elif chunk:
            chunks.append(chunk)
    return "".join(chunks)


def _dependency_resource_error(context: DependencyExecutionContext | None) -> str | None:
    """Map an Attempt's bounded kernel counters to a stable dependency code."""
    if context is None:
        return None
    try:
        events = {}
        for line in (
            (context.cgroup_path / "memory.events").read_text(encoding="ascii").splitlines()
        ):
            key, separator, value = line.partition(" ")
            if separator:
                with suppress(ValueError):
                    events[key] = int(value.strip())
        if events.get("oom_kill", 0) or events.get("oom", 0):
            return "resource_exceeded_memory"
    except OSError:
        pass
    try:
        events = {}
        for line in (context.cgroup_path / "pids.events").read_text(encoding="ascii").splitlines():
            key, separator, value = line.partition(" ")
            if separator:
                with suppress(ValueError):
                    events[key] = int(value.strip())
        if events.get("max", 0):
            return "resource_exceeded_pids"
    except OSError:
        pass
    try:
        if os.statvfs(context.tmpdir).f_bavail == 0:
            return "resource_exceeded_disk"
    except OSError:
        pass
    return None


def _run_logged(
    command: list[str],
    timeout_seconds: int,
    *,
    context: DependencyExecutionContext | None = None,
) -> str:
    """Run a dependency command with bounded output and optional Attempt join."""
    env = _dependency_env()
    if context is not None:
        context.tmpdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        dependency_home = context.tmpdir / ".dependency-home"
        package_cache = context.tmpdir / ".package-cache"
        dependency_home.mkdir(mode=0o700, exist_ok=True)
        package_cache.mkdir(mode=0o700, exist_ok=True)
        env.update(
            {
                # Keep every ordinary package-manager write in the same
                # bounded Attempt tmpfs as the build staging tree.  A cache
                # miss must not grow /root/.cache, /root/.npm or /root/.m2
                # outside the Resource Profile's disk boundary.
                "HOME": str(dependency_home),
                "XDG_CACHE_HOME": str(package_cache / "xdg"),
                "UV_CACHE_DIR": str(package_cache / "uv"),
                "npm_config_cache": str(package_cache / "npm"),
                "TMPDIR": str(context.tmpdir),
                "TMP": str(context.tmpdir),
                "TEMP": str(context.tmpdir),
            }
        )
    sensitive_values = [
        value for argument in command for value in package_index_secret_values(argument)
    ]

    def assert_reservation() -> None:
        if context is None:
            return
        if context.reservation_lost is not None and context.reservation_lost.is_set():
            raise cache.CacheError("cache_reservation_expired")
        if context.reservation_check is not None:
            context.reservation_check()

    if subprocess.run is not _ORIGINAL_SUBPROCESS_RUN:
        # Compatibility seam for existing in-process tests and embedders that
        # replace the runner.  Production never enters this branch.
        try:
            assert_reservation()
            completed = subprocess.run(  # noqa: S603 - fixed dependency command list
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env=env,
            )
            assert_reservation()
        except cache.CacheError as error:
            raise DependencyPreparationError(
                "dependency cache reservation is no longer active",
                "",
                error_code="dependency_cache_reservation_expired",
            ) from error
        except subprocess.TimeoutExpired as error:
            raise DependencyPreparationError(
                f"{' '.join(command[:3])} timed out after {timeout_seconds}s",
                _redact_sensitive(_partial_log(error), sensitive_values),
                error_code="dependency_timeout",
            ) from error
        log = _redact_sensitive(completed.stdout + completed.stderr, sensitive_values)
        if completed.returncode != 0:
            raise DependencyPreparationError(f"{' '.join(command[:3])} failed", log)
        return log

    selector = selectors.DefaultSelector()
    process: subprocess.Popen[bytes] | None = None
    gate_read_fd: int | None = None
    gate_write_fd: int | None = None
    log_limit = context.log_max_bytes if context is not None else 1 * 1024 * 1024
    ring = bytearray()
    total_bytes = 0
    truncated = False

    def append_output(chunk: bytes) -> None:
        nonlocal total_bytes, truncated
        total_bytes += len(chunk)
        if len(ring) < log_limit:
            remaining = log_limit - len(ring)
            ring.extend(chunk[:remaining])
        if total_bytes > log_limit:
            truncated = True

    def terminate() -> None:
        if context is not None:
            try:
                (context.cgroup_path / "cgroup.kill").write_text("1\n", encoding="ascii")
            except OSError:
                logger.warning("dependency cgroup kill failed", exc_info=True)
        if process is not None and process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except OSError:
                process.kill()

    try:
        assert_reservation()
        pass_fds: tuple[int, ...] = ()
        launch_command = command
        if context is not None:
            gate_read_fd, gate_write_fd = os.pipe()
            os.set_inheritable(gate_read_fd, True)
            pass_fds = (gate_read_fd,)
            launch_command = _dependency_helper_command(command, gate_read_fd, context.nofile)
        process = subprocess.Popen(  # noqa: S603 - fixed dependency command list
            launch_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            pass_fds=pass_fds,
        )
        if gate_read_fd is not None:
            os.close(gate_read_fd)
            gate_read_fd = None
        if context is not None:
            assert gate_write_fd is not None
            with (context.cgroup_path / "cgroup.procs").open("w", encoding="ascii") as stream:
                stream.write(f"{process.pid}\n")
            members = (context.cgroup_path / "cgroup.procs").read_text(encoding="ascii").split()
            if str(process.pid) not in members:
                raise OSError("dependency process cgroup membership readback failed")
            assert_reservation()
            os.write(gate_write_fd, b"1")
            os.close(gate_write_fd)
            gate_write_fd = None
        assert process.stdout is not None
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_seconds
        while selector.get_map():
            assert_reservation()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate()
                process.wait()
                raise DependencyPreparationError(
                    f"{' '.join(command[:3])} timed out after {timeout_seconds}s",
                    _redact_sensitive(bytes(ring).decode(errors="replace"), sensitive_values),
                    error_code="dependency_timeout",
                )
            for key, _ in selector.select(min(0.25, remaining)):
                stream = cast(Any, key.fileobj)
                reader = getattr(stream, "read1", None)
                chunk = (
                    cast(bytes, reader(_DEPENDENCY_READ_CHUNK))
                    if callable(reader)
                    else os.read(stream.fileno(), _DEPENDENCY_READ_CHUNK)
                )
                if chunk:
                    append_output(chunk)
                else:
                    selector.unregister(key.fileobj)
        returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        assert_reservation()
    except subprocess.TimeoutExpired as error:
        terminate()
        if process is not None:
            process.wait()
        raise DependencyPreparationError(
            f"{' '.join(command[:3])} timed out after {timeout_seconds}s",
            _redact_sensitive(bytes(ring).decode(errors="replace"), sensitive_values),
            error_code="dependency_timeout",
        ) from error
    except cache.CacheError as error:
        terminate()
        if process is not None:
            process.wait()
        raise DependencyPreparationError(
            "dependency cache reservation is no longer active",
            _redact_sensitive(bytes(ring).decode(errors="replace"), sensitive_values),
            error_code="dependency_cache_reservation_expired",
        ) from error
    except (OSError, subprocess.SubprocessError) as error:
        terminate()
        raise DependencyPreparationError(
            f"{' '.join(command[:3])} failed",
            _redact_sensitive(bytes(ring).decode(errors="replace"), sensitive_values),
            error_code="dependency_sandbox_failed",
        ) from error
    finally:
        if gate_read_fd is not None:
            with suppress(OSError):
                os.close(gate_read_fd)
        if gate_write_fd is not None:
            with suppress(OSError):
                os.close(gate_write_fd)
        if process is not None and process.poll() is None:
            terminate()
            process.wait()
        selector.close()
    log = _redact_sensitive(bytes(ring).decode(errors="replace"), sensitive_values)
    if truncated:
        marker = f"\n...[truncated dependency log after {total_bytes} bytes]...\n".encode("ascii")
        if len(marker) >= log_limit:
            log = marker[:log_limit].decode("ascii", errors="replace")
        else:
            prefix = bytes(ring)[: log_limit - len(marker)]
            log = (prefix + marker).decode("utf-8", errors="replace")
    if returncode != 0:
        raise DependencyPreparationError(
            f"{' '.join(command[:3])} failed",
            log,
            error_code=_dependency_resource_error(context) or "dependency_preparation_failed",
        )
    return log


def prepare_version_venv(
    runtime_root: Path,
    adapter_id: int,
    version_id: int,
    requirements: str,
    *,
    timeout_seconds: int,
    index_url: str | None = None,
    dependency_log: DependencyLogCallback | None = None,
    dependency_context: DependencyExecutionContext | None = None,
) -> Path:
    """Return the venv Python path, building the venv on first use."""
    directory = version_dir(runtime_root, adapter_id, version_id)
    python_path = venv_python(directory)
    dependencies = dependency_specs(requirements)
    with _lock_for(adapter_id, version_id):
        identity = _cache_identity(adapter_id, version_id, "python", requirements)
        try:
            _version_cache, directory, build = _begin_version_build(
                runtime_root,
                adapter_id,
                version_id,
                identity=identity,
                dependency_context=dependency_context,
            )
        except cache.CacheError as error:
            raise DependencyPreparationError("version cache is unavailable", "") from error
        if build is None:
            python_path = venv_python(directory)
            if python_path.is_file():
                if dependency_log is not None:
                    for dependency in dependencies:
                        dependency_log(f"{dependency} 已安装，检查通过")
                return python_path
            # A manifest can still match when a cached venv contains a
            # dangling interpreter symlink.  Remove that unusable entry and
            # reserve a fresh build instead of escaping through an assertion.
            try:
                _version_cache.remove_entry(directory)
                _version_cache, directory, build = _begin_version_build(
                    runtime_root,
                    adapter_id,
                    version_id,
                    identity=identity,
                    dependency_context=dependency_context,
                )
            except cache.CacheError as error:
                raise DependencyPreparationError("version cache is unavailable", "") from error
        if build is None:  # pragma: no cover - removal above makes this unreachable
            raise DependencyPreparationError("version cache is unavailable", "")
        if dependency_context is not None:
            dependency_context = dependency_context.with_reservation(
                build.assert_live, build.lease_lost
            )
        try:
            build.assert_live()
        except cache.CacheError as error:
            build.abort()
            raise DependencyPreparationError(
                "dependency cache reservation is no longer active",
                "",
                error_code="dependency_cache_reservation_expired",
            ) from error
        directory = build.staging
        python_path = venv_python(directory)
        try:
            (directory / "requirements.txt").write_text(requirements, encoding="utf-8")
        except OSError as error:
            build.abort()
            raise DependencyPreparationError("version cache staging failed", "") from error

        install_log = ""
        try:
            install_log += _run_logged_in_context(
                ["uv", "venv", str(directory / ".venv")],
                timeout_seconds,
                dependency_context,
            )
            if requirements.strip():
                if dependency_log is not None:
                    for dependency in dependencies:
                        dependency_log(f"{dependency} 未安装，开始安装")
                base_command = [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python_path),
                    "-r",
                    str(directory / "requirements.txt"),
                ]
                # Offline-first: a warm local cache must not need any network.
                try:
                    install_log += _run_install_logged_in_context(
                        base_command + ["--offline"],
                        timeout_seconds,
                        dependency_context,
                    )
                except DependencyPreparationError as offline_error:
                    if not index_url:
                        raise DependencyPreparationError(
                            "dependencies are not available from the local cache and no "
                            "package source is configured; ask the platform admin to add "
                            "a package source in System Settings (or set DLR_PYPI_INDEX_URL "
                            "on the Worker)",
                            offline_error.install_log,
                            dependency=dependency_failure_label(
                                dependencies, offline_error.install_log
                            ),
                            no_source=True,
                            error_code=offline_error.error_code,
                        ) from offline_error
                    install_log += offline_error.install_log
                    install_log += f"\n{OFFLINE_CACHE_FALLBACK_MARKER}\n"
                    try:
                        install_log += _run_install_logged_in_context(
                            base_command + ["--index-url", index_url],
                            timeout_seconds,
                            dependency_context,
                        )
                    except DependencyPreparationError as source_error:
                        raise DependencyPreparationError(
                            str(source_error),
                            install_log + source_error.install_log,
                            dependency=dependency_failure_label(
                                dependencies, install_log + source_error.install_log
                            ),
                            error_code=source_error.error_code,
                        ) from source_error
                if dependency_log is not None:
                    for dependency in dependencies:
                        dependency_log(f"{dependency} 安装成功")
        except DependencyPreparationError:
            # Leave no half-built staging entry behind; next attempt rebuilds
            # cleanly without publishing an unverified runtime.
            build.abort()
            raise
        try:
            final_directory = build.finish(identity)
        except cache.CacheError as error:
            build.abort()
            raise DependencyPreparationError("version cache promotion failed", "") from error
        logger.info("venv ready for adapter %s version %s", adapter_id, version_id)
        return venv_python(final_directory)


def cleanup_stale_venvs(runtime_root: Path, adapter_id: int, keep_version_ids: set[int]) -> None:
    """Best-effort removal of venvs for versions that are no longer needed.

    Failures only land in the Worker log; cleanup never affects Execution
    outcome. Kept versions are rebuilt lazily if executed again later.
    """
    base = runtime_root / "version-cache" / "entries"
    if base.exists():
        for child in base.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            prefix, separator, raw_version = child.name.partition("-")
            if not separator or prefix != str(adapter_id):
                continue
            try:
                version_id = int(raw_version)
            except ValueError:
                continue
            if version_id in keep_version_ids:
                continue
            try:
                cache.VerifiedVersionCache(runtime_root / "version-cache").remove_entry(child)
            except cache.CacheError:
                logger.warning("could not clean stale venv for adapter %s", adapter_id)
            else:
                logger.info("cleaned stale venv for adapter %s version %s", adapter_id, version_id)


def cleanup_adapter_environment(runtime_root: Path, adapter_id: int) -> None:
    """Remove only one Adapter's private runtime tree.

    The shared ``uv``/npm/Maven caches live outside this path and are never
    removed by permanent Adapter deletion.
    """
    base = runtime_root / "version-cache" / "entries"
    prefix = f"{adapter_id}-"
    if base.is_dir():
        for child in base.iterdir():
            if child.name.startswith(prefix):
                try:
                    cache.VerifiedVersionCache(runtime_root / "version-cache").remove_entry(child)
                except cache.CacheError:
                    logger.warning("could not clean runtime environment for adapter %s", adapter_id)
    # Remove the pre-cache layout left by older Workers as well; this is the
    # exact Adapter subtree, never the shared package cache.
    base = runtime_root / "adapters" / str(adapter_id)
    if base.is_symlink() or base.is_file():
        base.unlink(missing_ok=True)
        return
    if base.is_dir():
        shutil.rmtree(base)
        logger.info("cleaned runtime environment for deleted adapter %s", adapter_id)


def _helper_main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--dependency-child", action="store_true")
    parser.add_argument("--command", required=True)
    parser.add_argument("--ready-fd", required=True, type=int)
    parser.add_argument("--nofile", required=True, type=int)
    args = parser.parse_args(argv)
    if not args.dependency_child:
        return 125
    try:
        command = json.loads(base64.urlsafe_b64decode(args.command.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return 125
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) for item in command)
    ):
        return 125
    return _dependency_child(command, args.ready_fd, args.nofile)


if __name__ == "__main__":
    raise SystemExit(_helper_main(sys.argv[1:]))
