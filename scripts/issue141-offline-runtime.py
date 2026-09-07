#!/usr/bin/env python3
"""Verify the three native installers with networking disabled.

Run inside the Worker image with --network none and a pre-populated Maven
repository containing commons-lang3:3.17.0 and dependency-plugin:3.8.1 materials.
No credentials, public indexes or global package caches are used by this probe.
"""

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from dlr.common.builtin_packages import inspect_package
from dlr.worker import javaenv, nodeenv, venv
from dlr.worker.builtin_packages import BuiltinMaterials


def wheel() -> tuple[str, bytes]:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        prefix = "dlr_offline_demo-1.0.dist-info"
        archive.writestr("dlr_offline_demo.py", "VALUE = 141\n")
        archive.writestr(
            f"{prefix}/METADATA", "Metadata-Version: 2.1\nName: dlr-offline-demo\nVersion: 1.0\n"
        )
        archive.writestr(
            f"{prefix}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        )
        archive.writestr(f"{prefix}/RECORD", "")
    return "dlr_offline_demo-1.0-py3-none-any.whl", target.getvalue()


def npm(name: str, dependencies: dict[str, str] | None = None) -> tuple[str, bytes]:
    target = io.BytesIO()
    package = {
        "name": name,
        "version": "1.0.0",
        "main": "index.js",
        "dependencies": dependencies or {},
    }
    with tarfile.open(fileobj=target, mode="w:gz") as archive:
        for filename, content in (
            ("package/package.json", json.dumps(package).encode()),
            ("package/index.js", b"module.exports = 141;"),
        ):
            member = tarfile.TarInfo(filename)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return f"{name}-1.0.0.tgz", target.getvalue()


def bundle(root: Path, kind: str, sources: list[tuple[Path, str]]) -> BuiltinMaterials:
    descriptors = []
    contents = {}
    for index, (path, repository_path) in enumerate(sources, 1):
        inspected = inspect_package(path, kind, path.name, repository_path)
        descriptors.append(
            {
                "id": index,
                "filename": path.name,
                "repository_path": inspected["repository_path"],
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "metadata": inspected["metadata"],
            }
        )
        contents[index] = path

    def download(item: Any, destination: Any) -> int:
        with contents[item["id"]].open("rb") as stream:
            shutil.copyfileobj(stream, destination, 64 * 1024)
        return item["size_bytes"]

    return BuiltinMaterials({"kind": kind, "files": descriptors}, root, download)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--maven-repository", type=Path, required=True)
    args = parser.parse_args()
    receipt: dict[str, Any] = {"network": "run with Docker --network none", "checks": {}}
    with tempfile.TemporaryDirectory(prefix="dlr-issue141-offline-") as temporary:
        root = Path(temporary)
        source = root / "sources"
        source.mkdir()
        for filename, data in (
            wheel(),
            npm("dlr-offline-demo", {"dlr-offline-child": "^1.0.0"}),
            npm("dlr-offline-child"),
        ):
            (source / filename).write_bytes(data)
        python_materials = bundle(
            root / "python-materials", "pypi", [(next(source.glob("*.whl")), "")]
        )
        python = venv.prepare_version_venv(
            root / "runtime",
            1,
            1,
            "dlr-offline-demo==1.0",
            timeout_seconds=120,
            builtin_materials=python_materials,
        )
        assert (
            subprocess.check_output(
                [str(python), "-B", "-c", "import dlr_offline_demo; print(dlr_offline_demo.VALUE)"],
                text=True,
            ).strip()
            == "141"
        )
        receipt["checks"]["python_install"] = "PASS"
        npm_materials = bundle(
            root / "npm-materials", "npm", [(path, "") for path in source.glob("*.tgz")]
        )
        node = nodeenv.prepare_version_node(
            root / "runtime",
            2,
            2,
            "export const ok = true;",
            "dlr-offline-demo@1.0.0",
            timeout_seconds=120,
            registry_url=None,
            builtin_materials=npm_materials,
        )
        assert (
            subprocess.check_output(
                [
                    "node",
                    "-e",
                    "console.log(require("
                    f"{json.dumps(str(node / 'node_modules/dlr-offline-child'))}))",
                ],
                text=True,
            ).strip()
            == "141"
        )
        receipt["checks"]["npm_transitive_install"] = "PASS"
        maven_sources = [
            (path, path.relative_to(args.maven_repository).as_posix())
            for path in sorted(args.maven_repository.rglob("*"))
            if path.suffix in (".jar", ".pom", ".xml")
        ]
        java_materials = bundle(root / "maven-materials", "maven", maven_sources)
        code = (
            "public class Adapter { public Object handle(Context context, Object input) {"
            ' return org.apache.commons.lang3.StringUtils.reverse("141"); }'
            " public static void main(String[] args) {"
            ' System.out.print(org.apache.commons.lang3.StringUtils.reverse("141")); } }'
        )
        java = javaenv.prepare_version_java(
            root / "runtime",
            3,
            3,
            code,
            "org.apache.commons:commons-lang3:3.17.0",
            timeout_seconds=120,
            repository_url=None,
            builtin_materials=java_materials,
        )
        assert (
            subprocess.check_output(
                ["java", "-cp", f"{java / 'classes'}:{java / 'deps'}/*", "Adapter"], text=True
            )
            == "141"
        )
        receipt["checks"]["java_install_compile_run"] = "PASS"
        receipt["maven_material_files"] = len(maven_sources)

        def no_download(*_: Any) -> int:
            raise AssertionError("verified warm environment performed a download")

        for item in (python_materials, npm_materials, java_materials):
            item.downloader = no_download
            item._downloaded = False
        assert (
            venv.prepare_version_venv(
                root / "runtime",
                1,
                1,
                "dlr-offline-demo==1.0",
                timeout_seconds=120,
                builtin_materials=python_materials,
            )
            == python
        )
        assert (
            nodeenv.prepare_version_node(
                root / "runtime",
                2,
                2,
                "export const ok = true;",
                "dlr-offline-demo@1.0.0",
                timeout_seconds=120,
                registry_url=None,
                builtin_materials=npm_materials,
            )
            == node
        )
        assert (
            javaenv.prepare_version_java(
                root / "runtime",
                3,
                3,
                code,
                "org.apache.commons:commons-lang3:3.17.0",
                timeout_seconds=120,
                repository_url=None,
                builtin_materials=java_materials,
            )
            == java
        )
        receipt["checks"]["all_languages_verified_reuse"] = "PASS"
        # The same package name/version from a different material identity must not
        # use the populated environment or a global package-manager cache.
        empty = BuiltinMaterials({"kind": "maven", "files": []}, root / "empty", no_download)
        try:
            javaenv.prepare_version_java(
                root / "runtime",
                3,
                3,
                code,
                "org.apache.commons:commons-lang3:3.17.0",
                timeout_seconds=120,
                repository_url="https://example.invalid",
                builtin_materials=empty,
            )
        except venv.DependencyPreparationError as error:
            assert error.error_code == "builtin_dependency_missing", error.error_code
            receipt["checks"]["java_missing_materials_no_fallback"] = "PASS"
        else:
            raise AssertionError("missing Maven materials reused an unrelated cache")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except venv.DependencyPreparationError as error:
        print(error.install_log)
        raise
