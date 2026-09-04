"""Version-scoped Java dependency and compilation environments."""

import html
import shutil
import tempfile
import threading
from pathlib import Path
from urllib import parse as url_parse

from dlr.runtime.java_runtime import SOURCE as RUNTIME_SOURCE
from dlr.worker import venv
from dlr.worker.cache import CacheError

_locks: dict[tuple[int, int], threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(adapter_id: int, version_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault((adapter_id, version_id), threading.Lock())


def parse_requirements(requirements: str) -> list[tuple[str, str, str]]:
    dependencies: list[tuple[str, str, str]] = []
    for number, raw in enumerate(requirements.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != 3 or any(not part for part in parts):
            raise venv.DependencyPreparationError(
                f"invalid Maven dependency on line {number}; expected groupId:artifactId:version",
                "",
            )
        dependencies.append((parts[0], parts[1], parts[2]))
    return dependencies


def _pom(dependencies: list[tuple[str, str, str]]) -> str:
    dependencies_xml = "".join(
        "<dependency><groupId>{}</groupId><artifactId>{}</artifactId>"
        "<version>{}</version></dependency>".format(*(html.escape(value) for value in dep))
        for dep in dependencies
    )
    return (
        '<project xmlns="http://maven.apache.org/POM/4.0.0">'
        "<modelVersion>4.0.0</modelVersion><groupId>dlr.runtime</groupId>"
        "<artifactId>adapter</artifactId><version>1</version>"
        f"<dependencies>{dependencies_xml}</dependencies></project>"
    )


def _maven_settings(repository_url: str) -> tuple[str, str]:
    parts = url_parse.urlsplit(repository_url)
    host = parts.netloc.rsplit("@", 1)[-1]
    clean_url = url_parse.urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    mirror_id = "dlr-mirror"
    mirror = (
        "<mirrors><mirror>"
        f"<id>{mirror_id}</id><url>{html.escape(clean_url)}</url>"
        "<mirrorOf>*</mirrorOf>"
        "</mirror></mirrors>"
    )
    server = ""
    if "@" in parts.netloc:
        username = html.escape(url_parse.unquote(parts.username or ""))
        password = html.escape(url_parse.unquote(parts.password or ""))
        server = (
            "<servers><server>"
            f"<id>{mirror_id}</id><username>{username}</username>"
            f"<password>{password}</password>"
            "</server></servers>"
        )
    return clean_url, f"<settings>{mirror}{server}</settings>"


def prepare_version_java(
    runtime_root: Path,
    adapter_id: int,
    version_id: int,
    code: str,
    requirements: str,
    *,
    timeout_seconds: int,
    repository_url: str | None,
    dependency_log: venv.DependencyLogCallback | None = None,
    dependency_context: venv.DependencyExecutionContext | None = None,
) -> Path:
    directory = venv.version_dir(runtime_root, adapter_id, version_id)
    classes = directory / "classes"
    dependencies = parse_requirements(requirements)
    with _lock_for(adapter_id, version_id):
        identity = venv._cache_identity(adapter_id, version_id, "java", f"{code}\0{requirements}")
        try:
            _version_cache, directory, build = venv._begin_version_build(
                runtime_root,
                adapter_id,
                version_id,
                identity=identity,
                dependency_context=dependency_context,
            )
        except CacheError as error:
            raise venv.DependencyPreparationError("version cache is unavailable", "") from error
        classes = directory / "classes"
        if build is None and (classes / "Adapter.class").exists():
            if dependency_log is not None:
                for group, artifact, version in dependencies:
                    dependency_log(f"{group}:{artifact}:{version} 已安装，检查通过")
            return directory
        assert build is not None
        directory = build.staging
        classes = directory / "classes"
        for command in ("java", "javac"):
            if shutil.which(command) is None:
                build.abort()
                raise venv.DependencyPreparationError(f"{command} Runtime is unavailable", "")
        deps = directory / "deps"
        try:
            deps.mkdir(parents=True, exist_ok=True)
            classes.mkdir(parents=True, exist_ok=True)
            (directory / "Adapter.java").write_text(code, encoding="utf-8")
            (directory / "DlrRuntime.java").write_text(RUNTIME_SOURCE, encoding="utf-8")
        except OSError as error:
            build.abort()
            raise venv.DependencyPreparationError("version cache staging failed", "") from error
        settings_path = None
        try:
            (directory / "pom.xml").write_text(_pom(dependencies), encoding="utf-8")
        except OSError as error:
            build.abort()
            raise venv.DependencyPreparationError("version cache staging failed", "") from error
        try:
            if dependencies:
                if shutil.which("mvn") is None:
                    raise venv.DependencyPreparationError("Maven Runtime is unavailable", "")
                if repository_url:
                    _, settings_xml = _maven_settings(repository_url)
                    with tempfile.NamedTemporaryFile(
                        mode="w",
                        encoding="utf-8",
                        prefix="dlr-maven-",
                        suffix=".xml",
                        delete=False,
                    ) as handle:
                        handle.write(settings_xml)
                        settings_path = Path(handle.name)
                    settings_path.chmod(0o600)
                base = [
                    "mvn",
                    "-q",
                    "-f",
                    str(directory / "pom.xml"),
                ]
                if settings_path is not None:
                    base.extend(["-s", str(settings_path)])
                base.extend(["dependency:copy-dependencies", f"-DoutputDirectory={deps}"])
                if dependency_log is not None:
                    for group, artifact, version in dependencies:
                        dependency_log(f"{group}:{artifact}:{version} 未安装，开始安装")
                try:
                    venv._run_install_logged_in_context(
                        base + ["-o"], timeout_seconds, dependency_context
                    )
                except venv.DependencyPreparationError as offline_error:
                    if not repository_url:
                        raise venv.DependencyPreparationError(
                            "Maven dependencies are not available from the local "
                            "repository and no Maven dependency source is configured",
                            offline_error.install_log,
                            dependency=venv.dependency_failure_label(
                                (":".join(parts) for parts in dependencies),
                                offline_error.install_log,
                            ),
                            no_source=True,
                            error_code=offline_error.error_code,
                        ) from offline_error
                    try:
                        venv._run_install_logged_in_context(
                            base, timeout_seconds, dependency_context
                        )
                    except venv.DependencyPreparationError as source_error:
                        combined_log = offline_error.install_log + source_error.install_log
                        raise venv.DependencyPreparationError(
                            str(source_error),
                            combined_log,
                            dependency=venv.dependency_failure_label(
                                (":".join(parts) for parts in dependencies), combined_log
                            ),
                            error_code=source_error.error_code,
                        ) from source_error
                if dependency_log is not None:
                    for group, artifact, version in dependencies:
                        dependency_log(f"{group}:{artifact}:{version} 安装成功")
            venv._run_logged_in_context(
                [
                    "javac",
                    "--release",
                    "21",
                    "-cp",
                    str(deps / "*"),
                    "-d",
                    str(classes),
                    str(directory / "Adapter.java"),
                    str(directory / "DlrRuntime.java"),
                ],
                timeout_seconds,
                dependency_context,
            )
        except venv.DependencyPreparationError:
            build.abort()
            raise
        finally:
            if settings_path is not None:
                settings_path.unlink(missing_ok=True)
        try:
            return build.finish(identity)
        except CacheError as error:
            build.abort()
            raise venv.DependencyPreparationError("version cache promotion failed", "") from error
