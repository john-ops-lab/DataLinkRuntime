"""Issue #141: real PostgreSQL library lifecycle, admission and verified offline installs."""

import hashlib
import io
import json
import subprocess
import tarfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.common.builtin_packages import PackageValidationError, inspect_package
from dlr.common.config import settings
from dlr.control import db
from dlr.control.models.builtin_package import BuiltinPackage
from dlr.control.models.execution import Execution, Worker
from dlr.control.services import builtin_package as library
from dlr.worker import nodeenv, venv
from dlr.worker.builtin_packages import BuiltinMaterials
from test_adapters import create_adapter, save_version
from test_executions import create_execution
from test_workers import claim, report
from test_workers import register_worker as legacy_register_worker


def register_worker(client: TestClient) -> dict:
    worker = legacy_register_worker(client)
    with db.SessionLocal.begin() as session:
        row = session.get(Worker, worker["id"])
        row.isolation_capabilities = {**row.isolation_capabilities, "builtin_packages_v1": True}
    return worker


def wheel(
    name: str = "offline_demo",
    version: str = "1.0",
    *,
    dependency: str = "",
    tag: str = "py3-none-any",
) -> tuple[str, bytes]:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        prefix = f"{name}-{version}.dist-info"
        archive.writestr(f"{name}.py", "VALUE = 141\n")
        archive.writestr(
            f"{prefix}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            + (f"Requires-Dist: {dependency}\n" if dependency else ""),
        )
        archive.writestr(
            f"{prefix}/WHEEL",
            f"Wheel-Version: 1.0\nGenerator: dlr-test\nRoot-Is-Purelib: true\nTag: {tag}\n",
        )
        archive.writestr(f"{prefix}/RECORD", "")
    return f"{name}-{version}-{tag}.whl", target.getvalue()


def npm_package(
    name: str = "offline-demo",
    *,
    dependencies: dict[str, str] | None = None,
    version: str = "1.0.0",
    os_values: list[str] | None = None,
) -> tuple[str, bytes]:
    target = io.BytesIO()
    package = {
        "name": name,
        "version": version,
        "main": "index.js",
        "dependencies": dependencies or {},
    }
    if os_values:
        package["os"] = os_values
    with tarfile.open(fileobj=target, mode="w:gz") as archive:
        for filename, content in (
            ("package/package.json", json.dumps(package).encode()),
            ("package/index.js", b"module.exports = 141;"),
        ):
            member = tarfile.TarInfo(filename)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return f"{name.replace('/', '-')}-{version}.tgz", target.getvalue()


@pytest.fixture(autouse=True)
def isolated_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "builtin_package_root", str(tmp_path / "library"))
    monkeypatch.setattr(settings, "builtin_package_min_free_bytes", 0)


def upload(
    client: TestClient, filename: str, body: bytes, kind: str = "pypi", repository_path: str = ""
) -> dict[str, Any]:
    reserved = client.post(
        "/api/builtin-packages/uploads",
        json={
            "filename": filename,
            "kind": kind,
            "size_bytes": len(body),
            "repository_path": repository_path,
        },
    )
    assert reserved.status_code == 201, reserved.text
    result = client.put(f"/api/builtin-packages/uploads/{reserved.json()['id']}", content=body)
    assert result.status_code == 200, result.text
    return result.json()["file"]


def select_builtin(client: TestClient, kind: str = "pypi") -> None:
    response = client.post(
        "/api/package-sources",
        json={
            "name": f"builtin-{kind}",
            "kind": kind,
            "index_url": f"dlr-builtin://{kind}",
            "is_default": True,
        },
    )
    assert response.status_code == 201, response.text


def test_upload_filter_download_delete_and_duplicate(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    filename, body = wheel()
    item = upload(api_client, filename, body)
    assert item["sha256"] == hashlib.sha256(body).hexdigest()
    assert item["status"] == "uploaded"
    assert upload(api_client, filename, body)["id"] == item["id"]
    state = api_client.get("/api/builtin-packages", params={"q": "offline", "kind": "pypi"}).json()
    assert len(state["files"]) == 1
    assert state["capacity"] == {
        "used_bytes": len(body),
        "reserved_bytes": 0,
        "quota_bytes": 1073741824,
    }
    assert api_client.get(f"/api/builtin-packages/{item['id']}/content").content == body
    with session_factory() as session:
        row = session.get(BuiltinPackage, item["id"])
        path = library.storage_path(row.storage_key)
    assert path.is_file()
    assert api_client.delete(f"/api/builtin-packages/{item['id']}").status_code == 204
    assert not path.exists()
    assert api_client.get(f"/api/builtin-packages/{item['id']}/content").status_code == 404


def test_conflicting_content_cannot_overwrite_and_failed_upload_stays_reserved(
    api_client: TestClient,
) -> None:
    filename, body = wheel()
    item = upload(api_client, filename, body)
    _, changed = wheel(dependency="missing==1")
    reserved = api_client.post(
        "/api/builtin-packages/uploads",
        json={"filename": filename, "kind": "pypi", "size_bytes": len(changed)},
    ).json()
    result = api_client.put(f"/api/builtin-packages/uploads/{reserved['id']}", content=changed)
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "builtin_package_conflict"
    assert api_client.get(f"/api/builtin-packages/{item['id']}/content").content == body
    assert api_client.get("/api/builtin-packages").json()["capacity"]["reserved_bytes"] == len(
        changed
    )
    assert api_client.delete(f"/api/builtin-packages/uploads/{reserved['id']}").status_code == 204
    assert api_client.get("/api/builtin-packages").json()["capacity"]["reserved_bytes"] == 0


def test_quota_counts_all_kinds_and_concurrent_reservations(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    assert (
        api_client.patch("/api/builtin-packages/capacity", json={"quota_bytes": 100}).status_code
        == 200
    )

    def reserve(kind: str) -> str:
        with session_factory() as session:
            try:
                return library.reserve_upload(
                    session, kind=kind, filename="material.tgz", size_bytes=60
                ).id
            except HTTPException as error:
                assert error.detail["code"] == "builtin_capacity_exceeded"
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, ("pypi", "npm")))
    assert outcomes.count("rejected") == 1
    assert api_client.get("/api/builtin-packages").json()["capacity"]["reserved_bytes"] == 60
    assert (
        api_client.patch("/api/builtin-packages/capacity", json={"quota_bytes": 59}).status_code
        == 409
    )


def test_disk_margin_and_upload_size_enforced(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "builtin_package_min_free_bytes", 2**62)
    payload = {"filename": "demo.whl", "kind": "pypi", "size_bytes": 10}
    assert api_client.post("/api/builtin-packages/uploads", json=payload).status_code == 409
    monkeypatch.setattr(settings, "builtin_package_min_free_bytes", 0)
    reserved = api_client.post("/api/builtin-packages/uploads", json=payload).json()
    assert (
        api_client.put(
            f"/api/builtin-packages/uploads/{reserved['id']}", content=b"x" * 11
        ).status_code
        == 413
    )


def test_active_upload_and_download_block_removal(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    filename, body = wheel()
    reservation = api_client.post(
        "/api/builtin-packages/uploads",
        json={"filename": filename, "kind": "pypi", "size_bytes": len(body)},
    ).json()
    with session_factory() as session:
        _, guard, stream = library.begin_upload(session, reservation["id"])
        try:
            assert (
                api_client.delete(f"/api/builtin-packages/uploads/{reservation['id']}").status_code
                == 409
            )
        finally:
            stream.close()
            guard.close()
    item = upload(api_client, filename, body)
    with session_factory() as session:
        _, guard, stream = library.open_download(session, item["id"])
        try:
            assert api_client.delete(f"/api/builtin-packages/{item['id']}").status_code == 409
        finally:
            stream.close()
            guard.close()


def test_real_delete_failure_is_visible(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = upload(api_client, *wheel())
    original = Path.unlink

    def fail_blob(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.suffix == ".blob":
            raise PermissionError("test deletion failure")
        original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_blob)
    result = api_client.delete(f"/api/builtin-packages/{item['id']}")
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "builtin_delete_failed"
    assert api_client.get("/api/builtin-packages").json()["files"][0]["status"] == "deleting"
    monkeypatch.setattr(Path, "unlink", original)
    assert api_client.delete(f"/api/builtin-packages/{item['id']}").status_code == 204


def test_admission_freezes_files_and_blocks_delete_before_claim(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    first = upload(api_client, *wheel())
    worker = register_worker(api_client)
    adapter = create_adapter(api_client)
    save_version(api_client, adapter["id"])
    select_builtin(api_client)
    execution = create_execution(api_client, adapter["id"])
    assert api_client.delete(f"/api/builtin-packages/{first['id']}").status_code == 409
    second = upload(api_client, *wheel("another_demo"))
    payload = claim(api_client, worker["id"]).json()
    assert [item["id"] for item in payload["builtin_package_snapshot"]["files"]] == [first["id"]]
    base = f"/api/workers/{worker['id']}/executions/{execution['id']}/builtin-packages"
    headers = {
        "Authorization": f"Bearer {WORKER_TOKEN}",
        "X-DLR-Claim-Token": payload["claim_token"],
    }
    assert api_client.get(f"{base}/{first['id']}/content", headers=headers).status_code == 200
    assert api_client.get(f"{base}/{second['id']}/content", headers=headers).status_code == 403
    assert api_client.get(f"{base}/{first['id']}/content").status_code in (401, 403)
    assert (
        report(api_client, worker["id"], execution["id"], {"status": "succeeded"}).status_code
        == 200
    )
    with session_factory.begin() as session:
        row = session.get(Execution, execution["id"])
        row.workspace_cleanup_status = "completed"
    assert api_client.delete(f"/api/builtin-packages/{first['id']}").status_code == 204


def test_dependency_check_targets_worker_without_changing_adapter(api_client: TestClient) -> None:
    worker = register_worker(api_client)
    adapter = create_adapter(api_client)
    save_version(api_client, adapter["id"])
    original_worker_id = api_client.get(f"/api/adapters/{adapter['id']}").json()[
        "runtime_worker_id"
    ]
    result = api_client.post(
        "/api/builtin-packages/checks",
        json={"adapter_id": adapter["id"], "worker_id": worker["id"]},
    )
    assert result.status_code == 202, result.text
    assert result.json()["dependency_check"] is True
    recent = api_client.get("/api/builtin-packages/checks/recent").json()[0]
    assert recent["version_id"] == result.json()["version_id"]
    assert not ({"stdout", "stderr", "input", "output", "builtin_package_snapshot"} & recent.keys())
    payload = claim(api_client, worker["id"]).json()
    assert payload["dependency_check"] is True
    assert payload["secrets"] == {}
    assert payload["builtin_package_snapshot"]["kind"] == "pypi"
    assert (
        api_client.get(f"/api/adapters/{adapter['id']}").json()["runtime_worker_id"]
        == original_worker_id
    )


def test_worker_token_cannot_manage_library(api_client: TestClient) -> None:
    headers = {"Authorization": f"Bearer {WORKER_TOKEN}"}
    assert api_client.get("/api/builtin-packages", headers=headers).status_code in (401, 403)
    assert api_client.patch(
        "/api/builtin-packages/capacity", json={"quota_bytes": 1}, headers=headers
    ).status_code in (401, 403)


@pytest.mark.parametrize("name", ["../escape.whl", "/absolute.whl", "a\\escape.whl"])
def test_unsafe_upload_names_rejected(api_client: TestClient, name: str) -> None:
    assert (
        api_client.post(
            "/api/builtin-packages/uploads",
            json={"filename": name, "kind": "pypi", "size_bytes": 10},
        ).status_code
        == 422
    )


def test_material_types_and_embedded_urls_are_validated(tmp_path: Path) -> None:
    for filename, content, kind in ((*wheel(), "pypi"), (*npm_package(), "npm")):
        path = tmp_path / filename
        path.write_bytes(content)
        assert inspect_package(path, kind, filename)["name"].startswith("offline")
    for filename, content, kind in (
        (*wheel(dependency="bad @ https://example.invalid/a.whl"), "pypi"),
        (*npm_package(dependencies={"bad": "https://example.invalid/a.tgz"}), "npm"),
    ):
        path = tmp_path / filename
        path.write_bytes(content)
        with pytest.raises(PackageValidationError):
            inspect_package(path, kind, filename)


def materials(tmp_path: Path, files: list[tuple[str, bytes]], kind: str) -> BuiltinMaterials:
    contents = {}
    descriptors = []
    for index, (filename, content) in enumerate(files, 1):
        source = tmp_path / f"upload-{index}"
        source.parent.mkdir(exist_ok=True, parents=True)
        source.write_bytes(content)
        metadata = inspect_package(source, kind, filename)
        descriptors.append(
            {
                "id": index,
                "filename": filename,
                "repository_path": metadata["repository_path"],
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "metadata": metadata["metadata"],
            }
        )
        contents[index] = content

    def download(item: Any, output: Any) -> int:
        return output.write(contents[item["id"]])

    return BuiltinMaterials({"kind": kind, "files": descriptors}, tmp_path / "materials", download)


def test_python_real_offline_install_reuse_and_source_identity(tmp_path: Path) -> None:
    bundle = materials(tmp_path / "uploads", [wheel()], "pypi")
    python = venv.prepare_version_venv(
        tmp_path / "runtime",
        1,
        1,
        "offline-demo==1.0",
        timeout_seconds=60,
        index_url="https://example.invalid",
        builtin_materials=bundle,
    )
    assert (
        subprocess.check_output(
            [str(python), "-c", "import offline_demo; print(offline_demo.VALUE)"], text=True
        ).strip()
        == "141"
    )
    bundle.downloader = lambda *_: pytest.fail("warm verified environment must not download again")
    assert (
        venv.prepare_version_venv(
            tmp_path / "runtime",
            1,
            1,
            "offline-demo==1.0",
            timeout_seconds=60,
            builtin_materials=bundle,
        )
        == python
    )
    missing = BuiltinMaterials(
        {"kind": "pypi", "files": []}, tmp_path / "missing", bundle.downloader
    )
    with pytest.raises(venv.DependencyPreparationError) as failure:
        venv.prepare_version_venv(
            tmp_path / "runtime",
            1,
            1,
            "offline-demo==1.0",
            timeout_seconds=60,
            builtin_materials=missing,
        )
    assert failure.value.error_code == "builtin_dependency_missing"


def test_npm_real_offline_transitive_install_and_missing(tmp_path: Path) -> None:
    bundle = materials(
        tmp_path / "uploads",
        [npm_package(dependencies={"offline-child": "^1.0.0"}), npm_package("offline-child")],
        "npm",
    )
    directory = nodeenv.prepare_version_node(
        tmp_path / "runtime",
        1,
        1,
        "export const x = 1;",
        "offline-demo@1.0.0",
        timeout_seconds=60,
        registry_url="https://example.invalid",
        builtin_materials=bundle,
    )
    assert (directory / "node_modules/offline-child/package.json").is_file()
    bundle.downloader = lambda *_: pytest.fail("warm environment should be reused")
    assert (
        nodeenv.prepare_version_node(
            tmp_path / "runtime",
            1,
            1,
            "export const x = 1;",
            "offline-demo@1.0.0",
            timeout_seconds=60,
            registry_url=None,
            builtin_materials=bundle,
        )
        == directory
    )
    missing = materials(
        tmp_path / "incomplete", [npm_package(dependencies={"offline-child": "^1.0.0"})], "npm"
    )
    with pytest.raises(venv.DependencyPreparationError) as failure:
        nodeenv.prepare_version_node(
            tmp_path / "runtime",
            2,
            2,
            "",
            "offline-demo@1.0.0",
            timeout_seconds=30,
            registry_url=None,
            builtin_materials=missing,
        )
    assert failure.value.error_code == "builtin_dependency_missing"


@pytest.mark.parametrize(
    "language,requirements",
    [
        ("python", "foo @ https://example.invalid/x.whl"),
        ("python", "--extra-index-url https://example.invalid"),
        ("javascript", "foo@https://example.invalid/x.tgz"),
    ],
)
def test_builtin_rejects_direct_urls_before_any_installer(
    tmp_path: Path, language: str, requirements: str
) -> None:
    bundle = BuiltinMaterials(
        {"files": []}, tmp_path / "materials", lambda *_: pytest.fail("should not download")
    )
    with pytest.raises(venv.DependencyPreparationError) as failure:
        if language == "python":
            venv.prepare_version_venv(
                tmp_path, 1, 1, requirements, timeout_seconds=10, builtin_materials=bundle
            )
        else:
            nodeenv.prepare_version_node(
                tmp_path,
                1,
                1,
                "",
                requirements,
                timeout_seconds=10,
                registry_url=None,
                builtin_materials=bundle,
            )
    assert failure.value.error_code == "builtin_external_reference"


def test_download_checksum_and_size_verified(tmp_path: Path) -> None:
    bundle = materials(tmp_path, [wheel()], "pypi")
    bundle.downloader = lambda item, output: output.write(b"tampered")
    with pytest.raises(venv.DependencyPreparationError) as failure:
        bundle.materialize()
    assert failure.value.error_code == "builtin_checksum_mismatch"


def test_native_install_reports_environment_mismatch(tmp_path: Path) -> None:
    bundle = materials(tmp_path / "uploads", [npm_package(os_values=["impossible-dlr-os"])], "npm")
    with pytest.raises(venv.DependencyPreparationError) as failure:
        nodeenv.prepare_version_node(
            tmp_path / "runtime",
            1,
            1,
            "module.exports = {};",
            "offline-demo@1.0.0",
            timeout_seconds=30,
            registry_url=None,
            builtin_materials=bundle,
        )
    assert failure.value.error_code == "builtin_environment_mismatch"


def test_check_never_runs_business_code(tmp_path: Path) -> None:
    from dlr.worker import executor
    from worker_runtime_support import run_with_test_sandbox

    bundle = materials(tmp_path / "uploads", [wheel()], "pypi")
    result = run_with_test_sandbox(
        {
            "execution_id": 141,
            "adapter_id": 1,
            "version_id": 1,
            "code": "raise RuntimeError('BUSINESS_CODE_MUST_NOT_RUN')",
            "requirements": "offline-demo==1.0",
            "dependency_check": True,
            "builtin_package_snapshot": bundle.snapshot,
        },
        executor.RuntimeSettings(tmp_path / "runtime", 30, 30),
        builtin_downloader=bundle.downloader,
    )
    assert result["status"] == "succeeded", result
    assert result["output"]["dependency_check"] == "installable"
    assert result["workspace_cleanup_status"] == "completed"
    assert "BUSINESS_CODE_MUST_NOT_RUN" not in result["stdout"]


def test_resource_and_integrity_errors_are_not_missing_dependencies() -> None:
    from dlr.worker.builtin_packages import install_error

    for code in (
        "dependency_timeout",
        "dependency_sandbox_failed",
        "builtin_checksum_mismatch",
        "dependency_cache_reservation_expired",
    ):
        error = venv.DependencyPreparationError("failure", "", error_code=code)
        assert install_error(error) is error


def test_rejects_embedded_npm_lock_and_archive_traversal(tmp_path: Path) -> None:
    for unsafe in (
        "package/npm-shrinkwrap.json",
        "package/../../outside",
        "package/node_modules/hidden/package.json",
    ):
        path = tmp_path / "unsafe.tgz"
        with tarfile.open(path, "w:gz") as archive:
            for name, content in (
                ("package/package.json", b'{"name":"unsafe","version":"1.0.0"}'),
                (unsafe, b"{}"),
            ):
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        with pytest.raises(PackageValidationError):
            inspect_package(path, "npm", "unsafe.tgz")


def test_upgrade_preserves_defaults_and_name_collisions() -> None:
    from sqlalchemy import text

    from test_unified_runtime_migration import _isolated_schema, _upgrade

    with _isolated_schema("builtin141", "0034_webhook_response") as (engine, database):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO package_sources (name, kind, index_url, is_default) "
                    "VALUES ('DLR builtin source (pypi)', 'pypi', "
                    "'https://custom.example/simple', false)"
                )
            )
            defaults = connection.execute(
                text("SELECT kind, index_url FROM package_sources WHERE is_default ORDER BY kind")
            ).all()
        _upgrade(database, "head")
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT kind, index_url FROM package_sources WHERE is_default ORDER BY kind"
                    )
                ).all()
                == defaults
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM package_sources "
                        "WHERE index_url LIKE 'dlr-builtin://%' AND NOT is_default"
                    )
                )
                == 3
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT index_url FROM package_sources "
                        "WHERE name = 'DLR builtin source (pypi)'"
                    )
                )
                == "https://custom.example/simple"
            )
            assert (
                connection.scalar(text("SELECT quota_bytes FROM builtin_package_settings"))
                == 1073741824
            )


def test_tar_extended_header_is_bounded_before_allocation(tmp_path: Path) -> None:
    import gzip

    header = tarfile.TarInfo("pax")
    header.type = tarfile.XHDTYPE
    header.size = 1 << 30
    path = tmp_path / "bomb.tgz"
    with gzip.open(path, "wb") as stream:
        stream.write(header.tobuf())
    with pytest.raises(PackageValidationError, match="allocation limit"):
        inspect_package(path, "npm", "bomb.tgz")
