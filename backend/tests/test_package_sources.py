"""M3.2 tests: Python package source management and the venv install strategy.

Covers package source CRUD/default semantics, the reachability probe, claim
payload index URL resolution (with embedded basic auth) and the offline-first
dependency strategy shared by test runs and production runs.
"""

import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.control.models import PackageSource
from dlr.control.services import package_source as package_source_service
from dlr.worker import executor
from dlr.worker import venv as venv_manager
from test_adapters import create_adapter, save_version
from test_executions import create_execution
from test_runtime import ECHO_CODE, make_payload, runtime_settings
from test_workers import claim, register_worker, report

WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}


def create_source(
    client: TestClient,
    name: str = "mirror",
    index_url: str = "https://mirror.example.com/simple/",
    is_default: bool = False,
    credential_id: int | None = None,
    kind: str = "pypi",
) -> dict:
    payload: dict = {
        "name": name,
        "kind": kind,
        "index_url": index_url,
        "is_default": is_default,
    }
    if credential_id is not None:
        payload["credential_id"] = credential_id
    response = client.post("/api/package-sources", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --- CRUD and default semantics -------------------------------------------------


def test_package_source_crud_and_name_conflict(api_client: TestClient) -> None:
    source = create_source(api_client)
    assert api_client.get("/api/package-sources").json()[0]["name"] == "mirror"

    conflict = api_client.post(
        "/api/package-sources",
        json={"name": "mirror", "index_url": "https://other.example.com/simple/"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "package_source_name_conflict"

    renamed = api_client.patch(
        f"/api/package-sources/{source['id']}",
        json={"name": "mirror-renamed", "index_url": "https://renamed.example.com/simple/"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["index_url"] == "https://renamed.example.com/simple/"

    assert api_client.delete(f"/api/package-sources/{source['id']}").status_code == 204
    assert api_client.get(f"/api/package-sources/{source['id']}").status_code == 404


def test_package_source_default_is_exclusive(api_client: TestClient) -> None:
    first = create_source(api_client, name="src-a", is_default=True)
    create_source(api_client, name="src-b", is_default=True)

    sources = {item["name"]: item for item in api_client.get("/api/package-sources").json()}
    assert sources["src-a"]["is_default"] is False, "a new default takes over"
    assert sources["src-b"]["is_default"] is True

    # PATCH can move the default flag back.
    api_client.patch(f"/api/package-sources/{first['id']}", json={"is_default": True})
    sources = {item["name"]: item for item in api_client.get("/api/package-sources").json()}
    assert sources["src-a"]["is_default"] is True
    assert sources["src-b"]["is_default"] is False


def test_package_source_defaults_are_independent_per_kind(api_client: TestClient) -> None:
    pypi = create_source(api_client, name="pypi", is_default=True, kind="pypi")
    npm = create_source(
        api_client,
        name="npm",
        index_url="https://registry.example.com/",
        is_default=True,
        kind="npm",
    )
    maven = create_source(
        api_client,
        name="maven",
        index_url="https://maven.example.com/repository/",
        is_default=True,
        kind="maven",
    )
    assert pypi["kind"] == "pypi"
    assert npm["kind"] == "npm"
    assert maven["kind"] == "maven"
    assert all(source["is_default"] for source in (pypi, npm, maven))


def test_package_source_credential_reference(api_client: TestClient) -> None:
    credential = api_client.post(
        "/api/credentials",
        json={
            "name": "index-auth",
            "type": "password",
            "fields": {"username": "u", "password": "p"},
        },
    ).json()

    unknown = api_client.post(
        "/api/package-sources",
        json={"name": "auth-src", "index_url": "https://x/simple/", "credential_id": 999999},
    )
    assert unknown.status_code == 404

    source = create_source(api_client, name="auth-src", credential_id=credential["id"])
    assert source["credential_name"] == "index-auth"

    # Explicit null clears the reference; omitting keeps it.
    untouched = api_client.patch(
        f"/api/package-sources/{source['id']}", json={"name": "auth-src-renamed"}
    )
    assert untouched.json()["credential_id"] == credential["id"]
    cleared = api_client.patch(f"/api/package-sources/{source['id']}", json={"credential_id": None})
    assert cleared.json()["credential_id"] is None


# --- reachability probe -----------------------------------------------------------


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args: object) -> None:
        pass


def test_package_source_reachability_probe(api_client: TestClient) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    source = create_source(api_client, index_url=f"http://127.0.0.1:{port}/simple/")
    dead = create_source(
        api_client,
        name="dead-mirror",
        # Malformed URL: the probe must report a transport error even when a
        # system proxy would happily answer for "dead" hosts.
        index_url="not a url",
    )
    try:
        reachable = api_client.post(f"/api/package-sources/{source['id']}/test")
        assert reachable.status_code == 200
        assert reachable.json()["ok"] is True
        assert reachable.json()["status_code"] == 200

        unreachable = api_client.post(f"/api/package-sources/{dead['id']}/test")
        assert unreachable.json()["ok"] is False
        assert unreachable.json()["error"]
    finally:
        server.shutdown()
        server.server_close()


# --- claim payload index URL resolution --------------------------------------------


def test_claim_payload_carries_default_source_url(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="pkg-src-adapter")
    save_version(api_client, adapter["id"])
    worker = register_worker(api_client, name="pkg-worker")
    updated = api_client.patch(
        f"/api/adapters/{adapter['id']}", json={"runtime_worker_id": worker["id"]}
    )
    assert updated.status_code == 200, updated.text

    # Without a default source the payload carries no index URL.
    execution = create_execution(api_client, adapter["id"])
    response = claim(api_client, worker["id"])
    assert response.status_code == 200
    assert response.json()["execution_id"] == execution["id"]
    assert response.json()["index_url"] is None
    assert (
        report(api_client, worker["id"], execution["id"], {"status": "succeeded"}).status_code
        == 200
    )

    # A non-default source is ignored.
    create_source(api_client, name="non-default", is_default=False)
    execution = create_execution(api_client, adapter["id"])
    response = claim(api_client, worker["id"])
    assert response.json()["execution_id"] == execution["id"]
    assert response.json()["index_url"] is None
    assert (
        report(api_client, worker["id"], execution["id"], {"status": "succeeded"}).status_code
        == 200
    )

    # The default source URL travels inside the payload.
    create_source(api_client, name="company-mirror", is_default=True)
    execution = create_execution(api_client, adapter["id"])
    response = claim(api_client, worker["id"])
    assert response.json()["execution_id"] == execution["id"]
    assert response.json()["index_url"] == "https://mirror.example.com/simple/"


def test_claim_payload_selects_source_by_adapter_language(api_client: TestClient) -> None:
    worker_response = api_client.post(
        "/api/workers/register",
        json={
            "name": "multilang-source-worker",
            "capabilities": ["python", "javascript", "java"],
        },
        headers=WORKER_HEADERS,
    )
    assert worker_response.status_code == 200, worker_response.text
    worker = worker_response.json()
    create_source(
        api_client,
        name="npm-default",
        kind="npm",
        index_url="https://registry.example.com/",
        is_default=True,
    )
    create_source(
        api_client,
        name="maven-default",
        kind="maven",
        index_url="https://maven.example.com/repository/",
        is_default=True,
    )

    cases = (
        ("javascript", "https://registry.example.com/"),
        ("java", "https://maven.example.com/repository/"),
    )
    for language, expected_url in cases:
        adapter = create_adapter(
            api_client,
            name=f"source-{language}",
            language=language,
        )
        save_version(api_client, adapter["id"], code="// source routing test")
        execution = create_execution(api_client, adapter["id"])
        response = claim(api_client, worker["id"])
        assert response.status_code == 200, response.text
        task = response.json()
        assert task["execution_id"] == execution["id"]
        assert task["language"] == language
        assert task["index_url"] == expected_url


def test_claim_payload_embeds_basic_auth(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    credential = api_client.post(
        "/api/credentials",
        json={
            "name": "index-auth",
            "type": "password",
            "fields": {"username": "svc user", "password": "p@ss/42"},
        },
    ).json()
    source = create_source(
        api_client,
        name="authed-mirror",
        index_url="https://mirror.example.com/simple/",
        is_default=True,
        credential_id=credential["id"],
    )

    with session_factory() as session:
        row = session.get(PackageSource, source["id"])
        assert row is not None
        # URL-encoded userinfo is embedded in the netloc.
        assert package_source_service.resolve_source_url(session, row) == (
            "https://svc%20user:p%40ss%2F42@mirror.example.com/simple/"
        )


# --- canonical defaults and restore (M5.5.8) -----------------------------------


def test_canonical_defaults_endpoint_returns_all_three_kinds(api_client: TestClient) -> None:
    defaults = api_client.get("/api/package-sources/defaults")
    assert defaults.status_code == 200
    body = defaults.json()
    assert body["pypi"]["index_url"] == "https://mirrors.aliyun.com/pypi/simple/"
    assert body["npm"]["index_url"] == "https://registry.npmmirror.com/"
    assert body["maven"]["index_url"] == "https://maven.aliyun.com/repository/public"
    assert all(body[kind]["name"] for kind in ("pypi", "npm", "maven"))


def test_restore_default_creates_missing_default_source(api_client: TestClient) -> None:
    restored = api_client.post("/api/package-sources/defaults/pypi")
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["kind"] == "pypi"
    assert body["index_url"] == "https://mirrors.aliyun.com/pypi/simple/"
    assert body["is_default"] is True

    sources = api_client.get("/api/package-sources").json()
    assert len(sources) == 1 and sources[0]["id"] == body["id"]


def test_restore_default_resets_existing_default_url(api_client: TestClient) -> None:
    source = create_source(api_client, name="custom", is_default=True)
    assert source["index_url"] == "https://mirror.example.com/simple/"

    restored = api_client.post("/api/package-sources/defaults/pypi")
    assert restored.status_code == 200, restored.text
    assert restored.json()["id"] == source["id"]
    assert restored.json()["index_url"] == "https://mirrors.aliyun.com/pypi/simple/"
    assert restored.json()["is_default"] is True

    sources = api_client.get("/api/package-sources").json()
    assert len(sources) == 1, "restore reuses the existing row, never duplicates"


def test_restore_default_promotes_existing_non_default_source(api_client: TestClient) -> None:
    create_source(api_client, name="plain-mirror", is_default=False)

    restored = api_client.post("/api/package-sources/defaults/pypi")
    assert restored.status_code == 200, restored.text
    assert restored.json()["name"] == "plain-mirror"
    assert restored.json()["index_url"] == "https://mirrors.aliyun.com/pypi/simple/"
    assert restored.json()["is_default"] is True

    sources = api_client.get("/api/package-sources").json()
    assert len(sources) == 1, "restore never creates a duplicate name"


def test_restore_default_rejects_unknown_kind(api_client: TestClient) -> None:
    response = api_client.post("/api/package-sources/defaults/cargo")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "package_source_kind_invalid"


# --- actionable install-error classification (M5.5.8) --------------------------


def test_classify_install_error_distinguishes_layers() -> None:
    cases = [
        (
            "Could not resolve host: registry.npmmirror.com",
            "域名解析失败",
        ),
        (
            "ERROR: Temporary failure in name resolution",
            "域名解析失败",
        ),
        (
            "Failed to connect to mirror.example.com port 443 after 30000 ms: Connection timed out",
            "网络不可达",
        ),
        (
            "curl: (7) Failed to connect to mirrors.aliyun.com port 80: Connection refused",
            "网络不可达",
        ),
        (
            "urllib3 HTTPSConnectionPool: Max retries exceeded ... Caused by SSLError "
            "(SSLCertVerificationError) certificate verify failed",
            "TLS 握手或证书校验失败",
        ),
        (
            "ERROR: HTTP error 401 while getting https://mirror.example.com/simple/pkg/",
            "认证失败",
        ),
        (
            "npm ERR! code E403\nnpm ERR! 403 Forbidden - GET https://registry.example.com/private",
            "认证失败",
        ),
        (
            "ERROR: Could not find a version that satisfies the requirement missing-pkg==1.0 "
            "(from versions: none)",
            "包或制品不存在",
        ),
        (
            "npm ERR! 404 Not Found - GET https://registry.example.com/not-a-package",
            "包或制品不存在",
        ),
        (
            "Could not find artifact org.example:no-such-artifact:1.0 in mirror (https://maven.example.com)",
            "包或制品不存在",
        ),
        (
            "Invalid index URL 'https:///simple/': Cannot parse",
            "仓库不存在或不可用",
        ),
        (
            "ERROR: HTTP error 404 while getting https://mirror.example.com/simple/",
            "仓库不存在或不可用",
        ),
        ("random unrelated output", None),
    ]
    for log, expected in cases:
        hint = venv_manager.classify_dependency_install_error(log)
        if expected is None:
            assert hint is None, log
        else:
            assert hint is not None and expected in hint, log


def test_install_error_hint_is_appended_to_preparation_error(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run_logged(command: list[str], timeout_seconds: int) -> str:
        if "venv" in command and "install" not in command:
            return ""
        if "--offline" in command:
            raise venv_manager.DependencyPreparationError("offline install failed", "offline log")
        raise venv_manager.DependencyPreparationError(
            "uv pip install failed",
            "ERROR: Could not resolve host: mirror.example.com",
        )

    monkeypatch.setattr(venv_manager, "_run_logged", fake_run_logged)
    with pytest.raises(venv_manager.DependencyPreparationError) as error:
        venv_manager.prepare_version_venv(
            tmp_path,  # type: ignore[arg-type]
            20,
            4,
            "requests",
            timeout_seconds=60,
            index_url="https://mirror.example.com/simple/",
        )
    assert "域名解析失败" in str(error.value)
    assert "Could not resolve host" in error.value.install_log


# --- offline-first venv strategy ----------------------------------------------------


def _install_calls(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if "install" in call]


def test_venv_offline_success_never_touches_package_source(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run_logged(command: list[str], timeout_seconds: int) -> str:
        calls.append(command)
        return ""

    monkeypatch.setattr(venv_manager, "_run_logged", fake_run_logged)
    venv_manager.prepare_version_venv(
        tmp_path,  # type: ignore[arg-type]
        20,
        1,
        "requests",
        timeout_seconds=60,
        index_url="https://mirror.example.com/simple/",
    )
    installs = _install_calls(calls)
    assert len(installs) == 1, "a satisfied offline install must not try the package source"
    assert "--offline" in installs[0]


def test_venv_falls_back_to_package_source_when_cache_misses(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run_logged(command: list[str], timeout_seconds: int) -> str:
        calls.append(command)
        if "--offline" in command:
            raise venv_manager.DependencyPreparationError("offline install failed", "offline log")
        return "source log"

    monkeypatch.setattr(venv_manager, "_run_logged", fake_run_logged)
    venv_manager.prepare_version_venv(
        tmp_path,  # type: ignore[arg-type]
        20,
        2,
        "requests",
        timeout_seconds=60,
        index_url="https://mirror.example.com/simple/",
    )
    installs = _install_calls(calls)
    assert len(installs) == 2
    assert "--offline" in installs[0]
    assert installs[1][-2:] == ["--index-url", "https://mirror.example.com/simple/"]
    directory = venv_manager.version_dir(tmp_path, 20, 2)  # type: ignore[arg-type]
    assert (directory / ".ready").exists()


def test_credentialed_index_failure_is_redacted_before_execution_persistence(
    api_client: TestClient,
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    username = "review user"
    password = "pa@ss/word"
    encoded_username = "review%20user"
    encoded_password = "pa%40ss%2Fword"
    raw_userinfo = f"{username}:{password}"
    encoded_userinfo = f"{encoded_username}:{encoded_password}"
    effective_url = f"https://{encoded_userinfo}@mirror.example.com/simple/"
    credential = api_client.post(
        "/api/credentials",
        json={
            "name": "redaction-index-auth",
            "type": "password",
            "fields": {"username": username, "password": password},
        },
    ).json()
    create_source(
        api_client,
        name="redaction-mirror",
        index_url="https://mirror.example.com/simple/",
        is_default=True,
        credential_id=credential["id"],
    )

    adapter = create_adapter(api_client, name="package-source-redaction")
    save_version(api_client, adapter["id"], requirements="missing-package==1")
    worker = register_worker(api_client, name="redaction-worker")
    updated = api_client.patch(
        f"/api/adapters/{adapter['id']}", json={"runtime_worker_id": worker["id"]}
    )
    assert updated.status_code == 200, updated.text
    execution = create_execution(api_client, adapter["id"])
    claimed = claim(api_client, worker["id"]).json()
    assert claimed["index_url"] == effective_url

    def fake_uv_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["uv", "venv"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if "--offline" in command:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="offline cache miss\n")
        echoed = (
            f"failed to fetch {effective_url}\n"
            f"resolved username {username}\n"
            f"resolved password {password}\n"
        )
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=echoed)

    monkeypatch.setattr(venv_manager.subprocess, "run", fake_uv_run)

    # The dependency error itself is safe before the executor sees it.
    with pytest.raises(venv_manager.DependencyPreparationError) as preparation_error:
        venv_manager.prepare_version_venv(
            tmp_path,  # type: ignore[arg-type]
            adapter["id"],
            claimed["version_id"],
            claimed["requirements"],
            timeout_seconds=60,
            index_url=effective_url,
        )
    install_log = preparation_error.value.install_log
    for sensitive in (
        username,
        password,
        encoded_username,
        encoded_password,
        raw_userinfo,
        encoded_userinfo,
    ):
        assert sensitive not in install_log
    assert "https://[REDACTED]@mirror.example.com/simple/" in install_log

    # The executor applies the same policy defensively, and Control persists
    # only the already-redacted result as Execution.stderr.
    result = executor.run(claimed, runtime_settings(tmp_path))
    assert result["status"] == "failed"
    response = report(api_client, worker["id"], execution["id"], result)
    assert response.status_code == 200, response.text
    persisted = api_client.get(f"/api/executions/{execution['id']}").json()
    for sensitive in (
        username,
        password,
        encoded_username,
        encoded_password,
        raw_userinfo,
        encoded_userinfo,
    ):
        assert sensitive not in persisted["stderr"]
        assert sensitive not in (persisted["error"] or "")
    assert "https://[REDACTED]@mirror.example.com/simple/" in persisted["stderr"]


def test_venv_cache_miss_without_source_fails_with_operator_hint(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run_logged(command: list[str], timeout_seconds: int) -> str:
        if "venv" in command and "install" not in command:
            return ""  # let venv creation succeed; only installs are faked
        if "--offline" in command:
            raise venv_manager.DependencyPreparationError("offline install failed", "offline log")
        raise AssertionError("no package source configured, nothing else may be tried")

    monkeypatch.setattr(venv_manager, "_run_logged", fake_run_logged)
    with pytest.raises(venv_manager.DependencyPreparationError) as error:
        venv_manager.prepare_version_venv(
            tmp_path,  # type: ignore[arg-type]
            20,
            3,
            "requests",
            timeout_seconds=60,
            index_url=None,
        )
    assert "package source" in str(error.value)
    # No half-built venv is left behind.
    assert not venv_manager.version_dir(tmp_path, 20, 3).exists()  # type: ignore[arg-type]


def test_executor_prefers_payload_index_url(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_prepare(
        runtime_root: Path,
        adapter_id: int,
        version_id: int,
        requirements: str,
        *,
        timeout_seconds: int,
        index_url: str | None = None,
    ) -> Path:
        captured["index_url"] = index_url
        return Path(sys.executable)

    monkeypatch.setattr(venv_manager, "prepare_version_venv", fake_prepare)
    settings = runtime_settings(tmp_path)
    settings = executor.RuntimeSettings(
        runtime_root=settings.runtime_root,
        execution_timeout_seconds=settings.execution_timeout_seconds,
        dep_install_timeout_seconds=settings.dep_install_timeout_seconds,
        pypi_index_url="https://worker-env.example.com/simple/",
    )

    # Control-provided source wins over the Worker environment fallback.
    payload = make_payload(code=ECHO_CODE)
    payload["index_url"] = "https://control.example.com/simple/"
    result = executor.run(payload, settings)
    assert result["status"] == "succeeded", result.get("error")
    assert captured["index_url"] == "https://control.example.com/simple/"

    # Without one, the Worker environment compatibility source is used.
    result = executor.run(make_payload(code=ECHO_CODE), settings)
    assert result["status"] == "succeeded", result.get("error")
    assert captured["index_url"] == "https://worker-env.example.com/simple/"
