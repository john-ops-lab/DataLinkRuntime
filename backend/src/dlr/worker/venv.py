"""Version-scoped virtual environments (M2 spec §9).

One independent venv per AdapterVersion, lazily built on first execution
with ``uv venv`` + ``uv pip install``. A ``.ready`` marker is written only
after dependencies are fully prepared; an incomplete directory is removed
and rebuilt. Within one Worker, concurrent first runs of the same Version
share a lightweight in-process lock.
"""

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger("dlr.worker.venv")

_build_locks: dict[tuple[int, int], threading.Lock] = {}
_build_locks_guard = threading.Lock()

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


def _redact_sensitive(text: str) -> str:
    """Defensively redact platform credentials from dependency logs.

    Scans for common sensitive patterns (tokens, database URLs, secrets) and
    replaces them with [REDACTED]. This is a safety net on top of the minimal
    environment; even if a build script somehow echoes a sensitive value, it
    will not be persisted to Execution.stderr.
    """
    import re

    redacted = text
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


class DependencyPreparationError(Exception):
    """venv creation or dependency installation failed."""

    def __init__(self, message: str, install_log: str) -> None:
        super().__init__(message)
        self.install_log = install_log


def _lock_for(adapter_id: int, version_id: int) -> threading.Lock:
    with _build_locks_guard:
        lock = _build_locks.get((adapter_id, version_id))
        if lock is None:
            lock = threading.Lock()
            _build_locks[(adapter_id, version_id)] = lock
        return lock


def version_dir(runtime_root: Path, adapter_id: int, version_id: int) -> Path:
    return runtime_root / "adapters" / str(adapter_id) / "versions" / str(version_id)


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


def _run_logged(command: list[str], timeout_seconds: int) -> str:
    """Run a command with a minimal environment, returning its redacted combined output."""
    env = _dependency_env()
    try:
        completed = subprocess.run(  # noqa: S603 - fixed uv command list
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise DependencyPreparationError(
            f"{' '.join(command[:3])} timed out after {timeout_seconds}s",
            _redact_sensitive(_partial_log(error)),
        ) from error
    log = _redact_sensitive(completed.stdout + completed.stderr)
    if completed.returncode != 0:
        raise DependencyPreparationError(f"{' '.join(command[:3])} failed", log)
    return log


def prepare_version_venv(
    runtime_root: Path,
    adapter_id: int,
    version_id: int,
    requirements: str,
    *,
    timeout_seconds: int,
    index_url: str | None = None,
) -> Path:
    """Return the venv Python path, building the venv on first use."""
    directory = version_dir(runtime_root, adapter_id, version_id)
    python_path = venv_python(directory)
    with _lock_for(adapter_id, version_id):
        if (directory / ".ready").exists() and python_path.exists():
            return python_path
        # Incomplete leftovers (no .ready marker) are rebuilt from scratch.
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "requirements.txt").write_text(requirements, encoding="utf-8")

        install_log = ""
        try:
            install_log += _run_logged(["uv", "venv", str(directory / ".venv")], timeout_seconds)
            if requirements.strip():
                command = [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python_path),
                    "-r",
                    str(directory / "requirements.txt"),
                ]
                if index_url:
                    command.extend(["--index-url", index_url])
                install_log += _run_logged(command, timeout_seconds)
        except DependencyPreparationError:
            # Leave no half-built venv behind; next attempt rebuilds cleanly.
            shutil.rmtree(directory, ignore_errors=True)
            raise
        (directory / ".ready").write_text("ready", encoding="utf-8")
        logger.info("venv ready for adapter %s version %s", adapter_id, version_id)
        return python_path


def cleanup_stale_venvs(runtime_root: Path, adapter_id: int, keep_version_ids: set[int]) -> None:
    """Best-effort removal of venvs for versions that are no longer needed.

    Failures only land in the Worker log; cleanup never affects Execution
    outcome. Kept versions are rebuilt lazily if executed again later.
    """
    base = runtime_root / "adapters" / str(adapter_id) / "versions"
    if not base.exists():
        return
    for child in base.iterdir():
        if not child.is_dir():
            continue
        try:
            version_id = int(child.name)
        except ValueError:
            continue
        if version_id in keep_version_ids:
            continue
        shutil.rmtree(child, ignore_errors=True)
        logger.info("cleaned stale venv for adapter %s version %s", adapter_id, version_id)
