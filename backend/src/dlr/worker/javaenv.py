"""Version-scoped Java dependency and compilation environments."""

import html
import shutil
import tempfile
import threading
from pathlib import Path
from urllib import parse as url_parse

from dlr.runtime.java_runtime import SOURCE as RUNTIME_SOURCE
from dlr.worker import venv

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
) -> Path:
    directory = venv.version_dir(runtime_root, adapter_id, version_id)
    classes = directory / "classes"
    dependencies = parse_requirements(requirements)
    with _lock_for(adapter_id, version_id):
        if (directory / ".ready").exists() and (classes / "Adapter.class").exists():
            if dependency_log is not None:
                for group, artifact, version in dependencies:
                    dependency_log(f"{group}:{artifact}:{version} 已安装，检查通过")
            return directory
        for command in ("java", "javac"):
            if shutil.which(command) is None:
                raise venv.DependencyPreparationError(f"{command} Runtime is unavailable", "")
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)
        deps = directory / "deps"
        deps.mkdir(parents=True, exist_ok=True)
        classes.mkdir(parents=True, exist_ok=True)
        (directory / "Adapter.java").write_text(code, encoding="utf-8")
        (directory / "DlrRuntime.java").write_text(RUNTIME_SOURCE, encoding="utf-8")
        settings_path = None
        (directory / "pom.xml").write_text(_pom(dependencies), encoding="utf-8")
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
                for dependency_parts in dependencies:
                    dependency = ":".join(dependency_parts)
                    if dependency_log is not None:
                        dependency_log(f"{dependency} 未安装，开始安装")
                    (directory / "pom.xml").write_text(_pom([dependency_parts]), encoding="utf-8")
                    try:
                        try:
                            venv._run_install_logged(base + ["-o"], timeout_seconds)
                        except venv.DependencyPreparationError as offline_error:
                            if not repository_url:
                                raise venv.DependencyPreparationError(
                                    "Maven dependencies are not available from the local "
                                    "repository and no Maven dependency source is configured",
                                    offline_error.install_log,
                                    dependency=dependency,
                                ) from offline_error
                            try:
                                venv._run_install_logged(base, timeout_seconds)
                            except venv.DependencyPreparationError as source_error:
                                raise venv.DependencyPreparationError(
                                    str(source_error),
                                    offline_error.install_log + source_error.install_log,
                                    dependency=dependency,
                                ) from source_error
                    finally:
                        (directory / "pom.xml").write_text(_pom(dependencies), encoding="utf-8")
                    if dependency_log is not None:
                        dependency_log(f"{dependency} 安装成功")
            venv._run_logged(
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
            )
        except venv.DependencyPreparationError:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        finally:
            if settings_path is not None:
                settings_path.unlink(missing_ok=True)
        (directory / ".ready").write_text("ready", encoding="utf-8")
        return directory
