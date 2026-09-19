"""Issue #159: executable Java documentation must match the real runtime API."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dlr.control.ai import tools as tools_service
from dlr.runtime.java_runtime import SOURCE as JAVA_RUNTIME_SOURCE

JAVA_DOC_ID = "runtime-contract-java"
SYNTHETIC_SECRET = "issue159-synthetic-secret-value"


def _read_doc(doc_id: str) -> str:
    result = tools_service.execute_tool_call("dlr_docs_read", json.dumps({"doc_id": doc_id}), None)
    assert result.status == "success"
    assert result.source == f"dlr-docs:v1:{doc_id}"
    content = json.loads(result.model_content)["item"]["content"]
    assert isinstance(content, str)
    return content


def _java_tools() -> tuple[Path, Path]:
    java = shutil.which("java")
    javac = shutil.which("javac")
    assert java is not None, "JDK 21 java is required for the Java documentation contract"
    assert javac is not None, "JDK 21 javac is required for the Java documentation contract"
    version = subprocess.run([java, "-version"], check=True, capture_output=True, text=True).stderr
    compiler_version = subprocess.run(
        [javac, "-version"], check=True, capture_output=True, text=True
    ).stdout
    assert re.search(r'version "21(?:\.|\")', version), version
    assert compiler_version.startswith("javac 21"), compiler_version
    return Path(java), Path(javac)


def _extract_documented_adapter() -> tuple[str, str]:
    search = tools_service.execute_tool_call(
        "dlr_docs_search", '{"query":"Java Adapter runtime contract"}', None
    )
    assert search.status == "success"
    searched = json.loads(search.model_content)
    assert any(item["id"] == JAVA_DOC_ID for item in searched["items"])

    body = _read_doc(JAVA_DOC_ID)
    snippets = re.findall(r"```java\n(.*?)\n```", body, flags=re.DOTALL)
    assert len(snippets) == 1
    return body, snippets[0]


def _compile(
    root: Path,
    adapter_source: str,
    javac: Path,
) -> subprocess.CompletedProcess[str]:
    source_root = root / "source"
    classes = root / "classes"
    source_root.mkdir(parents=True)
    classes.mkdir()
    (source_root / "Adapter.java").write_text(adapter_source, encoding="utf-8")
    (source_root / "DlrRuntime.java").write_text(JAVA_RUNTIME_SOURCE, encoding="utf-8")
    return subprocess.run(
        [
            str(javac),
            "-encoding",
            "UTF-8",
            "-d",
            str(classes),
            str(source_root / "DlrRuntime.java"),
            str(source_root / "Adapter.java"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_retrieved_java_example_compiles_and_runs_against_real_runtime(
    tmp_path: Path,
) -> None:
    java, javac = _java_tools()
    body, adapter_source = _extract_documented_adapter()
    compiled = _compile(tmp_path / "valid", adapter_source, javac)
    assert compiled.returncode == 0, compiled.stderr

    workspace = tmp_path / "dlr-exec-159"
    (workspace / "input").mkdir(parents=True)
    input_value = {"numbers": [2, 8], "enabled": True}
    config_value = {"stage": "docs-contract", "limit": 2}
    (workspace / "input.json").write_text(json.dumps(input_value), encoding="utf-8")
    (workspace / "runtime_config.json").write_text(json.dumps(config_value), encoding="utf-8")
    (workspace / "input_manifest.json").write_text(
        json.dumps({"execution_id": 159, "files": []}), encoding="utf-8"
    )
    environment = os.environ.copy()
    environment["DLR_SECRET_DOC_TOKEN"] = SYNTHETIC_SECRET
    executed = subprocess.run(
        [str(java), "-cp", str(tmp_path / "valid" / "classes"), "DlrRuntime", str(workspace)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert executed.returncode == 0, executed.stderr

    output_text = (workspace / "output.json").read_text(encoding="utf-8")
    assert json.loads(output_text) == {
        "input": input_value,
        "stage": "docs-contract",
        "secretPresent": True,
    }
    assert "[INFO] docs info" in executed.stdout
    assert "[WARN] docs warn" in executed.stderr
    assert "[ERROR] docs error" in executed.stderr
    assert SYNTHETIC_SECRET not in "\n".join(
        (body, adapter_source, executed.stdout, executed.stderr, output_text)
    )

    config_doc = _read_doc("runtime-config-json")
    assert "context.config (Java)" in config_doc
    assert "context.config()" not in config_doc


@pytest.mark.parametrize(
    ("current", "obsolete"),
    [
        ('context.config.get("stage")', 'context.config().get("stage")'),
        ('context.secrets.get("DOC_TOKEN")', 'context.secrets().get("DOC_TOKEN")'),
        ('context.logger.info("docs info")', 'context.logger().info("docs info")'),
        ('context.logger.warn("docs warn")', 'context.logger.warning("docs warn")'),
    ],
)
def test_obsolete_documented_method_forms_do_not_compile(
    tmp_path: Path,
    current: str,
    obsolete: str,
) -> None:
    _, javac = _java_tools()
    _, adapter_source = _extract_documented_adapter()
    assert adapter_source.count(current) == 1

    compiled = _compile(tmp_path, adapter_source.replace(current, obsolete), javac)

    assert compiled.returncode != 0
    assert SYNTHETIC_SECRET not in compiled.stdout
    assert SYNTHETIC_SECRET not in compiled.stderr
