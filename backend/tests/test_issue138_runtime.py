"""TypeScript / Go compiler, Context, offline-module and asset contracts."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dlr.common.builtin_packages import PackageValidationError, inspect_package
from dlr.control.schemas.portable import PortablePackage
from dlr.control.services.portable_zip import decode_package, encode_package
from dlr.runtime.node_harness import SOURCE as NODE_HARNESS
from dlr.runtime.typescript_runtime import DECLARATIONS
from dlr.worker import goenv, nodeenv, venv
from dlr.worker.builtin_packages import BuiltinMaterials


@pytest.fixture(params=["typescript", "go"])
def language(request: pytest.FixtureRequest) -> str:
    language = str(request.param)
    if not shutil.which("tsc" if language == "typescript" else "go"):
        pytest.skip(f"{language} toolchain is not installed")
    return language


def build(root: Path, language: str, code: str, *, version: int = 1) -> Path:
    if language == "typescript":
        return nodeenv.prepare_version_node(
            root,
            1,
            version,
            code,
            "",
            timeout_seconds=120,
            registry_url=None,
            language="typescript",
        )
    return goenv.prepare_version_go(
        root,
        1,
        version,
        code,
        "",
        timeout_seconds=180,
        proxy_url=None,
    )


def workspace(root: Path) -> Path:
    directory = root / "dlr-exec-101"
    directory.mkdir()
    (directory / "input").mkdir()
    contents = b"hello from file"
    (directory / "input/input-00.txt").write_bytes(contents)
    (directory / "input.json").write_text('{"n":7}', encoding="utf-8")
    (directory / "runtime_config.json").write_text('{"stage":"issue138"}', encoding="utf-8")
    (directory / "input_manifest.json").write_text(
        json.dumps(
            {
                "execution_id": 101,
                "files": [
                    {
                        "artifact_id": 91,
                        "ordinal": 0,
                        "mount_name": "input-00.txt",
                        "original_filename": "fixture.txt",
                        "content_type": "text/plain",
                        "size_bytes": len(contents),
                        "sha256": hashlib.sha256(contents).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return directory


def execute(directory: Path, target: Path, language: str) -> subprocess.CompletedProcess[str]:
    if language == "typescript":
        (target / "harness.mjs").write_text(NODE_HARNESS, encoding="utf-8")
        command = [
            "node",
            "--enable-source-maps",
            str(target / "harness.mjs"),
            str(target),
            str(directory / "adapter.mjs"),
        ]
    else:
        command = [str(directory / "adapter"), str(target)]
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
        env={**os.environ, "DLR_SECRET_TOKEN": "fixture-token"},
    )


CODES = {
    "typescript": """import fs from "node:fs";
import type { Context } from "dlr";
export async function handle(context: Context, input: {n: number}) {
  context.logger.info("started");
  return {n: input.n, stage: context.config.stage, token: context.secrets.get("TOKEN"),
          file: fs.readFileSync(context.inputFiles[0].path, "utf8")};
}
""",
    "go": """package main
import "os"
func Handle(ctx *Context, input any) (any, error) {
    var data struct { N int `json:"n"` }
    if err := DecodeInput(input, &data); err != nil { return nil, err }
    ctx.Logger.Info("started")
    file, err := os.ReadFile(ctx.InputFiles[0].Path)
    if err != nil { return nil, err }
    return map[string]any{"n":data.N, "stage":ctx.Config["stage"],
        "token":ctx.Secrets.Get("TOKEN"), "file":string(file)}, nil
}
""",
}


def test_real_compilation_context_and_cache(tmp_path: Path, language: str) -> None:
    directory = build(tmp_path, language, CODES[language])
    artifact = directory / ("adapter.mjs" if language == "typescript" else "adapter")
    previous = artifact.stat().st_mtime_ns
    assert build(tmp_path, language, CODES[language]) == directory
    assert artifact.stat().st_mtime_ns == previous
    target = workspace(tmp_path)
    completed = execute(directory, target, language)
    assert completed.returncode == 0, completed.stderr
    assert "[INFO] started" in completed.stdout
    assert json.loads((target / "output.json").read_text()) == {
        "n": 7,
        "stage": "issue138",
        "token": "fixture-token",
        "file": "hello from file",
    }
    (target / "input/input-00.txt").unlink()
    rejected = execute(directory, target, language)
    assert rejected.returncode != 0
    assert "DLR_INPUT_ERROR:input_artifact_not_ready" in rejected.stderr


def test_compiler_failure_never_publishes_cache(tmp_path: Path, language: str) -> None:
    code = (
        "export function handle(context: Context, input: unknown) { "
        'const n: number = "wrong"; return n; }'
        if language == "typescript"
        else (
            "package main\n"
            "func Handle(ctx *Context, input any) (any, error) { "
            'var n int = "wrong"; return n, nil }'
        )
    )
    with pytest.raises(venv.DependencyPreparationError) as caught:
        build(tmp_path, language, code)
    assert "adapter." in caught.value.install_log
    assert not (venv.version_dir(tmp_path, 1, 1) / ".ready").exists()


def test_runtime_error_has_source_diagnostic(tmp_path: Path, language: str) -> None:
    code = (
        (
            "export function handle(context: Context, input: unknown) {\n"
            '  throw new Error("fixture-error");\n'
            "}"
        )
        if language == "typescript"
        else (
            "package main\n"
            'func Handle(ctx *Context, input any) (any, error) { panic("fixture-error") }'
        )
    )
    directory = build(tmp_path, language, code)
    completed = execute(directory, workspace(tmp_path), language)
    assert completed.returncode != 0
    assert "fixture-error" in completed.stderr
    assert ("adapter.mts:2" if language == "typescript" else "adapter.go:2") in completed.stderr


def test_editor_and_compiler_declarations_match() -> None:
    path = Path(__file__).parents[2] / "web/src/runtime-types.ts"
    if not path.exists():
        pytest.skip("web source is not included in this distribution")
    serialized = (
        path.read_text().split("export const DLR_TYPES = ", 1)[1].rstrip().removesuffix(";")
    )
    assert json.loads(serialized) == DECLARATIONS


@pytest.mark.parametrize("language", ["typescript", "go"])
def test_new_language_api_and_portable_contract(api_client: TestClient, language: str) -> None:
    response = api_client.post(
        "/api/adapters",
        json={
            "name": f"issue138-{language}",
            "language": language,
            "adapter_type": "task",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["language"] == language
    value = PortablePackage.model_validate(
        {
            "object_type": "adapter",
            "name": "Example",
            "adapter_type": "task",
            "variants": [{"language": language, "code": CODES[language], "requirements": ""}],
        }
    )
    assert decode_package(encode_package(value)) == value


def go_materials(root: Path) -> BuiltinMaterials:
    module, version = "example.com/demo", "v1.0.0"
    mod = f"module {module}\n\ngo 1.23.0\n".encode()
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr(f"{module}@{version}/go.mod", mod)
        archive.writestr(
            f"{module}@{version}/demo.go", "package demo\nfunc Value() int { return 138 }\n"
        )
    contents = {
        f"{module}/@v/{version}.mod": mod,
        f"{module}/@v/{version}.info": json.dumps({"Version": version}).encode(),
        f"{module}/@v/{version}.zip": archive_bytes.getvalue(),
    }
    files = []
    for index, (path, body) in enumerate(contents.items()):
        artifact = root / f"material-{index}"
        artifact.write_bytes(body)
        result = inspect_package(artifact, "goproxy", path.rsplit("/", 1)[1], path)
        assert result["name"] == module
        files.append(
            {
                "id": index + 1,
                "repository_path": path,
                "size_bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    return BuiltinMaterials(
        {"kind": "goproxy", "files": files},
        root / "materials",
        lambda descriptor, writer: writer.write(contents[descriptor["repository_path"]]),
    )


def test_go_builtin_modules_and_missing_transitive_failure(tmp_path: Path) -> None:
    if not shutil.which("go"):
        pytest.skip("Go toolchain is not installed")
    materials = go_materials(tmp_path)
    code = (
        "package main\n"
        'import "example.com/demo"\n'
        "func Handle(ctx *Context, input any) (any, error) { return demo.Value(), nil }"
    )
    result = goenv.prepare_version_go(
        tmp_path / "runtime",
        1,
        1,
        code,
        "example.com/demo@v1.0.0",
        timeout_seconds=180,
        proxy_url=None,
        builtin_materials=materials,
    )
    target = workspace(tmp_path)
    assert execute(result, target, "go").returncode == 0
    assert json.loads((target / "output.json").read_text()) == 138
    missing = (
        "package main\n"
        'import "example.com/missing"\n'
        "func Handle(ctx *Context, input any) (any, error) { return missing.Value(), nil }"
    )
    with pytest.raises(venv.DependencyPreparationError) as caught:
        goenv.prepare_version_go(
            tmp_path / "runtime",
            1,
            2,
            missing,
            "example.com/missing@v1.0.0",
            timeout_seconds=30,
            proxy_url=None,
            builtin_materials=materials,
        )
    assert caught.value.error_code.startswith("builtin_")
    assert not (venv.version_dir(tmp_path / "runtime", 1, 2) / ".ready").exists()


@pytest.mark.parametrize(
    "spec",
    [
        "https://example.com/sdk@v1.0.0",
        "../sdk@v1.0.0",
        "example.com/sdk@latest",
        "example.com/sdk@v1.0.0\nexample.com/sdk@v2.0.0",
    ],
)
def test_go_module_declarations_reject_external_or_ambiguous_references(spec: str) -> None:
    with pytest.raises(venv.DependencyPreparationError):
        goenv.parse_requirements(spec)


def test_go_material_path_and_module_identity_validation(tmp_path: Path) -> None:
    path = tmp_path / "metadata"
    path.write_text("module example.com/other\n")
    with pytest.raises(PackageValidationError, match="disagrees"):
        inspect_package(path, "goproxy", "v1.0.0.mod", "example.com/demo/@v/v1.0.0.mod")
    with pytest.raises(PackageValidationError):
        inspect_package(path, "goproxy", "v1.0.0.mod", "../example.com/demo/@v/v1.0.0.mod")


@pytest.mark.parametrize("language,kind", [("typescript", "npm"), ("go", "goproxy")])
def test_new_language_library_snapshot_and_authorization(
    api_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    kind: str,
) -> None:
    from conftest import WORKER_TOKEN
    from dlr.common.config import settings
    from runtime_api_support import mark_broker_ready, ready_registration
    from test_adapters import create_adapter, save_version
    from test_builtin_packages import npm_package, select_builtin, upload
    from test_executions import create_execution
    from test_workers import claim

    monkeypatch.setattr(settings, "builtin_package_root", str(tmp_path / "library"))
    monkeypatch.setattr(settings, "builtin_package_min_free_bytes", 0)
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}"}
    registration = ready_registration("issue138-worker", [language])
    registration["isolation_capabilities"]["builtin_packages_v1"] = True
    worker = api_client.post("/api/workers/register", json=registration, headers=headers).json()
    mark_broker_ready()
    if kind == "npm":
        item = upload(api_client, *npm_package(), kind="npm")
    else:
        item = upload(
            api_client,
            "v1.0.0.mod",
            b"module example.com/demo\n\ngo 1.23.0\n",
            kind="goproxy",
            repository_path="example.com/demo/@v/v1.0.0.mod",
        )
    adapter = create_adapter(api_client, language=language)
    save_version(api_client, adapter["id"], CODES[language])
    select_builtin(api_client, kind)
    execution = create_execution(api_client, adapter["id"])
    assert api_client.delete(f"/api/builtin-packages/{item['id']}").status_code == 409
    payload = claim(api_client, worker["id"]).json()
    assert payload["language"] == language
    assert payload["builtin_package_snapshot"]["kind"] == kind
    assert [file["id"] for file in payload["builtin_package_snapshot"]["files"]] == [item["id"]]
    url = (
        f"/api/workers/{worker['id']}/executions/{execution['id']}"
        f"/builtin-packages/{item['id']}/content"
    )
    assert api_client.get(url).status_code in (401, 403)
    assert api_client.get(url, headers=headers).status_code == 422
    assert (
        api_client.get(url, headers={**headers, "X-DLR-Claim-Token": "wrong-token"}).status_code
        == 422
    )
    assert (
        api_client.get(
            url, headers={**headers, "X-DLR-Claim-Token": payload["claim_token"]}
        ).status_code
        == 200
    )


def test_typescript_uses_builtin_npm_without_registry(tmp_path: Path) -> None:
    if not shutil.which("tsc"):
        pytest.skip("TypeScript toolchain is not installed")
    from test_builtin_packages import npm_package

    filename, content = npm_package()
    archive = tmp_path / filename
    archive.write_bytes(content)
    metadata = inspect_package(archive, "npm", filename)
    materials = BuiltinMaterials(
        {
            "kind": "npm",
            "files": [
                {
                    "id": 1,
                    "repository_path": filename,
                    "filename": filename,
                    "metadata": metadata,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            ],
        },
        tmp_path / "materials",
        lambda _descriptor, writer: writer.write(content),
    )
    code = (
        'import { createRequire } from "node:module";\n'
        'const value: number = createRequire(import.meta.url)("offline-demo");\n'
        "export function handle(ctx: Context, input: unknown) { return {value}; }"
    )
    built = nodeenv.prepare_version_node(
        tmp_path / "runtime",
        1,
        1,
        code,
        "offline-demo@1.0.0",
        timeout_seconds=60,
        registry_url=None,
        language="typescript",
        builtin_materials=materials,
    )
    target = workspace(tmp_path)
    assert execute(built, target, "typescript").returncode == 0
    assert json.loads((target / "output.json").read_text()) == {"value": 141}
