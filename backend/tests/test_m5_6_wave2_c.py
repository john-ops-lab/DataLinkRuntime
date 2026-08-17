"""M5.6 Wave 2 C contracts: frozen Execution locale and Worker messages."""

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from conftest import WORKER_TOKEN
from dlr.control.models import AdapterSchedule, Execution
from dlr.control.services.schedule import scheduler_tick
from dlr.runtime.java_runtime import SOURCE as JAVA_RUNTIME_SOURCE
from dlr.worker import executor, javaenv, nodeenv, venv
from test_adapters import create_adapter, save_version
from test_executions import create_execution
from test_workers import claim, register_worker, report

WORKER_HEADERS = {"Authorization": f"Bearer {WORKER_TOKEN}"}


def _set_locale(client: TestClient, locale: str) -> None:
    response = client.put("/api/locale", json={"locale": locale})
    assert response.status_code == 200, response.text


def test_manual_schedule_and_webhook_capture_locale_and_keep_it_after_switch(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    worker = register_worker(api_client, name="wave2-c-all-runtimes")
    worker_id = worker["id"]

    manual_adapter = create_adapter(api_client, name="wave2-c-manual")
    save_version(api_client, manual_adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{manual_adapter['id']}",
            json={"runtime_worker_id": worker_id},
        ).status_code
        == 200
    )

    _set_locale(api_client, "en")
    manual = create_execution(api_client, manual_adapter["id"])
    assert manual["locale"] == "en"
    claimed = claim(api_client, worker_id)
    assert claimed.status_code == 200
    assert claimed.json()["locale"] == "en"

    # A mid-run system switch cannot change the claimed Execution language.
    _set_locale(api_client, "zh-CN")
    assert claim(api_client, worker_id).status_code == 204
    assert report(api_client, worker_id, manual["id"], {"status": "succeeded"}).status_code == 200
    assert api_client.get(f"/api/executions/{manual['id']}").json()["locale"] == "en"

    schedule_adapter = create_adapter(api_client, name="wave2-c-schedule")
    save_version(api_client, schedule_adapter["id"])
    assert (
        api_client.patch(
            f"/api/adapters/{schedule_adapter['id']}",
            json={"runtime_worker_id": worker_id, "run_mode": "schedule"},
        ).status_code
        == 200
    )
    schedule = api_client.put(
        f"/api/adapters/{schedule_adapter['id']}/schedule",
        json={"enabled": True, "cron": "* * * * *", "timezone": "UTC", "input": None},
    )
    assert schedule.status_code == 200, schedule.text
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        session.execute(
            update(AdapterSchedule)
            .where(AdapterSchedule.adapter_id == schedule_adapter["id"])
            .values(next_run_at=now - timedelta(minutes=1))
        )
    _set_locale(api_client, "zh-CN")
    with session_factory() as session:
        assert scheduler_tick(session, now=now) == 1
        scheduled = session.query(Execution).filter_by(adapter_id=schedule_adapter["id"]).one()
        assert scheduled.locale == "zh-CN"

    webhook_adapter = create_adapter(
        api_client,
        name="wave2-c-webhook",
        adapter_type="webhook",
    )
    credential = api_client.post(
        "/api/credentials",
        json={"name": "wave2-c-hook-token", "type": "token", "fields": {"token": "hook-secret"}},
    )
    assert credential.status_code == 201, credential.text
    webhook = api_client.get(f"/api/adapters/{webhook_adapter['id']}/webhook").json()
    configured = api_client.put(
        f"/api/adapters/{webhook_adapter['id']}/webhook",
        json={
            "enabled": False,
            "public_id": webhook["public_id"],
            "credential_id": credential.json()["id"],
        },
    )
    assert configured.status_code == 200, configured.text
    save_version(api_client, webhook_adapter["id"])
    started = api_client.put(
        f"/api/adapters/{webhook_adapter['id']}/webhook",
        json={
            "enabled": True,
            "public_id": webhook["public_id"],
            "credential_id": credential.json()["id"],
        },
    )
    assert started.status_code == 200, started.text
    _set_locale(api_client, "en")
    accepted = api_client.post(
        f"/api/hooks/{webhook['public_id']}",
        json={"event": "created"},
        headers={"Authorization": "Bearer hook-secret"},
    )
    assert accepted.status_code == 202, accepted.text
    with session_factory() as session:
        received = session.get(Execution, accepted.json()["execution_id"])
        assert received is not None and received.locale == "en"


def _runtime_settings(root: Path) -> executor.RuntimeSettings:
    return executor.RuntimeSettings(
        runtime_root=root,
        execution_timeout_seconds=10,
        dep_install_timeout_seconds=10,
    )


def _payload(language: str, code: str, locale: str) -> dict[str, object]:
    return {
        "execution_id": 701,
        "adapter_id": 702,
        "version_id": 703,
        "language": language,
        "code": code,
        "requirements": "fixture@1.0.0",
        "runtime_config": {},
        "input": {"locale": locale},
        "execution_timeout_seconds": 10,
        "locale": locale,
    }


def test_all_three_runtimes_localize_platform_dependency_lines_without_translating_user_logs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def prepare_python(*args: object, dependency_log=None, **kwargs: object) -> Path:
        assert dependency_log is not None
        dependency_log("fixture==1.0.0 未安装，开始安装")
        dependency_log("fixture==1.0.0 安装成功")
        return Path(sys.executable)

    def prepare_node(
        runtime_root: Path, *args: object, dependency_log=None, **kwargs: object
    ) -> Path:
        assert dependency_log is not None
        dependency_log("fixture@1.0.0 未安装，开始安装")
        dependency_log("fixture@1.0.0 安装成功")
        directory = venv.version_dir(runtime_root, 702, 703)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "node_modules").mkdir()
        (directory / "adapter.mjs").write_text(
            "export async function handle(context, input) {\n"
            '  context.logger.info("user logger");\n'
            '  console.log("user stdout");\n'
            "  return { input };\n"
            "}\n",
            encoding="utf-8",
        )
        return directory

    def prepare_java(
        runtime_root: Path, *args: object, dependency_log=None, **kwargs: object
    ) -> Path:
        assert dependency_log is not None
        dependency_log("com.example:fixture:1.0.0 未安装，开始安装")
        dependency_log("com.example:fixture:1.0.0 安装成功")
        directory = venv.version_dir(runtime_root, 702, 703)
        classes = directory / "classes"
        deps = directory / "deps"
        classes.mkdir(parents=True, exist_ok=True)
        deps.mkdir(parents=True, exist_ok=True)
        (directory / "Adapter.java").write_text(
            "public class Adapter {\n"
            "  public Object handle(Context context, Object input) {\n"
            '    context.logger.info("user logger");\n'
            '    System.out.println("user stdout");\n'
            "    return input;\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        (directory / "DlrRuntime.java").write_text(JAVA_RUNTIME_SOURCE, encoding="utf-8")
        subprocess.run(
            [
                "javac",
                "--release",
                "21",
                "-cp",
                str(deps / "*"),
                "-d",
                str(classes),
                str(directory / "Adapter.java"),
                str(directory / "DlrRuntime.java"),
            ],
            check=True,
        )
        return directory

    monkeypatch.setattr(venv, "prepare_version_venv", prepare_python)
    monkeypatch.setattr(nodeenv, "prepare_version_node", prepare_node)
    monkeypatch.setattr(javaenv, "prepare_version_java", prepare_java)

    cases = {
        "python": (
            "def handle(context, input):\n"
            "    context.logger.info('user logger')\n"
            "    print('user stdout')\n"
            "    return input\n"
        ),
        "javascript": (
            "export async function handle(context, input) {\n"
            "  context.logger.info('user logger');\n"
            "  console.log('user stdout');\n"
            "  return input;\n"
            "}"
        ),
        "java": (
            "public class Adapter { public Object handle(Context context, Object input) { "
            'context.logger.info("user logger"); return input; } }'
        ),
    }
    for locale in ("zh-CN", "en"):
        for language, code in cases.items():
            result = executor.run(
                _payload(language, code, locale),
                _runtime_settings(tmp_path / locale / language),
            )
            assert result["status"] == "succeeded", result
            assert "fixture" in result["stdout"]
            assert "user logger" in result["stdout"]
            assert "user stdout" in result["stdout"]
            if locale == "en":
                assert "installation succeeded" in result["stdout"]
                assert "安装成功" not in result["stdout"]
            else:
                assert "安装成功" in result["stdout"]


def test_user_traceback_and_output_are_not_translated(tmp_path: Path) -> None:
    result = executor.run(
        {
            "execution_id": 704,
            "adapter_id": 705,
            "version_id": 706,
            "language": "python",
            "code": (
                "def handle(context, input):\n"
                "    print('用户 stdout')\n"
                "    raise RuntimeError('用户 traceback')\n"
            ),
            "requirements": "",
            "runtime_config": {},
            "input": {"用户输入": "保留"},
            "execution_timeout_seconds": 10,
            "locale": "en",
        },
        _runtime_settings(tmp_path),
    )
    assert result["status"] == "failed"
    assert "用户 stdout" in result["stdout"]
    assert "用户 traceback" in result["stdout"]
    assert "Traceback" in result["stdout"]


def test_english_dependency_error_keeps_localized_hint_and_raw_tool_detail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail_prepare(*args: object, **kwargs: object) -> Path:
        raise venv.DependencyPreparationError(
            "uv pip install failed",
            "ERROR: Could not resolve host: mirror.example.com",
            dependency="missing==1.0",
        )

    monkeypatch.setattr(venv, "prepare_version_venv", fail_prepare)
    result = executor.run(
        {
            "execution_id": 707,
            "adapter_id": 708,
            "version_id": 709,
            "language": "python",
            "code": "def handle(context, input):\n    return input\n",
            "requirements": "missing==1.0",
            "runtime_config": {},
            "input": None,
            "execution_timeout_seconds": 10,
            "locale": "en",
        },
        _runtime_settings(tmp_path),
    )
    assert result["status"] == "failed"
    assert "Dependency source DNS resolution failed" in result["error"]
    assert "域名解析失败" not in result["error"]
    assert "Could not resolve host" in result["stdout"]


def test_dependency_source_presets_have_stable_ids_and_user_names_are_unchanged(
    api_client: TestClient,
) -> None:
    defaults = api_client.get("/api/package-sources/defaults")
    assert defaults.status_code == 200
    default_body = defaults.json()
    assert {default_body[kind]["preset_id"] for kind in ("pypi", "npm", "maven")} == {
        "pypi.aliyun",
        "npm.npmmirror",
        "maven.aliyun",
    }

    restored = api_client.post("/api/package-sources/defaults/pypi")
    assert restored.status_code == 200
    builtin = restored.json()
    assert builtin["preset_id"] == "pypi.aliyun"
    renamed = api_client.patch(
        f"/api/package-sources/{builtin['id']}",
        json={"name": "改名后的系统源"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "改名后的系统源"
    assert renamed.json()["preset_id"] is None

    created = api_client.post(
        "/api/package-sources",
        json={
            "name": "我的私有源",
            "kind": "pypi",
            "index_url": "https://mirrors.aliyun.com/pypi/simple/",
        },
    )
    assert created.status_code == 201
    assert created.json()["name"] == "我的私有源"
    assert created.json()["preset_id"] is None
