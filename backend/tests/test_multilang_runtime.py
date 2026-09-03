"""M3.3 regression tests for language dispatch and Worker capabilities."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import WORKER_TOKEN
from dlr.worker import agent as worker_agent
from dlr.worker import executor, javaenv, nodeenv, venv
from dlr.worker import workspace as workspace_manager

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


C2_INPUT_CONTENT = b"same C2 manifest fixture\n"

# One shared table drives the Worker pre-start contract and all three harness
# diagnostics.  Size/SHA failures deliberately have no harness expectation:
# the Worker is authoritative for content verification, while each harness
# only defends the minimum manifest/path/existence/openability boundary.
C2_MANIFEST_CONFORMANCE = (
    ("accepted", None, None),
    ("invalid", "input_artifact_not_ready", "input_artifact_not_ready"),
    ("ordinal", "input_artifact_not_ready", "input_artifact_not_ready"),
    ("mount", "input_artifact_not_ready", "input_artifact_not_ready"),
    ("missing", "input_artifact_not_ready", "input_artifact_not_ready"),
    ("size", "input_artifact_checksum_mismatch", None),
    ("sha", "input_artifact_checksum_mismatch", None),
)


def c2_input_file() -> dict[str, object]:
    return {
        "id": 9001,
        "ordinal": 0,
        "mount_name": "input-00.txt",
        "original_filename": "fixture.txt",
        "content_type": "text/plain",
        "size_bytes": len(C2_INPUT_CONTENT),
        "sha256": hashlib.sha256(C2_INPUT_CONTENT).hexdigest(),
    }


C2_ORDERED_INPUT_CONTENTS = (b"first ordered file\n", b"second ordered file\n")


def c2_input_files() -> list[dict[str, object]]:
    return [
        {
            "id": 9001 + ordinal,
            "ordinal": ordinal,
            "mount_name": f"input-{ordinal:02d}.txt",
            "original_filename": f"fixture-{ordinal}.txt",
            "content_type": "text/plain",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for ordinal, content in enumerate(C2_ORDERED_INPUT_CONTENTS)
    ]


@pytest.mark.parametrize(
    ("language", "code"),
    [
        (
            "python",
            """
from pathlib import Path

def handle(context, input):
    item = context.input_files[0]
    return {
        "input": input,
        "file": {
            "path": str(item.path),
            "original_name": item.original_name,
            "content_type": item.content_type,
            "size_bytes": item.size_bytes,
            "sha256": item.sha256,
            "ordinal": item.ordinal,
            "content": Path(item.path).read_text(),
        },
    }
""",
        ),
        (
            "javascript",
            """
import fs from "node:fs";

export function handle(context, input) {
  const item = context.inputFiles[0];
  return {
    input,
    file: {
      path: item.path,
      originalName: item.originalName,
      contentType: item.contentType,
      sizeBytes: item.sizeBytes,
      sha256: item.sha256,
      ordinal: item.ordinal,
      content: fs.readFileSync(item.path, "utf8"),
    },
  };
}
""",
        ),
        (
            "java",
            """
import java.nio.file.Files;
import java.util.LinkedHashMap;
import java.util.Map;

public class Adapter {
    public Object handle(Context context, Object input) throws Exception {
        InputFile item = context.inputFiles.get(0);
        Map<String, Object> file = new LinkedHashMap<>();
        file.put("path", item.path.toString());
        file.put("originalName", item.originalName);
        file.put("contentType", item.contentType);
        file.put("sizeBytes", item.sizeBytes);
        file.put("sha256", item.sha256);
        file.put("ordinal", item.ordinal);
        file.put("content", Files.readString(item.path));
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("input", input);
        result.put("file", file);
        return result;
    }
}
""",
        ),
    ],
)
def test_c2_same_manifest_fixture_exposes_ordered_input_file(
    tmp_path: Path,
    language: str,
    code: str,
) -> None:
    result = executor.run(
        payload(language, code) | {"input": None, "input_files": [c2_input_file()]},
        runtime_settings(tmp_path / language),
        input_downloader=lambda _file, destination: destination.write(C2_INPUT_CONTENT),
    )

    assert result["status"] == "succeeded", result
    assert result["output"]["input"] is None
    item = result["output"]["file"]
    assert Path(item["path"]).is_absolute()
    assert Path(item["path"]).name == "input-00.txt"
    assert Path(item["path"]).parent.name == "input"
    assert item["ordinal"] == 0
    assert item["content"] == C2_INPUT_CONTENT.decode()
    if language == "python":
        assert item["original_name"] == "fixture.txt"
        assert item["content_type"] == "text/plain"
        assert item["size_bytes"] == len(C2_INPUT_CONTENT)
    else:
        assert item["originalName"] == "fixture.txt"
        assert item["contentType"] == "text/plain"
        assert item["sizeBytes"] == len(C2_INPUT_CONTENT)
    assert item["sha256"] == hashlib.sha256(C2_INPUT_CONTENT).hexdigest()


@pytest.mark.parametrize(
    ("language", "code"),
    [
        (
            "python",
            """
from pathlib import Path

def handle(context, input):
    return {
        "input": input,
        "files": [
            {
                "ordinal": item.ordinal,
                "name": item.original_name,
                "content": Path(item.path).read_text(),
            }
            for item in context.input_files
        ],
    }
""",
        ),
        (
            "javascript",
            """
import fs from "node:fs";

export function handle(context, input) {
  return {
    input,
    files: context.inputFiles.map((item) => ({
      ordinal: item.ordinal,
      name: item.originalName,
      content: fs.readFileSync(item.path, "utf8"),
    })),
  };
}
""",
        ),
        (
            "java",
            """
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class Adapter {
    public Object handle(Context context, Object input) throws Exception {
        List<Map<String, Object>> files = new ArrayList<>();
        for (InputFile item : context.inputFiles) {
            Map<String, Object> file = new LinkedHashMap<>();
            file.put("ordinal", item.ordinal);
            file.put("name", item.originalName);
            file.put("content", Files.readString(item.path));
            files.add(file);
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("input", input);
        result.put("files", files);
        return result;
    }
}
""",
        ),
    ],
)
def test_c2_all_languages_preserve_input_file_order(
    tmp_path: Path,
    language: str,
    code: str,
) -> None:
    result = executor.run(
        payload(language, code) | {"input": None, "input_files": c2_input_files()},
        runtime_settings(tmp_path / language),
        input_downloader=lambda file, destination: destination.write(
            C2_ORDERED_INPUT_CONTENTS[int(file["ordinal"])]
        ),
    )

    assert result["status"] == "succeeded", result
    assert result["output"] == {
        "input": None,
        "files": [
            {"ordinal": 0, "name": "fixture-0.txt", "content": "first ordered file\n"},
            {"ordinal": 1, "name": "fixture-1.txt", "content": "second ordered file\n"},
        ],
    }


def _c2_started_adapter_code(language: str, marker: Path) -> str:
    if language == "python":
        return (
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('imported')\n"
            "def handle(context, input):\n"
            "    return {}\n"
        )
    if language == "javascript":
        return (
            'import fs from "node:fs";\n'
            f"const marker = {json.dumps(str(marker))};\n"
            "fs.writeFileSync(marker, 'imported');\n"
            "export function handle(context, input) { "
            "return {}; }\n"
        )
    java_marker = str(marker).replace("\\", "\\\\").replace('"', '\\"')
    return (
        "import java.nio.file.Files;\n"
        "import java.nio.file.Path;\n"
        "public class Adapter {\n"
        "    static {\n"
        "        try {\n"
        f'            Files.writeString(Path.of("{java_marker}"), "imported");\n'
        "        } catch (Exception error) {\n"
        "            throw new ExceptionInInitializerError(error);\n"
        "        }\n"
        "    }\n"
        "    public Object handle(Context context, Object input) throws Exception {\n"
        "        return new java.util.LinkedHashMap<String, Object>();\n"
        "    }\n"
        "}\n"
    )


def _c2_fake_input_error_adapter_code(language: str) -> str:
    marker = "DLR_INPUT_ERROR:input_artifact_checksum_mismatch"
    if language == "python":
        return (
            "def handle(context, input):\n"
            f"    print({marker!r})\n"
            "    raise RuntimeError('adapter failure')\n"
        )
    if language == "javascript":
        return (
            "export function handle(context, input) { "
            f"console.error({json.dumps(marker)}); "
            "throw new Error('adapter failure'); "
            "}"
        )
    return (
        "public class Adapter {\n"
        "    public Object handle(Context context, Object input) {\n"
        f'        System.err.println("{marker}");\n'
        '        throw new RuntimeException("adapter failure");\n'
        "    }\n"
        "}\n"
    )


def _mutate_c2_manifest(
    layout: workspace_manager.WorkspaceLayout,
    mutation: str,
) -> None:
    manifest_path = layout.root / workspace_manager.MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["original_filename"] = "/control/internal/secret-input.txt"
    if mutation == "invalid":
        manifest["unexpected"] = "must not be accepted"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif mutation == "ordinal":
        manifest["files"][0]["ordinal"] = 8
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif mutation == "mount":
        manifest["files"][0]["mount_name"] = "input-08.txt"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif mutation == "missing":
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (layout.input).chmod(0o755)
        (layout.input / "input-00.txt").unlink()
    elif mutation == "size":
        manifest["files"][0]["size_bytes"] += 1
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif mutation == "sha":
        manifest["files"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        raise AssertionError(f"unknown C2 mutation: {mutation}")


def _run_c2_worker_manifest_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    mutation: str,
) -> dict[str, object]:
    marker = tmp_path / f"{language}-{mutation}-prestart"
    original_prepare = workspace_manager.prepare_input_files

    def prepare_then_mutate(
        layout: workspace_manager.WorkspaceLayout,
        input_files: list[object],
        input_downloader: workspace_manager.InputDownloader | None,
    ) -> None:
        original_prepare(layout, input_files, input_downloader)
        if mutation != "accepted":
            _mutate_c2_manifest(layout, mutation)

    monkeypatch.setattr(workspace_manager, "prepare_input_files", prepare_then_mutate)
    return executor.run(
        payload(language, _c2_started_adapter_code(language, marker))
        | {"input": None, "input_files": [c2_input_file()]},
        runtime_settings(tmp_path / "runtime"),
        input_downloader=lambda _file, destination: destination.write(C2_INPUT_CONTENT),
    )


@pytest.mark.parametrize(
    ("mutation", "expected_code", "_harness_expected_code"), C2_MANIFEST_CONFORMANCE
)
def test_c2_worker_manifest_conformance_is_prestart_and_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_code: str | None,
    _harness_expected_code: str | None,
) -> None:
    marker = tmp_path / f"python-{mutation}-prestart"
    result = _run_c2_worker_manifest_case(tmp_path, monkeypatch, "python", mutation)

    if expected_code is None:
        assert mutation == "accepted"
        assert result["status"] == "succeeded", result
        assert marker.exists()
        return

    assert result["status"] == "failed", result
    assert result["error_code"] == expected_code
    assert result["stdout"] == ""
    assert result["stderr"] == ""
    assert not marker.exists()
    assert "/control/internal/secret-input.txt" not in str(result)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (mutation, harness_code)
        for mutation, _worker_code, harness_code in C2_MANIFEST_CONFORMANCE
        if harness_code is not None
    ],
)
@pytest.mark.parametrize("language", ["python", "javascript", "java"])
def test_c2_harness_manifest_conformance_is_diagnostic_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    mutation: str,
    expected_code: str,
) -> None:
    marker = tmp_path / f"{language}-{mutation}-started"
    original_validate = workspace_manager.validate_input_manifest

    def validate_then_mutate(layout: workspace_manager.WorkspaceLayout) -> None:
        original_validate(layout)
        _mutate_c2_manifest(layout, mutation)

    monkeypatch.setattr(workspace_manager, "validate_input_manifest", validate_then_mutate)
    result = executor.run(
        payload(language, _c2_started_adapter_code(language, marker))
        | {"input": None, "input_files": [c2_input_file()]},
        runtime_settings(tmp_path / "runtime"),
        input_downloader=lambda _file, destination: destination.write(C2_INPUT_CONTENT),
    )

    assert result["status"] == "failed", result
    assert result.get("error_code") is None
    assert f"DLR_INPUT_ERROR:{expected_code}" in result["stdout"]
    assert not marker.exists()
    assert "/control/internal/secret-input.txt" not in str(result)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (mutation, worker_code)
        for mutation, worker_code, _harness_code in C2_MANIFEST_CONFORMANCE
        if mutation in {"invalid", "sha"}
    ],
)
@pytest.mark.parametrize("language", ["python", "javascript", "java"])
def test_c2_worker_manifest_conformance_prestart_samples_cover_all_languages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    mutation: str,
    expected_code: str,
) -> None:
    marker = tmp_path / f"{language}-{mutation}-prestart"
    result = _run_c2_worker_manifest_case(tmp_path, monkeypatch, language, mutation)

    assert result["status"] == "failed", result
    assert result["error_code"] == expected_code
    assert result["stdout"] == ""
    assert result["stderr"] == ""
    assert not marker.exists()
    assert "/control/internal/secret-input.txt" not in str(result)


@pytest.mark.parametrize("language", ["python", "javascript", "java"])
def test_c2_adapter_cannot_forge_input_error_code(
    tmp_path: Path,
    language: str,
) -> None:
    result = executor.run(
        payload(language, _c2_fake_input_error_adapter_code(language)),
        runtime_settings(tmp_path / language),
    )

    assert result["status"] == "failed", result
    assert result.get("error_code") is None
    assert "DLR_INPUT_ERROR:input_artifact_checksum_mismatch" in result["stdout"]
    assert "adapter failure" in result["stdout"]


def test_c2_input_permissions_are_documented_as_best_effort_only() -> None:
    doc = workspace_manager.validate_input_manifest.__doc__ or ""
    assert "Worker owns the size/SHA-256 verification" in doc
    assert "minimal structure, path, existence, and openability checks" in doc
    assert "best-effort accidental-write protection only" in doc
    assert "never a same-OS-user security boundary" in doc


@pytest.mark.parametrize(
    ("language", "code"),
    [
        ("python", "def handle(context, input):\n    return input\n"),
        ("javascript", "export function handle(context, input) { return input; }"),
        (
            "java",
            "public class Adapter { "
            "public Object handle(Context context, Object input) { return input; } }",
        ),
    ],
)
def test_c2_json_handle_contract_remains_unwrapped_retained(
    tmp_path: Path,
    language: str,
    code: str,
) -> None:
    result = executor.run(payload(language, code), runtime_settings(tmp_path / language))
    assert result["status"] == "succeeded", result
    assert result["output"] == {"n": 7}


def test_c2_v1_invalid_input_file_container_remains_a_stable_failure(tmp_path: Path) -> None:
    result = executor.run(
        payload("python", "def handle(context, input):\n    return input\n")
        | {"input_files": "not-a-list"},
        runtime_settings(tmp_path),
    )

    assert result["status"] == "failed", result
    assert result["error_code"] == "input_artifact_not_ready"


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
    assert "javascript stderr" in result["stdout"], (
        "M5.5.10: stderr merges into the unified stdout-channel log"
    )
    assert result["stderr"] == ""
    delivered = "".join(stdout for stdout, _ in progress)
    assert "javascript stdout" in delivered
    assert "javascript stderr" in delivered


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
    assert "broken" in raised["stdout"]

    invalid = executor.run(
        payload("javascript", "export function handle() { return 1n; }"),
        runtime_settings(tmp_path / "invalid"),
    )
    assert invalid["status"] == "failed"
    assert "JSON" in invalid["stdout"]

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
    assert "java stderr" in result["stdout"]
    delivered = "".join(stdout for stdout, _ in progress)
    assert "java stdout" in delivered
    assert "java stderr" in delivered


def test_java_compile_error_runtime_error_timeout_and_cancel(tmp_path: Path) -> None:
    compile_error = executor.run(
        payload("java", "public class Adapter { this is not Java; }"),
        runtime_settings(tmp_path / "compile"),
    )
    assert compile_error["status"] == "failed"
    # M5.6 Wave 4 E: the error field carries the localized DLR message; the
    # raw javac diagnostics live in the unified log stream, untouched.
    assert "Java 依赖准备失败" in compile_error["error"]
    assert "javac" not in compile_error["error"]
    assert compile_error["stdout"]

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
    assert "broken" in runtime_error["stdout"]

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


def test_dependency_logs_are_unified_and_ready_environments_skip_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    npm_packages: list[dict[str, object]] = []

    def fake_run(command: list[str], _timeout: int) -> str:
        calls.append(command)
        if command[:2] == ["uv", "venv"]:
            python = Path(command[2]) / "bin" / "python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
        if command[0] == "javac":
            classes = Path(command[command.index("-d") + 1])
            (classes / "Adapter.class").write_bytes(b"fixture")
        if command[0] == "npm":
            package_path = Path(command[command.index("--prefix") + 1]) / "package.json"
            npm_packages.append(json.loads(package_path.read_text(encoding="utf-8")))
        return ""

    monkeypatch.setattr(venv, "_run_logged", fake_run)
    monkeypatch.setattr(nodeenv.shutil, "which", lambda command: f"/fake/{command}")
    monkeypatch.setattr(javaenv.shutil, "which", lambda command: f"/fake/{command}")

    python_logs: list[str] = []
    venv.prepare_version_venv(
        tmp_path / "python",
        30,
        1,
        "--extra-index-url https://mirror.example.test/simple/\n"
        "-c constraints.txt\n"
        "requests==2.32.3\nurllib3==2.2.3",
        timeout_seconds=5,
        dependency_log=python_logs.append,
    )
    python_ready_logs: list[str] = []
    venv.prepare_version_venv(
        tmp_path / "python",
        30,
        1,
        "--extra-index-url https://mirror.example.test/simple/\n"
        "-c constraints.txt\n"
        "requests==2.32.3\nurllib3==2.2.3",
        timeout_seconds=5,
        dependency_log=python_ready_logs.append,
    )

    javascript_logs: list[str] = []
    nodeenv.prepare_version_node(
        tmp_path / "javascript",
        31,
        1,
        "export function handle(context, input) { return input; }",
        "axios@1.7.7\nlodash@4.17.21",
        timeout_seconds=5,
        registry_url=None,
        dependency_log=javascript_logs.append,
    )
    javascript_ready_logs: list[str] = []
    nodeenv.prepare_version_node(
        tmp_path / "javascript",
        31,
        1,
        "export function handle(context, input) { return input; }",
        "axios@1.7.7\nlodash@4.17.21",
        timeout_seconds=5,
        registry_url=None,
        dependency_log=javascript_ready_logs.append,
    )

    java_logs: list[str] = []
    javaenv.prepare_version_java(
        tmp_path / "java",
        32,
        1,
        "public class Adapter { public Object handle(Context c, Object i) { return i; } }",
        "com.example:fixture:1.0.0\ncom.example:other:2.0.0",
        timeout_seconds=5,
        repository_url=None,
        dependency_log=java_logs.append,
    )
    java_ready_logs: list[str] = []
    javaenv.prepare_version_java(
        tmp_path / "java",
        32,
        1,
        "public class Adapter { public Object handle(Context c, Object i) { return i; } }",
        "com.example:fixture:1.0.0\ncom.example:other:2.0.0",
        timeout_seconds=5,
        repository_url=None,
        dependency_log=java_ready_logs.append,
    )

    assert python_logs == [
        "requests==2.32.3 未安装，开始安装",
        "urllib3==2.2.3 未安装，开始安装",
        "requests==2.32.3 安装成功",
        "urllib3==2.2.3 安装成功",
    ]
    assert python_ready_logs == [
        "requests==2.32.3 已安装，检查通过",
        "urllib3==2.2.3 已安装，检查通过",
    ]
    assert javascript_logs == [
        "axios@1.7.7 未安装，开始安装",
        "lodash@4.17.21 未安装，开始安装",
        "axios@1.7.7 安装成功",
        "lodash@4.17.21 安装成功",
    ]
    assert javascript_ready_logs == [
        "axios@1.7.7 已安装，检查通过",
        "lodash@4.17.21 已安装，检查通过",
    ]
    assert java_logs == [
        "com.example:fixture:1.0.0 未安装，开始安装",
        "com.example:other:2.0.0 未安装，开始安装",
        "com.example:fixture:1.0.0 安装成功",
        "com.example:other:2.0.0 安装成功",
    ]
    assert java_ready_logs == [
        "com.example:fixture:1.0.0 已安装，检查通过",
        "com.example:other:2.0.0 已安装，检查通过",
    ]

    uv_installs = [command for command in calls if command[0] == "uv" and "install" in command]
    npm_installs = [command for command in calls if command[0] == "npm"]
    maven_installs = [command for command in calls if command[0] == "mvn"]
    assert len(uv_installs) == 1, "uv must resolve all Python requirements jointly"
    assert len(npm_installs) == 1, "npm must install the complete manifest once"
    assert len(maven_installs) == 1, "Maven must mediate the complete dependency graph once"
    requirements_path = next((tmp_path / "python").rglob("requirements.txt"))
    assert "--extra-index-url" in requirements_path.read_text(encoding="utf-8")
    assert npm_packages == [
        {
            "private": True,
            "type": "module",
            "dependencies": {"axios": "1.7.7", "lodash": "4.17.21"},
        }
    ]


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
            output_argument = next(
                argument for argument in command if argument.startswith("-DoutputDirectory=")
            )
            deps = Path(output_argument.split("=", 1)[1])
            deps.mkdir(parents=True, exist_ok=True)
            (deps / "fixture.jar").write_bytes(b"fixture")
        elif command[0] == "javac":
            classes = Path(command[command.index("-d") + 1])
            classes.mkdir(parents=True, exist_ok=True)
            (classes / "Adapter.class").write_bytes(b"fixture")
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
