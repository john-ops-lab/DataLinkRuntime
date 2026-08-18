"""Version-scoped Node.js dependency environments."""

import base64
import json
import shutil
import tempfile
import threading
from pathlib import Path
from urllib import parse as url_parse

from dlr.worker import venv

_locks: dict[tuple[int, int], threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(adapter_id: int, version_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault((adapter_id, version_id), threading.Lock())


def parse_requirements(requirements: str) -> dict[str, str]:
    dependencies: dict[str, str] = {}
    for number, raw in enumerate(requirements.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        split_at = line.rfind("@")
        if split_at <= 0 or split_at == len(line) - 1:
            raise venv.DependencyPreparationError(
                f"invalid npm dependency on line {number}; expected package@version",
                "",
            )
        name, version = line[:split_at], line[split_at + 1 :]
        if name.startswith("@") and "/" not in name:
            raise venv.DependencyPreparationError(
                f"invalid scoped npm dependency on line {number}", ""
            )
        dependencies[name] = version
    return dependencies


def _npm_auth(registry_url: str) -> tuple[str, str | None]:
    parts = url_parse.urlsplit(registry_url)
    if "@" not in parts.netloc:
        return registry_url, None
    host = parts.netloc.rsplit("@", 1)[1]
    clean_url = url_parse.urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    scope = f"//{host}{parts.path.rstrip('/')}/:"
    username = url_parse.unquote(parts.username or "")
    password = url_parse.unquote(parts.password or "")
    if password == "":
        return clean_url, f"{scope}_authToken={username}\n"
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return clean_url, f"{scope}_auth={encoded}\n{scope}always-auth=true\n"


def prepare_version_node(
    runtime_root: Path,
    adapter_id: int,
    version_id: int,
    code: str,
    requirements: str,
    *,
    timeout_seconds: int,
    registry_url: str | None,
    dependency_log: venv.DependencyLogCallback | None = None,
) -> Path:
    directory = venv.version_dir(runtime_root, adapter_id, version_id)
    dependencies = parse_requirements(requirements)
    with _lock_for(adapter_id, version_id):
        if (directory / ".ready").exists() and (directory / "adapter.mjs").exists():
            if dependency_log is not None:
                for name, version in dependencies.items():
                    dependency_log(f"{name}@{version} 已安装，检查通过")
            return directory
        if shutil.which("node") is None:
            raise venv.DependencyPreparationError("Node.js Runtime is unavailable", "")
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)
        dependencies = parse_requirements(requirements)
        (directory / "adapter.mjs").write_text(code, encoding="utf-8")
        package = {"private": True, "type": "module", "dependencies": dependencies}
        (directory / "package.json").write_text(
            json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (directory / "node_modules").mkdir(exist_ok=True)
        if dependencies:
            if shutil.which("npm") is None:
                raise venv.DependencyPreparationError("npm Runtime is unavailable", "")
            clean_registry = None
            npmrc = None
            try:
                if registry_url:
                    clean_registry, auth_config = _npm_auth(registry_url)
                    if auth_config:
                        with tempfile.NamedTemporaryFile(
                            mode="w",
                            encoding="utf-8",
                            prefix="dlr-npm-",
                            suffix=".npmrc",
                            delete=False,
                        ) as handle:
                            handle.write(auth_config)
                            npmrc = Path(handle.name)
                        npmrc.chmod(0o600)
                command = [
                    "npm",
                    "install",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    "--prefix",
                    str(directory),
                ]
                if npmrc is not None:
                    command.extend(["--userconfig", str(npmrc)])
                if dependency_log is not None:
                    for name, version in dependencies.items():
                        dependency_log(f"{name}@{version} 未安装，开始安装")
                try:
                    venv._run_install_logged(command + ["--offline"], timeout_seconds)
                except venv.DependencyPreparationError as offline_error:
                    if not registry_url:
                        raise venv.DependencyPreparationError(
                            "npm dependencies are not available from the local cache and "
                            "no npm dependency source is configured",
                            offline_error.install_log,
                            dependency=venv.dependency_failure_label(
                                (f"{name}@{version}" for name, version in dependencies.items()),
                                offline_error.install_log,
                            ),
                            no_source=True,
                        ) from offline_error
                    assert clean_registry is not None
                    try:
                        venv._run_install_logged(
                            command + ["--registry", clean_registry], timeout_seconds
                        )
                    except venv.DependencyPreparationError as source_error:
                        combined_log = offline_error.install_log + source_error.install_log
                        raise venv.DependencyPreparationError(
                            str(source_error),
                            combined_log,
                            dependency=venv.dependency_failure_label(
                                (f"{name}@{version}" for name, version in dependencies.items()),
                                combined_log,
                            ),
                        ) from source_error
                if dependency_log is not None:
                    for name, version in dependencies.items():
                        dependency_log(f"{name}@{version} 安装成功")
            except venv.DependencyPreparationError:
                shutil.rmtree(directory, ignore_errors=True)
                raise
            finally:
                if npmrc is not None:
                    npmrc.unlink(missing_ok=True)
        (directory / ".ready").write_text("ready", encoding="utf-8")
        return directory
