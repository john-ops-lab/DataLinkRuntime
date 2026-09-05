"""Focused parity gates for the ServiceNow template Recipe."""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dlr.runtime.java_runtime import SOURCE as JAVA_RUNTIME_SOURCE
from test_template_recipes import _java_result, _javascript_result, _python_result

CATALOG_ROOT = Path(__file__).parents[1] / "src/dlr/control/template_catalog"
SOURCE_ROOT = CATALOG_ROOT / "variants/servicenow-cmdb-ci-snapshot"


@pytest.mark.parametrize(
    ("override", "error_code"),
    [
        ({"offset": -1}, "invalid_offset"),
        ({"offset": True}, "invalid_offset"),
        ({"display_value": "false"}, "invalid_display_value"),
        ({"encoded_query": "x" * 4097}, "invalid_encoded_query"),
    ],
)
def test_servicenow_contract_inputs_fail_before_network_in_all_languages(
    tmp_path: Path,
    override: dict[str, object],
    error_code: str,
) -> None:
    input_value = {
        "mode": "preview",
        "instance_url": "https://fixture.service-now.example",
        "instance_id": "fixture",
    } | override

    with pytest.raises(ValueError, match=f"^{error_code}$"):
        _python_result(SOURCE_ROOT / "python.py", input_value)
    with pytest.raises(subprocess.CalledProcessError) as javascript_error:
        _javascript_result(SOURCE_ROOT / "javascript.mjs", input_value)
    assert error_code in javascript_error.value.stderr
    with pytest.raises(subprocess.CalledProcessError) as java_error:
        _java_result(SOURCE_ROOT / "java.java", input_value, tmp_path / error_code)
    assert error_code in java_error.value.stderr


def test_servicenow_external_key_uses_frozen_component_escape_in_all_languages(
    tmp_path: Path,
) -> None:
    account = "acct!%:left"
    identifier = "ci*1!%:tail"
    expected = "servicenow:acct!%25%3Aleft:global:cmdb_ci:ci*1!%25%3Atail"

    python = runpy.run_path(str(SOURCE_ROOT / "python.py"))
    assert python["_asset"](account, {"sys_id": identifier})["external_key"] == expected

    javascript_script = """
import { pathToFileURL } from "node:url";
globalThis.fetch = async () => new Response(
  JSON.stringify({ result: [{ sys_id: process.argv[3] }] }),
  { status: 200, headers: { "Content-Type": "application/json" } },
);
const module = await import(pathToFileURL(process.argv[1]));
const result = await module.handle(
  {
    config: {},
    secrets: new Map([["SERVICENOW_BEARER_TOKEN", "fixture"]]),
    inputFiles: [], logger: {},
  },
  {
    mode: "preview", instance_url: "https://fixture.service-now.example",
    instance_id: process.argv[2], page_size: 10, max_pages: 1,
  },
);
process.stdout.write(result.assets[0].external_key);
"""
    javascript = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            javascript_script,
            str(SOURCE_ROOT / "javascript.mjs"),
            account,
            identifier,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert javascript.stdout == expected

    compile_root = tmp_path / "servicenow-external-key-java"
    compile_root.mkdir()
    (compile_root / "DlrRuntime.java").write_text(JAVA_RUNTIME_SOURCE, encoding="utf-8")
    (compile_root / "Adapter.java").write_text(
        (SOURCE_ROOT / "java.java").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (compile_root / "Probe.java").write_text(
        """
import java.lang.reflect.Method;
import java.util.Map;
public final class Probe {
  public static void main(String[] args) throws Exception {
    Method asset = Adapter.class.getDeclaredMethod("asset", String.class, Map.class);
    asset.setAccessible(true);
    @SuppressWarnings("unchecked")
    Map<String,Object> value = (Map<String,Object>) asset.invoke(
      null, args[0], Map.of("sys_id", args[1])
    );
    System.out.print(value.get("external_key"));
  }
}
""",
        encoding="utf-8",
    )
    subprocess.run(
        ["javac", "-encoding", "UTF-8", "DlrRuntime.java", "Adapter.java", "Probe.java"],
        cwd=compile_root,
        check=True,
        capture_output=True,
        text=True,
    )
    java = subprocess.run(
        ["java", "-cp", str(compile_root), "Probe", account, identifier],
        check=True,
        capture_output=True,
        text=True,
    )
    assert java.stdout == expected


def _should_fail(scan_id: str, body: dict[str, Any]) -> bool:
    stage = scan_id.removeprefix("fail-")
    operation = body.get("operation")
    return (
        (stage == "begin" and operation == "begin_scan")
        or (stage == "first" and operation == "upsert_assets" and body.get("batch_index") == 0)
        or (stage == "later" and operation == "upsert_assets" and body.get("batch_index") == 1)
        or (stage == "finish" and operation == "finish_scan")
    )


@contextmanager
def _failing_cmdb() -> Iterator[tuple[str, dict[str, list[str]]]]:
    operations: dict[str, list[str]] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(payload)
            scan_id = body["scan_id"]
            operations.setdefault(scan_id, []).append(body["operation"])
            self.send_response(500 if _should_fail(scan_id, body) else 200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", operations
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _assert_acknowledged_counts(results: dict[str, dict[str, Any]]) -> None:
    expected = {"begin": 0, "first": 0, "later": 1, "finish": 3}
    for stage, count in expected.items():
        result = results[stage]
        assert result["partial"] is True
        assert result["summary"] == {
            "assets": count,
            "relationships": 0,
            "pages": 1,
            "failures": ["target_batch"],
        }
        assert result["failed"] == ["target_batch"]
        assert result["checkpoint"] == {"scan_id": f"fail-{stage}"}


def test_servicenow_sync_reports_only_acknowledged_target_assets_in_all_languages(
    tmp_path: Path,
) -> None:
    assets = [
        {"external_key": f"servicenow:fixture:global:cmdb_ci:ci-{index}"} for index in range(3)
    ]
    summary = {"assets": 3, "relationships": 0, "pages": 1, "failures": []}
    stages = ("begin", "first", "later", "finish")

    python = runpy.run_path(str(SOURCE_ROOT / "python.py"))
    python_results: dict[str, dict[str, Any]] = {}
    python_operations: dict[str, list[str]] = {}
    for stage in stages:
        scan_id = f"fail-{stage}"

        def fake_post(
            _base: str,
            _path: str,
            body: dict[str, Any],
            _token: str,
            _idem: str,
            _deadline: float,
            *,
            expected_scan: str = scan_id,
        ) -> None:
            python_operations.setdefault(expected_scan, []).append(body["operation"])
            if _should_fail(expected_scan, body):
                raise ValueError("cmdb_target_error")

        python["_sync"].__globals__["_post"] = fake_post
        python_results[stage] = python["_sync"](
            SimpleNamespace(
                config={"cmdb_base_url": "http://localhost"},
                secrets={"CMDB_TOKEN": "fixture"},
            ),
            {
                "scan_id": scan_id,
                "source_scope": "servicenow:fixture",
                "batch_size": 1,
            },
            assets,
            summary,
            time.monotonic() + 30,
        )
    _assert_acknowledged_counts(python_results)

    javascript_script = """
import { pathToFileURL } from "node:url";
const stages = ["begin", "first", "later", "finish"];
const operations = {};
function shouldFail(scanId, body) {
  const stage = scanId.slice(5);
  return (stage === "begin" && body.operation === "begin_scan")
    || (stage === "first" && body.operation === "upsert_assets" && body.batch_index === 0)
    || (stage === "later" && body.operation === "upsert_assets" && body.batch_index === 1)
    || (stage === "finish" && body.operation === "finish_scan");
}
globalThis.fetch = async (_url, options = {}) => {
  if ((options.method ?? "GET") === "GET") {
    return new Response(JSON.stringify({ result: [
      { sys_id: "ci-0" }, { sys_id: "ci-1" }, { sys_id: "ci-2" },
    ] }), { status: 200, headers: { "Content-Type": "application/json" } });
  }
  const body = JSON.parse(options.body);
  operations[body.scan_id] ??= [];
  operations[body.scan_id].push(body.operation);
  return new Response("", { status: shouldFail(body.scan_id, body) ? 500 : 200 });
};
const module = await import(pathToFileURL(process.argv[1]));
const results = {};
for (const stage of stages) {
  const scanId = `fail-${stage}`;
  results[stage] = await module.handle({
    config: { cmdb_base_url: "http://localhost" },
    secrets: new Map([
      ["SERVICENOW_BEARER_TOKEN", "fixture"], ["CMDB_TOKEN", "fixture"],
    ]),
    inputFiles: [], logger: {},
  }, {
    mode: "sync", scan_id: scanId, source_scope: "servicenow:fixture",
    instance_url: "https://fixture.service-now.example", instance_id: "fixture",
    page_size: 10, max_pages: 1, batch_size: 1,
  });
}
process.stdout.write(JSON.stringify({ results, operations }));
"""
    javascript = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            javascript_script,
            str(SOURCE_ROOT / "javascript.mjs"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    javascript_output = json.loads(javascript.stdout)
    _assert_acknowledged_counts(javascript_output["results"])

    with _failing_cmdb() as (cmdb_url, java_operations):
        compile_root = tmp_path / "servicenow-target-ack-java"
        compile_root.mkdir()
        (compile_root / "DlrRuntime.java").write_text(JAVA_RUNTIME_SOURCE, encoding="utf-8")
        (compile_root / "Adapter.java").write_text(
            (SOURCE_ROOT / "java.java").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (compile_root / "Probe.java").write_text(
            """
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
public final class Probe {
  public static void main(String[] args) throws Exception {
    Method sync = Adapter.class.getDeclaredMethod(
      "sync", Context.class, Map.class, List.class, Map.class, long.class
    );
    sync.setAccessible(true);
    List<Map<String,Object>> assets = new ArrayList<>();
    for (int index = 0; index < 3; index++) {
      assets.add(Map.of("external_key", "servicenow:fixture:global:cmdb_ci:ci-" + index));
    }
    Map<String,Object> summary = new LinkedHashMap<>();
    summary.put("assets", 3); summary.put("relationships", 0);
    summary.put("pages", 1); summary.put("failures", List.of());
    Map<String,Object> results = new LinkedHashMap<>();
    for (String stage : List.of("begin", "first", "later", "finish")) {
      Map<String,Object> input = new LinkedHashMap<>();
      input.put("scan_id", "fail-" + stage);
      input.put("source_scope", "servicenow:fixture"); input.put("batch_size", 1);
      Object result = sync.invoke(
        null, new Context(Map.of("cmdb_base_url", args[0])), input, assets, summary,
        System.nanoTime() + java.time.Duration.ofSeconds(30).toNanos()
      );
      results.put(stage, result);
    }
    System.out.print(Json.stringify(results));
  }
}
""",
            encoding="utf-8",
        )
        subprocess.run(
            ["javac", "-encoding", "UTF-8", "DlrRuntime.java", "Adapter.java", "Probe.java"],
            cwd=compile_root,
            check=True,
            capture_output=True,
            text=True,
        )
        environment = os.environ.copy()
        environment["DLR_SECRET_CMDB_TOKEN"] = "fixture"
        java = subprocess.run(
            ["java", "-cp", str(compile_root), "Probe", cmdb_url],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    java_results = json.loads(java.stdout)
    _assert_acknowledged_counts(java_results)

    for operations in (python_operations, javascript_output["operations"], java_operations):
        assert "finish_scan" not in operations["fail-begin"]
        assert "finish_scan" not in operations["fail-first"]
        assert "finish_scan" not in operations["fail-later"]
        assert operations["fail-finish"][-1] == "finish_scan"
