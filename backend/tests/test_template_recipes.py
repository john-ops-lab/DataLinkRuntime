"""Static and direct-source gates for the Issue #132 Recipe catalog."""

from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import shutil
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

import pytest

from dlr.control.template_catalog import TemplateCatalog
from dlr.runtime.java_runtime import SOURCE as JAVA_RUNTIME_SOURCE
from dlr.worker import javaenv, nodeenv, venv

CATALOG_ROOT = Path(__file__).parents[1] / "src/dlr/control/template_catalog"
LANGUAGES = {"python", "javascript", "java"}
CLOUD_SCENARIOS = (
    "alicloud-compute-container-topology",
    "alicloud-network-ingress-topology",
    "alicloud-database-middleware-inventory",
    "tencentcloud-compute-container-topology",
    "tencentcloud-network-ingress-topology",
    "tencentcloud-database-middleware-inventory",
)
OFFICIAL_PROVENANCE_IDS = {
    "alicloud-openapi-2026-09-05",
    "tencentcloud-api-2026-09-05",
    "servicenow-table-api-2026-09-05",
    "http-rfc9110-9112",
    "csv-rfc4180",
    "excel-formats-and-libraries-2026-09-05",
    "json-pointer-rfc6901",
    "postgresql-17-docs",
    "mysql-9-docs",
    "s3-api-2006-03-01",
    "sftp-v3-openssh",
}
ALICLOUD_JAVA_STUBS = {
    "com/aliyun/teaopenapi/Client.java": """
package com.aliyun.teaopenapi;
import com.aliyun.teaopenapi.models.*;
import com.aliyun.teautil.models.RuntimeOptions;
import java.util.Map;
public class Client {
  public Client(Config value) {}
  public Map<String, Object> callApi(Params p, OpenApiRequest r, RuntimeOptions o) {
    throw new IllegalStateException("fixture must not call SDK");
  }
}
""",
    "com/aliyun/teaopenapi/models/Config.java": """
package com.aliyun.teaopenapi.models;
public class Config {
  public Config setAccessKeyId(String v){return this;}
  public Config setAccessKeySecret(String v){return this;}
  public Config setSecurityToken(String v){return this;}
  public Config setEndpoint(String v){return this;}
  public Config setRegionId(String v){return this;}
  public Config setProtocol(String v){return this;}
  public Config setReadTimeout(int v){return this;}
  public Config setConnectTimeout(int v){return this;}
}
""",
    "com/aliyun/teaopenapi/models/OpenApiRequest.java": """
package com.aliyun.teaopenapi.models;
import java.util.Map;
public class OpenApiRequest {
  public OpenApiRequest setQuery(Map<String,String> v){return this;}
}
""",
    "com/aliyun/teaopenapi/models/Params.java": """
package com.aliyun.teaopenapi.models;
public class Params {
  public Params setAction(String v){return this;}
  public Params setVersion(String v){return this;}
  public Params setProtocol(String v){return this;}
  public Params setPathname(String v){return this;}
  public Params setMethod(String v){return this;}
  public Params setAuthType(String v){return this;}
  public Params setBodyType(String v){return this;}
  public Params setReqBodyType(String v){return this;}
  public Params setStyle(String v){return this;}
}
""",
    "com/aliyun/teautil/models/RuntimeOptions.java": """
package com.aliyun.teautil.models;
public class RuntimeOptions {
  public RuntimeOptions setReadTimeout(int v){return this;}
  public RuntimeOptions setConnectTimeout(int v){return this;}
  public RuntimeOptions setAutoretry(boolean v){return this;}
}
""",
}


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _catalog_assets() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = _json(CATALOG_ROOT / "catalog.json")
    scenarios = [_json(CATALOG_ROOT / item["metadata_resource"]) for item in manifest["scenarios"]]
    return manifest, scenarios


def _variant_source(variant: dict[str, Any]) -> Path:
    return CATALOG_ROOT / variant["code_resource"]


def _python_result(
    source: Path,
    input_value: dict[str, Any],
    secrets: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    namespace = runpy.run_path(str(source))
    context = SimpleNamespace(
        config=config or {}, secrets=secrets or {}, input_files=[], logger=SimpleNamespace()
    )
    return namespace["handle"](context, input_value)


def _javascript_result(
    source: Path,
    input_value: dict[str, Any],
    secrets: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    script = """
import { pathToFileURL } from "node:url";
const module = await import(pathToFileURL(process.argv[1]));
const input = JSON.parse(process.argv[2]);
const secrets = JSON.parse(process.argv[3]);
const config = JSON.parse(process.argv[4]);
const context = {
  config, secrets: new Map(Object.entries(secrets)), inputFiles: [], logger: {},
};
process.stdout.write(JSON.stringify(await module.handle(context, input)));
"""
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            str(source),
            json.dumps(input_value),
            json.dumps(secrets or {}),
            json.dumps(config or {}),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _javascript_result_with_alicloud_stubs(
    source: Path,
    input_value: dict[str, Any],
    fixture_root: Path,
    secrets: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fixture_root.mkdir(parents=True)
    recipe = fixture_root / "recipe.mjs"
    recipe.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    openapi = fixture_root / "node_modules/@alicloud/openapi-client"
    tea = fixture_root / "node_modules/@alicloud/tea-util"
    for module in (openapi, tea):
        module.mkdir(parents=True)
        (module / "package.json").write_text(
            json.dumps({"type": "module", "exports": "./index.js"}), encoding="utf-8"
        )
    (openapi / "index.js").write_text(
        """
export class Config { constructor(value) { Object.assign(this, value); } }
export class OpenApiRequest { constructor(value) { Object.assign(this, value); } }
export class Params { constructor(value) { Object.assign(this, value); } }
export default class OpenApi {
  constructor() { throw new Error("fixture must not call SDK"); }
}
""",
        encoding="utf-8",
    )
    (tea / "index.js").write_text(
        "export class RuntimeOptions { constructor(value) { Object.assign(this, value); } }\n",
        encoding="utf-8",
    )
    return _javascript_result(recipe, input_value, secrets, config)


def _java_result(
    source: Path,
    input_value: dict[str, Any],
    tmp_path: Path,
    secrets: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
    support_sources: dict[str, str] | None = None,
    java_options: list[str] | None = None,
) -> dict[str, Any]:
    compile_root = tmp_path / source.parent.name
    compile_root.mkdir(parents=True)
    (compile_root / "DlrRuntime.java").write_text(JAVA_RUNTIME_SOURCE, encoding="utf-8")
    (compile_root / "Adapter.java").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    for name, content in (support_sources or {}).items():
        support_path = compile_root / name
        support_path.parent.mkdir(parents=True, exist_ok=True)
        support_path.write_text(content, encoding="utf-8")
    subprocess.run(
        [
            "javac",
            "-encoding",
            "UTF-8",
            "DlrRuntime.java",
            "Adapter.java",
            *(support_sources or {}),
        ],
        cwd=compile_root,
        check=True,
        capture_output=True,
        text=True,
    )
    workspace = compile_root / "dlr-exec-1"
    (workspace / "input").mkdir(parents=True)
    (workspace / "input.json").write_text(json.dumps(input_value), encoding="utf-8")
    (workspace / "runtime_config.json").write_text(json.dumps(config or {}), encoding="utf-8")
    (workspace / "input_manifest.json").write_text(
        json.dumps({"execution_id": 1, "files": []}), encoding="utf-8"
    )
    environment = os.environ.copy()
    environment.update({f"DLR_SECRET_{key}": value for key, value in (secrets or {}).items()})
    subprocess.run(
        [
            "java",
            *(java_options or []),
            "-cp",
            str(compile_root),
            "DlrRuntime",
            str(workspace.resolve()),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return _json(workspace / "output.json")


def _javascript_errors(
    source: Path,
    input_values: list[dict[str, Any]],
    secrets: dict[str, str] | None = None,
) -> list[str | None]:
    script = """
import { pathToFileURL } from "node:url";
const module = await import(pathToFileURL(process.argv[1]));
const inputs = JSON.parse(process.argv[2]);
const secrets = JSON.parse(process.argv[3]);
const context = {
  config: {}, secrets: new Map(Object.entries(secrets)), inputFiles: [], logger: {},
};
const errors = [];
for (const input of inputs) {
  try { await module.handle(context, input); errors.push(null); }
  catch (error) { errors.push(error instanceof Error ? error.message : String(error)); }
}
process.stdout.write(JSON.stringify(errors));
"""
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            str(source),
            json.dumps(input_values),
            json.dumps(secrets or {}),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _java_errors(
    source: Path,
    input_values: list[dict[str, Any]],
    tmp_path: Path,
    secrets: dict[str, str] | None = None,
) -> list[str | None]:
    compile_root = tmp_path / source.parent.name
    compile_root.mkdir(parents=True)
    (compile_root / "DlrRuntime.java").write_text(JAVA_RUNTIME_SOURCE, encoding="utf-8")
    (compile_root / "Adapter.java").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run(
        ["javac", "-encoding", "UTF-8", "DlrRuntime.java", "Adapter.java"],
        cwd=compile_root,
        check=True,
        capture_output=True,
        text=True,
    )
    environment = os.environ.copy()
    environment.update({f"DLR_SECRET_{key}": value for key, value in (secrets or {}).items()})
    errors: list[str | None] = []
    for index, input_value in enumerate(input_values, start=1):
        workspace = compile_root / f"dlr-exec-{index}"
        (workspace / "input").mkdir(parents=True)
        (workspace / "input.json").write_text(json.dumps(input_value), encoding="utf-8")
        (workspace / "runtime_config.json").write_text("{}", encoding="utf-8")
        (workspace / "input_manifest.json").write_text(
            json.dumps({"execution_id": index, "files": []}), encoding="utf-8"
        )
        result = subprocess.run(
            ["java", "-cp", str(compile_root), "DlrRuntime", str(workspace.resolve())],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        errors.append(None if result.returncode == 0 else result.stdout + result.stderr)
    return errors


def test_inventory_hashes_receipts_and_maturity_are_exact() -> None:
    manifest, scenarios = _catalog_assets()
    assert len(manifest["themes"]) == 5
    assert len(scenarios) == 17
    assert len({item["slug"] for item in scenarios}) == 17
    assert len({item["logo_key"] for item in scenarios}) == 17
    assert sum(len(item["variants"]) for item in scenarios) == 51

    for scenario in scenarios:
        assert {variant["language"] for variant in scenario["variants"]} == LANGUAGES
        for variant in scenario["variants"]:
            source = _variant_source(variant)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            receipt = _json(CATALOG_ROOT / variant["receipt_resource"])
            assert variant["code_sha256"] == digest
            assert receipt["source_sha256"] == digest
            assert receipt["scenario_slug"] == scenario["slug"]
            assert receipt["version"] == scenario["version"]
            assert receipt["language"] == variant["language"]
            assert receipt["maturity"] == variant["maturity"]
            if receipt["maturity"] == "reference-generated":
                assert receipt["evidence"] == []
                assert receipt["verified_at"] is None

    assert TemplateCatalog(CATALOG_ROOT).validate_all_variant_sources() is None


def test_all_requirements_use_existing_parsers_and_exact_versions() -> None:
    _, scenarios = _catalog_assets()
    human_matrix = (CATALOG_ROOT.parents[4] / "docs/templates/source-coverage-matrix.md").read_text(
        encoding="utf-8"
    )
    python_pin = re.compile(r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^\s=<>~!*]+$")
    npm_pin = re.compile(
        r"^(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+@\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$"
    )
    maven_pin = re.compile(
        r"^[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+:\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.-]+)?$"
    )
    for scenario in scenarios:
        assert f"`{scenario['slug']}`" in human_matrix
        for variant in scenario["variants"]:
            requirements = variant["requirements"]
            lines = [line for line in requirements.splitlines() if line.strip()]
            assert all(f"`{line}`" in human_matrix for line in lines)
            if variant["language"] == "python":
                assert venv.dependency_specs(requirements) == lines
                assert all(python_pin.fullmatch(line) for line in lines)
            elif variant["language"] == "javascript":
                parsed = nodeenv.parse_requirements(requirements)
                assert len(parsed) == len(lines)
                assert all(npm_pin.fullmatch(line) for line in lines)
            else:
                assert len(javaenv.parse_requirements(requirements)) == len(lines)
                assert all(maven_pin.fullmatch(line) for line in lines)


def _required_use_mode(source: dict[str, Any]) -> str:
    if source["id"] in OFFICIAL_PROVENANCE_IDS:
        return "official-api"
    license_name = source["license"].lower()
    if any(
        marker in license_name for marker in ("gpl-", "elastic license", "no repository license")
    ):
        return "behavior-research-only"
    if license_name.startswith(("apache-2.0", "mit", "bsd-3-clause")):
        return "adaptation-allowed"
    raise AssertionError(f"unclassified provenance license: {source['license']}")


def _validate_source_use_mode(source: dict[str, Any]) -> None:
    required_mode = _required_use_mode(source)
    if source["use_mode"] != required_mode:
        raise ValueError(f"{source['id']} requires {required_mode}, got {source['use_mode']}")


def test_provenance_license_use_mode_policy_and_notice_are_closed() -> None:
    provenance = _json(CATALOG_ROOT / "provenance.json")
    notice = (CATALOG_ROOT.parents[4] / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert {source["id"] for source in provenance["sources"]} >= OFFICIAL_PROVENANCE_IDS
    for source in provenance["sources"]:
        required_mode = _required_use_mode(source)
        _validate_source_use_mode(source)
        if required_mode != "official-api":
            assert source["url"] in notice
            assert source["revision"] in notice

        canary_modes = {
            "official-api",
            "behavior-research-only",
            "adaptation-allowed",
        } - {required_mode}
        for mode in canary_modes:
            with pytest.raises(ValueError):
                _validate_source_use_mode(source | {"use_mode": mode})


def test_sources_pass_language_static_gates(tmp_path: Path) -> None:
    _, scenarios = _catalog_assets()
    variants = [variant for scenario in scenarios for variant in scenario["variants"]]
    python_sources = [_variant_source(item) for item in variants if item["language"] == "python"]
    javascript_sources = [
        _variant_source(item) for item in variants if item["language"] == "javascript"
    ]
    java_sources = [_variant_source(item) for item in variants if item["language"] == "java"]
    for source in python_sources:
        compile(source.read_text(encoding="utf-8"), str(source), "exec")
    for source in javascript_sources:
        subprocess.run(
            ["node", "--check", str(source)],
            check=True,
            capture_output=True,
            text=True,
        )

    if shutil.which("javac") is None:
        pytest.skip("javac is unavailable")
    parser_root = tmp_path / "java-parser"
    parser_root.mkdir()
    (parser_root / "SyntaxCheck.java").write_text(
        """
import com.sun.source.util.JavacTask;
import java.io.File;
import java.util.List;
import javax.tools.Diagnostic;
import javax.tools.DiagnosticCollector;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

public final class SyntaxCheck {
  public static void main(String[] args) throws Exception {
    var compiler = ToolProvider.getSystemJavaCompiler();
    var diagnostics = new DiagnosticCollector<JavaFileObject>();
    try (StandardJavaFileManager files = compiler.getStandardFileManager(
        diagnostics, null, null)) {
      var units = files.getJavaFileObjects(new File(args[0]));
      JavacTask task = (JavacTask) compiler.getTask(
          null, files, diagnostics, List.of("-proc:none"), null, units);
      task.parse();
    }
    var errors = diagnostics.getDiagnostics().stream()
        .filter(item -> item.getKind() == Diagnostic.Kind.ERROR).toList();
    if (!errors.isEmpty()) {
      errors.forEach(System.err::println);
      System.exit(1);
    }
  }
}
""",
        encoding="utf-8",
    )
    subprocess.run(
        ["javac", "SyntaxCheck.java"],
        cwd=parser_root,
        check=True,
        capture_output=True,
        text=True,
    )
    for source in java_sources:
        subprocess.run(
            ["java", "-cp", str(parser_root), "SyntaxCheck", str(source)],
            check=True,
            capture_output=True,
            text=True,
        )
    for scenario in scenarios:
        variant = next(item for item in scenario["variants"] if item["language"] == "java")
        if variant["requirements"]:
            continue
        source = _variant_source(variant)
        compile_root = tmp_path / scenario["slug"]
        compile_root.mkdir()
        (compile_root / "DlrRuntime.java").write_text(JAVA_RUNTIME_SOURCE, encoding="utf-8")
        (compile_root / "Adapter.java").write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
        subprocess.run(
            ["javac", "-encoding", "UTF-8", "DlrRuntime.java", "Adapter.java"],
            cwd=compile_root,
            check=True,
            capture_output=True,
            text=True,
        )


def test_sftp_javascript_stream_cap_and_private_key_passphrase(tmp_path: Path) -> None:
    source = CATALOG_ROOT / "variants/sftp-list-read/javascript.mjs"
    fixture_root = tmp_path / "sftp-js"
    module_root = fixture_root / "node_modules/ssh2-sftp-client"
    module_root.mkdir(parents=True)
    (fixture_root / "recipe.mjs").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    (module_root / "package.json").write_text(
        json.dumps({"type": "module", "exports": "./index.js"}), encoding="utf-8"
    )
    (module_root / "index.js").write_text(
        """
import { Readable } from "node:stream";
export default class MockSftpClient {
  constructor() {
    this.readCount = 0;
    this.sftp = {
      opendir: (_path, callback) => callback(null, Buffer.from("handle")),
      readdir: (_handle, callback) => {
        if (this.readCount++ === 0) {
          callback(null, [{
            filename: "large.json", attrs: { mode: 0o100000, size: 1, mtime: 0 },
          }]);
        } else {
          const error = new Error("EOF"); error.code = 1; callback(error);
        }
      },
      close: (_handle, callback) => callback(null),
    };
  }
  async connect(options) { globalThis.connectPassphrase = options.passphrase; }
  async realPath(value) { return value === "." ? "/base" : value; }
  createReadStream() { return Readable.from([Buffer.alloc(700), Buffer.alloc(700)]); }
  async end() {}
}
""",
        encoding="utf-8",
    )
    script = """
import { handle } from "./recipe.mjs";
const secrets = new Map([
  ["SFTP_USERNAME", "fixture-user"],
  ["SFTP_PRIVATE_KEY", "fixture-private-key"],
  ["SFTP_PRIVATE_KEY_PASSPHRASE", "fixture-passphrase"],
]);
const result = await handle(
  { config: {}, secrets, inputFiles: [], logger: {} },
  {
    host: "sftp.example", host_fingerprint_sha256: "SHA256:fixture",
    base_directory: "/base", path: ".", read_paths: ["large.json"],
    max_file_bytes: 1024, max_total_bytes: 1024,
  },
);
process.stdout.write(JSON.stringify({ result, passphrase: globalThis.connectPassphrase }));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=fixture_root,
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(completed.stdout)
    assert output["passphrase"] == "fixture-passphrase"
    assert output["result"]["summary"]["bytes_read"] == 0
    assert output["result"]["files"] == []
    assert output["result"]["contents"] == []
    assert output["result"]["partial"] is True
    assert output["result"]["checkpoint"] == {
        "start_at": "large.json",
        "reason": "output_limit",
    }


def test_transfer_read_selection_limits_are_rejected_before_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s3_python = runpy.run_path(str(CATALOG_ROOT / "variants/s3-compatible-list-read/python.py"))
    with pytest.raises(ValueError, match="invalid_read_keys"):
        s3_python["handle"](
            SimpleNamespace(
                secrets={"S3_ACCESS_KEY_ID": "fixture", "S3_SECRET_ACCESS_KEY": "fixture"}
            ),
            {"bucket": "fixture", "read_keys": ["key"] * 1_001},
        )

    monkeypatch.setitem(sys.modules, "paramiko", ModuleType("paramiko"))
    sftp_python = runpy.run_path(str(CATALOG_ROOT / "variants/sftp-list-read/python.py"))
    with pytest.raises(ValueError, match="invalid_read_paths"):
        sftp_python["handle"](
            SimpleNamespace(secrets={"SFTP_PASSWORD": "fixture"}),
            {
                "host": "sftp.example",
                "username": "fixture",
                "host_fingerprint_sha256": "SHA256:fixture",
                "base_directory": "/fixture",
                "read_paths": ["path"] * 5_001,
            },
        )

    cases = [
        (
            "s3-compatible-list-read",
            "@aws-sdk/client-s3",
            """
globalThis.constructed = 0;
export class S3Client { constructor() { globalThis.constructed += 1; } }
export class GetObjectCommand {}
export class ListObjectsV2Command {}
""",
            {
                "bucket": "fixture",
                "read_keys": ["key"] * 1_001,
            },
            {"S3_ACCESS_KEY_ID": "fixture", "S3_SECRET_ACCESS_KEY": "fixture"},
            "invalid_read_keys",
        ),
        (
            "sftp-list-read",
            "ssh2-sftp-client",
            """
globalThis.constructed = 0;
export default class MockClient { constructor() { globalThis.constructed += 1; } }
""",
            {
                "host": "sftp.example",
                "username": "fixture",
                "host_fingerprint_sha256": "SHA256:fixture",
                "base_directory": "/fixture",
                "read_paths": ["path"] * 5_001,
            },
            {"SFTP_PASSWORD": "fixture"},
            "invalid_read_paths",
        ),
    ]
    for slug, package, module_source, input_value, secrets, expected in cases:
        fixture_root = tmp_path / f"{slug}-selection"
        fixture_root.mkdir()
        (fixture_root / "recipe.mjs").write_text(
            (CATALOG_ROOT / f"variants/{slug}/javascript.mjs").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        module_root = fixture_root / "node_modules" / package
        module_root.mkdir(parents=True)
        (module_root / "package.json").write_text(
            json.dumps({"type": "module", "exports": "./index.js"}), encoding="utf-8"
        )
        (module_root / "index.js").write_text(module_source, encoding="utf-8")
        script = """
import { handle } from "./recipe.mjs";
const input = JSON.parse(process.argv[1]);
const secrets = new Map(Object.entries(JSON.parse(process.argv[2])));
try { await handle({ config: {}, secrets, inputFiles: [], logger: {} }, input); }
catch (error) {
  process.stdout.write(JSON.stringify({
    error: error.message, constructed: globalThis.constructed,
  }));
}
"""
        completed = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                script,
                json.dumps(input_value),
                json.dumps(secrets),
            ],
            cwd=fixture_root,
            check=True,
            capture_output=True,
            text=True,
        )
        assert json.loads(completed.stdout) == {"error": expected, "constructed": 0}

    s3_java = (CATALOG_ROOT / "variants/s3-compatible-list-read/java.java").read_text(
        encoding="utf-8"
    )
    sftp_java = (CATALOG_ROOT / "variants/sftp-list-read/java.java").read_text(encoding="utf-8")
    assert s3_java.index("values.size() > 1_000") < s3_java.index("S3Client.builder()")
    assert sftp_java.index('strings(input.get("read_paths"))') < sftp_java.index("new JSch()")


def test_sftp_directory_enumeration_stops_at_max_files_plus_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fingerprint = "SHA256:" + __import__("base64").b64encode(
        hashlib.sha256(b"fixture-key").digest()
    ).decode().rstrip("=")
    yielded = 0

    class Key:
        def asbytes(self) -> bytes:
            return b"fixture-key"

    class Transport:
        def __init__(self, _socket: object) -> None:
            return

        def start_client(self, **_kwargs: object) -> None:
            return

        def get_remote_server_key(self) -> Key:
            return Key()

        def auth_password(self, *_args: object) -> None:
            return

        def close(self) -> None:
            return

    class Sftp:
        def normalize(self, value: str) -> str:
            return value

        def listdir_iter(self, _path: str, *, read_aheads: int) -> Iterator[object]:
            nonlocal yielded
            assert read_aheads == 1
            for index in range(100_000):
                yielded += 1
                yield SimpleNamespace(
                    filename=f"file-{index:06d}",
                    st_mode=0o100000,
                    st_size=0,
                    st_mtime=0,
                )

        def close(self) -> None:
            return

    paramiko = ModuleType("paramiko")
    paramiko.Transport = Transport  # type: ignore[attr-defined]
    paramiko.SFTPClient = SimpleNamespace(  # type: ignore[attr-defined]
        from_transport=lambda _transport: Sftp()
    )
    monkeypatch.setitem(sys.modules, "paramiko", paramiko)
    monkeypatch.setattr("socket.create_connection", lambda *_args, **_kwargs: object())
    python = runpy.run_path(str(CATALOG_ROOT / "variants/sftp-list-read/python.py"))
    result = python["handle"](
        SimpleNamespace(secrets={"SFTP_PASSWORD": "fixture"}),
        {
            "host": "sftp.example",
            "username": "fixture",
            "host_fingerprint_sha256": fingerprint,
            "base_directory": "/base",
            "max_files": 2,
        },
    )
    assert yielded == 3
    assert result["summary"]["files"] == 2
    assert result["partial"] is True

    fixture_root = tmp_path / "sftp-bounded-js"
    fixture_root.mkdir()
    (fixture_root / "recipe.mjs").write_text(
        (CATALOG_ROOT / "variants/sftp-list-read/javascript.mjs").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    module_root = fixture_root / "node_modules/ssh2-sftp-client"
    module_root.mkdir(parents=True)
    (module_root / "package.json").write_text(
        json.dumps({"type": "module", "exports": "./index.js"}), encoding="utf-8"
    )
    (module_root / "index.js").write_text(
        """
export default class MockClient {
  constructor() {
    globalThis.reads = 0; globalThis.closed = false;
    this.sftp = {
      opendir: (_path, callback) => callback(null, Buffer.from("handle")),
      readdir: (_handle, callback) => {
        const at = globalThis.reads++;
        callback(null, [{
          filename: `file-${String(at).padStart(6, "0")}`,
          attrs: { mode: 0o100000, size: 0, mtime: 0 },
        }]);
      },
      close: (_handle, callback) => { globalThis.closed = true; callback(null); },
    };
  }
  async connect() {}
  async realPath(value) { return value === "." ? "/base" : value; }
  async end() {}
}
""",
        encoding="utf-8",
    )
    script = """
import { handle } from "./recipe.mjs";
const result = await handle({
  config: {}, secrets: new Map([["SFTP_PASSWORD", "fixture"]]),
  inputFiles: [], logger: {},
}, {
  host: "sftp.example", username: "fixture",
  host_fingerprint_sha256: "SHA256:fixture", base_directory: "/base", max_files: 2,
});
process.stdout.write(JSON.stringify({
  result, reads: globalThis.reads, closed: globalThis.closed,
}));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=fixture_root,
        check=True,
        capture_output=True,
        text=True,
    )
    javascript = json.loads(completed.stdout)
    assert javascript["reads"] == 3
    assert javascript["closed"] is True
    assert javascript["result"]["summary"]["files"] == 2
    assert javascript["result"]["partial"] is True

    java_source = (CATALOG_ROOT / "variants/sftp-list-read/java.java").read_text(encoding="utf-8")
    assert "ChannelSftp.LsEntrySelector.BREAK" in java_source
    assert "channel.ls(directory, entry ->" in java_source
    assert "for (Object value : channel.ls(directory))" not in java_source


def test_python_file_and_transfer_failures_are_stable_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = str(tmp_path / "AUDIT_PATH_ENDPOINT_SECRET")

    def assert_stable(call: Any, expected: str) -> None:
        with pytest.raises(ValueError) as captured:
            call()
        assert str(captured.value) == expected
        assert captured.value.__cause__ is None
        assert captured.value.__suppress_context__ is True
        assert sentinel not in repr(captured.value)

    csv = runpy.run_path(str(CATALOG_ROOT / "variants/csv-to-json/python.py"))
    assert_stable(
        lambda: csv["handle"](
            SimpleNamespace(
                input_files=[SimpleNamespace(path=sentinel, size_bytes=1)],
            ),
            {},
        ),
        "csv_operation_failed",
    )

    excel = runpy.run_path(str(CATALOG_ROOT / "variants/excel-to-json/python.py"))
    assert_stable(
        lambda: excel["handle"](
            SimpleNamespace(
                input_files=[
                    SimpleNamespace(path=sentinel, original_name="fixture.xlsx", size_bytes=1)
                ],
            ),
            {},
        ),
        "excel_operation_failed",
    )

    boto3 = ModuleType("boto3")
    botocore = ModuleType("botocore")
    botocore.__path__ = []  # type: ignore[attr-defined]
    botocore_config = ModuleType("botocore.config")
    botocore_config.Config = lambda **_kwargs: object()  # type: ignore[attr-defined]

    def fail_s3_client(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"driver leaked endpoint {sentinel}")

    boto3.client = fail_s3_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", botocore_config)
    s3 = runpy.run_path(str(CATALOG_ROOT / "variants/s3-compatible-list-read/python.py"))
    assert_stable(
        lambda: s3["handle"](
            SimpleNamespace(
                secrets={"S3_ACCESS_KEY_ID": "fixture", "S3_SECRET_ACCESS_KEY": "fixture"}
            ),
            {"bucket": "fixture", "endpoint": "https://storage.example"},
        ),
        "s3_operation_failed",
    )

    class CleanupFailingS3:
        def list_objects_v2(self, **_kwargs: object) -> dict[str, object]:
            return {"Contents": [], "IsTruncated": False}

        def close(self) -> None:
            raise RuntimeError(f"cleanup leaked {sentinel}")

    boto3.client = lambda *_args, **_kwargs: CleanupFailingS3()  # type: ignore[attr-defined]
    assert s3["handle"](
        SimpleNamespace(secrets={"S3_ACCESS_KEY_ID": "fixture", "S3_SECRET_ACCESS_KEY": "fixture"}),
        {"bucket": "fixture", "endpoint": "https://storage.example"},
    )["summary"] == {"objects": 0, "bytes_read": 0, "pages": 1}

    paramiko = ModuleType("paramiko")
    monkeypatch.setitem(sys.modules, "paramiko", paramiko)
    monkeypatch.setattr(
        "socket.create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(f"socket leaked endpoint {sentinel}")
        ),
    )
    sftp = runpy.run_path(str(CATALOG_ROOT / "variants/sftp-list-read/python.py"))
    assert_stable(
        lambda: sftp["handle"](
            SimpleNamespace(secrets={"SFTP_PASSWORD": "fixture"}),
            {
                "host": "sftp.example",
                "username": "fixture",
                "host_fingerprint_sha256": "SHA256:fixture",
                "base_directory": "/fixture",
            },
        ),
        "sftp_operation_failed",
    )


def test_javascript_file_and_transfer_failures_are_stable_and_redacted(
    tmp_path: Path,
) -> None:
    sentinel = "AUDIT_PATH_ENDPOINT_SECRET"
    cases = {
        "csv-to-json": {
            "context": {"inputFiles": [{"path": f"/{sentinel}", "sizeBytes": 1}]},
            "input": {},
            "expected": "csv_operation_failed",
        },
        "excel-to-json": {
            "context": {
                "inputFiles": [
                    {
                        "path": f"/{sentinel}",
                        "originalName": "fixture.xlsx",
                        "sizeBytes": 1,
                    }
                ]
            },
            "input": {},
            "expected": "excel_operation_failed",
        },
        "s3-compatible-list-read": {
            "context": {
                "secrets": {
                    "S3_ACCESS_KEY_ID": "fixture",
                    "S3_SECRET_ACCESS_KEY": "fixture",
                }
            },
            "input": {"bucket": "fixture", "endpoint": "https://storage.example"},
            "expected": "s3_operation_failed",
        },
        "sftp-list-read": {
            "context": {"secrets": {"SFTP_USERNAME": "fixture", "SFTP_PASSWORD": "fixture"}},
            "input": {
                "host": "sftp.example",
                "host_fingerprint_sha256": "SHA256:fixture",
                "base_directory": "/fixture",
            },
            "expected": "sftp_operation_failed",
        },
    }
    for slug, case in cases.items():
        fixture_root = tmp_path / slug
        fixture_root.mkdir()
        (fixture_root / "recipe.mjs").write_text(
            (CATALOG_ROOT / f"variants/{slug}/javascript.mjs").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        if slug == "excel-to-json":
            module = fixture_root / "node_modules/@e965/xlsx"
            module.mkdir(parents=True)
            (module / "package.json").write_text(
                json.dumps({"type": "module", "exports": "./index.js"}), encoding="utf-8"
            )
            (module / "index.js").write_text(
                "export function read() { throw new Error('parser should not run'); }\n"
                "export const utils = {};\n",
                encoding="utf-8",
            )
        elif slug == "s3-compatible-list-read":
            module = fixture_root / "node_modules/@aws-sdk/client-s3"
            module.mkdir(parents=True)
            (module / "package.json").write_text(
                json.dumps({"type": "module", "exports": "./index.js"}), encoding="utf-8"
            )
            (module / "index.js").write_text(
                f"""
export class S3Client {{
  async send() {{ throw new Error("provider leaked {sentinel}"); }}
  destroy() {{ throw new Error("cleanup leaked {sentinel}"); }}
}}
export class GetObjectCommand {{ constructor(value) {{ this.value = value; }} }}
export class ListObjectsV2Command {{ constructor(value) {{ this.value = value; }} }}
""",
                encoding="utf-8",
            )
        elif slug == "sftp-list-read":
            module = fixture_root / "node_modules/ssh2-sftp-client"
            module.mkdir(parents=True)
            (module / "package.json").write_text(
                json.dumps({"type": "module", "exports": "./index.js"}), encoding="utf-8"
            )
            (module / "index.js").write_text(
                f"""
export default class MockSftpClient {{
  async connect() {{ throw new Error("provider leaked {sentinel}"); }}
  end() {{ throw new Error("cleanup leaked {sentinel}"); }}
}}
""",
                encoding="utf-8",
            )
        script = """
import { handle } from "./recipe.mjs";
const raw = JSON.parse(process.argv[1]);
const context = {
  config: {}, inputFiles: raw.context.inputFiles ?? [], logger: {},
  secrets: new Map(Object.entries(raw.context.secrets ?? {})),
};
try {
  await handle(context, raw.input);
  process.stdout.write(JSON.stringify({ error: null }));
} catch (error) {
  process.stdout.write(JSON.stringify({
    error: error instanceof Error ? error.message : String(error),
    stack: error instanceof Error ? error.stack : "",
    cause: error instanceof Error ? error.cause ?? null : null,
  }));
}
"""
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", script, json.dumps(case)],
            cwd=fixture_root,
            check=True,
            capture_output=True,
            text=True,
        )
        output = json.loads(completed.stdout)
        assert output["error"] == case["expected"], slug
        assert output["cause"] is None, slug
        assert sentinel not in json.dumps(output), slug


def test_java_recipe_error_boundaries_strip_raw_filesystem_failures(tmp_path: Path) -> None:
    source = CATALOG_ROOT / "variants/csv-to-json/java.java"
    compile_root = tmp_path / "csv-java-redaction"
    compile_root.mkdir()
    (compile_root / "DlrRuntime.java").write_text(JAVA_RUNTIME_SOURCE, encoding="utf-8")
    (compile_root / "Adapter.java").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    (compile_root / "Probe.java").write_text(
        """
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
public final class Probe {
  public static void main(String[] args) throws Exception {
    Context context = new Context(Map.of(), List.of(
      new InputFile(0, Path.of(args[0]), "fixture.csv", "text/csv", 1, "0".repeat(64))
    ));
    try { new Adapter().handle(context, Map.of()); }
    catch (Exception error) {
      System.out.print(error.getMessage() + "|" + (error.getCause() == null));
    }
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
    sentinel = str(tmp_path / "AUDIT_JAVA_PATH_SECRET")
    completed = subprocess.run(
        ["java", "-cp", str(compile_root), "Probe", sentinel],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == "csv_operation_failed|true"
    assert sentinel not in completed.stdout + completed.stderr

    for slug, code in [
        ("excel-to-json", "excel_operation_failed"),
        ("s3-compatible-list-read", "s3_operation_failed"),
        ("sftp-list-read", "sftp_operation_failed"),
    ]:
        java_source = (CATALOG_ROOT / f"variants/{slug}/java.java").read_text(encoding="utf-8")
        assert f'throw new IllegalArgumentException("{code}")' in java_source
        assert "cleanup must not replace the stable result" in java_source
        assert f'new IllegalArgumentException("{code}", error)' not in java_source


def test_mysql_javascript_redacts_driver_and_cleanup_failures(tmp_path: Path) -> None:
    source = CATALOG_ROOT / "variants/mysql-readonly-snapshot/javascript.mjs"
    fixture_root = tmp_path / "mysql-js"
    module_root = fixture_root / "node_modules/mysql2"
    module_root.mkdir(parents=True)
    (fixture_root / "recipe.mjs").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    (module_root / "package.json").write_text(
        json.dumps({"type": "module", "exports": "./index.js"}),
        encoding="utf-8",
    )
    (module_root / "index.js").write_text(
        """
export default {
  createConnection(options) {
    const audit = globalThis.mysqlAudit[options.host] = {
      connect: 0, queries: [], destroy: 0, end: 0,
    };
    return {
      connect(callback) {
        audit.connect += 1;
        callback(options.host === "connect.example"
          ? new Error(`driver leaked ${options.password} mysql://${options.host}`)
          : null);
      },
      query(sql, values, callback) {
        audit.queries.push(typeof sql === "string" ? sql : sql.sql);
        const done = typeof values === "function" ? values : callback;
        done(new Error(`query leaked ${options.password}`));
      },
      destroy() {
        audit.destroy += 1;
        throw new Error(`destroy leaked ${options.password}`);
      },
      end() {
        audit.end += 1;
        throw new Error(`end leaked ${options.password}`);
      },
    };
  },
};
""",
        encoding="utf-8",
    )
    script = """
import { handle } from "./recipe.mjs";
globalThis.mysqlAudit = {};
const input = { sql: "SELECT id FROM inventory", max_rows: 10 };
const connect = await handle(
  { secrets: new Map([["MYSQL_DSN", "mysql://user:CONNECT_SECRET@connect.example/db"]]) },
  input,
);
const query = await handle(
  { secrets: new Map([["MYSQL_DSN", "mysql://user:QUERY_SECRET@query.example/db"]]) },
  input,
);
process.stdout.write(JSON.stringify({ connect, query, audit: globalThis.mysqlAudit }));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=fixture_root,
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(completed.stdout)
    assert output["connect"]["error"] == "database_connection_failed"
    assert output["query"]["error"] == "database_query_failed"
    assert output["audit"] == {
        "connect.example": {
            "connect": 1,
            "queries": [],
            "destroy": 1,
            "end": 0,
        },
        "query.example": {
            "connect": 1,
            "queries": ["START TRANSACTION READ ONLY", "ROLLBACK"],
            "destroy": 0,
            "end": 1,
        },
    }
    assert "SECRET" not in completed.stdout
    assert "mysql://" not in completed.stdout


def test_python_database_variants_redact_driver_and_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingConnection:
        def execute(self, *args: object) -> None:
            raise RuntimeError("AUDIT_QUERY_SECRET postgresql://secret.example/db")

        def cursor(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("AUDIT_QUERY_SECRET mysql://secret.example/db")

        def rollback(self) -> None:
            raise RuntimeError("AUDIT_ROLLBACK_SECRET")

        def close(self) -> None:
            raise RuntimeError("AUDIT_CLOSE_SECRET")

    cases = [
        ("postgresql-readonly-snapshot", "psycopg", "POSTGRES_DSN"),
        ("mysql-readonly-snapshot", "pymysql", "MYSQL_DSN"),
    ]
    input_value = {"sql": "SELECT id FROM inventory", "max_rows": 10}
    for slug, module_name, secret_name in cases:
        module = ModuleType(module_name)
        if module_name == "psycopg":
            rows_module = ModuleType("psycopg.rows")
            rows_module.dict_row = object()  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, "psycopg.rows", rows_module)
        else:
            cursors_module = ModuleType("pymysql.cursors")
            cursors_module.SSDictCursor = object()  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, "pymysql.cursors", cursors_module)
        monkeypatch.setitem(sys.modules, module_name, module)
        namespace = runpy.run_path(str(CATALOG_ROOT / f"variants/{slug}/python.py"))

        def fail_connect(*args: object, **kwargs: object) -> None:
            raise RuntimeError("AUDIT_CONNECT_SECRET database://secret.example/db")

        module.connect = fail_connect  # type: ignore[attr-defined]
        scheme = "postgresql" if module_name == "psycopg" else "mysql"
        context = SimpleNamespace(
            secrets={secret_name: f"{scheme}://user:AUDIT_DSN_SECRET@secret.example/db"}
        )
        connect_result = namespace["handle"](context, input_value)
        module.connect = lambda *args, **kwargs: FailingConnection()  # type: ignore[attr-defined]
        query_result = namespace["handle"](context, input_value)

        assert connect_result == {
            "rows": [],
            "count": 0,
            "partial": True,
            "error": "database_connection_failed",
        }
        assert query_result == {
            "rows": [],
            "count": 0,
            "partial": True,
            "error": "database_query_failed",
        }
        rendered = json.dumps([connect_result, query_result])
        assert "SECRET" not in rendered
        assert "database://" not in rendered


def test_postgresql_javascript_redacts_driver_and_cleanup_failures(tmp_path: Path) -> None:
    source = CATALOG_ROOT / "variants/postgresql-readonly-snapshot/javascript.mjs"
    fixture_root = tmp_path / "postgres-js"
    module_root = fixture_root / "node_modules/pg"
    module_root.mkdir(parents=True)
    (fixture_root / "recipe.mjs").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    (module_root / "package.json").write_text(
        json.dumps({"type": "module", "exports": "./index.js"}), encoding="utf-8"
    )
    (module_root / "index.js").write_text(
        """
class Client {
  constructor(options) {
    this.dsn = options.connectionString;
    if (this.dsn.includes("CONNECT_SECRET")) {
      throw new Error(`driver leaked ${this.dsn}`);
    }
  }
  async connect() {}
  async query() { throw new Error(`query leaked ${this.dsn}`); }
  async end() { throw new Error(`end leaked ${this.dsn}`); }
}
export default { Client };
""",
        encoding="utf-8",
    )
    script = """
import { handle } from "./recipe.mjs";
const input = { sql: "SELECT id FROM inventory", max_rows: 10 };
const connect = await handle(
  { secrets: new Map([["POSTGRES_DSN", "postgresql://user:CONNECT_SECRET@connect.example/db"]]) },
  input,
);
const query = await handle(
  { secrets: new Map([["POSTGRES_DSN", "postgresql://user:QUERY_SECRET@query.example/db"]]) },
  input,
);
process.stdout.write(JSON.stringify({ connect, query }));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=fixture_root,
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(completed.stdout)
    assert output["connect"]["error"] == "database_connection_failed"
    assert output["query"]["error"] == "database_query_failed"
    assert "SECRET" not in completed.stdout
    assert "postgresql://" not in completed.stdout


def test_java_database_variants_redact_driver_and_cleanup_failures(tmp_path: Path) -> None:
    fake_driver = r"""
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.Driver;
import java.sql.DriverManager;
import java.sql.DriverPropertyInfo;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.Properties;
import java.util.logging.Logger;

public final class FakeDriver implements Driver {
    static {
        try { DriverManager.registerDriver(new FakeDriver()); }
        catch (SQLException error) { throw new ExceptionInInitializerError(error); }
    }
    public Connection connect(String url, Properties info) throws SQLException {
        if (!acceptsURL(url)) return null;
        if (url.contains("connect")) {
            throw new SQLException("AUDIT_CONNECT_SECRET jdbc://secret.example/db");
        }
        return (Connection) Proxy.newProxyInstance(
            FakeDriver.class.getClassLoader(), new Class<?>[] {Connection.class},
            (proxy, method, args) -> switch (method.getName()) {
                case "createStatement" -> Proxy.newProxyInstance(
                    FakeDriver.class.getClassLoader(), new Class<?>[] {Statement.class},
                    (statement, called, values) -> {
                        if (called.getName().equals("execute")) {
                            throw new SQLException("AUDIT_QUERY_SECRET jdbc://secret.example/db");
                        }
                        return defaultValue(called.getReturnType());
                    }
                );
                case "getAutoCommit" -> false;
                case "rollback" -> throw new SQLException("AUDIT_ROLLBACK_SECRET");
                case "close" -> throw new SQLException("AUDIT_CLOSE_SECRET");
                case "isClosed", "isWrapperFor" -> false;
                case "unwrap" -> throw new SQLException("not_a_wrapper");
                default -> defaultValue(method.getReturnType());
            }
        );
    }
    private static Object defaultValue(Class<?> type) {
        if (!type.isPrimitive()) return null;
        if (type == boolean.class) return false;
        if (type == byte.class) return (byte) 0;
        if (type == short.class) return (short) 0;
        if (type == int.class) return 0;
        if (type == long.class) return 0L;
        if (type == float.class) return 0F;
        if (type == double.class) return 0D;
        if (type == char.class) return '\0';
        return null;
    }
    public boolean acceptsURL(String url) { return url != null && url.startsWith("jdbc:fixture:"); }
    public DriverPropertyInfo[] getPropertyInfo(String url, Properties info) {
        return new DriverPropertyInfo[0];
    }
    public int getMajorVersion() { return 1; }
    public int getMinorVersion() { return 0; }
    public boolean jdbcCompliant() { return false; }
    public Logger getParentLogger() { return Logger.getGlobal(); }
}
"""
    cases = [
        ("postgresql-readonly-snapshot", "POSTGRES_DSN"),
        ("mysql-readonly-snapshot", "MYSQL_DSN"),
    ]
    for slug, secret_name in cases:
        source = CATALOG_ROOT / f"variants/{slug}/java.java"
        for mode, expected in [
            ("connect", "database_connection_failed"),
            ("query", "database_query_failed"),
        ]:
            result = _java_result(
                source,
                {"sql": "SELECT id FROM inventory", "max_rows": 10},
                tmp_path / f"{slug}-{mode}",
                {secret_name: f"jdbc:fixture:{mode}"},
                support_sources={"FakeDriver.java": fake_driver},
                java_options=["-Djdbc.drivers=FakeDriver"],
            )
            assert result["error"] == expected
            assert result["count"] == 0
            assert "SECRET" not in json.dumps(result)


def test_excel_xlsx_preflight_and_legacy_xls_data_only_contract(tmp_path: Path) -> None:
    scenario = _json(CATALOG_ROOT / "scenarios/excel-to-json/metadata.json")
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}
    malicious = tmp_path / "external-link.xlsx"
    with ZipFile(malicious, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships><Relationship TargetMode="External" '
            'Target="https://secret.example/workbook.xlsx"/></Relationships>',
        )

    python_namespace = runpy.run_path(str(sources["python"]))
    with pytest.raises(ValueError, match="workbook_external_links_rejected"):
        python_namespace["_inspect_xlsx"](malicious)
    python_source = sources["python"].read_text(encoding="utf-8")
    assert "def _xls(" in python_source
    assert "xlrd.open_workbook" in python_source
    assert "formatting_info=False" in python_source
    assert "formula_evaluator" not in python_source.casefold()

    fixture_root = tmp_path / "excel-js"
    module_root = fixture_root / "node_modules/@e965/xlsx"
    module_root.mkdir(parents=True)
    (fixture_root / "recipe.mjs").write_text(
        sources["javascript"].read_text(encoding="utf-8"), encoding="utf-8"
    )
    (module_root / "package.json").write_text(
        json.dumps({"type": "module", "exports": "./index.js"}), encoding="utf-8"
    )
    (module_root / "index.js").write_text(
        """
export function read(bytes, options) {
  globalThis.parserCalls.push(options);
  return {
    SheetNames: ["Sheet1"],
    Sheets: { Sheet1: { "!ref": "A1:A1", A1: { v: "stored-value" } } },
    files: {},
  };
}
export const utils = {
  decode_range() { return { s: { r: 0, c: 0 }, e: { r: 0, c: 0 } }; },
  encode_cell() { return "A1"; },
};
""",
        encoding="utf-8",
    )
    script = """
import { handle } from "./recipe.mjs";
globalThis.parserCalls = [];
const context = { inputFiles: [{
  path: process.argv[1], originalName: "external-link.xlsx", sizeBytes: Number(process.argv[2]),
}] };
let externalError;
try { handle(context, {}); } catch (error) { externalError = error.message; }
const legacy = handle(
  { inputFiles: [{
    path: process.argv[1], originalName: "legacy.xls", sizeBytes: Number(process.argv[2]),
  }] },
  {},
);
process.stdout.write(JSON.stringify({
  externalError, legacy, parserCalls: globalThis.parserCalls,
}));
"""
    completed = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            str(malicious),
            str(malicious.stat().st_size),
        ],
        cwd=fixture_root,
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(completed.stdout)
    assert output["externalError"] == "workbook_external_links_rejected"
    assert len(output["parserCalls"]) == 1
    assert output["parserCalls"][0]["cellFormula"] is True
    assert output["legacy"]["active_content"] == {
        "executed": False,
        "formulas_replaced_with_null": False,
        "legacy_xls_data_only": True,
        "ooxml_preflight": False,
    }

    java_source = sources["java"].read_text(encoding="utf-8")
    assert java_source.index("if (!legacyXls) inspectXlsx(item.path)") < java_source.index(
        "WorkbookFactory.create"
    )
    assert 'boolean legacyXls = name.endsWith(".xls")' in java_source
    assert "FormulaEvaluator" not in java_source
    assert all(
        variant["input_contract"]["execution_input_file"]["extensions"] == [".xlsx", ".xls"]
        for variant in scenario["variants"]
    )
    assert "xlrd==2.0.2" in scenario["variants"][0]["requirements"]


def test_webhook_fixture_matches_all_three_published_sources(tmp_path: Path) -> None:
    scenario = _json(CATALOG_ROOT / "scenarios/webhook-json-normalization/metadata.json")
    input_value = {
        "payload": {
            "event": {"id": "evt-7", "at": "2026-09-05T09:30:00+08:00"},
            "owner": {"name": "Ada"},
        },
        "required": ["event.id"],
        "mappings": [
            {"source": "event.id", "target": "id", "required": True},
            {"source": "event.at", "target": "observed_at", "type": "datetime"},
            {"source": "owner.name", "target": "actor.name"},
            {"source": "missing", "target": "kind", "default": "example"},
        ],
    }
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}
    results = {
        "python": _python_result(sources["python"], input_value),
        "javascript": _javascript_result(sources["javascript"], input_value),
        "java": _java_result(sources["java"], input_value, tmp_path),
    }
    assert results["python"] == results["javascript"] == results["java"]
    assert results["python"] == {
        "valid": True,
        "data": {
            "id": "evt-7",
            "observed_at": "2026-09-05T01:30:00.000Z",
            "actor": {"name": "Ada"},
            "kind": "example",
        },
        "errors": [],
        "partial": False,
    }


def test_webhook_javascript_rejects_prototype_pollution_and_handles_wide_payload() -> None:
    source = CATALOG_ROOT / "variants/webhook-json-normalization/javascript.mjs"
    script = """
import { pathToFileURL } from "node:url";
const { handle } = await import(pathToFileURL(process.argv[1]));
const context = { config: {}, secrets: new Map(), inputFiles: [], logger: {} };
const polluted = handle(context, {
  payload: { value: "fixture" },
  mappings: [{ source: "value", target: "__proto__.AUDIT_POLLUTED" }],
});
const wide = handle(context, {
  payload: { values: Array.from({ length: 150_000 }, () => 0) },
  mappings: [], max_input_bytes: 1_048_576, max_depth: 32,
});
process.stdout.write(JSON.stringify({
  polluted, wide, objectPrototypePolluted: Object.prototype.AUDIT_POLLUTED ?? null,
}));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(completed.stdout)
    assert output["polluted"]["valid"] is False
    assert output["polluted"]["data"] is None
    assert output["objectPrototypePolluted"] is None
    assert output["wide"] == {"valid": True, "data": {}, "errors": [], "partial": False}


def test_webhook_mapping_fanout_stops_before_constructing_oversized_output(
    tmp_path: Path,
) -> None:
    source_root = CATALOG_ROOT / "variants/webhook-json-normalization"
    payload = "x" * 100_000
    input_value = {
        "payload": {"blob": payload},
        "mappings": [{"source": "blob", "target": f"copy_{index}"} for index in range(100)],
        "max_input_bytes": 200_000,
        "max_output_bytes": 120_000,
    }
    results = {
        "python": _python_result(source_root / "python.py", input_value),
        "javascript": _javascript_result(source_root / "javascript.mjs", input_value),
        "java": _java_result(source_root / "java.java", input_value, tmp_path / "webhook-fanout"),
    }
    assert results["python"] == results["javascript"] == results["java"]
    assert results["python"] == {
        "valid": False,
        "data": None,
        "errors": [{"field": "", "code": "output_limit"}],
        "partial": False,
    }


def test_webhook_datetime_validation_and_defaults_match_all_languages(tmp_path: Path) -> None:
    source_root = CATALOG_ROOT / "variants/webhook-json-normalization"
    sources = {
        "python": source_root / "python.py",
        "javascript": source_root / "javascript.mjs",
        "java": source_root / "java.java",
    }
    invalid = {
        "payload": {"event": {"at": "2023-02-30T00:00:00Z"}},
        "mappings": [{"source": "event.at", "target": "observed_at", "type": "datetime"}],
    }
    invalid_results = {
        "python": _python_result(sources["python"], invalid),
        "javascript": _javascript_result(sources["javascript"], invalid),
        "java": _java_result(sources["java"], invalid, tmp_path / "invalid-calendar"),
    }
    assert invalid_results["python"] == invalid_results["javascript"] == invalid_results["java"]
    assert invalid_results["python"] == {
        "valid": False,
        "data": None,
        "errors": [{"field": "mappings[0]", "code": "invalid_value"}],
        "partial": False,
    }

    defaulted = {
        "payload": {},
        "mappings": [
            {
                "source": "event.at",
                "target": "observed_at",
                "type": "datetime",
                "default": "2024-01-01T08:00:00+08:00",
            }
        ],
    }
    default_results = {
        "python": _python_result(sources["python"], defaulted),
        "javascript": _javascript_result(sources["javascript"], defaulted),
        "java": _java_result(sources["java"], defaulted, tmp_path / "default-calendar"),
    }
    assert default_results["python"] == default_results["javascript"] == default_results["java"]
    assert default_results["python"] == {
        "valid": True,
        "data": {"observed_at": "2024-01-01T00:00:00.000Z"},
        "errors": [],
        "partial": False,
    }


def test_json_mapping_numeric_domain_matches_all_languages(tmp_path: Path) -> None:
    scenario = _json(CATALOG_ROOT / "scenarios/json-mapping-cleaning/metadata.json")
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}
    input_value = {
        "records": [
            {
                "safe_max": "9007199254740991",
                "safe_min": "-9007199254740991",
                "whole_number": 1.0,
                "decimal": "1.25",
                "exponent": "1e2",
            }
        ],
        "mappings": [
            {"pointer": "/safe_max", "target": "safe_max", "type": "integer"},
            {"pointer": "/safe_min", "target": "safe_min", "type": "integer"},
            {"pointer": "/whole_number", "target": "whole_number", "type": "integer"},
            {"pointer": "/decimal", "target": "decimal", "type": "number"},
            {"pointer": "/exponent", "target": "exponent", "type": "number"},
        ],
    }
    results = {
        "python": _python_result(sources["python"], input_value),
        "javascript": _javascript_result(sources["javascript"], input_value),
        "java": _java_result(sources["java"], input_value, tmp_path / "valid"),
    }
    assert results["python"] == results["javascript"] == results["java"]
    assert results["python"]["records"] == [
        {
            "safe_max": 9_007_199_254_740_991,
            "safe_min": -9_007_199_254_740_991,
            "whole_number": 1,
            "decimal": 1.25,
            "exponent": 100.0,
        }
    ]
    json.dumps(results, allow_nan=False)

    invalid_cases = [
        ("integer", "1.0"),
        ("integer", "9007199254740992"),
        ("number", "NaN"),
        ("number", "Infinity"),
        ("number", "1e9999"),
    ]
    for index, (kind, value) in enumerate(invalid_cases):
        invalid = {
            "records": [{"value": value}],
            "mappings": [{"pointer": "/value", "target": "value", "type": kind}],
        }
        with pytest.raises(ValueError, match="^conversion_failed$"):
            _python_result(sources["python"], invalid)
        with pytest.raises(subprocess.CalledProcessError):
            _javascript_result(sources["javascript"], invalid)
        with pytest.raises(subprocess.CalledProcessError):
            _java_result(sources["java"], invalid, tmp_path / f"invalid-{index}")


def test_json_mapping_blocks_prototype_injection_and_bounds_sorted_candidates(
    tmp_path: Path,
) -> None:
    scenario = _json(CATALOG_ROOT / "scenarios/json-mapping-cleaning/metadata.json")
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}
    prototype_input = {
        "records": [{"value": {"AUDIT_POLLUTED": True}}],
        "mappings": [{"pointer": "/value", "target": "__proto__"}],
    }
    prototype_results = {
        "python": _python_result(sources["python"], prototype_input),
        "javascript": _javascript_result(sources["javascript"], prototype_input),
        "java": _java_result(sources["java"], prototype_input, tmp_path / "prototype"),
    }
    assert (
        prototype_results["python"] == prototype_results["javascript"] == prototype_results["java"]
    )
    assert prototype_results["python"]["records"] == [{"__proto__": {"AUDIT_POLLUTED": True}}]

    probe = """
import { pathToFileURL } from "node:url";
const { handle } = await import(pathToFileURL(process.argv[1]));
const result = handle({ config: {}, secrets: new Map(), inputFiles: [] }, {
  records: [{ value: { AUDIT_POLLUTED: true } }],
  mappings: [{ pointer: "/value", target: "__proto__" }],
});
const record = result.records[0];
process.stdout.write(JSON.stringify({
  own: Object.hasOwn(record, "__proto__"),
  nullPrototype: Object.getPrototypeOf(record) === null,
  inherited: record.AUDIT_POLLUTED ?? null,
  global: Object.prototype.AUDIT_POLLUTED ?? null,
}));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", probe, str(sources["javascript"])],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {
        "own": True,
        "nullPrototype": True,
        "inherited": None,
        "global": None,
    }

    bounded_input = {
        "records": [
            {"key": "b", "payload": "x" * 2_000},
            {"key": "a", "payload": "fits"},
            {"key": "c", "payload": "must-not-skip-blocker"},
        ],
        "mappings": [
            {"pointer": "/key", "target": "key"},
            {"pointer": "/payload", "target": "payload"},
        ],
        "sort": {"field": "key", "direction": "asc"},
        "max_output_bytes": 96,
    }
    bounded_results = {
        "python": _python_result(sources["python"], bounded_input),
        "javascript": _javascript_result(sources["javascript"], bounded_input),
        "java": _java_result(sources["java"], bounded_input, tmp_path / "bounded"),
    }
    assert bounded_results["python"] == bounded_results["javascript"] == bounded_results["java"]
    assert bounded_results["python"] == {
        "records": [{"key": "a", "payload": "fits"}],
        "count": 1,
        "partial": True,
        "checkpoint": {"reason": "output_limit", "emitted": 1},
    }
    assert (
        len(json.dumps(bounded_results["python"]["records"], separators=(",", ":")).encode()) <= 96
    )
    assert "bounded + [item]" not in sources["python"].read_text(encoding="utf-8")
    assert "[...bounded, item]" not in sources["javascript"].read_text(encoding="utf-8")
    assert "new ArrayList<>(bounded)" not in sources["java"].read_text(encoding="utf-8")


def test_json_mapping_rejects_malformed_filters_and_bounds_field_fanout(
    tmp_path: Path,
) -> None:
    source_root = CATALOG_ROOT / "variants/json-mapping-cleaning"
    malformed_filter = {
        "records": [{"value": "fixture"}],
        "mappings": [{"pointer": "/value", "target": "value"}],
        "filters": [{"op": "exists", "value": False}],
    }
    with pytest.raises(ValueError, match="^invalid_filter$"):
        _python_result(source_root / "python.py", malformed_filter)
    with pytest.raises(subprocess.CalledProcessError) as javascript_filter:
        _javascript_result(source_root / "javascript.mjs", malformed_filter)
    assert "invalid_filter" in javascript_filter.value.stderr
    with pytest.raises(subprocess.CalledProcessError) as java_filter:
        _java_result(source_root / "java.java", malformed_filter, tmp_path / "invalid-filter")
    assert "invalid_filter" in java_filter.value.stderr

    invalid_pointer = {
        "records": [{"value": "fixture"}],
        "mappings": [{"pointer": "not/a/pointer", "target": "value"}],
    }
    with pytest.raises(ValueError, match="^invalid_json_pointer$"):
        _python_result(source_root / "python.py", invalid_pointer)
    with pytest.raises(subprocess.CalledProcessError) as javascript_pointer:
        _javascript_result(source_root / "javascript.mjs", invalid_pointer)
    assert "invalid_json_pointer" in javascript_pointer.value.stderr
    with pytest.raises(subprocess.CalledProcessError) as java_pointer:
        _java_result(source_root / "java.java", invalid_pointer, tmp_path / "invalid-pointer")
    assert "invalid_json_pointer" in java_pointer.value.stderr

    large_value = "x" * 100_000
    fanout = {
        "records": [{"blob": large_value}],
        "mappings": [{"pointer": "/blob", "target": f"copy_{index}"} for index in range(100)],
        "max_output_bytes": 120_000,
    }
    results = {
        "python": _python_result(source_root / "python.py", fanout),
        "javascript": _javascript_result(source_root / "javascript.mjs", fanout),
        "java": _java_result(source_root / "java.java", fanout, tmp_path / "mapping-fanout"),
    }
    assert results["python"] == results["javascript"] == results["java"]
    assert results["python"] == {
        "records": [],
        "count": 0,
        "partial": True,
        "checkpoint": {"reason": "output_limit", "emitted": 0},
    }


def test_alicloud_regions_are_rejected_before_sdk_use_in_all_languages(
    tmp_path: Path,
) -> None:
    slugs = CLOUD_SCENARIOS[:3]
    malicious = [
        "cn-hangzhou@audit.example",
        "cn-hangzhou/path",
        "cn-hangzhou?query",
        "cn-hangzhou#fragment",
    ]
    base_input = {
        "mode": "preview",
        "account": "fixture-account",
        "max_pages": 5,
        "max_records": 10,
        "max_bytes": 65_536,
    }
    secrets = {
        "ALICLOUD_ACCESS_KEY_ID": "AUDIT_ACCESS_SECRET",
        "ALICLOUD_ACCESS_KEY_SECRET": "AUDIT_SIGNING_SECRET",
    }

    for slug in slugs:
        scenario = _json(CATALOG_ROOT / f"scenarios/{slug}/metadata.json")
        sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}
        python_namespace = runpy.run_path(str(sources["python"]))
        assert (
            python_namespace["_alicloud_endpoint"]("ecs.cn-hangzhou.aliyuncs.com", "cn-shanghai")
            == "ecs.cn-shanghai.aliyuncs.com"
        )
        for region in malicious:
            with pytest.raises(ValueError, match="^account_and_regions_required$"):
                _python_result(sources["python"], base_input | {"regions": [region]}, secrets)

        fixture_root = tmp_path / f"{slug}-js"
        fixture_root.mkdir()
        (fixture_root / "recipe.mjs").write_text(
            sources["javascript"].read_text(encoding="utf-8"), encoding="utf-8"
        )
        openapi = fixture_root / "node_modules/@alicloud/openapi-client"
        tea = fixture_root / "node_modules/@alicloud/tea-util"
        openapi.mkdir(parents=True)
        tea.mkdir(parents=True)
        for module in (openapi, tea):
            (module / "package.json").write_text(
                json.dumps({"type": "module", "exports": "./index.js"}), encoding="utf-8"
            )
        (openapi / "index.js").write_text(
            """
export class Config { constructor(value) { this.value = value; } }
export class OpenApiRequest { constructor(value) { this.value = value; } }
export class Params { constructor(value) { this.value = value; } }
export default class OpenApi {
  constructor() { globalThis.AUDIT_SDK_CALLS += 1; }
  async callApi() { globalThis.AUDIT_SDK_CALLS += 1; return { body: {} }; }
}
""",
            encoding="utf-8",
        )
        (tea / "index.js").write_text(
            "export class RuntimeOptions { constructor(value) { this.value = value; } }\n",
            encoding="utf-8",
        )
        javascript_probe = """
globalThis.AUDIT_SDK_CALLS = 0;
const { handle } = await import("./recipe.mjs");
const regions = JSON.parse(process.argv[1]);
const base = JSON.parse(process.argv[2]);
const secrets = new Map(Object.entries(JSON.parse(process.argv[3])));
const errors = [];
for (const region of regions) {
  try {
    await handle(
      { config: {}, secrets, inputFiles: [], logger: {} },
      { ...base, regions: [region] },
    );
    errors.push("NO_ERROR");
  } catch (error) {
    errors.push(error instanceof Error ? error.message : String(error));
  }
}
process.stdout.write(JSON.stringify({ errors, calls: globalThis.AUDIT_SDK_CALLS }));
"""
        completed = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                javascript_probe,
                json.dumps(malicious),
                json.dumps(base_input),
                json.dumps(secrets),
            ],
            cwd=fixture_root,
            check=True,
            capture_output=True,
            text=True,
        )
        assert json.loads(completed.stdout) == {
            "errors": ["account_and_regions_required"] * len(malicious),
            "calls": 0,
        }

        java_root = tmp_path / f"{slug}-java"
        java_root.mkdir()
        (java_root / "DlrRuntime.java").write_text(JAVA_RUNTIME_SOURCE, encoding="utf-8")
        (java_root / "Adapter.java").write_text(
            sources["java"].read_text(encoding="utf-8"), encoding="utf-8"
        )
        java_stubs = {
            "com/aliyun/teaopenapi/Client.java": """
package com.aliyun.teaopenapi;
import com.aliyun.teaopenapi.models.*;
import com.aliyun.teautil.models.RuntimeOptions;
import java.util.Map;
public class Client {
  public Client(Config value) {}
  public Map<String, Object> callApi(Params p, OpenApiRequest r, RuntimeOptions o) {
    return Map.of("body", Map.of());
  }
}
""",
            "com/aliyun/teaopenapi/models/Config.java": """
package com.aliyun.teaopenapi.models;
public class Config {
  public Config setAccessKeyId(String v){return this;}
  public Config setAccessKeySecret(String v){return this;}
  public Config setSecurityToken(String v){return this;}
  public Config setEndpoint(String v){return this;}
  public Config setRegionId(String v){return this;}
  public Config setProtocol(String v){return this;}
  public Config setReadTimeout(int v){return this;}
  public Config setConnectTimeout(int v){return this;}
}
""",
            "com/aliyun/teaopenapi/models/OpenApiRequest.java": """
package com.aliyun.teaopenapi.models;
import java.util.Map;
public class OpenApiRequest { public OpenApiRequest setQuery(Map<String,String> v){return this;} }
""",
            "com/aliyun/teaopenapi/models/Params.java": """
package com.aliyun.teaopenapi.models;
public class Params {
  public Params setAction(String v){return this;} public Params setVersion(String v){return this;}
  public Params setProtocol(String v){return this;}
  public Params setPathname(String v){return this;}
  public Params setMethod(String v){return this;} public Params setAuthType(String v){return this;}
  public Params setBodyType(String v){return this;}
  public Params setReqBodyType(String v){return this;}
  public Params setStyle(String v){return this;}
}
""",
            "com/aliyun/teautil/models/RuntimeOptions.java": """
package com.aliyun.teautil.models;
public class RuntimeOptions {
  public RuntimeOptions setReadTimeout(int v){return this;}
  public RuntimeOptions setConnectTimeout(int v){return this;}
  public RuntimeOptions setAutoretry(boolean v){return this;}
}
""",
        }
        for name, content in java_stubs.items():
            path = java_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (java_root / "Probe.java").write_text(
            """
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
public final class Probe {
  public static void main(String[] args) throws Exception {
    List<String> errors = new ArrayList<>();
    for (String region : args) {
      Map<String,Object> input = new LinkedHashMap<>();
      input.put("mode", "preview"); input.put("account", "fixture-account");
      input.put("regions", List.of(region)); input.put("max_pages", 5);
      input.put("max_records", 10); input.put("max_bytes", 65536);
      try { new Adapter().handle(new Context(Map.of()), input); errors.add("NO_ERROR"); }
      catch (Exception error) { errors.add(error.getMessage()); }
    }
    System.out.print(Json.stringify(errors));
  }
}
""",
            encoding="utf-8",
        )
        java_sources = ["DlrRuntime.java", "Adapter.java", "Probe.java", *java_stubs]
        subprocess.run(
            ["javac", "-encoding", "UTF-8", "-d", str(java_root), *java_sources],
            cwd=java_root,
            check=True,
            capture_output=True,
            text=True,
        )
        environment = os.environ.copy()
        environment.update(
            {
                "DLR_SECRET_ALICLOUD_ACCESS_KEY_ID": secrets["ALICLOUD_ACCESS_KEY_ID"],
                "DLR_SECRET_ALICLOUD_ACCESS_KEY_SECRET": secrets["ALICLOUD_ACCESS_KEY_SECRET"],
            }
        )
        completed = subprocess.run(
            ["java", "-cp", str(java_root), "Probe", *malicious],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert json.loads(completed.stdout) == ["account_and_regions_required"] * len(malicious)
        assert '.replace("cn-hangzhou", region)' not in sources["java"].read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("slug", "region", "resource", "body", "expected_targets"),
    [
        (
            "alicloud-compute-container-topology",
            "cn-hangzhou",
            "ecs_instance",
            {
                "Instances": {
                    "Instance": [
                        {
                            "InstanceId": "i-example-1",
                            "VpcAttributes": {
                                "VpcId": "vpc-example-1",
                                "VSwitchId": "vsw-example-1",
                            },
                            "SecurityGroupIds": {
                                "SecurityGroupId": [
                                    "sg-example-1",
                                    {"invalid": "object"},
                                    True,
                                    7,
                                    ["invalid-nested-list"],
                                ]
                            },
                        }
                    ]
                }
            },
            {
                ("located_in", "vpc", "vpc-example-1"),
                ("located_in", "vswitch", "vsw-example-1"),
                ("protected_by", "security_group", "sg-example-1"),
            },
        ),
        (
            "alicloud-network-ingress-topology",
            "cn-hangzhou",
            "vswitch",
            {"VSwitches": {"VSwitch": [{"VSwitchId": "vsw-example-1", "VpcId": "vpc-example-1"}]}},
            {("member_of", "vpc", "vpc-example-1")},
        ),
        (
            "alicloud-database-middleware-inventory",
            "cn-hangzhou",
            "rds",
            {
                "Items": {
                    "DBInstance": [
                        {
                            "DBInstanceId": "rds-example-1",
                            "VpcId": "vpc-example-1",
                            "VSwitchId": "vsw-example-1",
                        }
                    ]
                }
            },
            {
                ("located_in", "vpc", "vpc-example-1"),
                ("located_in", "vswitch", "vsw-example-1"),
            },
        ),
        (
            "tencentcloud-compute-container-topology",
            "ap-example-1",
            "cvm_instance",
            {
                "InstanceSet": [
                    {
                        "InstanceId": "ins-example-1",
                        "VirtualPrivateCloud": {
                            "VpcId": "vpc-example-1",
                            "SubnetId": "subnet-example-1",
                        },
                        "SecurityGroupIds": [
                            "sg-example-1",
                            {"invalid": "object"},
                            True,
                            7,
                            ["invalid-nested-list"],
                        ],
                    }
                ]
            },
            {
                ("located_in", "vpc", "vpc-example-1"),
                ("located_in", "subnet", "subnet-example-1"),
                ("protected_by", "security_group", "sg-example-1"),
            },
        ),
        (
            "tencentcloud-network-ingress-topology",
            "ap-example-1",
            "subnet",
            {"SubnetSet": [{"SubnetId": "subnet-example-1", "VpcId": "vpc-example-1"}]},
            {("member_of", "vpc", "vpc-example-1")},
        ),
        (
            "tencentcloud-database-middleware-inventory",
            "ap-example-1",
            "cdb",
            {
                "Items": [
                    {
                        "InstanceId": "cdb-example-1",
                        "VpcId": "vpc-example-1",
                        "SubnetId": "subnet-example-1",
                    }
                ]
            },
            {
                ("located_in", "vpc", "vpc-example-1"),
                ("located_in", "subnet", "subnet-example-1"),
            },
        ),
    ],
)
def test_cloud_fixtures_preserve_stable_cross_scenario_relationship_keys(
    tmp_path: Path,
    slug: str,
    region: str,
    resource: str,
    body: dict[str, Any],
    expected_targets: set[tuple[str, str, str]],
) -> None:
    scenario = _json(CATALOG_ROOT / f"scenarios/{slug}/metadata.json")
    input_value = {
        "mode": "preview",
        "account": "example-account",
        "regions": [region],
        "max_pages": 100,
        "max_records": 10,
        "max_bytes": 65536,
        "page_size": 100,
        "fixture_pages": {resource: [body]},
    }
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}
    javascript = (
        _javascript_result_with_alicloud_stubs(
            sources["javascript"], input_value, tmp_path / f"{slug}-javascript"
        )
        if slug.startswith("alicloud-")
        else _javascript_result(sources["javascript"], input_value)
    )
    results = {
        "python": _python_result(sources["python"], input_value),
        "javascript": javascript,
        "java": _java_result(
            sources["java"],
            input_value,
            tmp_path / slug,
            support_sources=ALICLOUD_JAVA_STUBS if slug.startswith("alicloud-") else None,
        ),
    }
    provider = slug.split("-", 1)[0]
    expected_relations = {
        (relation_type, f"{provider}:example-account:{region}:{target_type}:{target_id}")
        for relation_type, target_type, target_id in expected_targets
    }
    for language, result in results.items():
        assert len(result["assets"]) == 1, language
        assert result["summary"]["relationships"] == len(expected_targets), language
        assert result["summary"]["failures"] == [], language
        assert result["partial"] is False, language
        asset_keys = {item["external_key"] for item in result["assets"]}
        assert all(
            item["from"] in asset_keys and item["to"] not in asset_keys
            for item in result["relationships"]
        ), language
        assert {
            (item["type"], item["to"]) for item in result["relationships"]
        } == expected_relations, language


def _failure_codes(result: dict[str, Any]) -> set[str]:
    values = [*result["summary"]["failures"], *result.get("failed", [])]
    return {str(item.get("error")) if isinstance(item, dict) else str(item) for item in values}


@pytest.mark.parametrize(
    ("slug", "resource", "body_path", "invalid_record"),
    [
        (
            "alicloud-compute-container-topology",
            "ecs_instance",
            ("Instances", "Instance"),
            "not-an-object",
        ),
        (
            "alicloud-compute-container-topology",
            "ecs_instance",
            ("Instances", "Instance"),
            {"InstanceName": "missing-stable-id"},
        ),
        (
            "tencentcloud-compute-container-topology",
            "cvm_instance",
            ("InstanceSet",),
            "not-an-object",
        ),
        (
            "tencentcloud-compute-container-topology",
            "cvm_instance",
            ("InstanceSet",),
            {"InstanceName": "missing-stable-id"},
        ),
    ],
)
def test_cloud_invalid_provider_records_fail_closed_before_sync_in_all_languages(
    tmp_path: Path,
    slug: str,
    resource: str,
    body_path: tuple[str, ...],
    invalid_record: object,
) -> None:
    scenario = _json(CATALOG_ROOT / f"scenarios/{slug}/metadata.json")
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}
    body: dict[str, Any] = {}
    target = body
    for key in body_path[:-1]:
        child: dict[str, Any] = {}
        target[key] = child
        target = child
    target[body_path[-1]] = [invalid_record]
    provider = slug.split("-", 1)[0]
    input_value = {
        "mode": "sync",
        "scan_id": "scan-invalid-provider-record",
        "source_scope": f"{provider}:fixture:example-region-1",
        "account": "fixture-account",
        "regions": ["example-region-1"],
        "max_pages": 50,
        "max_records": 10,
        "max_bytes": 4096,
        "page_size": 100,
        "fixture_pages": {resource: [body]},
    }
    with _fake_cmdb() as (base_url, state):
        secrets = {"CMDB_TOKEN": "fixture-token"}
        config = {"cmdb_base_url": base_url}
        javascript = (
            _javascript_result_with_alicloud_stubs(
                sources["javascript"],
                input_value,
                tmp_path / f"{slug}-javascript-{type(invalid_record).__name__}",
                secrets,
                config,
            )
            if slug.startswith("alicloud-")
            else _javascript_result(sources["javascript"], input_value, secrets, config)
        )
        results = {
            "python": _python_result(sources["python"], input_value, secrets, config),
            "javascript": javascript,
            "java": _java_result(
                sources["java"],
                input_value,
                tmp_path / f"{slug}-java-{type(invalid_record).__name__}",
                secrets,
                config,
                support_sources=ALICLOUD_JAVA_STUBS if slug.startswith("alicloud-") else None,
            ),
        }
    assert state.operations == []
    for language, result in results.items():
        assert result["mode"] == "sync", language
        assert result["partial"] is True, language
        assert result["checkpoint"] is not None, language
        assert "invalid_source_record" in _failure_codes(result), language
        assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 4096


@pytest.mark.parametrize("mode", ["preview", "sync"])
def test_tencent_transport_and_final_envelopes_share_global_byte_bounds_in_all_languages(
    tmp_path: Path,
    mode: str,
) -> None:
    slug = "tencentcloud-compute-container-topology"
    scenario = _json(CATALOG_ROOT / f"scenarios/{slug}/metadata.json")
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}
    first_page = {
        "InstanceSet": [
            {"InstanceId": "cvm-example-1", "InstanceName": "first", "Padding": "a" * 470}
        ]
    }
    second_page = {
        "InstanceSet": [
            {"InstanceId": "cvm-example-2", "InstanceName": "second", "Padding": "b" * 470}
        ]
    }
    page_sizes = [
        len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode())
        for page in (first_page, second_page)
    ]
    assert max(page_sizes) <= 1024 < sum(page_sizes)
    input_value = {
        "mode": mode,
        "scan_id": "scan-transport-budget",
        "source_scope": "tencentcloud:fixture:ap-example-1",
        "account": "fixture-account",
        "regions": ["ap-example-1"],
        "max_pages": 50,
        "max_records": 10,
        "max_bytes": 1024,
        "page_size": 1,
        "fixture_pages": {"cvm_instance": [first_page, second_page]},
    }
    with _fake_cmdb() as (base_url, state):
        secrets = {"CMDB_TOKEN": "fixture-token"}
        config = {"cmdb_base_url": base_url}
        results = {
            "python": _python_result(sources["python"], input_value, secrets, config),
            "javascript": _javascript_result(sources["javascript"], input_value, secrets, config),
            "java": _java_result(
                sources["java"], input_value, tmp_path / f"transport-{mode}", secrets, config
            ),
        }
    assert state.operations == []
    assert results["python"] == results["javascript"] == results["java"]
    result = results["python"]
    assert result["partial"] is True
    assert result["checkpoint"]["limit_reached"] is True
    assert result["summary"]["pages"] == 1
    assert result["summary"]["assets"] == 1
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 1024


def test_tencent_sync_compacts_failure_scope_to_keep_invalid_record_envelope_bounded(
    tmp_path: Path,
) -> None:
    slug = "tencentcloud-compute-container-topology"
    scenario = _json(CATALOG_ROOT / f"scenarios/{slug}/metadata.json")
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}
    input_value = {
        "mode": "sync",
        "scan_id": "s" * 128,
        "source_scope": "x" * 128,
        "account": "fixture-account",
        "regions": ["r" * 128],
        "max_pages": 50,
        "max_records": 10,
        "max_bytes": 1024,
        "page_size": 100,
        "fixture_pages": {"cvm_instance": [{"InstanceSet": [{"name": "missing-id"}]}]},
    }
    results = {
        "python": _python_result(sources["python"], input_value),
        "javascript": _javascript_result(sources["javascript"], input_value),
        "java": _java_result(sources["java"], input_value, tmp_path / "compact-failure"),
    }
    assert results["python"] == results["javascript"] == results["java"]
    result = results["python"]
    assert result["partial"] is True
    assert "invalid_source_record" in _failure_codes(result)
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 1024


def test_tencent_java_preview_commits_only_full_envelopes_within_byte_budget(
    tmp_path: Path,
) -> None:
    source = CATALOG_ROOT / "variants/tencentcloud-network-ingress-topology/java.java"
    input_value = {
        "mode": "preview",
        "account": "fixture-account",
        "regions": ["ap-example-1"],
        "max_pages": 50,
        "max_records": 100,
        "max_bytes": 2048,
        "page_size": 100,
        "fixture_pages": {
            "vpc": [{"VpcSet": [{"VpcId": "vpc-example-1", "VpcName": "fixture-vpc"}]}],
            "subnet": [
                {
                    "SubnetSet": [
                        {
                            "SubnetId": f"subnet-example-{index:03d}",
                            "SubnetName": f"fixture-subnet-{index:03d}",
                            "VpcId": "vpc-example-1",
                            "Zone": "ap-example-1a",
                        }
                        for index in range(15)
                    ]
                }
            ],
        },
    }
    result = _java_result(source, input_value, tmp_path)
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= input_value["max_bytes"]
    assert result["partial"] is True
    assert result["checkpoint"]["limit_reached"] is True
    assert result["summary"]["relationships"] > 0

    for slug in CLOUD_SCENARIOS:
        java_source = (CATALOG_ROOT / f"variants/{slug}/java.java").read_text(encoding="utf-8")
        assert "candidateAssets" in java_source
        assert "candidateRelationships" in java_source
        assert "previewBytes(candidateAssets, candidateRelationships" in java_source


@contextmanager
def _echo_server() -> Iterator[tuple[str, list[str]]]:
    paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            paths.append(self.path)
            payload = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/echo?visible=1", paths
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def _credential_echo_server() -> Iterator[tuple[str, str]]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query_values = dict(
                item.split("=", 1) if "=" in item else (item, "")
                for item in urlsplit(self.path).query.split("&")
                if item
            )
            header_value = self.headers.get("Authorization") or self.headers.get("X-Audit-Key", "")
            if urlsplit(self.path).path == "/single":
                value = {
                    "header": header_value,
                    "nested": [query_values.get("api_key", "")],
                }
            else:
                value = {
                    "items": [{"header": header_value, "nested": [header_value]}],
                    "next": f"/page?signed={header_value}",
                }
            payload = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        root = f"http://127.0.0.1:{server.server_port}"
        yield root + "/single", root + "/page"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def _short_secret_rest_server() -> Iterator[tuple[str, str, int, int, list[str]]]:
    requests: list[str] = []
    single_payload = b'{"value":"x"}'
    first_page = json.dumps(
        {"items": [{"value": "x"} for _ in range(20)], "next": None},
        separators=(",", ":"),
    ).encode()
    empty_page = b'{"items":[],"next":null}'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            requests.append(self.path)
            parsed = urlsplit(self.path)
            if parsed.path == "/single":
                payload = single_payload
            elif parsed.path == "/page":
                page = int(parse_qs(parsed.query).get("page", ["1"])[0])
                payload = first_page if page == 1 else empty_page
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        root = f"http://127.0.0.1:{server.server_port}"
        yield root + "/single", root + "/page", len(single_payload), len(first_page), requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def _normalized_number_page_server() -> Iterator[tuple[str, int, list[str]]]:
    requests: list[str] = []
    first_page = b'{"items":[' + b",".join([b"1e20"] * 50) + b'],"next":null}'
    empty_page = b'{"items":[],"next":null}'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            requests.append(self.path)
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            first = (
                int(query["offset"][0]) == 0
                if "offset" in query
                else int(query.get("page", ["1"])[0]) == 1
            )
            payload = first_page if first else empty_page
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/numbers", len(first_page), requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def _rest_checkpoint_server() -> Iterator[tuple[str, list[str], str]]:
    requests: list[str] = []
    opaque_secret = "AUDIT_OPAQUE_CONTINUATION_SECRET_132"
    records = [
        {"id": "record-1"},
        {"id": "record-2"},
        {"id": "record-3"},
        {"id": "record-4"},
    ]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            requests.append(self.path)
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/page":
                start = (int(query.get("page", ["1"])[0]) - 1) * 2
                payload = {"items": records[start : start + 2], "next": None}
            elif parsed.path == "/offset":
                start = int(query.get("offset", ["0"])[0])
                payload = {"items": records[start : start + 2], "next": None}
            elif parsed.path == "/cursor":
                cursor = query.get("cursor", [None])[0]
                payload = (
                    {"items": records[:2], "next": opaque_secret}
                    if cursor is None
                    else {"items": records[2:], "next": None}
                )
            elif parsed.path == "/next-url":
                payload = (
                    {
                        "items": records[:2],
                        "next": f"/next-url?signed={opaque_secret}",
                    }
                    if "signed" not in query
                    else {"items": records[2:], "next": None}
                )
            else:
                self.send_error(404)
                return
            encoded = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, opaque_secret
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def _cross_origin_pagination_servers(
    *,
    redirect: bool,
) -> Iterator[tuple[str, list[dict[str, str]], list[dict[str, str]]]]:
    origin_headers: list[dict[str, str]] = []
    sink_headers: list[dict[str, str]] = []

    class SinkHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            sink_headers.append({key.lower(): value for key, value in self.headers.items()})
            payload = b'{"items":[{"id":"sink"}],"next":null}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
    sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
    sink_thread.start()
    sink_url = f"http://127.0.0.1:{sink.server_port}/sink"

    class OriginHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            origin_headers.append({key.lower(): value for key, value in self.headers.items()})
            if redirect:
                self.send_response(302)
                self.send_header("Location", sink_url)
                self.end_headers()
                return
            payload = json.dumps(
                {"items": [{"id": "origin"}], "next": sink_url},
                separators=(",", ":"),
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    origin = ThreadingHTTPServer(("127.0.0.1", 0), OriginHandler)
    origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
    origin_thread.start()
    try:
        yield (
            f"http://127.0.0.1:{origin.server_port}/origin",
            origin_headers,
            sink_headers,
        )
    finally:
        origin.shutdown()
        sink.shutdown()
        origin_thread.join(timeout=5)
        sink_thread.join(timeout=5)
        origin.server_close()
        sink.server_close()


@contextmanager
def _redirecting_cmdb_server() -> Iterator[tuple[str, list[dict[str, str]]]]:
    requests: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def _record(self) -> None:
            requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization", ""),
                }
            )

        def do_POST(self) -> None:  # noqa: N802
            self._record()
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(302)
            self.send_header("Location", "/redirected-success")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            self._record()
            payload = b'{"accepted":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_rest_query_api_key_uses_secret_binding_and_never_enters_output(
    tmp_path: Path,
) -> None:
    scenario = _json(CATALOG_ROOT / "scenarios/rest-single-request/metadata.json")
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}
    secret = "fixture-query-secret"
    secrets = {"HTTP_API_KEY": secret}
    with _echo_server() as (url, paths):
        input_value = {
            "url": url,
            "method": "GET",
            "query": {"page": "2"},
            "query_auth": {
                "parameter": "api_key",
                "secret_binding": "HTTP_API_KEY",
            },
            "response_type": "json",
            "allowed_statuses": [200],
        }
        results = {
            "python": _python_result(sources["python"], input_value, secrets),
            "javascript": _javascript_result(sources["javascript"], input_value, secrets),
            "java": _java_result(sources["java"], input_value, tmp_path, secrets),
        }
    assert results["python"] == results["javascript"] == results["java"]
    assert len(paths) == 3
    assert all("visible=1" in path and "page=2" in path for path in paths)
    assert all("api_key=fixture-query-secret" in path for path in paths)
    assert secret not in json.dumps(results)


def test_rest_success_outputs_scrub_bound_credentials_and_checkpoint_queries(
    tmp_path: Path,
) -> None:
    single_meta = _json(CATALOG_ROOT / "scenarios/rest-single-request/metadata.json")
    page_meta = _json(CATALOG_ROOT / "scenarios/rest-paginated-collection/metadata.json")
    single_sources = {item["language"]: _variant_source(item) for item in single_meta["variants"]}
    page_sources = {item["language"]: _variant_source(item) for item in page_meta["variants"]}
    header_secret = "AUDIT_ECHO_HEADER_SECRET"
    query_secret = "AUDIT/ECHO+QUERY?SECRET&"
    secrets = {"HTTP_HEADER": header_secret, "HTTP_QUERY": query_secret}
    with _credential_echo_server() as (single_url, page_url):
        single_input = {
            "url": single_url,
            "headers": {"DLR-Auth": "bearer:HTTP_HEADER"},
            "query_auth": {
                "parameter": "api_key",
                "secret_binding": "HTTP_QUERY",
            },
            "response_type": "json",
        }
        page_input = {
            "url": page_url,
            "strategy": "next-url",
            "headers": {"DLR-Auth": "api-key/X-Audit-Key:HTTP_HEADER"},
            "max_pages": 1,
            "max_retries": 0,
        }
        for language, single_runner, page_runner in [
            ("python", _python_result, _python_result),
            ("javascript", _javascript_result, _javascript_result),
            ("java", _java_result, _java_result),
        ]:
            if language == "java":
                single_result = single_runner(
                    single_sources[language], single_input, tmp_path / "single-echo", secrets
                )
                page_result = page_runner(
                    page_sources[language], page_input, tmp_path / "page-echo", secrets
                )
            else:
                single_result = single_runner(single_sources[language], single_input, secrets)
                page_result = page_runner(page_sources[language], page_input, secrets)
            rendered = json.dumps([single_result, page_result])
            assert header_secret not in rendered
            assert query_secret not in rendered
            assert single_result["response"] == {
                "header": "<redacted>",
                "nested": ["<redacted>"],
            }
            assert page_result["records"] == [{"header": "<redacted>", "nested": ["<redacted>"]}]
            assert page_result["checkpoint"] is None


def test_rest_credential_like_url_names_fail_closed_without_input_leakage(
    tmp_path: Path,
) -> None:
    names = (
        "access_key",
        "accesskey",
        "api_key",
        "X-Api-Key",
        "authorization",
        "authentication",
        "client_secret",
        "cookie",
        "credential",
        "password",
        "private_key",
        "secret",
        "signature",
        "sig",
        "token",
        "oauth",
    )
    canary = "AUDIT_DIRECT_INPUT_SECRET_132"
    scenarios = {
        slug: {
            item["language"]: _variant_source(item)
            for item in _json(CATALOG_ROOT / f"scenarios/{slug}/metadata.json")["variants"]
        }
        for slug in ("rest-single-request", "rest-paginated-collection")
    }
    with _echo_server() as (url, paths):
        inputs = [{"url": f"{url}&{name}={canary}"} for name in names]
        for slug, sources in scenarios.items():
            python_errors: list[str | None] = []
            for input_value in inputs:
                try:
                    _python_result(sources["python"], input_value)
                except ValueError as error:
                    python_errors.append(str(error))
                else:
                    python_errors.append(None)
            javascript_errors = _javascript_errors(sources["javascript"], inputs)
            java_errors = _java_errors(sources["java"], inputs, tmp_path / slug)
            assert python_errors == ["direct_credential_query_forbidden"] * len(inputs)
            assert javascript_errors == python_errors
            assert all(
                error is not None and "direct_credential_query_forbidden" in error
                for error in java_errors
            )
            assert canary not in json.dumps([python_errors, javascript_errors, java_errors])
    assert paths == []


def test_rest_plain_query_header_and_pagination_credential_names_fail_closed(
    tmp_path: Path,
) -> None:
    canary = "AUDIT_PLAIN_INPUT_SECRET_132"
    scenarios = {
        slug: {
            item["language"]: _variant_source(item)
            for item in _json(CATALOG_ROOT / f"scenarios/{slug}/metadata.json")["variants"]
        }
        for slug in ("rest-single-request", "rest-paginated-collection")
    }
    with _echo_server() as (url, paths):
        inputs = {
            "rest-single-request": [
                {"url": url, "query": {"api_key": canary}},
                {"url": url, "headers": {"X-Api-Key": canary}},
            ],
            "rest-paginated-collection": [
                {"url": url, "headers": {"X-Api-Key": canary}},
                {"url": url, "strategy": "page", "page_parameter": "access_key"},
            ],
        }
        for slug, input_values in inputs.items():
            sources = scenarios[slug]
            python_errors: list[str | None] = []
            for input_value in input_values:
                try:
                    _python_result(sources["python"], input_value)
                except ValueError as error:
                    python_errors.append(str(error))
                else:
                    python_errors.append(None)
            javascript_errors = _javascript_errors(sources["javascript"], input_values)
            java_errors = _java_errors(sources["java"], input_values, tmp_path / slug)
            assert python_errors == [
                "direct_credential_query_forbidden"
                if "query" in input_value or "page_parameter" in input_value
                else "direct_credential_header_forbidden"
                for input_value in input_values
            ]
            assert javascript_errors == python_errors
            assert all(
                error is not None and expected in error
                for error, expected in zip(java_errors, python_errors, strict=True)
            )
            assert canary not in json.dumps([python_errors, javascript_errors, java_errors])
    assert paths == []


def test_rest_non_sensitive_business_names_and_bound_query_auth_remain_usable(
    tmp_path: Path,
) -> None:
    single = _json(CATALOG_ROOT / "scenarios/rest-single-request/metadata.json")
    paginated = _json(CATALOG_ROOT / "scenarios/rest-paginated-collection/metadata.json")
    single_sources = {item["language"]: _variant_source(item) for item in single["variants"]}
    page_sources = {item["language"]: _variant_source(item) for item in paginated["variants"]}
    secret = "AUDIT_BOUND_QUERY_SECRET_132"
    secrets = {"HTTP_API_KEY": secret}
    with (
        _echo_server() as (single_url, single_paths),
        _rest_checkpoint_server() as (root, page_paths, _opaque_secret),
    ):
        single_input = {
            "url": single_url + "&author=ada&design=clean",
            "query": {"filter": "active"},
            "headers": {"X-Trace-ID": "trace-132"},
            "response_type": "json",
        }
        page_input = {
            "url": root + "/page?author=ada&design=clean",
            "strategy": "page",
            "page_size": 2,
            "headers": {"X-Trace-ID": "trace-132"},
            "query_auth": {"parameter": "api_key", "secret_binding": "HTTP_API_KEY"},
            "max_pages": 1,
            "max_retries": 0,
        }
        single_results = {
            "python": _python_result(single_sources["python"], single_input),
            "javascript": _javascript_result(single_sources["javascript"], single_input),
            "java": _java_result(single_sources["java"], single_input, tmp_path / "single"),
        }
        page_results = {
            "python": _python_result(page_sources["python"], page_input, secrets),
            "javascript": _javascript_result(page_sources["javascript"], page_input, secrets),
            "java": _java_result(page_sources["java"], page_input, tmp_path / "page", secrets),
        }
    assert single_results["python"] == single_results["javascript"] == single_results["java"]
    assert page_results["python"] == page_results["javascript"] == page_results["java"]
    assert len(single_paths) == 3
    assert all(
        "author=ada" in path and "design=clean" in path and "filter=active" in path
        for path in single_paths
    )
    assert len(page_paths) == 3
    assert all("author=ada" in path and "design=clean" in path for path in page_paths)
    assert all("api_key=AUDIT_BOUND_QUERY_SECRET_132" in path for path in page_paths)
    assert secret not in json.dumps([single_results, page_results])


def test_rest_one_byte_secret_scrubbing_does_not_amplify_outputs_or_page_counts(
    tmp_path: Path,
) -> None:
    scenarios = {
        slug: {
            item["language"]: _variant_source(item)
            for item in _json(CATALOG_ROOT / f"scenarios/{slug}/metadata.json")["variants"]
        }
        for slug in ("rest-single-request", "rest-paginated-collection")
    }
    secrets = {"ONE_BYTE": "x"}
    with _short_secret_rest_server() as (
        single_url,
        page_url,
        single_size,
        page_size,
        requests,
    ):
        single_input = {
            "url": single_url,
            "headers": {"DLR-Auth": "api-key/X-Audit-Key:ONE_BYTE"},
            "response_type": "json",
            "max_response_bytes": single_size,
        }
        page_input = {
            "url": page_url,
            "strategy": "page",
            "headers": {"DLR-Auth": "api-key/X-Audit-Key:ONE_BYTE"},
            "max_bytes": page_size,
            "max_pages": 10,
            "max_retries": 0,
        }
        single = scenarios["rest-single-request"]
        page = scenarios["rest-paginated-collection"]
        single_results = {
            "python": _python_result(single["python"], single_input, secrets),
            "javascript": _javascript_result(single["javascript"], single_input, secrets),
            "java": _java_result(single["java"], single_input, tmp_path / "single", secrets),
        }
        page_results = {
            "python": _python_result(page["python"], page_input, secrets),
            "javascript": _javascript_result(page["javascript"], page_input, secrets),
            "java": _java_result(page["java"], page_input, tmp_path / "page", secrets),
        }
    assert single_results["python"] == single_results["javascript"] == single_results["java"]
    assert page_results["python"] == page_results["javascript"] == page_results["java"]
    assert single_results["python"]["partial"] is False
    assert single_results["python"]["response"] == {"value": "*"}
    assert page_results["python"]["records"] == [{"value": "*"}] * 20
    assert page_results["python"]["pages"] == 1
    assert page_results["python"]["partial"] is True
    assert page_results["python"]["checkpoint"] == {"strategy": "page", "start_page": 2}
    assert len(requests) == 6
    assert sum(path.startswith("/page") for path in requests) == 3
    assert "x" not in json.dumps([single_results, page_results])


def test_rest_single_request_checks_normalized_response_output_bytes(
    tmp_path: Path,
) -> None:
    scenario = _json(CATALOG_ROOT / "scenarios/rest-single-request/metadata.json")
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}
    with _normalized_number_page_server() as (url, raw_size, requests):
        input_value = {
            "url": url,
            "response_type": "json",
            "max_response_bytes": raw_size,
        }
        results = {
            "python": _python_result(sources["python"], input_value),
            "javascript": _javascript_result(sources["javascript"], input_value),
            "java": _java_result(sources["java"], input_value, tmp_path),
        }
    expected = {
        "ok": True,
        "status": 200,
        "partial": True,
        "bytes_read": raw_size,
        "response": None,
    }
    assert results == {language: expected for language in LANGUAGES}
    assert len(requests) == 3


@pytest.mark.parametrize(
    ("strategy", "expected_checkpoint"),
    [
        ("page", {"strategy": "page", "start_page": 1}),
        ("offset", {"strategy": "offset", "start_offset": 0}),
        ("cursor", None),
        ("next-url", None),
    ],
)
def test_rest_pagination_output_byte_overflow_omits_the_whole_page_at_checkpoint(
    tmp_path: Path,
    strategy: str,
    expected_checkpoint: dict[str, object] | None,
) -> None:
    scenario = _json(CATALOG_ROOT / "scenarios/rest-paginated-collection/metadata.json")
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}

    def run_all(input_value: dict[str, Any], suffix: str) -> dict[str, dict[str, Any]]:
        return {
            "python": _python_result(sources["python"], input_value),
            "javascript": _javascript_result(sources["javascript"], input_value),
            "java": _java_result(sources["java"], input_value, tmp_path / suffix),
        }

    with _normalized_number_page_server() as (url, first_page_size, requests):
        input_value = {
            "url": url,
            "strategy": strategy,
            "max_bytes": first_page_size,
            "max_pages": 10,
            "max_retries": 0,
        }
        overflow = run_all(input_value, "overflow")
        resumed = (
            run_all(input_value | {"max_bytes": 4096}, "resumed")
            if strategy in {"page", "offset"}
            else None
        )
    assert overflow["python"] == overflow["javascript"] == overflow["java"]
    assert overflow["python"]["records"] == []
    assert overflow["python"]["count"] == 0
    assert overflow["python"]["pages"] == 1
    assert overflow["python"]["partial"] is True
    assert overflow["python"]["checkpoint"] == expected_checkpoint
    if resumed is not None:
        assert resumed["python"] == resumed["javascript"] == resumed["java"]
        assert resumed["python"]["count"] == 50
        assert resumed["python"]["pages"] == 2
        assert resumed["python"]["partial"] is False
        assert resumed["python"]["checkpoint"] is None
        assert len(requests) == 9
    else:
        assert len(requests) == 3


@pytest.mark.parametrize(
    ("strategy", "path", "checkpoint"),
    [
        ("page", "/page", {"strategy": "page", "start_page": 2}),
        ("offset", "/offset", {"strategy": "offset", "start_offset": 2}),
    ],
)
def test_rest_pagination_commits_whole_pages_and_resumes_without_record_gaps(
    tmp_path: Path,
    strategy: str,
    path: str,
    checkpoint: dict[str, object],
) -> None:
    scenario = _json(CATALOG_ROOT / "scenarios/rest-paginated-collection/metadata.json")
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}

    def run_all(input_value: dict[str, Any], suffix: str) -> dict[str, dict[str, Any]]:
        return {
            "python": _python_result(sources["python"], input_value),
            "javascript": _javascript_result(sources["javascript"], input_value),
            "java": _java_result(sources["java"], input_value, tmp_path / suffix),
        }

    with _rest_checkpoint_server() as (root, requests, _opaque_secret):
        base_input = {
            "url": root + path,
            "strategy": strategy,
            "page_size": 2,
            "max_pages": 10,
            "max_records": 2,
            "max_retries": 0,
        }
        exact = run_all(base_input, "exact")
        assert len(requests) == 3
        overflow = run_all(base_input | {"max_records": 3}, "overflow")
        assert len(requests) == 9
        resumed = run_all(base_input | checkpoint | {"max_records": 3}, "resumed")

    for results in (exact, overflow, resumed):
        assert results["python"] == results["javascript"] == results["java"]
    for results in (exact, overflow):
        assert results["python"]["records"] == [
            {"id": "record-1"},
            {"id": "record-2"},
        ]
        assert results["python"]["partial"] is True
        assert results["python"]["checkpoint"] == checkpoint
    assert resumed["python"]["records"] == [
        {"id": "record-3"},
        {"id": "record-4"},
    ]
    assert resumed["python"]["partial"] is False
    assert resumed["python"]["checkpoint"] is None
    combined = exact["python"]["records"] + resumed["python"]["records"]
    assert [item["id"] for item in combined] == [f"record-{index}" for index in range(1, 5)]


@pytest.mark.parametrize(("strategy", "path"), [("cursor", "/cursor"), ("next-url", "/next-url")])
def test_rest_pagination_opaque_continuations_are_not_exposed_as_resumable_checkpoints(
    tmp_path: Path,
    strategy: str,
    path: str,
) -> None:
    scenario = _json(CATALOG_ROOT / "scenarios/rest-paginated-collection/metadata.json")
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}
    with _rest_checkpoint_server() as (root, _requests, opaque_secret):
        input_value = {
            "url": root + path,
            "strategy": strategy,
            "page_size": 2,
            "max_pages": 1,
            "max_records": 10,
            "max_retries": 0,
        }
        results = {
            "python": _python_result(sources["python"], input_value),
            "javascript": _javascript_result(sources["javascript"], input_value),
            "java": _java_result(sources["java"], input_value, tmp_path),
        }
    assert results["python"] == results["javascript"] == results["java"]
    assert results["python"]["records"] == [
        {"id": "record-1"},
        {"id": "record-2"},
    ]
    assert results["python"]["partial"] is True
    assert results["python"]["checkpoint"] is None
    assert opaque_secret not in json.dumps(results)

    for variant in scenario["variants"]:
        contract = variant["output_contract"]["checkpoint_contract"]
        assert "null on partial output" in contract[strategy]


def test_rest_python_malformed_url_and_content_type_are_stable() -> None:
    for slug, input_value, expected in [
        ("rest-single-request", {"url": "https://[invalid"}, "invalid_url"),
        ("rest-paginated-collection", {"url": "https://[invalid"}, "invalid_url"),
    ]:
        source = CATALOG_ROOT / f"variants/{slug}/python.py"
        with pytest.raises(ValueError) as captured:
            _python_result(source, input_value)
        assert str(captured.value) == expected
        assert "Invalid IPv6" not in repr(captured.value)

    source = CATALOG_ROOT / "variants/rest-single-request/python.py"
    result = _python_result(
        source,
        {"url": "http://127.0.0.1:9/never-contact", "body": {}, "content_type": 123},
    )
    assert result["error"] == "request_failed"


def test_rest_header_secret_with_newline_is_safely_redacted_in_all_languages(
    tmp_path: Path,
) -> None:
    scenario = _json(CATALOG_ROOT / "scenarios/rest-single-request/metadata.json")
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}
    secret = "AUDIT_SECRET_CANARY\nInjected: yes"
    secrets = {"HTTP_BEARER_TOKEN": secret}
    with _echo_server() as (url, paths):
        input_value = {
            "url": url,
            "headers": {"DLR-Auth": "bearer:HTTP_BEARER_TOKEN"},
            "response_type": "json",
        }
        results = {
            "python": _python_result(sources["python"], input_value, secrets),
            "javascript": _javascript_result(sources["javascript"], input_value, secrets),
            "java": _java_result(sources["java"], input_value, tmp_path, secrets),
        }
    assert results["python"] == results["javascript"] == results["java"]
    assert results["python"] == {
        "ok": False,
        "error": "request_failed",
        "side_effect_uncertain": False,
        "retried": False,
    }
    assert paths == []
    assert secret not in json.dumps(results)


@pytest.mark.parametrize(
    ("auth", "secret"),
    [
        ("api-key/Host:HTTP_API_KEY", "AUDIT_RESTRICTED_HEADER_SECRET"),
        ("api-key/Proxy-Audit:HTTP_API_KEY", "AUDIT_PROXY_HEADER_SECRET"),
        ("api-key/X-Audit-Key:HTTP_API_KEY", "AUDIT_VALUE_SECRET\r\nInjected: yes"),
    ],
)
def test_rest_header_name_and_value_are_validated_before_network_in_all_languages(
    tmp_path: Path,
    auth: str,
    secret: str,
) -> None:
    secrets = {"HTTP_API_KEY": secret}
    input_value = {
        "url": "http://127.0.0.1:9/never-contact",
        "headers": {"DLR-Auth": auth},
    }
    single = _json(CATALOG_ROOT / "scenarios/rest-single-request/metadata.json")
    single_sources = {item["language"]: _variant_source(item) for item in single["variants"]}
    single_results = {
        "python": _python_result(single_sources["python"], input_value, secrets),
        "javascript": _javascript_result(single_sources["javascript"], input_value, secrets),
        "java": _java_result(single_sources["java"], input_value, tmp_path / "single", secrets),
    }
    expected = {
        "ok": False,
        "error": "request_failed",
        "side_effect_uncertain": False,
        "retried": False,
    }
    assert single_results == {language: expected for language in LANGUAGES}
    assert secret not in json.dumps(single_results)

    paginated = _json(CATALOG_ROOT / "scenarios/rest-paginated-collection/metadata.json")
    paginated_sources = {item["language"]: _variant_source(item) for item in paginated["variants"]}
    with pytest.raises(ValueError) as python_error:
        _python_result(paginated_sources["python"], input_value, secrets)
    assert str(python_error.value) == "request_failed"
    assert python_error.value.__cause__ is None
    assert secret not in repr(python_error.value)

    for language, runner in [
        ("javascript", _javascript_result),
        ("java", _java_result),
    ]:
        with pytest.raises(subprocess.CalledProcessError) as captured:
            if language == "java":
                runner(
                    paginated_sources[language],
                    input_value,
                    tmp_path / f"paginated-{language}",
                    secrets,
                )
            else:
                runner(paginated_sources[language], input_value, secrets)
        rendered = (captured.value.stdout or "") + (captured.value.stderr or "")
        assert "request_failed" in rendered
        assert secret not in rendered


def test_rest_proxy_prefixed_header_is_rejected_without_network(
    tmp_path: Path,
) -> None:
    secret = "AUDIT_PROXY_HEADER_SECRET"
    secrets = {"HTTP_API_KEY": secret}
    scenarios = {
        slug: {
            item["language"]: _variant_source(item)
            for item in _json(CATALOG_ROOT / f"scenarios/{slug}/metadata.json")["variants"]
        }
        for slug in ("rest-single-request", "rest-paginated-collection")
    }
    with _echo_server() as (url, paths):
        input_value = {
            "url": url,
            "headers": {"DLR-Auth": "api-key/Proxy-Audit:HTTP_API_KEY"},
        }
        single = scenarios["rest-single-request"]
        expected = {
            "ok": False,
            "error": "request_failed",
            "side_effect_uncertain": False,
            "retried": False,
        }
        assert _python_result(single["python"], input_value, secrets) == expected
        assert _javascript_result(single["javascript"], input_value, secrets) == expected
        assert _java_result(single["java"], input_value, tmp_path / "single", secrets) == expected

        paginated = scenarios["rest-paginated-collection"]
        with pytest.raises(ValueError, match="^request_failed$"):
            _python_result(paginated["python"], input_value, secrets)
        for language, runner in (("javascript", _javascript_result), ("java", _java_result)):
            with pytest.raises(subprocess.CalledProcessError) as captured:
                if language == "java":
                    runner(paginated[language], input_value, tmp_path / language, secrets)
                else:
                    runner(paginated[language], input_value, secrets)
            rendered = (captured.value.stdout or "") + (captured.value.stderr or "")
            assert "request_failed" in rendered
            assert secret not in rendered
    assert paths == []


def test_rest_pagination_python_rejects_cross_origin_transport_redirect() -> None:
    scenario = _json(CATALOG_ROOT / "scenarios/rest-paginated-collection/metadata.json")
    source = _variant_source(
        next(item for item in scenario["variants"] if item["language"] == "python")
    )
    secret = "AUDIT_REDIRECT_SECRET"
    with (
        _cross_origin_pagination_servers(redirect=True) as (
            url,
            origin_headers,
            sink_headers,
        ),
        pytest.raises(ValueError, match="unexpected_status"),
    ):
        _python_result(
            source,
            {
                "url": url,
                "strategy": "next-url",
                "headers": {"DLR-Auth": "bearer:HTTP_BEARER_TOKEN"},
                "max_retries": 0,
            },
            {"HTTP_BEARER_TOKEN": secret},
        )
    assert [item.get("authorization") for item in origin_headers] == [f"Bearer {secret}"]
    assert sink_headers == []


def test_rest_pagination_strips_credential_derived_safe_header_cross_origin(
    tmp_path: Path,
) -> None:
    scenario = _json(CATALOG_ROOT / "scenarios/rest-paginated-collection/metadata.json")
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}
    secret = "AUDIT_CROSS_ORIGIN_USER_AGENT_SECRET"
    input_value: dict[str, Any]
    with _cross_origin_pagination_servers(redirect=False) as (
        url,
        origin_headers,
        sink_headers,
    ):
        input_value = {
            "url": url,
            "strategy": "next-url",
            "headers": {"DLR-Auth": "api-key/User-Agent:HTTP_API_KEY"},
            "allow_cross_origin_next": True,
            "max_pages": 3,
            "max_retries": 0,
        }
        results = {
            "python": _python_result(sources["python"], input_value, {"HTTP_API_KEY": secret}),
            "javascript": _javascript_result(
                sources["javascript"], input_value, {"HTTP_API_KEY": secret}
            ),
            "java": _java_result(sources["java"], input_value, tmp_path, {"HTTP_API_KEY": secret}),
        }
    assert results["python"] == results["javascript"] == results["java"]
    assert results["python"]["count"] == 2
    assert results["python"]["pages"] == 2
    assert [item.get("user-agent") for item in origin_headers] == [secret, secret, secret]
    assert len(sink_headers) == 3
    assert all(secret not in json.dumps(item) for item in sink_headers)
    assert secret not in json.dumps(results)


def test_cloud_sync_rejects_redirect_in_all_languages(tmp_path: Path) -> None:
    scenario = _json(
        CATALOG_ROOT / "scenarios/tencentcloud-compute-container-topology/metadata.json"
    )
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}
    input_value = {
        "mode": "sync",
        "scan_id": "scan-redirect-fixture",
        "source_scope": "tencentcloud:fixture:ap-example-1",
        "account": "fixture",
        "regions": ["ap-example-1"],
        "max_pages": 50,
        "max_records": 10,
        "max_bytes": 65_536,
        "fixture_pages": {},
    }
    cmdb_secret = "AUDIT_CMDB_REDIRECT_SECRET"
    secrets = {"CMDB_TOKEN": cmdb_secret}
    with _redirecting_cmdb_server() as (base_url, requests):
        config = {"cmdb_base_url": base_url}
        results = {
            "python": _python_result(sources["python"], input_value, secrets, config),
            "javascript": _javascript_result(sources["javascript"], input_value, secrets, config),
            "java": _java_result(sources["java"], input_value, tmp_path, secrets, config),
        }
    assert results["python"] == results["javascript"] == results["java"]
    assert results["python"]["partial"] is True
    assert results["python"]["failed"] == ["target_batch"]
    assert [item["method"] for item in requests] == ["POST", "POST", "POST"]
    assert all(item["authorization"] == f"Bearer {cmdb_secret}" for item in requests)
    assert all(item["path"] == "/api/v1/import-scans:begin" for item in requests)


def test_javascript_provider_streams_stop_at_source_byte_cap(tmp_path: Path) -> None:
    cases = [
        (
            CATALOG_ROOT / "variants/tencentcloud-compute-container-topology/javascript.mjs",
            {
                "mode": "preview",
                "account": "fixture",
                "regions": ["ap-example-1"],
                "max_pages": 1,
                "max_bytes": 1024,
            },
            {
                "TENCENTCLOUD_SECRET_ID": "fixture-id",
                "TENCENTCLOUD_SECRET_KEY": "fixture-key",
            },
        ),
        (
            CATALOG_ROOT / "variants/servicenow-cmdb-ci-snapshot/javascript.mjs",
            {
                "mode": "preview",
                "instance_url": "https://fixture.service-now.example",
                "instance_id": "fixture",
                "max_pages": 1,
                "max_bytes": 1024,
            },
            {"SERVICENOW_BEARER_TOKEN": "fixture-token"},
        ),
    ]
    script = """
import { pathToFileURL } from "node:url";
let pulls = 0;
let cancelled = false;
globalThis.fetch = async () => new Response(new ReadableStream({
  pull(controller) {
    pulls += 1;
    controller.enqueue(new Uint8Array(512));
    if (pulls > 20_000) controller.close();
  },
  cancel() { cancelled = true; },
}));
const module = await import(pathToFileURL(process.argv[1]));
const input = JSON.parse(process.argv[2]);
const secrets = new Map(Object.entries(JSON.parse(process.argv[3])));
const result = await module.handle(
  { config: {}, secrets, inputFiles: [], logger: {} },
  input,
);
process.stdout.write(JSON.stringify({ result, pulls, cancelled }));
"""
    for index, (source, input_value, secrets) in enumerate(cases):
        completed = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                script,
                str(source),
                json.dumps(input_value),
                json.dumps(secrets),
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        output = json.loads(completed.stdout)
        assert output["result"]["partial"] is True, index
        assert output["result"]["summary"]["failures"], index
        # WHATWG streams may prefetch one additional chunk; retained bytes are still max + 1.
        assert output["pulls"] <= 4, index
        assert output["cancelled"] is True, index

    for slug in CLOUD_SCENARIOS[3:]:
        source_text = (CATALOG_ROOT / f"variants/{slug}/javascript.mjs").read_text(encoding="utf-8")
        assert "response.json()" not in source_text
        assert "boundedJson(response,maximum)" in source_text


def test_tencent_python_and_java_stop_at_callers_remaining_source_bytes(
    tmp_path: Path,
) -> None:
    for slug in CLOUD_SCENARIOS[3:]:
        source_root = CATALOG_ROOT / f"variants/{slug}"
        python = runpy.run_path(str(source_root / "python.py"))
        read_limits: list[int] = []

        class Response:
            def __init__(self, limits: list[int]) -> None:
                self.limits = limits

            def __enter__(self) -> Response:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, maximum: int) -> bytes:
                self.limits.append(maximum)
                return b"x" * maximum

        class Opener:
            def __init__(self, limits: list[int]) -> None:
                self.limits = limits

            def open(self, *_args: object, **_kwargs: object) -> Response:
                return Response(self.limits)

        python["_tencentcloud"].__globals__["_NO_REDIRECT_OPENER"] = Opener(read_limits)
        context = SimpleNamespace(
            secrets={
                "TENCENTCLOUD_SECRET_ID": "fixture-id",
                "TENCENTCLOUD_SECRET_KEY": "fixture-key",
            }
        )
        with pytest.raises(ValueError, match="provider_response_too_large"):
            python["_tencentcloud"](
                python["OPERATIONS"][0], "ap-example-1", 1, 1, context, 1, 1_024
            )
        assert read_limits == [1_025]

        compile_root = tmp_path / f"{slug}-source-cap"
        compile_root.mkdir()
        (compile_root / "DlrRuntime.java").write_text(JAVA_RUNTIME_SOURCE, encoding="utf-8")
        java_source = (source_root / "java.java").read_text(encoding="utf-8")
        (compile_root / "Adapter.java").write_text(java_source, encoding="utf-8")
        (compile_root / "Probe.java").write_text(
            """
import java.io.ByteArrayInputStream;
import java.io.InputStream;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
public final class Probe {
  public static void main(String[] args) throws Exception {
    Method read = Adapter.class.getDeclaredMethod("readProviderBody", InputStream.class, int.class);
    read.setAccessible(true);
    try {
      read.invoke(null, new ByteArrayInputStream(new byte[1_025]), 1_024);
      System.out.print("NO_ERROR");
    } catch (InvocationTargetException error) {
      System.out.print(error.getCause().getMessage());
    }
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
        completed = subprocess.run(
            ["java", "-cp", str(compile_root), "Probe"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert completed.stdout == "provider_response_too_large"
        assert "int sourceMaxBytes = Math.min(4_194_304, maxBytes - sourceBytes)" in java_source
        assert "sourceBytes += pageValue.bytes()" in java_source


@contextmanager
def _servicenow_https_fixture(
    tmp_path: Path, records: list[object]
) -> Iterator[tuple[str, list[str], Path, Path]]:
    openssl = shutil.which("openssl")
    keytool = shutil.which("keytool")
    if openssl is None or keytool is None:
        pytest.skip("OpenSSL and keytool are required for the local HTTPS fixture")
    certificate = tmp_path / "servicenow-fixture-cert.pem"
    private_key = tmp_path / "servicenow-fixture-key.pem"
    truststore = tmp_path / "servicenow-fixture-truststore.p12"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            "2",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            keytool,
            "-importcert",
            "-noprompt",
            "-alias",
            "servicenow-fixture",
            "-file",
            str(certificate),
            "-keystore",
            str(truststore),
            "-storetype",
            "PKCS12",
            "-storepass",
            "changeit",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    requests: list[str] = []
    payload = json.dumps({"result": records}, ensure_ascii=False, separators=(",", ":")).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            requests.append(self.path)
            if urlsplit(self.path).path != "/api/now/table/cmdb_ci":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, private_key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"https://localhost:{server.server_port}",
            requests,
            certificate,
            truststore,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _servicenow_https_results(
    source_root: Path,
    input_value: dict[str, Any],
    tmp_path: Path,
    certificate: Path,
    truststore: Path,
    config: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    secrets = {
        "SERVICENOW_BEARER_TOKEN": "fixture-source-token",
        "CMDB_TOKEN": "fixture-cmdb-token",
    }
    python = runpy.run_path(str(source_root / "python.py"))
    source_context = ssl.create_default_context(cafile=str(certificate))
    source_opener = python["request"].build_opener(
        python["_NoRedirect"](),
        python["request"].HTTPSHandler(context=source_context),
    )
    python["handle"].__globals__["_NO_REDIRECT_OPENER"] = source_opener
    python_result = python["handle"](
        SimpleNamespace(config=config, secrets=secrets, input_files=[], logger=SimpleNamespace()),
        input_value,
    )

    script = """
import { pathToFileURL } from "node:url";
const module = await import(pathToFileURL(process.argv[1]));
const result = await module.handle({
  config: JSON.parse(process.argv[3]),
  secrets: new Map([
    ["SERVICENOW_BEARER_TOKEN", "fixture-source-token"],
    ["CMDB_TOKEN", "fixture-cmdb-token"],
  ]),
  inputFiles: [], logger: {},
}, JSON.parse(process.argv[2]));
process.stdout.write(JSON.stringify(result));
"""
    environment = os.environ.copy()
    environment["NODE_EXTRA_CA_CERTS"] = str(certificate)
    javascript = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            str(source_root / "javascript.mjs"),
            json.dumps(input_value),
            json.dumps(config),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    java_result = _java_result(
        source_root / "java.java",
        input_value,
        tmp_path,
        secrets,
        config,
        java_options=[
            f"-Djavax.net.ssl.trustStore={truststore}",
            "-Djavax.net.ssl.trustStorePassword=changeit",
            "-Djavax.net.ssl.trustStoreType=PKCS12",
        ],
    )
    return {
        "python": python_result,
        "javascript": json.loads(javascript.stdout),
        "java": java_result,
    }


def test_servicenow_javascript_short_final_page_is_complete(tmp_path: Path) -> None:
    source = CATALOG_ROOT / "variants/servicenow-cmdb-ci-snapshot/javascript.mjs"
    script = """
import { pathToFileURL } from "node:url";
globalThis.fetch = async () => new Response('{"result":[]}', {
  status: 200,
  headers: { "Content-Type": "application/json" },
});
const module = await import(pathToFileURL(process.argv[1]));
const result = await module.handle(
  {
    config: {},
    secrets: new Map([["SERVICENOW_BEARER_TOKEN", "fixture-token"]]),
    inputFiles: [], logger: {},
  },
  {
    mode: "preview", instance_url: "https://fixture.service-now.example",
    instance_id: "fixture", max_pages: 1, page_size: 10,
  },
);
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(source)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)
    assert result["partial"] is False
    assert result["summary"]["pages"] == 1


def test_servicenow_sync_rejects_nonzero_offset_before_io_in_all_languages(
    tmp_path: Path,
) -> None:
    source_root = CATALOG_ROOT / "variants/servicenow-cmdb-ci-snapshot"
    input_value = {
        "mode": "sync",
        "scan_id": "scan-resume-without-prefix",
        "source_scope": "servicenow:fixture",
        "offset": 50,
    }

    python = runpy.run_path(str(source_root / "python.py"))
    python_calls: list[str] = []
    python["handle"].__globals__["_get_page"] = lambda *_args: python_calls.append("source")
    python["handle"].__globals__["_sync"] = lambda *_args: python_calls.append("target")
    with pytest.raises(ValueError, match="sync_offset_must_be_zero"):
        python["handle"](SimpleNamespace(config={}, secrets={}), input_value)
    assert python_calls == []

    script = """
import { pathToFileURL } from "node:url";
let requests = 0;
globalThis.fetch = async () => {
  requests += 1;
  throw new Error("request must not run");
};
const module = await import(pathToFileURL(process.argv[1]));
try {
  await module.handle(
    { config: {}, secrets: new Map(), inputFiles: [], logger: {} },
    JSON.parse(process.argv[2]),
  );
  process.stdout.write(JSON.stringify({ error: null, requests }));
} catch (error) {
  process.stdout.write(JSON.stringify({ error: error.message, requests }));
}
"""
    completed = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            str(source_root / "javascript.mjs"),
            json.dumps(input_value),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    javascript = json.loads(completed.stdout)
    assert javascript == {"error": "sync_offset_must_be_zero", "requests": 0}

    compile_root = tmp_path / "servicenow-sync-offset-java"
    compile_root.mkdir()
    (compile_root / "DlrRuntime.java").write_text(JAVA_RUNTIME_SOURCE, encoding="utf-8")
    (compile_root / "Adapter.java").write_text(
        (source_root / "java.java").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (compile_root / "Probe.java").write_text(
        """
import java.util.Map;
public final class Probe {
  public static void main(String[] args) throws Exception {
    try {
      new Adapter().handle(new Context(Map.of()), Map.of(
        "mode", "sync",
        "scan_id", "scan-resume-without-prefix",
        "source_scope", "servicenow:fixture",
        "offset", 50
      ));
      System.out.print("NO_ERROR");
    } catch (IllegalArgumentException error) {
      System.out.print(error.getMessage());
    }
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
    completed = subprocess.run(
        ["java", "-cp", str(compile_root), "Probe"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == "sync_offset_must_be_zero"


def test_servicenow_invalid_records_block_sync_in_all_languages(tmp_path: Path) -> None:
    source_root = CATALOG_ROOT / "variants/servicenow-cmdb-ci-snapshot"
    input_value = {
        "mode": "sync",
        "scan_id": "scan-invalid-record",
        "source_scope": "servicenow:fixture",
        "instance_url": "https://fixture.service-now.example",
        "instance_id": "fixture",
        "page_size": 10,
        "max_pages": 1,
    }

    python = runpy.run_path(str(source_root / "python.py"))
    python["handle"].__globals__["_get_page"] = lambda *_args: ([{"name": "missing-id"}], 1)
    python["handle"].__globals__["_sync"] = lambda *_args: (_ for _ in ()).throw(
        AssertionError("sync must not run")
    )
    python_result = python["handle"](
        SimpleNamespace(config={}, secrets={"SERVICENOW_BEARER_TOKEN": "fixture"}),
        input_value,
    )
    assert python_result["partial"] is True
    assert python_result["failed"] == ["invalid_source_record"]

    script = """
import { pathToFileURL } from "node:url";
const methods = [];
globalThis.fetch = async (_url, options = {}) => {
  methods.push(options.method ?? "GET");
  if (options.method === "POST") throw new Error("sync must not run");
  return new Response('{"result":[{"name":"missing-id"}]}', {
    status: 200, headers: { "Content-Type": "application/json" },
  });
};
const module = await import(pathToFileURL(process.argv[1]));
const input = JSON.parse(process.argv[2]);
const result = await module.handle({
  config: {}, secrets: new Map([["SERVICENOW_BEARER_TOKEN", "fixture"]]),
  inputFiles: [], logger: {},
}, input);
process.stdout.write(JSON.stringify({ result, methods }));
"""
    completed = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            str(source_root / "javascript.mjs"),
            json.dumps(input_value),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    javascript = json.loads(completed.stdout)
    assert javascript["result"]["partial"] is True
    assert javascript["result"]["failed"] == ["invalid_source_record"]
    assert javascript["methods"] == ["GET"]

    java_source = (source_root / "java.java").read_text(encoding="utf-8")
    compile_root = tmp_path / "servicenow-invalid-java"
    compile_root.mkdir()
    (compile_root / "DlrRuntime.java").write_text(JAVA_RUNTIME_SOURCE, encoding="utf-8")
    (compile_root / "Adapter.java").write_text(java_source, encoding="utf-8")
    (compile_root / "Probe.java").write_text(
        """
import java.lang.reflect.Method;
import java.util.Map;
public final class Probe {
  public static void main(String[] args) throws Exception {
    Method asset = Adapter.class.getDeclaredMethod("asset", String.class, Map.class);
    asset.setAccessible(true);
    System.out.print(asset.invoke(null, "fixture", Map.of("name", "missing-id")) == null);
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
    completed = subprocess.run(
        ["java", "-cp", str(compile_root), "Probe"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == "true"
    assert (
        java_source.index('failure = "invalid_source_record"')
        < java_source.index("if (partial) {")
        < java_source.index("Object result = sync(context")
    )


def test_servicenow_page_checkpoint_stops_at_first_invalid_row_in_all_languages(
    tmp_path: Path,
) -> None:
    source_root = CATALOG_ROOT / "variants/servicenow-cmdb-ci-snapshot"
    records = [
        {"sys_id": "ci-1", "name": "first"},
        {"name": "missing-stable-id"},
        {"sys_id": "ci-3", "name": "must-not-be-skipped"},
    ]
    input_value = {
        "mode": "sync",
        "scan_id": "scan-invalid-middle-record",
        "source_scope": "servicenow:fixture:cmdb_ci",
        "instance_id": "fixture",
        "page_size": 3,
        "max_pages": 1,
        "max_records": 10,
        "max_bytes": 1024,
    }
    with (
        _servicenow_https_fixture(tmp_path, records) as (
            source_url,
            source_requests,
            certificate,
            truststore,
        ),
        _fake_cmdb() as (cmdb_url, cmdb_state),
    ):
        results = _servicenow_https_results(
            source_root,
            input_value | {"instance_url": source_url},
            tmp_path / "invalid-middle",
            certificate,
            truststore,
            {"cmdb_base_url": cmdb_url},
        )
    assert results["python"] == results["javascript"] == results["java"]
    result = results["python"]
    assert result["partial"] is True
    assert result["summary"] == {
        "assets": 1,
        "relationships": 0,
        "pages": 1,
        "failures": ["invalid_source_record"],
    }
    assert result["failed"] == ["invalid_source_record"]
    assert result["checkpoint"] == {"offset": 1}
    assert cmdb_state.operations == []
    assert len(source_requests) == 3
    assert {
        int(parse_qs(urlsplit(path).query)["sysparm_offset"][0]) for path in source_requests
    } == {0}


def test_servicenow_page_checkpoint_stops_before_first_record_over_max_records(
    tmp_path: Path,
) -> None:
    source_root = CATALOG_ROOT / "variants/servicenow-cmdb-ci-snapshot"
    records = [
        {"sys_id": "ci-1", "name": "first"},
        {"sys_id": "ci-2", "name": "second"},
        {"sys_id": "ci-3", "name": "must-remain-resumable"},
    ]
    input_value = {
        "mode": "sync",
        "scan_id": "scan-record-boundary",
        "source_scope": "servicenow:fixture:cmdb_ci",
        "instance_id": "fixture",
        "page_size": 3,
        "max_pages": 1,
        "max_records": 2,
        "max_bytes": 2048,
    }
    with (
        _servicenow_https_fixture(tmp_path, records) as (
            source_url,
            source_requests,
            certificate,
            truststore,
        ),
        _fake_cmdb() as (cmdb_url, cmdb_state),
    ):
        results = _servicenow_https_results(
            source_root,
            input_value | {"instance_url": source_url},
            tmp_path / "max-records-middle",
            certificate,
            truststore,
            {"cmdb_base_url": cmdb_url},
        )
    assert results["python"] == results["javascript"] == results["java"]
    result = results["python"]
    assert result["partial"] is True
    assert result["summary"] == {
        "assets": 2,
        "relationships": 0,
        "pages": 1,
        "failures": [],
    }
    assert result["failed"] == ["bounded"]
    assert result["checkpoint"] == {"offset": 2}
    assert cmdb_state.operations == []
    assert len(source_requests) == 3


@pytest.mark.parametrize("mode", ["preview", "sync"])
def test_servicenow_final_envelope_and_normalized_assets_obey_max_bytes_in_all_languages(
    tmp_path: Path,
    mode: str,
) -> None:
    source_root = CATALOG_ROOT / "variants/servicenow-cmdb-ci-snapshot"
    records = [{"sys_id": "ci-1", "name": "x" * 350}]
    raw_page = json.dumps({"result": records}, separators=(",", ":")).encode()
    assert len(raw_page) < 1024
    input_value = {
        "mode": mode,
        "scan_id": "scan-output-bound",
        "source_scope": "servicenow:fixture:cmdb_ci",
        "instance_id": "fixture",
        "page_size": 10,
        "max_pages": 1,
        "max_records": 10,
        "max_bytes": 1024,
    }
    with (
        _servicenow_https_fixture(tmp_path, records) as (
            source_url,
            source_requests,
            certificate,
            truststore,
        ),
        _fake_cmdb() as (cmdb_url, cmdb_state),
    ):
        results = _servicenow_https_results(
            source_root,
            input_value | {"instance_url": source_url},
            tmp_path / f"output-bound-{mode}",
            certificate,
            truststore,
            {"cmdb_base_url": cmdb_url},
        )
    assert results["python"] == results["javascript"] == results["java"]
    result = results["python"]
    assert result["partial"] is True
    assert result["summary"]["assets"] == 0
    assert result["summary"]["failures"] == ["max_bytes_exceeded"]
    assert result["checkpoint"] == {"offset": 0}
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 1024
    assert cmdb_state.operations == []
    assert len(source_requests) == 3


def test_servicenow_rejects_unrepresentable_byte_budget_and_oversized_instance_id(
    tmp_path: Path,
) -> None:
    source_root = CATALOG_ROOT / "variants/servicenow-cmdb-ci-snapshot"
    base_input = {
        "mode": "preview",
        "instance_url": "https://fixture.service-now.example",
        "instance_id": "fixture",
        "max_bytes": 100,
    }
    with pytest.raises(ValueError, match="^max_bytes_too_small$"):
        _python_result(source_root / "python.py", base_input)
    with pytest.raises(subprocess.CalledProcessError) as javascript_small:
        _javascript_result(source_root / "javascript.mjs", base_input)
    assert "max_bytes_too_small" in javascript_small.value.stderr
    with pytest.raises(subprocess.CalledProcessError) as java_small:
        _java_result(source_root / "java.java", base_input, tmp_path / "small-budget")
    assert "max_bytes_too_small" in java_small.value.stderr

    oversized = base_input | {"instance_id": "x" * 10_000}
    with pytest.raises(ValueError, match="https_instance_url_and_instance_id_required"):
        _python_result(source_root / "python.py", oversized)
    with pytest.raises(subprocess.CalledProcessError) as javascript_instance:
        _javascript_result(source_root / "javascript.mjs", oversized)
    assert "https_instance_url_and_instance_id_required" in javascript_instance.value.stderr
    with pytest.raises(subprocess.CalledProcessError) as java_instance:
        _java_result(source_root / "java.java", oversized, tmp_path / "oversized-instance")
    assert "https_instance_url_required" in java_instance.value.stderr

    scenario = _json(CATALOG_ROOT / "scenarios/servicenow-cmdb-ci-snapshot/metadata.json")
    assert all(
        variant["input_contract"]["properties"]["max_bytes"]["minimum"] == 1024
        and variant["input_contract"]["properties"]["instance_id"]["max_length"] == 128
        for variant in scenario["variants"]
    )


def test_webhook_deep_payloads_fail_before_recursive_serialization(tmp_path: Path) -> None:
    source_root = CATALOG_ROOT / "variants/webhook-json-normalization"
    payload: dict[str, Any] = {}
    current = payload
    for _ in range(1_500):
        child: dict[str, Any] = {}
        current["nested"] = child
        current = child
    python = runpy.run_path(str(source_root / "python.py"))
    with pytest.raises(ValueError) as captured:
        python["handle"](SimpleNamespace(), {"payload": payload, "max_depth": 64})
    assert str(captured.value) == "payload_too_deep"
    assert "RecursionError" not in repr(captured.value)

    script = """
import { pathToFileURL } from "node:url";
const module = await import(pathToFileURL(process.argv[1]));
const payload = {};
let current = payload;
for (let at = 0; at < 15_000; at += 1) {
  current.nested = {};
  current = current.nested;
}
try {
  await module.handle({}, { payload, max_depth: 64 });
  process.stdout.write("NO_ERROR");
} catch (error) {
  process.stdout.write(JSON.stringify({ name: error.name, message: error.message }));
}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(source_root / "javascript.mjs")],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {"name": "Error", "message": "payload_too_deep"}

    compile_root = tmp_path / "webhook-depth-java"
    compile_root.mkdir()
    (compile_root / "DlrRuntime.java").write_text(JAVA_RUNTIME_SOURCE, encoding="utf-8")
    (compile_root / "Adapter.java").write_text(
        (source_root / "java.java").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (compile_root / "Probe.java").write_text(
        """
import java.util.LinkedHashMap;
import java.util.Map;
public final class Probe {
  public static void main(String[] args) throws Exception {
    Map<String, Object> payload = new LinkedHashMap<>();
    Map<String, Object> current = payload;
    for (int at = 0; at < 15_000; at++) {
      Map<String, Object> child = new LinkedHashMap<>();
      current.put("nested", child);
      current = child;
    }
    try {
      new Adapter().handle(new Context(Map.of()), Map.of("payload", payload, "max_depth", 64));
      System.out.print("NO_ERROR");
    } catch (Throwable error) {
      System.out.print(error.getClass().getSimpleName() + "|" + error.getMessage());
    }
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
    completed = subprocess.run(
        ["java", "-cp", str(compile_root), "Probe"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == "IllegalArgumentException|payload_too_deep"


def test_all_credentialed_cloud_and_servicenow_transports_disable_redirects() -> None:
    slugs = [*CLOUD_SCENARIOS, "servicenow-cmdb-ci-snapshot"]
    for slug in slugs:
        python_source = (CATALOG_ROOT / f"variants/{slug}/python.py").read_text(encoding="utf-8")
        javascript_source = (CATALOG_ROOT / f"variants/{slug}/javascript.mjs").read_text(
            encoding="utf-8"
        )
        java_source = (CATALOG_ROOT / f"variants/{slug}/java.java").read_text(encoding="utf-8")
        assert python_source.count("_NO_REDIRECT_OPENER.open(") == 2
        assert len(re.findall(r'redirect\s*:\s*"manual"', javascript_source)) == 2
        assert "HttpClient.Redirect.NEVER" in java_source
        assert java_source.count(".send(") == 2


def _cloud_operations(source: Path, language: str) -> list[list[Any]]:
    text = source.read_text(encoding="utf-8")
    if language == "python":
        match = re.search(r'OPERATIONS = json\.loads\(\n\s*r?"""(.*?)"""\n\)', text, re.S)
    elif language == "javascript":
        match = re.search(r"const OPERATIONS = (\[.*\]);", text)
    else:
        match = re.search(r'OPERATIONS_JSON = """\n(.*?)\n""";', text, re.S)
    assert match is not None
    return json.loads(match.group(1))


def test_cloud_provenance_matches_published_operation_tables() -> None:
    provenance = _json(CATALOG_ROOT / "provenance.json")
    for slug in CLOUD_SCENARIOS:
        operation_sets = [
            _cloud_operations(CATALOG_ROOT / f"variants/{slug}/python.py", "python"),
            _cloud_operations(CATALOG_ROOT / f"variants/{slug}/javascript.mjs", "javascript"),
            _cloud_operations(CATALOG_ROOT / f"variants/{slug}/java.java", "java"),
        ]
        assert operation_sets[0] == operation_sets[1] == operation_sets[2]
        supported_rows = [
            item
            for item in provenance["coverage"]
            if item["scenario_slug"] == slug
            and item["support_status"] == "supported"
            and item["api_operations"]
        ]
        gap_pairs = {
            (item["external_key"].split(":")[3], api_operation)
            for item in provenance["coverage"]
            if item["scenario_slug"] == slug and item["support_status"] == "gap"
            for api_operation in item["api_operations"]
        }
        executable_pairs = {(operation[0], operation[3]) for operation in operation_sets[0]}
        assert executable_pairs.isdisjoint(gap_pairs)
        assert len(supported_rows) == len(operation_sets[0])
        for row, operation in zip(supported_rows, operation_sets[0], strict=True):
            assert row["api_operations"] == [operation[3]]
            assert row["external_key"].split(":")[3] == operation[0]
            assert row["relationships"] == list(
                dict.fromkeys(relation[2] for relation in operation[10])
            )


def test_schema_accepts_contract_examples_and_rejects_invalid_examples() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    snapshot_schema = _json(CATALOG_ROOT / "schemas/asset-snapshot-v1.schema.json")
    upsert_schema = _json(CATALOG_ROOT / "schemas/cmdb-upsert-v1.schema.json")
    jsonschema.Draft202012Validator.check_schema(snapshot_schema)
    jsonschema.Draft202012Validator.check_schema(upsert_schema)
    asset = {
        "external_key": "example:account:region:server:id-1",
        "class": "server",
        "provider_type": "fixture",
        "name": "fixture",
        "account": "account",
        "region": "region",
        "zone": None,
        "status": "ready",
        "tags": {},
        "attributes": {},
    }
    snapshot = {
        "schema_version": "dlr-asset-snapshot/v1",
        "assets": [asset],
        "relationships": [],
        "summary": {"assets": 1, "relationships": 0, "pages": 1, "failures": []},
        "partial": True,
        "checkpoint": {"failed": [], "limit_reached": True},
    }
    jsonschema.validate(snapshot, snapshot_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(snapshot | {"schema_version": "unknown"}, snapshot_schema)

    common = {
        "schema_version": "dlr-cmdb-upsert/v1",
        "source_scope": "example:account:region",
        "scan_id": "scan-1",
    }
    begin_key = "a" * 64
    asset_key = "b" * 64
    finish_key = "c" * 64
    requests = [
        common
        | {
            "operation": "begin_scan",
            "idempotency_key": begin_key,
            "provider": "alicloud",
            "catalog_version": "1.0.0",
        },
        common
        | {
            "operation": "upsert_assets",
            "idempotency_key": asset_key,
            "batch_id": "assets:alicloud:example:000000",
            "batch_index": 0,
            "assets": [asset],
        },
        common
        | {
            "operation": "finish_scan",
            "idempotency_key": finish_key,
            "complete": True,
            "summary": snapshot["summary"],
        },
    ]
    for body in requests:
        jsonschema.validate(body, upsert_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(requests[0] | {"idempotency_key": "floating"}, upsert_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(requests[-1] | {"complete": False}, upsert_schema)


class _FakeCmdbState:
    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}
        self.operations: list[str] = []


@contextmanager
def _fake_cmdb() -> Iterator[tuple[str, _FakeCmdbState]]:
    state = _FakeCmdbState()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            size = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(size)
            key = self.headers.get("Idempotency-Key", "")
            body = json.loads(payload)
            if body.get("idempotency_key") != key:
                self.send_response(400)
            elif key in state.payloads and state.payloads[key] != payload:
                self.send_response(409)
            else:
                state.payloads[key] = payload
                state.operations.append(body["operation"])
                self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_python_cloud_sync_replay_and_conflict_never_finish_conflict() -> None:
    source = CATALOG_ROOT / "variants/tencentcloud-compute-container-topology/python.py"
    namespace = runpy.run_path(str(source))
    assets = [
        {
            "external_key": "tencentcloud:account:region:cvm_instance:id-1",
            "class": "cvm_instance",
            "provider_type": "DescribeInstances",
            "name": "fixture",
            "account": "account",
            "region": "region",
            "zone": None,
            "status": "RUNNING",
            "tags": {},
            "attributes": {},
        }
    ]
    summary = {"assets": 1, "relationships": 0, "pages": 1, "failures": []}
    input_value = {"scan_id": "scan-1", "source_scope": "tc:account:region"}
    with _fake_cmdb() as (url, state):
        context = SimpleNamespace(config={"cmdb_base_url": url}, secrets={"CMDB_TOKEN": "fixture"})
        deadline = time.monotonic() + 30
        first = namespace["_sync"](context, input_value, assets, [], summary, deadline)
        replay = namespace["_sync"](context, input_value, assets, [], summary, deadline)
        finish_count = state.operations.count("finish_scan")
        conflicting_assets = [assets[0] | {"name": "changed"}]
        conflict = namespace["_sync"](
            context, input_value, conflicting_assets, [], summary, deadline
        )

    assert first["partial"] is False
    assert replay["partial"] is False
    assert len(state.payloads) == 3
    assert conflict["partial"] is True
    assert state.operations.count("finish_scan") == finish_count


def test_public_assets_do_not_embed_machine_paths_secrets_or_remote_logos() -> None:
    _, scenarios = _catalog_assets()
    forbidden = (
        re.compile(r"/Users/[^/\s]+/"),
        re.compile(r"[A-Za-z]:\\\\Users\\\\[^\\\s]+"),
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    )
    for path in CATALOG_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".json", ".py", ".mjs", ".java"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert not any(pattern.search(text) for pattern in forbidden), path

    for scenario in scenarios:
        assert not scenario["logo_key"].startswith(("http://", "https://"))
        serialized = json.dumps(scenario)
        assert "<svg" not in serialized.casefold()
        for variant in scenario["variants"]:
            for value in variant["input_skeleton"].values():
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    hostname = urlsplit(value).hostname or ""
                    assert hostname == "localhost" or hostname.endswith(".example")


def test_csv_variants_stream_to_the_first_real_overflow_and_use_exact_utf8_bounds(
    tmp_path: Path,
) -> None:
    scenario = _json(CATALOG_ROOT / "scenarios/csv-to-json/metadata.json")
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}
    early_stop = {
        "content": 'name\nfirst\nsecond\n"unterminated',
        "encoding": "utf-8",
        "max_rows": 1,
    }
    early_results = {
        "python": _python_result(sources["python"], early_stop),
        "javascript": _javascript_result(sources["javascript"], early_stop),
        "java": _java_result(sources["java"], early_stop, tmp_path / "early"),
    }
    for result in early_results.values():
        assert result["rows"] == [{"name": "first"}]
        assert result["count"] == 1
        assert result["partial"] is True
        assert result["checkpoint"] == {"next_physical_row": 3}

    exact_bytes = len(
        json.dumps([{"name": "é"}], ensure_ascii=False, separators=(",", ":")).encode()
    )
    exact_input = {
        "content": "name\né\n",
        "encoding": "utf-8",
        "max_output_bytes": exact_bytes,
    }
    exact_results = {
        "python": _python_result(sources["python"], exact_input),
        "javascript": _javascript_result(sources["javascript"], exact_input),
        "java": _java_result(sources["java"], exact_input, tmp_path / "exact"),
    }
    for result in exact_results.values():
        assert result["rows"] == [{"name": "é"}]
        assert result["partial"] is False
        assert result["checkpoint"] is None

    below_results = {
        "python": _python_result(
            sources["python"], exact_input | {"max_output_bytes": exact_bytes - 1}
        ),
        "javascript": _javascript_result(
            sources["javascript"], exact_input | {"max_output_bytes": exact_bytes - 1}
        ),
        "java": _java_result(
            sources["java"],
            exact_input | {"max_output_bytes": exact_bytes - 1},
            tmp_path / "below",
        ),
    }
    for result in below_results.values():
        assert result["rows"] == []
        assert result["partial"] is True
        assert result["checkpoint"] == {"next_physical_row": 2}

    assert (
        "const rows = []"
        not in sources["javascript"]
        .read_text(encoding="utf-8")
        .split("function* parseCsv", 1)[1]
        .split("function validateHeaders", 1)[0]
    )
    assert "List<List<String>>" not in sources["java"].read_text(encoding="utf-8")

    large_csv = tmp_path / "streaming.csv"
    large_csv.write_bytes(b"name\nfirst\nsecond\n" + b"tail\n" * 1_500_000)
    javascript_probe = """
import { pathToFileURL } from "node:url";
const { handle } = await import(pathToFileURL(process.argv[1]));
const result = handle({
  inputFiles: [{ path: process.argv[2], sizeBytes: Number(process.argv[3]) }],
}, { max_input_bytes: 16_777_216, max_rows: 1 });
process.stdout.write(JSON.stringify(result));
"""
    javascript_streamed = subprocess.run(
        [
            "node",
            "--max-old-space-size=64",
            "--input-type=module",
            "-e",
            javascript_probe,
            str(sources["javascript"]),
            str(large_csv),
            str(large_csv.stat().st_size),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(javascript_streamed.stdout)["checkpoint"] == {"next_physical_row": 3}

    java_streamed = _java_result(
        sources["java"],
        {
            "content": "name\nfirst\nsecond\n" + "tail\n" * 600_000,
            "max_input_bytes": 16_777_216,
            "max_rows": 1,
        },
        tmp_path / "stream-memory",
        java_options=["-Xmx64m"],
    )
    assert java_streamed["checkpoint"] == {"next_physical_row": 3}


def test_csv_headers_apply_the_same_limits_in_all_published_languages(tmp_path: Path) -> None:
    scenario = _json(CATALOG_ROOT / "scenarios/csv-to-json/metadata.json")
    sources = {item["language"]: _variant_source(item) for item in scenario["variants"]}
    cases = [
        (
            {"content": "value\n", "headers": ["duplicate", "duplicate"]},
            "invalid_or_duplicate_header",
        ),
        ({"content": "value\n", "headers": [""]}, "invalid_or_duplicate_header"),
        ({"content": "value\n", "headers": ["   "]}, "invalid_or_duplicate_header"),
        ({"content": "value\n", "headers": ["a", "b"], "max_columns": 1}, "column_limit_exceeded"),
        ({"content": "value\n", "headers": ["é"], "max_field_bytes": 1}, "field_limit_exceeded"),
        ({"content": "a,b\nvalue\n", "max_columns": 1}, "column_limit_exceeded"),
        ({"content": "é\nvalue\n", "max_field_bytes": 1}, "field_limit_exceeded"),
    ]
    for index, (input_value, code) in enumerate(cases):
        with pytest.raises(ValueError, match=f"^{code}$"):
            _python_result(sources["python"], input_value)
        with pytest.raises(subprocess.CalledProcessError) as javascript_error:
            _javascript_result(sources["javascript"], input_value)
        assert code in javascript_error.value.stderr
        with pytest.raises(subprocess.CalledProcessError) as java_error:
            _java_result(sources["java"], input_value, tmp_path / f"case-{index}")
        assert code in java_error.value.stderr


def test_excel_output_limit_is_incremental_and_exact_for_runnable_variants(
    tmp_path: Path,
) -> None:
    source = CATALOG_ROOT / "variants/excel-to-json/python.py"
    namespace = runpy.run_path(str(source))
    workbook = tmp_path / "fixture.xlsx"
    workbook.write_bytes(b"")
    namespace["handle"].__globals__["_xlsx"] = lambda *_args: (
        ["Sheet1"],
        [["name"], ["é"]],
        False,
        False,
    )
    context = SimpleNamespace(
        input_files=[SimpleNamespace(path=workbook, original_name="fixture.xlsx", size_bytes=0)]
    )
    exact_bytes = len(
        json.dumps([{"name": "é"}], ensure_ascii=False, separators=(",", ":")).encode()
    )
    python_exact = namespace["handle"](context, {"max_output_bytes": exact_bytes})
    python_below = namespace["handle"](context, {"max_output_bytes": exact_bytes - 1})
    assert python_exact["rows"] == [{"name": "é"}]
    assert python_exact["partial"] is False
    assert python_below["rows"] == []
    assert python_below["partial"] is True

    fixture_root = tmp_path / "excel-js-bound"
    module_root = fixture_root / "node_modules/@e965/xlsx"
    module_root.mkdir(parents=True)
    (fixture_root / "recipe.mjs").write_text(
        (CATALOG_ROOT / "variants/excel-to-json/javascript.mjs").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (fixture_root / "fixture.xls").write_bytes(b"")
    (module_root / "package.json").write_text(
        json.dumps({"type": "module", "exports": "./index.js"}), encoding="utf-8"
    )
    (module_root / "index.js").write_text(
        """
export function read() {
  return {
    SheetNames: ["Sheet1"], files: {}, vbaraw: null,
    Sheets: { Sheet1: { "!ref": "A1:A2", A1: { v: "name" }, A2: { v: "é" } } },
  };
}
export const utils = {
  decode_range: () => ({ s: { r: 0, c: 0 }, e: { r: 1, c: 0 } }),
  encode_cell: ({ r }) => `A${r + 1}`,
};
""",
        encoding="utf-8",
    )
    script = """
import { handle } from "./recipe.mjs";
const context = {
  inputFiles: [{ path: "./fixture.xls", originalName: "fixture.xls", sizeBytes: 0 }],
};
const exact = handle(context, { max_output_bytes: Number(process.argv[1]) });
const below = handle(context, { max_output_bytes: Number(process.argv[1]) - 1 });
process.stdout.write(JSON.stringify({ exact, below }));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(exact_bytes)],
        cwd=fixture_root,
        check=True,
        capture_output=True,
        text=True,
    )
    javascript = json.loads(completed.stdout)
    assert javascript["exact"]["rows"] == [{"name": "é"}]
    assert javascript["exact"]["partial"] is False
    assert javascript["below"]["rows"] == []
    assert javascript["below"]["partial"] is True

    o_n_squared_markers = {
        "python.py": "rows + [value]",
        "javascript.mjs": "[...rows, value]",
        "java.java": "new ArrayList<>(rows)",
    }
    for name, marker in o_n_squared_markers.items():
        text = (CATALOG_ROOT / f"variants/excel-to-json/{name}").read_text(encoding="utf-8")
        assert marker not in text


def test_python_database_variants_probe_n_plus_one_and_bound_cells_and_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cursor:
        def __init__(self, records: list[dict[str, object]]) -> None:
            self.records = records
            self.at = 0
            self.fetch_sizes: list[int] = []

        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: object = None) -> None:
            if sql.lstrip().casefold().startswith("select"):
                self.at = 0

        def fetchmany(self, size: int) -> list[dict[str, object]]:
            self.fetch_sizes.append(size)
            batch = self.records[self.at : self.at + size]
            self.at += len(batch)
            return batch

    class Connection:
        def __init__(self, records: list[dict[str, object]]) -> None:
            self.records = records
            self.cursors: list[Cursor] = []

        def execute(self, *_args: object) -> None:
            return None

        def cursor(self, *_args: object, **_kwargs: object) -> Cursor:
            cursor = Cursor(self.records)
            self.cursors.append(cursor)
            return cursor

        def begin(self) -> None:
            return None

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    cases = [
        ("postgresql-readonly-snapshot", "psycopg", "POSTGRES_DSN", "postgresql://fixture"),
        ("mysql-readonly-snapshot", "pymysql", "MYSQL_DSN", "mysql://user@host/db"),
    ]
    exact_output_bytes = len(
        json.dumps([{"value": "é"}], ensure_ascii=False, separators=(",", ":")).encode()
    )
    for slug, module_name, secret_name, dsn in cases:
        module = ModuleType(module_name)
        if module_name == "psycopg":
            rows_module = ModuleType("psycopg.rows")
            rows_module.dict_row = object()  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, "psycopg.rows", rows_module)
        else:
            cursors_module = ModuleType("pymysql.cursors")
            cursors_module.SSDictCursor = object()  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, "pymysql.cursors", cursors_module)
        monkeypatch.setitem(sys.modules, module_name, module)
        namespace = runpy.run_path(str(CATALOG_ROOT / f"variants/{slug}/python.py"))
        connections: list[Connection] = []

        def result(
            records: list[dict[str, object]],
            _module: ModuleType = module,
            _namespace: dict[str, Any] = namespace,
            _connections: list[Connection] = connections,
            _secret_name: str = secret_name,
            _dsn: str = dsn,
            **limits: int,
        ) -> dict[str, Any]:
            def connect(*_args: object, **_kwargs: object) -> Connection:
                connection = Connection(records)
                _connections.append(connection)
                return connection

            _module.connect = connect  # type: ignore[attr-defined]
            return _namespace["handle"](
                SimpleNamespace(secrets={_secret_name: _dsn}),
                {
                    "sql": "SELECT value FROM fixture",
                    "max_rows": 2,
                    "batch_size": 2,
                    **limits,
                },
            )

        exact = result([{"value": "a"}, {"value": "b"}])
        assert exact == {
            "rows": [{"value": "a"}, {"value": "b"}],
            "count": 2,
            "partial": False,
            "checkpoint": None,
        }
        assert [size for cursor in connections[-1].cursors for size in cursor.fetch_sizes] == [2, 1]

        overflow = result([{"value": "a"}, {"value": "b"}, {"value": "c"}])
        assert overflow == {
            "rows": [{"value": "a"}, {"value": "b"}],
            "count": 2,
            "partial": True,
            "checkpoint": {"row_offset": 2},
        }

        giant = result([{"value": b"x" * 1_048_577}])
        assert giant == {
            "rows": [],
            "count": 0,
            "partial": True,
            "checkpoint": {"row_offset": 0},
        }

        exact_output = result([{"value": "é"}], max_output_bytes=exact_output_bytes)
        below_output = result([{"value": "é"}], max_output_bytes=exact_output_bytes - 1)
        assert exact_output["rows"] == [{"value": "é"}]
        assert exact_output["partial"] is False
        assert below_output["rows"] == []
        assert below_output["partial"] is True
        assert below_output["checkpoint"] == {"row_offset": 0}


@pytest.mark.parametrize(
    ("slug", "module_name", "secret_name", "dsn"),
    [
        ("postgresql-readonly-snapshot", "pg", "POSTGRES_DSN", "postgresql://fixture"),
        ("mysql-readonly-snapshot", "mysql2", "MYSQL_DSN", "mysql://user@host/db"),
    ],
)
def test_javascript_database_variants_bound_published_results(
    tmp_path: Path,
    slug: str,
    module_name: str,
    secret_name: str,
    dsn: str,
) -> None:
    fixture_root = tmp_path / slug
    (fixture_root / "recipe.mjs").parent.mkdir(parents=True)
    (fixture_root / "recipe.mjs").write_text(
        (CATALOG_ROOT / f"variants/{slug}/javascript.mjs").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    if module_name == "pg":
        module_root = fixture_root / "node_modules/pg"
        module_root.mkdir(parents=True)
        (module_root / "package.json").write_text(
            json.dumps({"type": "module", "exports": "./index.js"}), encoding="utf-8"
        )
        (module_root / "index.js").write_text(
            """
class Client {
  constructor(options) {
    globalThis.fixtureTrace.connectionOptions = options;
    this.at = 0;
  }
  async connect() {}
  async query(value) {
    if (typeof value === "string") {
      globalThis.fixtureTrace.commands.push(value);
      return { rows: [], fields: [], rowCount: 0 };
    }
    if (value.text.startsWith("DECLARE ")) {
      this.at = 0;
      globalThis.fixtureTrace.declare = value;
      return { rows: [], fields: [], rowCount: 0 };
    }
    if (value.text.startsWith("FETCH FORWARD ")) {
      const count = Number(value.text.match(/^FETCH FORWARD (\\d+) /)[1]);
      globalThis.fixtureTrace.fetchSizes.push(count);
      const values = globalThis.fixtureRows.slice(this.at, this.at + count);
      this.at += values.length;
      globalThis.fixtureTrace.yielded += values.length;
      return {
        rows: values.map((item) => [item]),
        fields: [{ name: "value" }], rowCount: values.length,
      };
    }
    throw new Error(`unexpected query: ${value.text}`);
  }
  async end() { globalThis.fixtureTrace.ended = true; }
}
export default { Client };
""",
            encoding="utf-8",
        )
    else:
        module_root = fixture_root / "node_modules/mysql2"
        module_root.mkdir(parents=True)
        (module_root / "package.json").write_text(
            json.dumps({"type": "module", "exports": "./index.js"}),
            encoding="utf-8",
        )
        (module_root / "index.js").write_text(
            """
export default {
  createConnection(options) {
    globalThis.fixtureTrace.connectionOptions = options;
    return {
      connect(callback) {
        globalThis.fixtureTrace.connected = true;
        callback(null);
      },
      query(sql, values, callback) {
        if (typeof sql === "string") {
          globalThis.fixtureTrace.commands.push(sql);
          const done = typeof values === "function" ? values : callback;
          done(null, {});
          return undefined;
        }
        globalThis.fixtureTrace.queryOptions = sql;
        const limit = Number(sql.sql.match(/ LIMIT (\\d+)$/)[1]);
        const selectedRows = globalThis.fixtureRows.slice(0, limit);
        return {
          once(event, listener) {
            if (event === "fields") listener([{ name: "value" }]);
            return this;
          },
          stream(streamOptions) {
            globalThis.fixtureTrace.highWaterMark = streamOptions.highWaterMark;
            return {
              async *[Symbol.asyncIterator]() {
                for (const value of selectedRows) {
                  globalThis.fixtureTrace.yielded += 1;
                  yield [value];
                }
              },
            };
          },
        };
      },
      destroy() { globalThis.fixtureTrace.destroyed = true; },
      end(callback) { globalThis.fixtureTrace.ended = true; callback(); },
    };
  },
};
""",
            encoding="utf-8",
        )
    script = """
import { handle } from "./recipe.mjs";
const secretName = process.argv[1];
const dsn = process.argv[2];
const exactBytes = Number(process.argv[3]);
const context = { secrets: new Map([[secretName, dsn]]) };
const traces = [];
const run = async (rows, limits = {}) => {
  globalThis.fixtureRows = rows;
  globalThis.fixtureTrace = {
    commands: [], fetchSizes: [], yielded: 0, connected: false,
    destroyed: false, ended: false,
  };
  const result = await handle(context, {
    sql: secretName === "POSTGRES_DSN"
      ? "SELECT value FROM fixture WHERE id = $1"
      : "SELECT value FROM fixture WHERE id = ?",
    params: [7], max_rows: 2, batch_size: 2, ...limits,
  });
  traces.push(globalThis.fixtureTrace);
  return result;
};
const exact = await run(["a", "b"]);
const overflow = await run(["a", "b", "c", "d", "e"]);
const giant = await run([Buffer.alloc(1_048_577)]);
const exactOutput = await run(["é"], { max_output_bytes: exactBytes });
const belowOutput = await run(["é"], { max_output_bytes: exactBytes - 1 });
process.stdout.write(JSON.stringify({ exact, overflow, giant, exactOutput, belowOutput, traces }));
"""
    exact_bytes = len(
        json.dumps([{"value": "é"}], ensure_ascii=False, separators=(",", ":")).encode()
    )
    completed = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            secret_name,
            dsn,
            str(exact_bytes),
        ],
        cwd=fixture_root,
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(completed.stdout)
    assert output["exact"] == {
        "rows": [{"value": "a"}, {"value": "b"}],
        "count": 2,
        "partial": False,
        "checkpoint": None,
    }
    assert output["overflow"] == {
        "rows": [{"value": "a"}, {"value": "b"}],
        "count": 2,
        "partial": True,
        "checkpoint": {"row_offset": 2},
    }
    assert output["giant"] == {
        "rows": [],
        "count": 0,
        "partial": True,
        "checkpoint": {"row_offset": 0},
    }
    assert output["exactOutput"]["rows"] == [{"value": "é"}]
    assert output["exactOutput"]["partial"] is False
    assert output["belowOutput"]["rows"] == []
    assert output["belowOutput"]["partial"] is True
    exact_trace, overflow_trace, giant_trace, exact_output_trace, below_output_trace = output[
        "traces"
    ]
    if module_name == "pg":
        expected_declare = (
            "DECLARE dlr_snapshot_cursor NO SCROLL CURSOR FOR "
            "SELECT * FROM (SELECT value FROM fixture WHERE id = $1) AS dlr_snapshot"
        )
        assert exact_trace["declare"] == {"text": expected_declare, "values": [7]}
        assert exact_trace["fetchSizes"] == [2, 1]
        assert exact_trace["yielded"] == 2
        assert overflow_trace["fetchSizes"] == [2, 1]
        assert overflow_trace["yielded"] == 3
        assert all(
            trace["commands"] == ["BEGIN READ ONLY", "SET LOCAL TIME ZONE 'UTC'", "ROLLBACK"]
            and trace["ended"] is True
            for trace in output["traces"]
        )
    else:
        assert exact_trace["connectionOptions"]["multipleStatements"] is False
        assert exact_trace["queryOptions"] == {
            "sql": "SELECT * FROM (SELECT value FROM fixture WHERE id = ?) AS dlr_snapshot LIMIT 3",
            "values": [7],
            "rowsAsArray": True,
            "timeout": 30_000,
        }
        assert exact_trace["highWaterMark"] == 2
        assert exact_trace["yielded"] == 2
        assert exact_trace["commands"] == [
            "START TRANSACTION READ ONLY",
            "SET time_zone = '+00:00'",
            "SET SESSION MAX_EXECUTION_TIME=?",
            "ROLLBACK",
        ]
        assert exact_trace["ended"] is True
        assert exact_trace["destroyed"] is False
        assert overflow_trace["highWaterMark"] == 2
        assert overflow_trace["yielded"] == 3
        assert overflow_trace["destroyed"] is True
        assert overflow_trace["ended"] is False
        assert giant_trace["destroyed"] is True
        assert exact_output_trace["ended"] is True
        assert below_output_trace["destroyed"] is True


def test_java_database_variants_probe_n_plus_one_and_reject_giant_cells(
    tmp_path: Path,
) -> None:
    fake_driver = r"""
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.Driver;
import java.sql.DriverManager;
import java.sql.DriverPropertyInfo;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.List;
import java.util.Properties;
import java.util.logging.Logger;

public final class BoundedDriver implements Driver {
    static {
        try { DriverManager.registerDriver(new BoundedDriver()); }
        catch (SQLException error) { throw new ExceptionInInitializerError(error); }
    }
    public Connection connect(String url, Properties info) {
        if (!acceptsURL(url)) return null;
        String mode = url.substring(url.lastIndexOf(':') + 1);
        List<Object> values = switch (mode) {
            case "exact" -> List.of("a", "b");
            case "overflow" -> List.of("a", "b", "c");
            case "giant" -> List.of(new byte[1_048_577]);
            default -> List.of();
        };
        return (Connection) Proxy.newProxyInstance(
            BoundedDriver.class.getClassLoader(), new Class<?>[] {Connection.class},
            (proxy, method, args) -> switch (method.getName()) {
                case "createStatement" -> statement();
                case "prepareStatement" -> prepared(values);
                case "getAutoCommit" -> false;
                case "isClosed", "isWrapperFor" -> false;
                case "unwrap" -> throw new SQLException("not_a_wrapper");
                default -> defaultValue(method.getReturnType());
            }
        );
    }
    private static Statement statement() {
        return (Statement) Proxy.newProxyInstance(
            BoundedDriver.class.getClassLoader(), new Class<?>[] {Statement.class},
            (proxy, method, args) -> defaultValue(method.getReturnType())
        );
    }
    private static PreparedStatement prepared(List<Object> values) {
        return (PreparedStatement) Proxy.newProxyInstance(
            BoundedDriver.class.getClassLoader(), new Class<?>[] {PreparedStatement.class},
            (proxy, method, args) -> method.getName().equals("executeQuery")
                ? result(values) : defaultValue(method.getReturnType())
        );
    }
    private static ResultSet result(List<Object> values) {
        int[] at = {-1};
        ResultSetMetaData metadata = (ResultSetMetaData) Proxy.newProxyInstance(
            BoundedDriver.class.getClassLoader(), new Class<?>[] {ResultSetMetaData.class},
            (proxy, method, args) -> switch (method.getName()) {
                case "getColumnCount" -> 1;
                case "getColumnLabel" -> "value";
                default -> defaultValue(method.getReturnType());
            }
        );
        return (ResultSet) Proxy.newProxyInstance(
            BoundedDriver.class.getClassLoader(), new Class<?>[] {ResultSet.class},
            (proxy, method, args) -> switch (method.getName()) {
                case "next" -> ++at[0] < values.size();
                case "getMetaData" -> metadata;
                case "getObject" -> values.get(at[0]);
                default -> defaultValue(method.getReturnType());
            }
        );
    }
    private static Object defaultValue(Class<?> type) {
        if (!type.isPrimitive()) return null;
        if (type == boolean.class) return false;
        if (type == byte.class) return (byte) 0;
        if (type == short.class) return (short) 0;
        if (type == int.class) return 0;
        if (type == long.class) return 0L;
        if (type == float.class) return 0F;
        if (type == double.class) return 0D;
        if (type == char.class) return '\0';
        return null;
    }
    public boolean acceptsURL(String url) { return url != null && url.startsWith("jdbc:bounded:"); }
    public DriverPropertyInfo[] getPropertyInfo(String url, Properties info) {
        return new DriverPropertyInfo[0];
    }
    public int getMajorVersion() { return 1; }
    public int getMinorVersion() { return 0; }
    public boolean jdbcCompliant() { return false; }
    public Logger getParentLogger() { return Logger.getGlobal(); }
}
"""
    cases = [
        ("postgresql-readonly-snapshot", "POSTGRES_DSN"),
        ("mysql-readonly-snapshot", "MYSQL_DSN"),
    ]
    for slug, secret_name in cases:
        source = CATALOG_ROOT / f"variants/{slug}/java.java"
        results = {}
        for mode in ("exact", "overflow", "giant"):
            results[mode] = _java_result(
                source,
                {"sql": "SELECT value FROM fixture", "max_rows": 2, "batch_size": 2},
                tmp_path / f"{slug}-{mode}",
                {secret_name: f"jdbc:bounded:{mode}"},
                support_sources={"BoundedDriver.java": fake_driver},
                java_options=["-Djdbc.drivers=BoundedDriver"],
            )
        exact_output_bytes = len(
            json.dumps([{"value": "a"}, {"value": "b"}], separators=(",", ":")).encode()
        )
        exact_limit = _java_result(
            source,
            {
                "sql": "SELECT value FROM fixture",
                "max_rows": 2,
                "max_output_bytes": exact_output_bytes,
            },
            tmp_path / f"{slug}-exact-limit",
            {secret_name: "jdbc:bounded:exact"},
            support_sources={"BoundedDriver.java": fake_driver},
            java_options=["-Djdbc.drivers=BoundedDriver"],
        )
        below_limit = _java_result(
            source,
            {
                "sql": "SELECT value FROM fixture",
                "max_rows": 2,
                "max_output_bytes": exact_output_bytes - 1,
            },
            tmp_path / f"{slug}-below-limit",
            {secret_name: "jdbc:bounded:exact"},
            support_sources={"BoundedDriver.java": fake_driver},
            java_options=["-Djdbc.drivers=BoundedDriver"],
        )
        assert results["exact"] == {
            "rows": [{"value": "a"}, {"value": "b"}],
            "count": 2,
            "partial": False,
            "checkpoint": None,
        }
        assert results["overflow"] == {
            "rows": [{"value": "a"}, {"value": "b"}],
            "count": 2,
            "partial": True,
            "checkpoint": {"row_offset": 2},
        }
        assert results["giant"] == {
            "rows": [],
            "count": 0,
            "partial": True,
            "checkpoint": {"row_offset": 0},
        }
        assert exact_limit["rows"] == [{"value": "a"}, {"value": "b"}]
        assert exact_limit["partial"] is False
        assert below_limit["rows"] == [{"value": "a"}]
        assert below_limit["partial"] is True
        assert below_limit["checkpoint"] == {"row_offset": 1}


def _compact_utf8_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def test_python_storage_envelopes_count_base64_and_resume_without_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s3 = runpy.run_path(str(CATALOG_ROOT / "variants/s3-compatible-list-read/python.py"))

    class Body:
        def __init__(self, value: bytes) -> None:
            self.value = value

        def read(self, _maximum: int) -> bytes:
            return self.value

        def close(self) -> None:
            return

    class S3:
        def __init__(self) -> None:
            self.gets = 0

        def list_objects_v2(self, **_kwargs: object) -> dict[str, object]:
            return {
                "Contents": [
                    {"Key": "a", "Size": 120},
                    {"Key": "b", "Size": 0},
                ],
                "IsTruncated": False,
            }

        def get_object(self, **_kwargs: object) -> dict[str, object]:
            self.gets += 1
            return {"Body": Body(b"x" * 120)}

    blocked_client = S3()
    blocked = s3["_list_and_read"](
        blocked_client,
        "fixture",
        {"max_total_bytes": 450, "max_object_bytes": 1024},
        {"a"},
    )
    assert blocked["objects"] == []
    assert blocked["checkpoint"]["object_offset"] == 0
    assert blocked_client.gets == 0
    assert _compact_utf8_size(blocked) <= 450

    client = S3()
    first = s3["_list_and_read"](
        client,
        "fixture",
        {"max_total_bytes": 500, "max_object_bytes": 1024},
        {"a"},
    )
    assert [item["key"] for item in first["objects"]] == ["a"]
    assert first["checkpoint"] == {
        "continuation_token": None,
        "object_offset": 1,
        "reason": "output_limit",
    }
    assert _compact_utf8_size(first) <= 500
    resumed = s3["_list_and_read"](
        S3(),
        "fixture",
        {"max_total_bytes": 500, "object_offset": 1},
        set(),
    )
    assert [item["key"] for item in resumed["objects"]] == ["b"]
    assert resumed["checkpoint"] is None

    fingerprint = "SHA256:" + __import__("base64").b64encode(
        hashlib.sha256(b"fixture-key").digest()
    ).decode().rstrip("=")

    class Key:
        def asbytes(self) -> bytes:
            return b"fixture-key"

    class Transport:
        def __init__(self, _socket: object) -> None:
            return

        def start_client(self, **_kwargs: object) -> None:
            return

        def get_remote_server_key(self) -> Key:
            return Key()

        def auth_password(self, *_args: object) -> None:
            return

        def close(self) -> None:
            return

    second_path = "a" * 30

    class Sftp:
        def normalize(self, value: str) -> str:
            return value

        def listdir_iter(self, _path: str, *, read_aheads: int) -> Iterator[object]:
            assert read_aheads == 1
            for name, size in (("b", 120), (second_path, 0), ("c", 0)):
                yield SimpleNamespace(
                    filename=name,
                    st_mode=0o100000,
                    st_size=size,
                    st_mtime=0,
                )

        def open(self, resolved: str, _mode: str) -> Any:
            return __import__("io").BytesIO(b"x" * 120 if resolved.endswith("/b") else b"")

        def close(self) -> None:
            return

    paramiko = ModuleType("paramiko")
    paramiko.Transport = Transport  # type: ignore[attr-defined]
    paramiko.SFTPClient = SimpleNamespace(  # type: ignore[attr-defined]
        from_transport=lambda _transport: Sftp()
    )
    monkeypatch.setitem(sys.modules, "paramiko", paramiko)
    monkeypatch.setattr("socket.create_connection", lambda *_args, **_kwargs: object())
    sftp = runpy.run_path(str(CATALOG_ROOT / "variants/sftp-list-read/python.py"))
    base_input = {
        "host": "sftp.example",
        "username": "fixture",
        "host_fingerprint_sha256": fingerprint,
        "base_directory": "/base",
        "max_files": 2,
        "max_file_bytes": 1024,
        "max_total_bytes": 450,
    }
    first = sftp["handle"](
        SimpleNamespace(secrets={"SFTP_PASSWORD": "fixture"}),
        base_input | {"read_paths": ["b"]},
    )
    assert [item["path"] for item in first["files"]] == ["b"]
    assert first["checkpoint"] == {
        "start_at": second_path,
        "reason": "output_limit",
    }
    assert _compact_utf8_size(first) <= 450
    resumed = sftp["handle"](
        SimpleNamespace(secrets={"SFTP_PASSWORD": "fixture"}),
        base_input | {"start_at": second_path},
    )
    assert [item["path"] for item in resumed["files"]] == [second_path, "c"]
    assert resumed["checkpoint"] is None


def test_storage_contracts_describe_complete_envelope_and_exact_checkpoint() -> None:
    for slug, checkpoint_field in (
        ("s3-compatible-list-read", "object_offset"),
        ("sftp-list-read", "start_at"),
    ):
        metadata = _json(CATALOG_ROOT / f"scenarios/{slug}/metadata.json")
        for variant in metadata["variants"]:
            assert variant["input_contract"]["properties"]["max_total_bytes"]["minimum"] == 256
            assert checkpoint_field in variant["input_contract"]["properties"]
            assert (
                "complete compact JSON UTF-8 envelope" in variant["output_contract"]["byte_budget"]
            )
            assert "checkpoint" in variant["output_contract"]["required"]


def test_javascript_storage_envelopes_count_base64_and_resume_without_skips(
    tmp_path: Path,
) -> None:
    package = json.dumps({"type": "module", "exports": "./index.js"})
    s3_root = tmp_path / "s3-storage-budget"
    s3_module = s3_root / "node_modules/@aws-sdk/client-s3"
    s3_module.mkdir(parents=True)
    (s3_root / "recipe.mjs").write_text(
        (CATALOG_ROOT / "variants/s3-compatible-list-read/javascript.mjs").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (s3_module / "package.json").write_text(package, encoding="utf-8")
    (s3_module / "index.js").write_text(
        """
import { Readable } from "node:stream";
export class ListObjectsV2Command { constructor(value) { this.value = value; this.kind = "list"; } }
export class GetObjectCommand { constructor(value) { this.value = value; this.kind = "get"; } }
export class S3Client {
  async send(command) {
    if (command.kind === "list") return {
      Contents: [{ Key: "a", Size: 120 }, { Key: "b", Size: 0 }],
      IsTruncated: false,
    };
    globalThis.s3Gets += 1;
    return { Body: Readable.from([Buffer.alloc(120, 120)]) };
  }
  destroy() {}
}
""",
        encoding="utf-8",
    )
    s3_script = """
import { handle } from "./recipe.mjs";
globalThis.s3Gets = 0;
const context = { config: {}, secrets: new Map([
  ["S3_ACCESS_KEY_ID", "fixture"], ["S3_SECRET_ACCESS_KEY", "fixture"],
]), inputFiles: [], logger: {} };
const blocked = await handle(context, {
  bucket: "fixture", read_keys: ["a"], max_object_bytes: 1024, max_total_bytes: 450,
});
const blockedGets = globalThis.s3Gets;
const first = await handle(context, {
  bucket: "fixture", read_keys: ["a"], max_object_bytes: 1024, max_total_bytes: 500,
});
const resumed = await handle(context, {
  bucket: "fixture", object_offset: 1, max_total_bytes: 500,
});
process.stdout.write(JSON.stringify({ blocked, blockedGets, first, resumed }));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", s3_script],
        cwd=s3_root,
        check=True,
        capture_output=True,
        text=True,
    )
    s3 = json.loads(completed.stdout)
    assert s3["blocked"]["objects"] == []
    assert s3["blockedGets"] == 0
    assert _compact_utf8_size(s3["blocked"]) <= 450
    assert [item["key"] for item in s3["first"]["objects"]] == ["a"]
    assert s3["first"]["checkpoint"]["object_offset"] == 1
    assert _compact_utf8_size(s3["first"]) <= 500
    assert [item["key"] for item in s3["resumed"]["objects"]] == ["b"]

    second_path = "a" * 30
    sftp_root = tmp_path / "sftp-storage-budget"
    sftp_module = sftp_root / "node_modules/ssh2-sftp-client"
    sftp_module.mkdir(parents=True)
    (sftp_root / "recipe.mjs").write_text(
        (CATALOG_ROOT / "variants/sftp-list-read/javascript.mjs").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (sftp_module / "package.json").write_text(package, encoding="utf-8")
    (sftp_module / "index.js").write_text(
        f"""
import {{ Readable }} from "node:stream";
export default class MockClient {{
  constructor() {{
    this.reads = 0;
    this.sftp = {{
      opendir: (_path, callback) => callback(null, Buffer.from("handle")),
      readdir: (_handle, callback) => {{
        if (this.reads++ === 0) callback(null, [
          {{ filename: "b", attrs: {{ mode: 0o100000, size: 120, mtime: 0 }} }},
          {{ filename: "{second_path}", attrs: {{ mode: 0o100000, size: 0, mtime: 0 }} }},
          {{ filename: "c", attrs: {{ mode: 0o100000, size: 0, mtime: 0 }} }},
        ]);
        else {{ const error = new Error("EOF"); error.code = 1; callback(error); }}
      }},
      close: (_handle, callback) => callback(null),
    }};
  }}
  async connect() {{}}
  async realPath(value) {{ return value === "." ? "/base" : value; }}
  createReadStream(remotePath) {{
    return Readable.from([remotePath.endsWith("/b") ? Buffer.alloc(120, 120) : Buffer.alloc(0)]);
  }}
  async end() {{}}
}}
""",
        encoding="utf-8",
    )
    sftp_script = f"""
import {{ handle }} from "./recipe.mjs";
const context = {{ config: {{}}, secrets: new Map([
  ["SFTP_USERNAME", "fixture"], ["SFTP_PASSWORD", "fixture"],
]), inputFiles: [], logger: {{}} }};
const common = {{
  host: "sftp.example", host_fingerprint_sha256: "SHA256:fixture",
  base_directory: "/base", max_files: 2, max_file_bytes: 1024, max_total_bytes: 450,
}};
const first = await handle(context, {{ ...common, read_paths: ["b"] }});
const resumed = await handle(context, {{ ...common, start_at: "{second_path}" }});
process.stdout.write(JSON.stringify({{ first, resumed }}));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", sftp_script],
        cwd=sftp_root,
        check=True,
        capture_output=True,
        text=True,
    )
    sftp = json.loads(completed.stdout)
    assert [item["path"] for item in sftp["first"]["files"]] == ["b"]
    assert sftp["first"]["checkpoint"] == {
        "start_at": second_path,
        "reason": "output_limit",
    }
    assert _compact_utf8_size(sftp["first"]) <= 450
    assert [item["path"] for item in sftp["resumed"]["files"]] == [second_path, "c"]


def test_java_storage_envelopes_count_base64_and_resume_without_skips(
    tmp_path: Path,
) -> None:
    s3_support = {
        "software/amazon/awssdk/auth/credentials/AwsCredentials.java": """
package software.amazon.awssdk.auth.credentials;
public interface AwsCredentials {}
""",
        "software/amazon/awssdk/auth/credentials/AwsBasicCredentials.java": """
package software.amazon.awssdk.auth.credentials;
public final class AwsBasicCredentials implements AwsCredentials {
  public static AwsBasicCredentials create(String access, String secret) {
    return new AwsBasicCredentials();
  }
}
""",
        "software/amazon/awssdk/auth/credentials/AwsSessionCredentials.java": """
package software.amazon.awssdk.auth.credentials;
public final class AwsSessionCredentials implements AwsCredentials {
  public static AwsSessionCredentials create(String access, String secret, String token) {
    return new AwsSessionCredentials();
  }
}
""",
        "software/amazon/awssdk/auth/credentials/StaticCredentialsProvider.java": """
package software.amazon.awssdk.auth.credentials;
public final class StaticCredentialsProvider {
  public static StaticCredentialsProvider create(AwsCredentials value) {
    return new StaticCredentialsProvider();
  }
}
""",
        "software/amazon/awssdk/core/ResponseInputStream.java": """
package software.amazon.awssdk.core;
import java.io.FilterInputStream;
import java.io.InputStream;
public final class ResponseInputStream<T> extends FilterInputStream {
  public ResponseInputStream(InputStream stream) { super(stream); }
}
""",
        "software/amazon/awssdk/regions/Region.java": """
package software.amazon.awssdk.regions;
public final class Region {
  public static Region of(String value) { return new Region(); }
}
""",
        "software/amazon/awssdk/services/s3/model/ListObjectsV2Request.java": """
package software.amazon.awssdk.services.s3.model;
public final class ListObjectsV2Request {
  public static Builder builder() { return new Builder(); }
  public static final class Builder {
    public Builder bucket(String value) { return this; }
    public Builder prefix(String value) { return this; }
    public Builder maxKeys(int value) { return this; }
    public Builder continuationToken(String value) { return this; }
    public ListObjectsV2Request build() { return new ListObjectsV2Request(); }
  }
}
""",
        "software/amazon/awssdk/services/s3/model/GetObjectRequest.java": """
package software.amazon.awssdk.services.s3.model;
public final class GetObjectRequest {
  public static Builder builder() { return new Builder(); }
  public static final class Builder {
    public Builder bucket(String value) { return this; }
    public Builder key(String value) { return this; }
    public Builder range(String value) { return this; }
    public GetObjectRequest build() { return new GetObjectRequest(); }
  }
}
""",
        "software/amazon/awssdk/services/s3/model/S3Object.java": """
package software.amazon.awssdk.services.s3.model;
import java.time.Instant;
public final class S3Object {
  private final String key; private final long size;
  public S3Object(String key, long size) { this.key = key; this.size = size; }
  public String key() { return key; }
  public long size() { return size; }
  public String eTag() { return null; }
  public Instant lastModified() { return null; }
}
""",
        "software/amazon/awssdk/services/s3/model/ListObjectsV2Response.java": """
package software.amazon.awssdk.services.s3.model;
import java.util.List;
public final class ListObjectsV2Response {
  public List<S3Object> contents() {
    return List.of(new S3Object("a", 120), new S3Object("b", 0));
  }
  public Boolean isTruncated() { return false; }
  public String nextContinuationToken() { return null; }
}
""",
        "software/amazon/awssdk/services/s3/S3Client.java": """
package software.amazon.awssdk.services.s3;
import java.io.ByteArrayInputStream;
import java.net.URI;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.core.ResponseInputStream;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.model.*;
public final class S3Client implements AutoCloseable {
  public static Builder builder() { return new Builder(); }
  public ListObjectsV2Response listObjectsV2(ListObjectsV2Request request) {
    return new ListObjectsV2Response();
  }
  public ResponseInputStream<Object> getObject(GetObjectRequest request) {
    if (Boolean.getBoolean("fixture.blockGet")) throw new IllegalStateException("read too early");
    return new ResponseInputStream<>(new ByteArrayInputStream(new byte[120]));
  }
  public void close() {}
  public static final class Builder {
    public Builder region(Region value) { return this; }
    public Builder credentialsProvider(StaticCredentialsProvider value) { return this; }
    public Builder forcePathStyle(boolean value) { return this; }
    public Builder endpointOverride(URI value) { return this; }
    public S3Client build() { return new S3Client(); }
  }
}
""",
    }
    s3_source = CATALOG_ROOT / "variants/s3-compatible-list-read/java.java"
    secrets = {"S3_ACCESS_KEY_ID": "fixture", "S3_SECRET_ACCESS_KEY": "fixture"}
    blocked = _java_result(
        s3_source,
        {
            "bucket": "fixture",
            "read_keys": ["a"],
            "max_object_bytes": 1024,
            "max_total_bytes": 450,
        },
        tmp_path / "s3-blocked",
        secrets,
        support_sources=s3_support,
        java_options=["-Dfixture.blockGet=true"],
    )
    assert blocked["objects"] == []
    assert blocked["checkpoint"]["object_offset"] == 0
    assert _compact_utf8_size(blocked) <= 450
    first = _java_result(
        s3_source,
        {
            "bucket": "fixture",
            "read_keys": ["a"],
            "max_object_bytes": 1024,
            "max_total_bytes": 500,
        },
        tmp_path / "s3-first",
        secrets,
        support_sources=s3_support,
    )
    assert [item["key"] for item in first["objects"]] == ["a"]
    assert first["checkpoint"]["object_offset"] == 1
    assert _compact_utf8_size(first) <= 500
    resumed = _java_result(
        s3_source,
        {"bucket": "fixture", "object_offset": 1, "max_total_bytes": 500},
        tmp_path / "s3-resumed",
        secrets,
        support_sources=s3_support,
    )
    assert [item["key"] for item in resumed["objects"]] == ["b"]

    second_path = "a" * 30
    sftp_support = {
        "com/jcraft/jsch/HostKeyRepository.java": """
package com.jcraft.jsch;
public interface HostKeyRepository {
  int OK = 0, NOT_INCLUDED = 1, CHANGED = 2;
  int check(String host, byte[] key);
  void add(HostKey key, UserInfo user);
  void remove(String host, String type);
  void remove(String host, String type, byte[] key);
  String getKnownHostsRepositoryID();
  HostKey[] getHostKey();
  HostKey[] getHostKey(String host, String type);
}
""",
        "com/jcraft/jsch/HostKey.java": "package com.jcraft.jsch; public class HostKey {}\n",
        "com/jcraft/jsch/UserInfo.java": "package com.jcraft.jsch; public interface UserInfo {}\n",
        "com/jcraft/jsch/JSchException.java": """
package com.jcraft.jsch; public class JSchException extends Exception {
  public JSchException(String value) { super(value); }
}
""",
        "com/jcraft/jsch/JSch.java": """
package com.jcraft.jsch;
public final class JSch {
  public void setHostKeyRepository(HostKeyRepository value) {}
  public void addIdentity(String name, byte[] key, byte[] pub, byte[] passphrase) {}
  public Session getSession(String username, String host, int port) { return new Session(); }
}
""",
        "com/jcraft/jsch/Session.java": """
package com.jcraft.jsch;
public final class Session {
  public void setConfig(String key, String value) {}
  public void setPassword(String value) {}
  public void connect(int timeout) {}
  public Object openChannel(String type) { return new ChannelSftp(); }
  public void disconnect() {}
}
""",
        "com/jcraft/jsch/SftpATTRS.java": """
package com.jcraft.jsch;
public final class SftpATTRS {
  private final long size;
  public SftpATTRS(long size) { this.size = size; }
  public boolean isReg() { return true; }
  public long getSize() { return size; }
  public int getMTime() { return 0; }
}
""",
        "com/jcraft/jsch/ChannelSftp.java": f"""
package com.jcraft.jsch;
import java.io.ByteArrayInputStream;
import java.io.InputStream;
public final class ChannelSftp {{
  public interface LsEntrySelector {{
    int CONTINUE = 0, BREAK = 1;
    int select(LsEntry entry);
  }}
  public static final class LsEntry {{
    private final String name; private final SftpATTRS attrs;
    public LsEntry(String name, long size) {{ this.name = name; this.attrs = new SftpATTRS(size); }}
    public String getFilename() {{ return name; }}
    public SftpATTRS getAttrs() {{ return attrs; }}
  }}
  public void connect(int timeout) {{}}
  public String realpath(String value) {{ return value; }}
  public void ls(String directory, LsEntrySelector selector) {{
    LsEntry[] values = {{
      new LsEntry("b", 120),
      new LsEntry("{second_path}", 0),
      new LsEntry("c", 0)
    }};
    for (LsEntry value : values) if (selector.select(value) == LsEntrySelector.BREAK) break;
  }}
  public InputStream get(String path) {{
    return new ByteArrayInputStream(path.endsWith("/b") ? new byte[120] : new byte[0]);
  }}
  public void disconnect() {{}}
}}
""",
    }
    sftp_source = CATALOG_ROOT / "variants/sftp-list-read/java.java"
    sftp_secrets = {"SFTP_USERNAME": "fixture", "SFTP_PASSWORD": "fixture"}
    common = {
        "host": "sftp.example",
        "host_fingerprint_sha256": "SHA256:fixture",
        "base_directory": "/base",
        "max_files": 2,
        "max_file_bytes": 1024,
        "max_total_bytes": 450,
    }
    first = _java_result(
        sftp_source,
        common | {"read_paths": ["b"]},
        tmp_path / "sftp-first",
        sftp_secrets,
        support_sources=sftp_support,
    )
    assert [item["path"] for item in first["files"]] == ["b"]
    assert first["checkpoint"] == {"start_at": second_path, "reason": "output_limit"}
    assert _compact_utf8_size(first) <= 450
    resumed = _java_result(
        sftp_source,
        common | {"start_at": second_path},
        tmp_path / "sftp-resumed",
        sftp_secrets,
        support_sources=sftp_support,
    )
    assert [item["path"] for item in resumed["files"]] == [second_path, "c"]
