"""Direct-source pagination regressions for Alibaba Cloud catalog Recipes."""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from dlr.runtime.java_runtime import SOURCE as JAVA_RUNTIME_SOURCE

CATALOG_ROOT = Path(__file__).parents[1] / "src/dlr/control/template_catalog"
ALICLOUD_SCENARIOS = (
    "alicloud-compute-container-topology",
    "alicloud-network-ingress-topology",
    "alicloud-database-middleware-inventory",
)
REGION = "ap-example-1"
SECRETS = {
    "ALICLOUD_ACCESS_KEY_ID": "AUDIT_ACCESS_KEY",
    "ALICLOUD_ACCESS_KEY_SECRET": "AUDIT_SIGNING_KEY",
}


def _scenario_sources(slug: str) -> dict[str, Path]:
    metadata = json.loads(
        (CATALOG_ROOT / f"scenarios/{slug}/metadata.json").read_text(encoding="utf-8")
    )
    return {
        variant["language"]: CATALOG_ROOT / variant["code_resource"]
        for variant in metadata["variants"]
    }


def _set_path(target: dict[str, Any], dotted: str, value: Any) -> None:
    current = target
    parts = dotted.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _stub_payload(
    plan: dict[str, Any], query: dict[str, str], key: str, repeat_key: str | None
) -> dict[str, Any]:
    pagination = plan["pagination"]
    kind = pagination[0]
    if kind in {"numbered", "current-page"}:
        count = int(query[pagination[1]]) if query[pagination[4]] == "1" else 0
        next_token = None
    elif kind == "none":
        count = 2
        next_token = None
    else:
        current_token = query.get(pagination[4])
        count = 1 if current_token is None else 0
        next_token = plan["token"] if current_token is None or key == repeat_key else None
    records: list[dict[str, Any]] = []
    for _ in range(count):
        record: dict[str, Any] = {}
        _set_path(record, plan["id_path"], f"AUDIT_{plan['resource']}")
        records.append(record)
    payload: dict[str, Any] = {}
    _set_path(payload, plan["response_path"], records)
    if next_token is not None:
        _set_path(payload, pagination[5], next_token)
    return payload


def _build_plan(source: Path) -> dict[str, dict[str, Any]]:
    namespace = runpy.run_path(str(source))
    plan = {}
    for index, operation in enumerate(namespace["OPERATIONS"]):
        endpoint = namespace["_alicloud_endpoint"](operation[2], REGION)
        key = f"{endpoint}|{operation[3]}"
        assert key not in plan
        plan[key] = {
            "resource": operation[0],
            "service": operation[1],
            "version": operation[4],
            "response_path": operation[5],
            "id_path": operation[6][0],
            "pagination": operation[11],
            "token": f"AUDIT_TOKEN_{index}",
        }
    return plan


def _input() -> dict[str, Any]:
    return {
        "mode": "preview",
        "account": "fixture-account",
        "regions": [REGION],
        "max_pages": 100,
        "max_records": 1_000,
        "max_bytes": 1_048_576,
        "page_size": 1_000,
        "timeout_seconds": 30,
    }


def _expected_calls(plan: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    calls = []
    for key, item in plan.items():
        pagination = item["pagination"]
        kind = pagination[0]
        query = {"RegionId": REGION}
        if kind != "none":
            size = max(pagination[2], min(_input()["page_size"], pagination[3]))
            query[pagination[1]] = str(size)
            if kind in {"numbered", "current-page"}:
                query[pagination[4]] = "1"
            if pagination[6]:
                query[pagination[6]] = pagination[7]
        calls.append({"key": key, "version": item["version"], "query": query})
        if kind == "none":
            continue
        second = dict(query)
        if kind in {"numbered", "current-page"}:
            second[pagination[4]] = "2"
        else:
            second[pagination[4]] = item["token"]
        calls.append({"key": key, "version": item["version"], "query": second})
    return calls


def _install_python_sdk_stub(
    monkeypatch: pytest.MonkeyPatch,
    plan: dict[str, dict[str, Any]],
    calls: list[dict[str, Any]],
    repeat_key: str | None,
) -> None:
    class Values:
        def __init__(self, **values: Any) -> None:
            self.__dict__.update(values)

    class Client:
        def __init__(self, config: Values) -> None:
            self.config = config

        def call_api(self, params: Values, request: Values, _runtime: Values) -> dict[str, Any]:
            key = f"{self.config.endpoint}|{params.action}"
            query = dict(request.query)
            calls.append({"key": key, "version": params.version, "query": query})
            return {"body": _stub_payload(plan[key], query, key, repeat_key)}

    openapi_package = ModuleType("alibabacloud_tea_openapi")
    openapi_package.__path__ = []  # type: ignore[attr-defined]
    openapi_client = ModuleType("alibabacloud_tea_openapi.client")
    openapi_client.Client = Client  # type: ignore[attr-defined]
    openapi_models = ModuleType("alibabacloud_tea_openapi.utils_models")
    openapi_models.Config = Values  # type: ignore[attr-defined]
    openapi_models.OpenApiRequest = Values  # type: ignore[attr-defined]
    openapi_models.Params = Values  # type: ignore[attr-defined]
    tea_package = ModuleType("alibabacloud_tea_util")
    tea_package.__path__ = []  # type: ignore[attr-defined]
    tea_models = ModuleType("alibabacloud_tea_util.models")
    tea_models.RuntimeOptions = Values  # type: ignore[attr-defined]
    for name, module in {
        "alibabacloud_tea_openapi": openapi_package,
        "alibabacloud_tea_openapi.client": openapi_client,
        "alibabacloud_tea_openapi.utils_models": openapi_models,
        "alibabacloud_tea_util": tea_package,
        "alibabacloud_tea_util.models": tea_models,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


def _python_probe(
    source: Path,
    plan: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    repeat_key: str | None,
) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    _install_python_sdk_stub(monkeypatch, plan, calls, repeat_key)
    namespace = runpy.run_path(str(source))
    context = SimpleNamespace(config={}, secrets=SECRETS, input_files=[], logger=SimpleNamespace())
    result = namespace["handle"](context, _input())
    return {"calls": calls, "result": result}


def _javascript_probe(
    source: Path,
    plan: dict[str, dict[str, Any]],
    fixture_root: Path,
    repeat_key: str | None,
) -> dict[str, Any]:
    fixture_root.mkdir(parents=True)
    (fixture_root / "recipe.mjs").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    openapi = fixture_root / "node_modules/@alicloud/openapi-client"
    tea = fixture_root / "node_modules/@alicloud/tea-util"
    openapi.mkdir(parents=True)
    tea.mkdir(parents=True)
    for module in (openapi, tea):
        (module / "package.json").write_text(
            json.dumps({"type": "module", "exports": "./index.js"}), encoding="utf-8"
        )
    (openapi / "index.js").write_text(
        r"""
function setPath(target, dotted, value) {
  const parts = dotted.split("."); let current = target;
  for (const part of parts.slice(0, -1)) current = current[part] ??= {};
  current[parts.at(-1)] = value;
}
function payload(plan, query, key) {
  const pagination = plan.pagination, kind = pagination[0];
  let count = 0, nextToken = null;
  if (["numbered", "current-page"].includes(kind)) {
    count = query[pagination[4]] === "1" ? Number(query[pagination[1]]) : 0;
  } else if (kind === "none") {
    count = 2;
  } else {
    const currentToken = query[pagination[4]];
    count = currentToken === undefined ? 1 : 0;
    if (currentToken === undefined || key === globalThis.AUDIT_REPEAT_KEY) nextToken = plan.token;
  }
  const records = Array.from({ length: count }, () => {
    const record = {};
    setPath(record, plan.id_path, `AUDIT_${plan.resource}`);
    return record;
  });
  const result = {}; setPath(result, plan.response_path, records);
  if (nextToken !== null) setPath(result, pagination[5], nextToken);
  return result;
}
export class Config { constructor(value) { Object.assign(this, value); } }
export class OpenApiRequest { constructor(value) { Object.assign(this, value); } }
export class Params { constructor(value) { Object.assign(this, value); } }
export default class OpenApi {
  constructor(config) { this.config = config; }
  async callApi(params, request) {
    const key = `${this.config.endpoint}|${params.action}`;
    const query = { ...request.query };
    globalThis.AUDIT_CALLS.push({ key, version: params.version, query });
    return { body: payload(globalThis.AUDIT_PLANS[key], query, key) };
  }
}
""",
        encoding="utf-8",
    )
    (tea / "index.js").write_text(
        "export class RuntimeOptions { constructor(value) { Object.assign(this, value); } }\n",
        encoding="utf-8",
    )
    probe = r"""
globalThis.AUDIT_PLANS = JSON.parse(process.argv[1]);
globalThis.AUDIT_REPEAT_KEY = JSON.parse(process.argv[2]);
globalThis.AUDIT_CALLS = [];
const { handle } = await import("./recipe.mjs");
const result = await handle({
  config: {},
  secrets: new Map([
    ["ALICLOUD_ACCESS_KEY_ID", "AUDIT_ACCESS_KEY"],
    ["ALICLOUD_ACCESS_KEY_SECRET", "AUDIT_SIGNING_KEY"],
  ]),
  inputFiles: [], logger: {},
}, {
  mode: "preview", account: "fixture-account", regions: ["ap-example-1"],
  max_pages: 100, max_records: 1000, max_bytes: 1048576,
  page_size: 1000, timeout_seconds: 30,
});
process.stdout.write(JSON.stringify({ calls: globalThis.AUDIT_CALLS, result }));
"""
    completed = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            probe,
            json.dumps(plan),
            json.dumps(repeat_key),
        ],
        cwd=fixture_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


JAVA_STUBS = {
    "com/aliyun/teaopenapi/models/Config.java": r"""
package com.aliyun.teaopenapi.models;
public class Config {
  public String endpoint;
  public Config setAccessKeyId(String v){return this;}
  public Config setAccessKeySecret(String v){return this;}
  public Config setSecurityToken(String v){return this;}
  public Config setEndpoint(String v){endpoint=v;return this;}
  public Config setRegionId(String v){return this;}
  public Config setProtocol(String v){return this;}
  public Config setReadTimeout(int v){return this;}
  public Config setConnectTimeout(int v){return this;}
}
""",
    "com/aliyun/teaopenapi/models/OpenApiRequest.java": r"""
package com.aliyun.teaopenapi.models;
import java.util.Map;
public class OpenApiRequest {
  public Map<String,String> query;
  public OpenApiRequest setQuery(Map<String,String> v){query=v;return this;}
}
""",
    "com/aliyun/teaopenapi/models/Params.java": r"""
package com.aliyun.teaopenapi.models;
public class Params {
  public String action;
  public String version;
  public Params setAction(String v){action=v;return this;}
  public Params setVersion(String v){version=v;return this;}
  public Params setProtocol(String v){return this;}
  public Params setPathname(String v){return this;}
  public Params setMethod(String v){return this;}
  public Params setAuthType(String v){return this;}
  public Params setBodyType(String v){return this;}
  public Params setReqBodyType(String v){return this;}
  public Params setStyle(String v){return this;}
}
""",
    "com/aliyun/teautil/models/RuntimeOptions.java": r"""
package com.aliyun.teautil.models;
public class RuntimeOptions {
  public RuntimeOptions setReadTimeout(int v){return this;}
  public RuntimeOptions setConnectTimeout(int v){return this;}
  public RuntimeOptions setAutoretry(boolean v){return this;}
}
""",
    "com/aliyun/teaopenapi/Client.java": r"""
package com.aliyun.teaopenapi;
import com.aliyun.teaopenapi.models.*;
import com.aliyun.teautil.models.RuntimeOptions;
import java.util.*;
public class Client {
  public static Map<String,Map<String,Object>> plans = Map.of();
  public static List<Map<String,Object>> calls = new ArrayList<>();
  public static String repeatKey;
  private final Config config;
  public Client(Config value){config=value;}
  public Map<String,Object> callApi(Params params, OpenApiRequest request, RuntimeOptions runtime) {
    String key = config.endpoint + "|" + params.action;
    Map<String,String> query = new LinkedHashMap<>(request.query);
    Map<String,Object> call = new LinkedHashMap<>();
    call.put("key", key); call.put("version", params.version); call.put("query", query);
    calls.add(call);
    return Map.of("body", payload(plans.get(key), query, key));
  }
  private static Map<String,Object> payload(
      Map<String,Object> plan, Map<String,String> query, String key) {
    List<?> pagination = (List<?>) plan.get("pagination");
    String kind = String.valueOf(pagination.get(0));
    int count = 0; String nextToken = null;
    if (List.of("numbered", "current-page").contains(kind)) {
      count = "1".equals(query.get(String.valueOf(pagination.get(4))))
        ? Integer.parseInt(query.get(String.valueOf(pagination.get(1)))) : 0;
    } else if ("none".equals(kind)) {
      count = 2;
    } else {
      String currentToken = query.get(String.valueOf(pagination.get(4)));
      count = currentToken == null ? 1 : 0;
      if (currentToken == null || key.equals(repeatKey)) {
        nextToken = String.valueOf(plan.get("token"));
      }
    }
    List<Object> items = new ArrayList<>();
    for (int at = 0; at < count; at++) {
      Map<String,Object> record = new LinkedHashMap<>();
      setPath(record, String.valueOf(plan.get("id_path")),
        "AUDIT_" + String.valueOf(plan.get("resource")));
      items.add(record);
    }
    Map<String,Object> result = new LinkedHashMap<>();
    setPath(result, String.valueOf(plan.get("response_path")), items);
    if (nextToken != null) setPath(result, String.valueOf(pagination.get(5)), nextToken);
    return result;
  }
  @SuppressWarnings("unchecked")
  private static void setPath(Map<String,Object> target, String dotted, Object value) {
    String[] parts = dotted.split("\\."); Map<String,Object> current = target;
    for (int at = 0; at < parts.length - 1; at++) {
      Object nested = current.get(parts[at]);
      if (!(nested instanceof Map<?,?>)) {
        nested = new LinkedHashMap<String,Object>();
        current.put(parts[at], nested);
      }
      current = (Map<String,Object>) nested;
    }
    current.put(parts[parts.length - 1], value);
  }
}
""",
}


def _java_probe(
    source: Path,
    plan: dict[str, dict[str, Any]],
    fixture_root: Path,
    repeat_key: str | None,
) -> dict[str, Any]:
    fixture_root.mkdir(parents=True)
    (fixture_root / "DlrRuntime.java").write_text(JAVA_RUNTIME_SOURCE, encoding="utf-8")
    (fixture_root / "Adapter.java").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    for name, content in JAVA_STUBS.items():
        path = fixture_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (fixture_root / "Probe.java").write_text(
        r"""
import com.aliyun.teaopenapi.Client;
import java.util.*;
public final class Probe {
  @SuppressWarnings("unchecked")
  public static void main(String[] args) throws Exception {
    Client.plans = (Map<String,Map<String,Object>>) (Map<?,?>) Json.parse(args[0]);
    Client.repeatKey = args[1].isEmpty() ? null : args[1]; Client.calls = new ArrayList<>();
    Map<String,Object> input = new LinkedHashMap<>();
    input.put("mode", "preview"); input.put("account", "fixture-account");
    input.put("regions", List.of("ap-example-1")); input.put("max_pages", 100);
    input.put("max_records", 1000); input.put("max_bytes", 1048576);
    input.put("page_size", 1000); input.put("timeout_seconds", 30);
    Object result = new Adapter().handle(new Context(Map.of()), input);
    Map<String,Object> output = new LinkedHashMap<>();
    output.put("calls", Client.calls); output.put("result", result);
    System.out.print(Json.stringify(output));
  }
}
""",
        encoding="utf-8",
    )
    sources = ["DlrRuntime.java", "Adapter.java", "Probe.java", *JAVA_STUBS]
    subprocess.run(
        ["javac", "-encoding", "UTF-8", "-d", str(fixture_root), *sources],
        cwd=fixture_root,
        check=True,
        capture_output=True,
        text=True,
    )
    environment = os.environ.copy()
    environment.update({f"DLR_SECRET_{key}": value for key, value in SECRETS.items()})
    completed = subprocess.run(
        [
            "java",
            "-cp",
            str(fixture_root),
            "Probe",
            json.dumps(plan),
            repeat_key or "",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return json.loads(completed.stdout)


def _all_language_probes(
    slug: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repeat_key: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    sources = _scenario_sources(slug)
    plan = _build_plan(sources["python"])
    outputs = {
        "python": _python_probe(sources["python"], plan, monkeypatch, repeat_key),
        "javascript": _javascript_probe(
            sources["javascript"], plan, tmp_path / "javascript", repeat_key
        ),
        "java": _java_probe(sources["java"], plan, tmp_path / "java", repeat_key),
    }
    return plan, outputs


@pytest.mark.parametrize("slug", ALICLOUD_SCENARIOS)
def test_alicloud_query_shapes_and_termination_match_all_languages(
    slug: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, outputs = _all_language_probes(slug, tmp_path, monkeypatch)
    expected = _expected_calls(plan)
    for output in outputs.values():
        assert output["calls"] == expected
        assert output["result"]["partial"] is False
        assert output["result"]["summary"]["pages"] == len(expected)
        assert output["result"]["summary"]["failures"] == []


@pytest.mark.parametrize(
    ("slug", "resource"),
    (
        ("alicloud-compute-container-topology", "ecs_instance"),
        ("alicloud-database-middleware-inventory", "ram_user"),
        ("alicloud-database-middleware-inventory", "security_center_asset"),
    ),
)
def test_alicloud_repeated_token_stops_in_all_languages(
    slug: str, resource: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = _scenario_sources(slug)
    plan = _build_plan(sources["python"])
    repeat_key = next(key for key, item in plan.items() if item["resource"] == resource)
    _, outputs = _all_language_probes(slug, tmp_path, monkeypatch, repeat_key)
    expected = _expected_calls(plan)
    for output in outputs.values():
        assert output["calls"] == expected
        assert output["result"]["partial"] is True
        assert output["result"]["summary"]["pages"] == len(expected)
        assert output["result"]["summary"]["failures"] == [
            {"region": REGION, "resource": resource, "error": "source_read_failed"}
        ]


def test_alicloud_provenance_pagination_matches_executable_plans() -> None:
    provenance = json.loads((CATALOG_ROOT / "provenance.json").read_text(encoding="utf-8"))
    alicloud_rows = [
        row for row in provenance["coverage"] if row["scenario_slug"] in ALICLOUD_SCENARIOS
    ]
    for row in alicloud_rows:
        if row["support_status"] == "gap" and row["api_operations"]:
            assert row["pagination"] == "not implemented; pagination not claimed"

    for slug in ALICLOUD_SCENARIOS:
        source = _scenario_sources(slug)["python"]
        operations = runpy.run_path(str(source))["OPERATIONS"]
        rows = {
            row["external_key"].split(":")[3]: row
            for row in alicloud_rows
            if row["scenario_slug"] == slug and row["support_status"] == "supported"
        }
        assert set(rows) == {operation[0] for operation in operations}
        for operation in operations:
            pagination = operation[11]
            claim = rows[operation[0]]["pagination"]
            kind, size_name, minimum, maximum, cursor_name, response_cursor, flag, value = (
                pagination
            )
            if kind == "none":
                assert claim.startswith("No pagination parameters")
                continue
            assert size_name in claim
            if minimum == maximum:
                assert f"fixed {size_name} {minimum}" in claim
            else:
                assert f"({minimum}..{maximum})" in claim
            assert cursor_name in claim
            if kind in {"next-token", "marker"}:
                assert response_cursor in claim
                assert "reject repeated" in claim
            else:
                assert "short page" in claim
            if flag:
                assert f"{flag}={value}" in claim
