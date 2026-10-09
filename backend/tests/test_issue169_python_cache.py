"""Real, offline-contained uv regression for Python cache promotion."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import stat
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zipfile import ZipFile

import pytest

from dlr.worker import venv
from dlr.worker.cache import CacheError, VerifiedVersionCache


def _probe_wheel(root: Path) -> None:
    files = {
        "dlr_issue169_probe.py": b"MARKER = 'dlr169-ready'\n",
        "dlr_issue169_probe-1.0.0.dist-info/METADATA": (
            b"Metadata-Version: 2.1\nName: dlr-issue169-probe\nVersion: 1.0.0\n"
        ),
        "dlr_issue169_probe-1.0.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: DLR regression\n"
            b"Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record = io.StringIO()
    for name, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
        record.write(f"{name},sha256={digest.decode()},{len(content)}\n")
    record.write("dlr_issue169_probe-1.0.0.dist-info/RECORD,,\n")
    files["dlr_issue169_probe-1.0.0.dist-info/RECORD"] = record.getvalue().encode()
    filename = "dlr_issue169_probe-1.0.0-py3-none-any.whl"
    with ZipFile(root / filename, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    simple = root / "simple" / "dlr-issue169-probe"
    simple.mkdir(parents=True)
    (simple / "index.html").write_text(f'<a href="../../{filename}">{filename}</a>')


def test_real_uv_source_install_promotes_and_reuses_verified_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert shutil.which("uv"), "the Worker preparation regression requires the real uv binary"
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.setenv("UV_PYTHON", sys.executable)
    source = tmp_path / "source"
    source.mkdir()
    _probe_wheel(source)
    requests: list[str] = []

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=str(source), **kwargs)

        def do_GET(self) -> None:
            requests.append(self.path)
            super().do_GET()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = tmp_path / "runtime"
    events: list[str] = []
    try:
        interpreter = venv.prepare_version_venv(
            root,
            169,
            1,
            "dlr-issue169-probe==1.0.0",
            timeout_seconds=30,
            index_url=f"http://127.0.0.1:{server.server_port}/simple",
            dependency_log=events.append,
        )
        assert any(event.endswith("安装成功") for event in events)
        assert any(path.endswith(".whl") for path in requests), (
            "must exercise a cold source install"
        )
        entry = venv.version_dir(root, 169, 1)
        lock = entry / ".venv" / ".lock"
        assert lock.is_file() and not lock.is_symlink()
        assert stat.S_IMODE(lock.stat().st_mode) == 0o444
        cache = VerifiedVersionCache(root / "version-cache")
        identity = venv._cache_identity(169, 1, "python", "dlr-issue169-probe==1.0.0")
        assert cache.verify(entry, identity)
        execution = subprocess.run(
            [
                str(interpreter),
                "-I",
                "-c",
                "import dlr_issue169_probe; print(dlr_issue169_probe.MARKER)",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert execution.stdout.strip() == "dlr169-ready"
        requests_before = len(requests)
        ready_mtime = (entry / ".ready").stat().st_mtime_ns
        reused = venv.prepare_version_venv(
            root, 169, 1, "dlr-issue169-probe==1.0.0", timeout_seconds=30
        )
        assert reused == interpreter
        assert (entry / ".ready").stat().st_mtime_ns == ready_mtime
        assert len(requests) == requests_before
        assert json.loads(cache._state_path.read_text())["reservations"] == {}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_lock_permission_normalization_does_not_accept_unsafe_package_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def install(command: list[str], _timeout: int) -> str:
        if command[:2] == ["uv", "venv"]:
            directory = Path(command[2])
            (directory / "bin").mkdir(parents=True)
            (directory / "bin" / "python").write_bytes(b"simulated interpreter")
            (directory / ".lock").write_bytes(b"")
            (directory / ".lock").chmod(0o666)
            (directory / "unsafe-package.py").write_bytes(b"package content")
            (directory / "unsafe-package.py").chmod(0o666)
        return ""

    monkeypatch.setattr(venv, "_run_logged", install)
    with pytest.raises(venv.DependencyPreparationError) as failure:
        venv.prepare_version_venv(tmp_path / "runtime", 169, 2, "probe==1.0", timeout_seconds=5)
    assert isinstance(failure.value.__cause__, CacheError)
    assert failure.value.__cause__.code == "cache_permissions_invalid"
    assert not (venv.version_dir(tmp_path / "runtime", 169, 2) / ".ready").exists()
    cache = VerifiedVersionCache(tmp_path / "runtime" / "version-cache")
    assert json.loads(cache._state_path.read_text())["reservations"] == {}
