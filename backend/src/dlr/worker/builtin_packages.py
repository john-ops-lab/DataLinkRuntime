"""Verified task-scoped materials, with no Control credential in package-manager commands."""

import base64
import hashlib
import json
import platform
import re
import shutil
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote

from dlr.common.builtin_packages import PackageValidationError, safe_path, validate_npm_dependencies
from dlr.worker import venv

Downloader = Callable[[Mapping[str, Any], Any], int]


class _VerifiedWriter:
    def __init__(self, stream: BinaryIO, expected: int) -> None:
        self.stream = stream
        self.expected = expected
        self.total = 0
        self.digest = hashlib.sha256()

    def write(self, chunk: bytes) -> int:
        self.total += len(chunk)
        if self.total > self.expected:
            raise venv.DependencyPreparationError(
                "builtin package exceeds declared size", "", error_code="builtin_checksum_mismatch"
            )
        self.digest.update(chunk)
        return self.stream.write(chunk)


@dataclass
class BuiltinMaterials:
    snapshot: dict[str, Any]
    directory: Path
    downloader: Downloader
    _downloaded: bool = False

    @property
    def identity(self) -> str:
        # Runtime changes cannot silently reuse a previously compatible binary environment.
        runtime = {
            "python": sys.version,
            "system": platform.system(),
            "machine": platform.machine(),
        }
        for command in ("node", "java", "javac", "uv", "mvn"):
            executable = shutil.which(command)
            if executable:
                info = Path(executable).resolve().stat()
                runtime[command] = f"{executable}:{info.st_size}:{info.st_mtime_ns}"
        return (
            "dlr-builtin-v1:"
            + hashlib.sha256(
                json.dumps(
                    {"snapshot": self.snapshot, "runtime": runtime},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )

    def materialize(self) -> Path:
        if self._downloaded:
            return self.directory
        files = self.snapshot.get("files")
        if not isinstance(files, list):
            raise venv.DependencyPreparationError(
                "invalid builtin snapshot", "", error_code="builtin_snapshot_invalid"
            )
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        seen = set()
        try:
            for item in files:
                relative = safe_path(item["repository_path"])
                if (
                    relative in seen
                    or not isinstance(item["size_bytes"], int)
                    or item["size_bytes"] <= 0
                    or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                ):
                    raise PackageValidationError("invalid builtin file identity")
                seen.add(relative)
                destination = self.directory / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as stream:
                    writer = _VerifiedWriter(stream, item["size_bytes"])
                    self.downloader(item, writer)
                    if (
                        writer.total != writer.expected
                        or writer.digest.hexdigest() != item["sha256"]
                    ):
                        raise venv.DependencyPreparationError(
                            "builtin package checksum mismatch",
                            "",
                            error_code="builtin_checksum_mismatch",
                        )
            self._downloaded = True
        except venv.DependencyPreparationError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise venv.DependencyPreparationError(
                "builtin materials unavailable", "", error_code="builtin_content_unavailable"
            ) from error
        return self.directory

    @contextmanager
    def npm_registry(self) -> Iterator[str]:
        directory = self.materialize()
        files = self.snapshot["files"]
        packages: dict[str, dict[str, Any]] = {}
        blobs: dict[str, Path] = {}
        for item in files:
            package = dict(item["metadata"])
            validate_npm_dependencies(package)
            name, version = package["name"], package["version"]
            entry = packages.setdefault(name, {"name": name, "versions": {}})
            if version in entry["versions"]:
                raise venv.DependencyPreparationError(
                    "ambiguous npm version materials", "", error_code="builtin_package_conflict"
                )
            entry["versions"][version] = package
            blobs[f"/tarballs/{item['id']}"] = directory / item["repository_path"]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                key = unquote(self.path.split("?", 1)[0])
                if key in blobs:
                    path = blobs[key]
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(path.stat().st_size))
                    self.end_headers()
                    with path.open("rb") as stream:
                        shutil.copyfileobj(stream, self.wfile, 64 * 1024)
                    return
                entry = packages.get(key.lstrip("/"))
                if entry is None:
                    self.send_error(404, "Package is not present in the DLR builtin snapshot")
                    return
                data = json.dumps(entry).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        url = f"http://127.0.0.1:{server.server_port}"
        for item in files:
            package = item["metadata"]
            packages[package["name"]]["versions"][package["version"]]["dist"] = {
                "tarball": f"{url}/tarballs/{item['id']}",
                "integrity": "sha256-" + base64.b64encode(bytes.fromhex(item["sha256"])).decode(),
            }
        thread = threading.Thread(target=server.serve_forever, name="dlr-builtin-npm", daemon=True)
        thread.start()
        try:
            yield url
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def validate_python_requirements(requirements: str) -> None:
    for line in requirements.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if (
            stripped.startswith("-")
            or "@" in stripped
            or "://" in stripped
            or "\\" in stripped
            or "/" in stripped
        ):
            raise venv.DependencyPreparationError(
                "builtin Python source only accepts package requirements, "
                "without options or direct URLs",
                "",
                error_code="builtin_external_reference",
            )


def install_error(error: venv.DependencyPreparationError) -> venv.DependencyPreparationError:
    log = error.install_log.lower()
    incompatible = any(
        marker in log
        for marker in (
            "ebadplatform",
            "ebadengine",
            "not compatible",
            "unsupported class file",
            "requires-python",
            "requires a different python",
            "no wheels with a matching",
            "not supported on this platform",
        )
    )
    if error.error_code != "dependency_preparation_failed":
        return error
    return venv.DependencyPreparationError(
        "Builtin materials are incompatible with the Worker environment"
        if incompatible
        else "Builtin dependencies or build materials are missing",
        error.install_log,
        error_code="builtin_environment_mismatch" if incompatible else "builtin_dependency_missing",
    )
