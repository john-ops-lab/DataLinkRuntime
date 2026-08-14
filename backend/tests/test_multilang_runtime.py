"""M3.3 regression tests for language dispatch and Worker capabilities."""

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import WORKER_TOKEN
from dlr.worker import agent as worker_agent
from dlr.worker import executor, javaenv, nodeenv, venv

WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}


def runtime_settings(root: Path, timeout: int = 5) -> executor.RuntimeSettings:
    return executor.RuntimeSettings(
        runtime_root=root,
        execution_timeout_seconds=timeout,
        dep_install_timeout_seconds=30,
    )


def payload(language: str, code: str) -> dict[str, object]:
    return {
        "execution_id": 101,
        "adapter_id": 11,
        "version_id": 12,
        "language": language,
        "code": code,
        "requirements": "",
        "runtime_config": {"stage": "m3.3"},
        "input": {"n": 7},
        "secrets": {"TOKEN": "secret-value"},
    }


def test_javascript_sync_async_config_secret_logs_and_output(tmp_path: Path) -> None:
    progress: list[tuple[str, str]] = []
    result = executor.run(
        payload(
            "javascript",
            """
export async function handle(context, input) {
  console.log("javascript stdout");
  console.error("javascript stderr");
  context.logger.info("logger info");
  return { input, stage: context.config.stage, secret: context.secrets.get("TOKEN") };
}
""",
        ),
        runtime_settings(tmp_path),
        progress_callback=lambda stdout, stderr: progress.append((stdout, stderr)) or False,
    )
    assert result["status"] == "succeeded"
    assert result["output"] == {
        "input": {"n": 7},
        "stage": "m3.3",
        "secret": "[REDACTED]",
    }
    assert "javascript stdout" in result["stdout"]
    assert "logger info" in result["stdout"]
    assert "javascript stderr" in result["stderr"]
    assert "javascript stdout" in "".join(stdout for stdout, _ in progress)
    assert "javascript stderr" in "".join(stderr for _, stderr in progress)


def test_javascript_exception_invalid_output_timeout_and_cancel(tmp_path: Path) -> None:
    loop = "".join(
        (
            "export async function handle() { ",
            "await new Promise(() => setInterval(() => {}, 1000)); }",
        )
    )
    raised = executor.run(
        payload("javascript", "export function handle() { throw new Error('broken'); }"),
        runtime_settings(tmp_path / "raised"),
    )
    assert raised["status"] == "failed"
    assert "broken" in raised["stderr"]

    invalid = executor.run(
        payload("javascript", "export function handle() { return 1n; }"),
        runtime_settings(tmp_path / "invalid"),
    )
    assert invalid["status"] == "failed"
    assert "JSON" in invalid["stderr"]

    timed_out = executor.run(
        payload(
            "javascript",
            loop,
        ),
        runtime_settings(tmp_path / "timeout", timeout=1),
    )
    assert timed_out["status"] == "timeout"

    cancelled = executor.run(
        payload(
            "javascript",
            loop,
        ),
        runtime_settings(tmp_path / "cancel", timeout=10),
        progress_callback=lambda _out, _err: True,
    )
    assert cancelled["status"] == "cancelled"


def test_java_compile_run_context_secret_logs_and_output(tmp_path: Path) -> None:
    progress: list[tuple[str, str]] = []
    result = executor.run(
        payload(
            "java",
            """
import java.util.LinkedHashMap;
import java.util.Map;

public class Adapter {
    public Object handle(Context context, Object input) {
        context.logger.info("java stdout");
        context.logger.warn("java stderr");
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("input", input);
        result.put("stage", context.config.get("stage"));
        result.put("secret", context.secrets.get("TOKEN"));
        return result;
    }
}
""",
        ),
        runtime_settings(tmp_path),
        progress_callback=lambda stdout, stderr: progress.append((stdout, stderr)) or False,
    )
    assert result["status"] == "succeeded", result
    assert result["output"] == {
        "input": {"n": 7},
        "stage": "m3.3",
        "secret": "[REDACTED]",
    }
    assert "java stdout" in result["stdout"]
    assert "java stderr" in result["stderr"]
    assert "java stdout" in "".join(stdout for stdout, _ in progress)
    assert "java stderr" in "".join(stderr for _, stderr in progress)


def test_java_compile_error_runtime_error_timeout_and_cancel(tmp_path: Path) -> None:
    compile_error = executor.run(
        payload("java", "public class Adapter { this is not Java; }"),
        runtime_settings(tmp_path / "compile"),
    )
    assert compile_error["status"] == "failed"
    assert "javac" in compile_error["error"]
    assert compile_error["stderr"]

    runtime_error = executor.run(
        payload(
            "java",
            """public class Adapter {
    public Object handle(Context context, Object input) { throw new RuntimeException("broken"); }
}""",
        ),
        runtime_settings(tmp_path / "runtime"),
    )
    assert runtime_error["status"] == "failed"
    assert "broken" in runtime_error["stderr"]

    loop = """public class Adapter {
    public Object handle(Context context, Object input) throws Exception {
        while (true) { Thread.sleep(1000); }
    }
}"""
    timed_out = executor.run(
        payload("java", loop), runtime_settings(tmp_path / "timeout", timeout=1)
    )
    assert timed_out["status"] == "timeout"
    cancelled = executor.run(
        payload("java", loop),
        runtime_settings(tmp_path / "cancel", timeout=10),
        progress_callback=lambda _out, _err: True,
    )
    assert cancelled["status"] == "cancelled"


def test_dependency_declaration_parsers() -> None:
    assert nodeenv.parse_requirements("axios@1.7.7\n@scope/pkg@1.2.3\n# comment") == {
        "axios": "1.7.7",
        "@scope/pkg": "1.2.3",
    }
    assert javaenv.parse_requirements("com.squareup.okhttp3:okhttp:4.12.0\n# comment") == [
        ("com.squareup.okhttp3", "okhttp", "4.12.0")
    ]
    with pytest.raises(venv.DependencyPreparationError):
        nodeenv.parse_requirements("axios")
    with pytest.raises(venv.DependencyPreparationError):
        javaenv.parse_requirements("invalid")


def test_dependency_auth_is_kept_out_of_generated_manifests() -> None:
    npm_url, npm_auth = nodeenv._npm_auth("https://npm-token:@registry.example.com/npm/")
    assert npm_url == "https://registry.example.com/npm/"
    assert npm_auth is not None and "npm-token" in npm_auth
    assert "npm-token" not in nodeenv._npm_auth(npm_url)[0]

    maven_url, maven_settings = javaenv._maven_settings(
        "https://maven-user:maven-password@maven.example.com/repository/"
    )
    assert maven_url == "https://maven.example.com/repository/"
    assert "<mirrorOf>*</mirrorOf>" in maven_settings
    assert "<url>https://maven.example.com/repository/</url>" in maven_settings
    assert "maven-password" in maven_settings
    pom = javaenv._pom([])
    assert "maven-user" not in pom
    assert "maven-password" not in pom
    assert "maven.example.com" not in pom

    _, anonymous_settings = javaenv._maven_settings("https://maven.example.com/repository/")
    assert "<mirrorOf>*</mirrorOf>" in anonymous_settings


def test_npm_auth_file_is_private_and_outside_version_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "runtime"
    directory = venv.version_dir(runtime_root, 1, 2)
    seen_auth_files: list[Path] = []
    monkeypatch.setattr(nodeenv.shutil, "which", lambda command: f"/fake/{command}")

    def fake_run(command: list[str], _timeout: int) -> str:
        if command[0] == "npm":
            auth_path = Path(command[command.index("--userconfig") + 1])
            assert auth_path.exists()
            assert not auth_path.is_relative_to(directory)
            assert auth_path.stat().st_mode & 0o777 == 0o600
            assert "npm-token" in auth_path.read_text(encoding="utf-8")
            seen_auth_files.append(auth_path)
        return ""

    monkeypatch.setattr(venv, "_run_logged", fake_run)
    result = nodeenv.prepare_version_node(
        runtime_root,
        1,
        2,
        "export function handle(context, input) { return input; }",
        "fixture@1.0.0",
        timeout_seconds=5,
        registry_url="https://npm-token:@registry.example.com/npm/",
    )
    assert seen_auth_files
    assert all(not path.exists() for path in seen_auth_files)
    assert (result / ".ready").exists()
    assert list(result.rglob("*.auth")) == []


@pytest.mark.parametrize(
    ("repository_url", "has_credentials"),
    [
        (
            "https://maven-user:maven-password@maven.example.com/repository/",
            True,
        ),
        ("https://maven.example.com/repository/", False),
    ],
)
def test_maven_mirror_controls_plugin_and_dependency_resolution_without_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repository_url: str,
    has_credentials: bool,
) -> None:
    runtime_root = tmp_path / "runtime"
    directory = venv.version_dir(runtime_root, 3, 4)
    settings_paths: list[Path] = []
    maven_commands: list[list[str]] = []
    monkeypatch.setattr(javaenv.shutil, "which", lambda command: f"/fake/{command}")

    def fake_run(command: list[str], _timeout: int) -> str:
        if command[0] == "mvn":
            settings_path = Path(command[command.index("-s") + 1])
            settings = settings_path.read_text(encoding="utf-8")
            assert not settings_path.is_relative_to(directory)
            assert settings_path.stat().st_mode & 0o777 == 0o600
            assert "<mirrorOf>*</mirrorOf>" in settings
            assert "<url>https://maven.example.com/repository/</url>" in settings
            if has_credentials:
                assert "maven-user" in settings and "maven-password" in settings
            else:
                assert "<servers>" not in settings
            assert "dependency:copy-dependencies" in command
            settings_paths.append(settings_path)
            maven_commands.append(command)
            if "-o" in command:
                raise venv.DependencyPreparationError("offline fixture miss", "")
            (directory / "deps" / "fixture.jar").write_bytes(b"fixture")
        elif command[0] == "javac":
            (directory / "classes" / "Adapter.class").write_bytes(b"fixture")
        return ""

    monkeypatch.setattr(venv, "_run_logged", fake_run)
    result = javaenv.prepare_version_java(
        runtime_root,
        3,
        4,
        "public class Adapter { public Object handle(Context c, Object i) { return i; } }",
        "example:fixture:1.0.0",
        timeout_seconds=5,
        repository_url=repository_url,
    )
    assert len(maven_commands) == 2
    assert all("-s" in command for command in maven_commands)
    assert settings_paths and all(not path.exists() for path in settings_paths)
    assert (result / ".ready").exists()
    assert "maven.example.com" not in (result / "pom.xml").read_text(encoding="utf-8")
    assert list(result.rglob("*.auth.xml")) == []


@pytest.mark.parametrize(
    ("java_version", "javac_version", "expected"),
    [
        ('openjdk version "21.0.2"', "javac 21.0.2", True),
        ('openjdk version "17.0.12"', "javac 17.0.12", False),
        ('openjdk version "21.0.2"', "javac 17.0.12", False),
    ],
)
def test_java_capability_requires_java_and_javac_21(
    monkeypatch: pytest.MonkeyPatch,
    java_version: str,
    javac_version: str,
    expected: bool,
) -> None:
    installed = {"java", "javac", "mvn"}
    monkeypatch.setattr(
        worker_agent.shutil,
        "which",
        lambda command: f"/fake/{command}" if command in installed else None,
    )

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        output = java_version if command[0] == "java" else javac_version
        return subprocess.CompletedProcess(command, 0, stdout="", stderr=output)

    monkeypatch.setattr(worker_agent.subprocess, "run", fake_run)
    assert ("java" in worker_agent.WorkerConfig().capabilities()) is expected


def register(client: TestClient, name: str, capabilities: list[str]) -> dict[str, object]:
    response = client.post(
        "/api/workers/register",
        json={"name": name, "capabilities": capabilities},
        headers=WORKER_HEADERS,
    )
    assert response.status_code == 200
    return response.json()


def create_adapter(client: TestClient, name: str, language: str) -> dict[str, object]:
    response = client.post(
        "/api/adapters",
        json={"name": name, "language": language, "adapter_type": "task"},
    )
    assert response.status_code == 201
    return response.json()


def test_worker_capability_is_hard_scheduling_constraint(api_client: TestClient) -> None:
    python_worker = register(api_client, "python-only", ["python"])
    js_worker = register(api_client, "js-only", ["javascript"])
    adapter = create_adapter(api_client, "capability-js", "javascript")

    incompatible = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"runtime_worker_id": python_worker["id"]},
    )
    assert incompatible.status_code == 409
    assert incompatible.json()["detail"]["code"] == "worker_capability_missing"

    compatible = api_client.patch(
        f"/api/adapters/{adapter['id']}",
        json={"runtime_worker_id": js_worker["id"]},
    )
    assert compatible.status_code == 200


@pytest.mark.parametrize(
    ("language", "capabilities"),
    [
        ("python", ["javascript"]),
        ("javascript", ["python"]),
        ("java", ["python", "javascript"]),
    ],
)
def test_execution_rejects_workers_without_language_capability(
    api_client: TestClient,
    language: str,
    capabilities: list[str],
) -> None:
    register(api_client, f"incompatible-{language}", capabilities)
    adapter = create_adapter(api_client, f"blocked-{language}", language)
    response = api_client.post(
        f"/api/adapters/{adapter['id']}/versions",
        json={"code": "// capability routing test"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "worker_capability_missing"


def test_worker_registration_rejects_unknown_or_empty_capabilities(
    api_client: TestClient,
) -> None:
    for capabilities in ([], ["ruby"]):
        response = api_client.post(
            "/api/workers/register",
            json={"name": "invalid-capability", "capabilities": capabilities},
            headers=WORKER_HEADERS,
        )
        assert response.status_code == 422


def test_single_compatible_worker_is_adopted_and_multiple_require_selection(
    api_client: TestClient,
) -> None:
    register(api_client, "python-unrelated", ["python"])
    javascript = register(api_client, "javascript-compatible", ["javascript"])
    adapter = create_adapter(api_client, "auto-js", "javascript")
    version_response = api_client.post(
        f"/api/adapters/{adapter['id']}/versions",
        json={"code": "export function handle(c, i) { return i; }"},
    )
    assert version_response.status_code == 201
    created = api_client.post(
        f"/api/adapters/{adapter['id']}/executions",
        json={},
    )
    assert created.status_code == 202
    assert created.json()["target_worker_id"] == javascript["id"]

    second = register(api_client, "javascript-compatible-2", ["javascript"])
    other = create_adapter(api_client, "multi-js", "javascript")
    rejected = api_client.post(
        f"/api/adapters/{other['id']}/versions",
        json={"code": "export function handle(c, i) { return i; }"},
    )
    assert second["id"] != javascript["id"]
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "runtime_worker_required"
