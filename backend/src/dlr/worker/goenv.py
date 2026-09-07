"""Version-scoped Go Modules builds using the bounded dependency runner."""

import hashlib
import json
import platform
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from dlr.runtime.go_runtime import SOURCE
from dlr.worker import venv
from dlr.worker.cache import CacheError

if TYPE_CHECKING:
    from dlr.worker.builtin_packages import BuiltinMaterials

GO_VERSION = "go1.27.1"
BUILD_RESERVATION_BYTES = 1024 * 1024 * 1024
_MODULE = re.compile(r"[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)+")
_VERSION = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.+-]+)?")


def parse_requirements(requirements: str) -> dict[str, str]:
    dependencies: dict[str, str] = {}
    for number, raw in enumerate(requirements.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, version = line.rpartition("@")
        if (
            not separator
            or not _MODULE.fullmatch(name)
            or not _VERSION.fullmatch(version)
            or any(part in {".", ".."} for part in name.split("/"))
            or (name in dependencies and dependencies[name] != version)
        ):
            raise venv.DependencyPreparationError(
                f"invalid Go dependency on line {number}; expected module/path@vX.Y.Z", ""
            )
        dependencies[name] = version
    return dependencies


def prepare_version_go(
    runtime_root: Path,
    adapter_id: int,
    version_id: int,
    code: str,
    requirements: str,
    *,
    timeout_seconds: int,
    proxy_url: str | None,
    dependency_log: venv.DependencyLogCallback | None = None,
    dependency_context: venv.DependencyExecutionContext | None = None,
    builtin_materials: "BuiltinMaterials | None" = None,
) -> Path:
    dependencies = parse_requirements(requirements)
    compiler = shutil.which("go")
    if compiler is None:
        raise venv.DependencyPreparationError("Go Runtime is unavailable", "")
    if proxy_url:
        parts = urlsplit(proxy_url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or any(char in proxy_url for char in (",", "|", "\n", "\r"))
        ):
            raise venv.DependencyPreparationError("invalid Go module proxy URL", "")
    info = Path(compiler).resolve().stat()
    with venv._lock_for(adapter_id, version_id):
        identity = venv._cache_identity(
            adapter_id,
            version_id,
            "go",
            json.dumps(
                {
                    "code": code,
                    "requirements": requirements,
                    "source": builtin_materials.identity if builtin_materials else proxy_url,
                    "toolchain": (str(Path(compiler).resolve()), info.st_size, info.st_mtime_ns),
                    "platform": (platform.system(), platform.machine()),
                    "harness": hashlib.sha256(SOURCE.encode()).hexdigest(),
                    "build_contract": "pure-go-v1",
                },
                sort_keys=True,
            ),
        )
        try:
            _, directory, build = venv._begin_version_build(
                runtime_root,
                adapter_id,
                version_id,
                identity=identity,
                dependency_context=dependency_context,
                reservation_bytes=BUILD_RESERVATION_BYTES,
            )
        except CacheError as error:
            raise venv.DependencyPreparationError("version cache is unavailable", "") from error
        if build is None:
            if dependency_log:
                dependency_log("Go build cache verified")
            return directory
        if dependency_context is not None:
            dependency_context = dependency_context.with_reservation(
                build.assert_live, build.lease_lost
            )
        directory = build.staging
        try:
            (directory / "adapter.go").write_text(code, encoding="utf-8")
            (directory / "dlr_runtime.go").write_text(SOURCE, encoding="utf-8")
            module = "module dlr.local/adapter\n\ngo 1.23.0\n"
            if dependencies:
                module += (
                    "\nrequire (\n"
                    + "".join(f"\t{name} {version}\n" for name, version in dependencies.items())
                    + ")\n"
                )
            (directory / "go.mod").write_text(module, encoding="utf-8")
            module_cache, build_cache = directory / ".gomodcache", directory / ".gocache"
            module_cache.mkdir()
            build_cache.mkdir()

            def command(proxy: str, *args: str) -> list[str]:
                return [
                    "env",
                    "GOTOOLCHAIN=local",
                    "GOENV=off",
                    "GOWORK=off",
                    "GOFLAGS=-modcacherw",
                    "CGO_ENABLED=0",
                    "GOVCS=*:off",
                    "GOPRIVATE=",
                    "GONOPROXY=none",
                    "GOSUMDB=off",
                    "GONOSUMDB=*",
                    f"GOPROXY={proxy}",
                    f"GOMODCACHE={module_cache}",
                    f"GOCACHE={build_cache}",
                    "GOMAXPROCS=1",
                    "GOMEMLIMIT=128MiB",
                    "GOGC=50",
                    compiler,
                    "-C",
                    str(directory),
                    *args,
                ]

            if dependencies:
                if dependency_log:
                    for name, version in dependencies.items():
                        dependency_log(f"{name}@{version} 未安装，开始安装")
                if builtin_materials is not None:
                    from dlr.worker.builtin_packages import install_error

                    proxy = builtin_materials.materialize().as_uri()
                    try:
                        venv._run_install_logged_in_context(
                            command(proxy, "mod", "tidy"),
                            timeout_seconds,
                            dependency_context,
                        )
                    except venv.DependencyPreparationError as error:
                        raise install_error(error) from error
                else:
                    try:
                        venv._run_install_logged_in_context(
                            command("off", "mod", "tidy"),
                            timeout_seconds,
                            dependency_context,
                        )
                    except venv.DependencyPreparationError as offline_error:
                        if not proxy_url:
                            raise venv.DependencyPreparationError(
                                "Go modules unavailable and no Go source configured",
                                offline_error.install_log,
                                no_source=True,
                                error_code=offline_error.error_code,
                            ) from offline_error
                        venv._run_install_logged_in_context(
                            command(proxy_url, "mod", "tidy"),
                            timeout_seconds,
                            dependency_context,
                        )
            # No module downloads or automatic toolchain changes during compilation.
            venv._run_logged_in_context(
                command(
                    "off",
                    "build",
                    "-mod=readonly",
                    "-p=1",
                    "-trimpath",
                    "-buildvcs=false",
                    "-o",
                    str(directory / "adapter"),
                    ".",
                ),
                timeout_seconds,
                dependency_context,
            )
            # Module cache contains read-only files; Go's own cleanup handles them.
            venv._run_logged_in_context(
                command("off", "clean", "-modcache", "-cache"),
                timeout_seconds,
                dependency_context,
            )
            for path in (module_cache, build_cache):
                if path.exists():
                    shutil.rmtree(path)
            if dependency_log:
                dependency_log("Go compilation completed")
            return build.finish(identity)
        except (OSError, CacheError, venv.DependencyPreparationError) as error:
            build.abort()
            if isinstance(error, venv.DependencyPreparationError):
                raise
            raise venv.DependencyPreparationError(
                "Go build preparation failed", str(error)
            ) from error
