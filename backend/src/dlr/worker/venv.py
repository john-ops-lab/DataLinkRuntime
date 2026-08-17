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

import logging
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib import parse as url_parse

logger = logging.getLogger("dlr.worker.venv")

DependencyLogCallback = Callable[[str], None]

_build_locks: dict[tuple[int, int], threading.Lock] = {}
_build_locks_guard = threading.Lock()

_URI_USERINFO_PATTERN = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^\s/?#@]+@")

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


def classify_dependency_install_error(log: str) -> str | None:
    """Map common dependency-install failure lines to an actionable hint.

    Category order matters: DNS first, then transport, then TLS, then
    authentication, then repository/package availability. Returns a Chinese
    hint naming the failing layer, or None when the log matches nothing.
    """
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
        return (
            "依赖源域名解析失败（DNS）：请检查容器的 DNS 配置；企业网络 / VPN 下"
            "可设置 DLR_DNS_SERVERS 指定可用 DNS（详见 README「容器网络与 DNS 排障」）"
        )
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
        return "依赖源网络不可达：请检查容器出站网络、防火墙与代理设置后重试"
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
        return "依赖源 TLS 握手或证书校验失败：请确认源地址证书有效，或按部署要求配置可信 CA"
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
        return "依赖源认证失败：请检查该依赖源绑定的凭据是否正确、有效"
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
        return "包或制品不存在：请检查依赖名称与版本是否真实存在于该依赖源"
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
        return "依赖源仓库不存在或不可用：请检查仓库地址是否正确，或改用其他镜像源"
    return None


def _run_install_logged(command: list[str], timeout_seconds: int) -> str:
    """Run a dependency-install command with an actionable error hint appended."""
    try:
        return _run_logged(command, timeout_seconds)
    except DependencyPreparationError as error:
        hint = classify_dependency_install_error(error.install_log)
        message = error.args[0]
        if hint is not None and hint not in message:
            raise DependencyPreparationError(f"{message}；{hint}", error.install_log) from error
        raise


class DependencyPreparationError(Exception):
    """venv creation or dependency installation failed."""

    def __init__(
        self,
        message: str,
        install_log: str,
        dependency: str | None = None,
    ) -> None:
        super().__init__(message)
        self.install_log = install_log
        self.dependency = dependency


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
    sensitive_values = [
        value for argument in command for value in package_index_secret_values(argument)
    ]
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
            _redact_sensitive(_partial_log(error), sensitive_values),
        ) from error
    log = _redact_sensitive(completed.stdout + completed.stderr, sensitive_values)
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
    dependency_log: DependencyLogCallback | None = None,
) -> Path:
    """Return the venv Python path, building the venv on first use."""
    directory = version_dir(runtime_root, adapter_id, version_id)
    python_path = venv_python(directory)
    dependencies = dependency_specs(requirements)
    with _lock_for(adapter_id, version_id):
        if (directory / ".ready").exists() and python_path.exists():
            if dependency_log is not None:
                for dependency in dependencies:
                    dependency_log(f"{dependency} 已安装，检查通过")
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
                    install_log += _run_install_logged(
                        base_command + ["--offline"], timeout_seconds
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
                        ) from offline_error
                    install_log += offline_error.install_log
                    install_log += (
                        "\n[offline cache insufficient; retrying with the configured "
                        "package source]\n"
                    )
                    try:
                        install_log += _run_install_logged(
                            base_command + ["--index-url", index_url], timeout_seconds
                        )
                    except DependencyPreparationError as source_error:
                        raise DependencyPreparationError(
                            str(source_error),
                            install_log + source_error.install_log,
                            dependency=dependency_failure_label(
                                dependencies, install_log + source_error.install_log
                            ),
                        ) from source_error
                if dependency_log is not None:
                    for dependency in dependencies:
                        dependency_log(f"{dependency} 安装成功")
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
