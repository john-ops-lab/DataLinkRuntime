"""M5.4.4 end-to-end smoke assertions executed inside the Control container."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, inspect, select

from dlr.common.config import settings
from dlr.control.db import SessionLocal
from dlr.control.models import AdapterVersion, Execution, Worker

BASE = "http://web/api"
ADMIN_TOKEN = os.environ["DLR_ADMIN_TOKEN"]
WORKER_TOKEN = os.environ["DLR_WORKER_TOKEN"]
STORED_SECRET = os.environ["SMOKE_STORED_SECRET"]
AI_FAKE_BASE_URL = os.environ["AI_FAKE_BASE_URL"]
AI_FAKE_DISABLED_BASE_URL = os.environ["AI_FAKE_DISABLED_BASE_URL"]


def request(
    method: str,
    path: str,
    payload: object | None = None,
    *,
    expected: int = 200,
    token: str | None = ADMIN_TOKEN,
) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    body = json.loads(raw) if raw else None
    assert status == expected, f"{method} {path}: expected {expected}, got {status}: {body}"
    return body


def hook(public_id: str, token: str, payload: object, *, expected: int = 202) -> Any:
    return request(
        "POST",
        f"/hooks/{public_id}",
        payload,
        expected=expected,
        token=token,
    )


def wait_terminal(execution_id: int, *, timeout: float = 90) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        execution = request("GET", f"/executions/{execution_id}")
        if execution["status"] not in {"pending", "running"}:
            return execution
        time.sleep(0.5)
    raise AssertionError(f"execution {execution_id} did not finish within {timeout}s")


def wait_schedule_execution(adapter_id: int, *, timeout: float = 100) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        history = request("GET", f"/adapters/{adapter_id}/executions")
        scheduled = next(
            (item for item in history["items"] if item["trigger"] == "schedule"),
            None,
        )
        if scheduled is not None:
            return wait_terminal(scheduled["id"], timeout=timeout)
        time.sleep(0.5)
    raise AssertionError(f"adapter {adapter_id} did not create a schedule Execution")


def create_adapter(name: str, language: str, adapter_type: str) -> dict[str, Any]:
    return request(
        "POST",
        "/adapters",
        {
            "name": name,
            "description": "compose smoke",
            "language": language,
            "adapter_type": adapter_type,
        },
        expected=201,
    )


def choose_worker(adapter_id: int, worker_id: int) -> dict[str, Any]:
    return request(
        "PATCH",
        f"/adapters/{adapter_id}",
        {"runtime_worker_id": worker_id},
    )


def save(
    adapter_id: int,
    code: str,
    *,
    requirements: str = "",
    runtime_config: dict[str, Any] | None = None,
    expected: int = 201,
) -> Any:
    return request(
        "POST",
        f"/adapters/{adapter_id}/versions",
        {
            "code": code,
            "requirements": requirements,
            "runtime_config": runtime_config or {},
        },
        expected=expected,
    )


def create_execution(adapter_id: int, input_: object, *, expected: int = 202) -> Any:
    return request(
        "POST",
        f"/adapters/{adapter_id}/executions",
        {"input": input_},
        expected=expected,
    )


def assert_locked(response: dict[str, Any]) -> None:
    assert response["detail"]["code"] == "adapter_runtime_locked", response


workers = request("GET", "/workers")
online_workers = [worker for worker in workers if worker["status"] == "online"]
assert online_workers, workers
runtime_worker = online_workers[0]
runtime_worker_id = runtime_worker["id"]
assert {"python", "javascript", "java"} <= set(runtime_worker["capabilities"]), runtime_worker

# The fresh Compose volume must represent the simplified Alembic head.
with SessionLocal() as session:
    inspector = inspect(session.bind)
    adapter_columns = {column["name"] for column in inspector.get_columns("adapters")}
    assert {
        "adapter_type",
        "run_mode",
        "latest_version_id",
        "runtime_worker_id",
        "archived_at",
    } <= adapter_columns
    assert {
        "published_version_id",
        "production_version_id",
        "production_worker_id",
        "production_state",
    }.isdisjoint(adapter_columns)
    index_names = {index["name"] for index in inspector.get_indexes("executions")}
    assert "uq_executions_active_adapter" in index_names
    webhook_columns = {
        column["name"]: column for column in inspector.get_columns("adapter_webhooks")
    }
    assert webhook_columns["credential_id"]["nullable"] is True
    webhook_indexes = {index["name"]: index for index in inspector.get_indexes("adapter_webhooks")}
    assert "uq_adapter_webhooks_enabled_public_id" in webhook_indexes

# A stored-online Worker whose heartbeat expired is unavailable without its
# stored status being rewritten.
stale = request(
    "POST",
    "/workers/register",
    {"name": "smoke-stale-worker", "capabilities": ["python"]},
    token=WORKER_TOKEN,
)
with SessionLocal.begin() as session:
    row = session.get(Worker, stale["id"])
    assert row is not None
    database_now = session.scalar(select(func.clock_timestamp()))
    assert isinstance(database_now, datetime)
    row.status = "online"
    row.last_heartbeat = database_now - timedelta(
        seconds=settings.worker_heartbeat_timeout_seconds + 1
    )
effective = next(worker for worker in request("GET", "/workers") if worker["id"] == stale["id"])
assert effective["status"] == "offline", effective
with SessionLocal() as session:
    row = session.get(Worker, stale["id"])
    assert row is not None and row.status == "online"

# Task foundation: immutable Revisions, fixed runtime Worker, latest execution,
# unified active lock, metadata exception, clone and soft delete.
task = create_adapter("smoke-m541-task", "python", "task")
task_id = task["id"]
assert (
    task["adapter_type"] == "task"
    and task["run_mode"] == "manual"
    and task["latest_version_id"] is None
)
offline = request(
    "PATCH",
    f"/adapters/{task_id}",
    {"runtime_worker_id": stale["id"]},
    expected=409,
)
assert offline["detail"]["code"] == "worker_offline", offline
choose_worker(task_id, runtime_worker_id)

credential = request(
    "POST",
    "/credentials",
    {"name": "smoke-runtime-secret", "type": "token", "fields": {"token": STORED_SECRET}},
    expected=201,
)
binding_payload = {
    "bindings": [
        {
            "env_key": "SMOKE_TOKEN",
            "credential_id": credential["id"],
            "field": "token",
        }
    ]
}
request("PUT", f"/adapters/{task_id}/credential-bindings", binding_payload)
v1_code = (
    "import hashlib\n"
    "import time\n\n"
    "def handle(context, input):\n"
    "    context.logger.info('任务开始')\n"
    "    try:\n"
    "        time.sleep(5)\n"
    "        token = context.secrets.get('SMOKE_TOKEN')\n"
    "        return {'revision': 1, 'input': input, 'secret_sha256': "
    "hashlib.sha256(token.encode()).hexdigest()}\n"
    "    finally:\n"
    "        context.logger.info('任务结束')\n"
)
v1 = save(task_id, v1_code, runtime_config={"revision": 1})
assert v1["seq"] == 1
run1 = create_execution(task_id, {"run": 1})
assert run1["version_id"] == v1["id"]
assert run1["target_worker_id"] == runtime_worker_id
busy = create_execution(task_id, {"run": "duplicate"}, expected=409)
assert busy["detail"]["code"] == "adapter_busy", busy
assert_locked(save(task_id, "def handle(context, input):\n    return 2\n", expected=409))
assert_locked(
    request(
        "PATCH",
        f"/adapters/{task_id}",
        {"runtime_worker_id": None},
        expected=409,
    )
)
assert_locked(
    request(
        "PUT",
        f"/adapters/{task_id}/credential-bindings",
        binding_payload,
        expected=409,
    )
)
assert_locked(request("DELETE", f"/adapters/{task_id}", expected=409))
metadata = request(
    "PATCH",
    f"/adapters/{task_id}",
    {"name": "smoke-m541-task-renamed", "description": "editable while running"},
)
assert metadata["runtime_locked"] is True

finished1 = wait_terminal(run1["id"])
assert finished1["status"] == "succeeded", finished1
assert finished1["version_id"] == v1["id"]
assert finished1["output"]["secret_sha256"] == hashlib.sha256(STORED_SECRET.encode()).hexdigest()
assert "任务开始" in finished1["stdout"] and "任务结束" in finished1["stdout"]
assert STORED_SECRET not in json.dumps(finished1)

v2_code = (
    "def handle(context, input):\n"
    "    context.logger.info('任务开始')\n"
    "    try:\n"
    "        return {'revision': 2, 'input': input}\n"
    "    finally:\n"
    "        context.logger.info('任务结束')\n"
)
v2 = save(task_id, v2_code, runtime_config={"revision": 2})
assert v2["seq"] == 2
assert request("GET", f"/adapters/{task_id}/versions/{v1['id']}")["code"] == v1_code
run2 = create_execution(task_id, {"run": 2})
assert run2["version_id"] == v2["id"]
finished2 = wait_terminal(run2["id"])
assert finished2["status"] == "succeeded" and finished2["output"]["revision"] == 2

request("PATCH", f"/adapters/{task_id}", {"run_mode": "schedule"})
schedule_payload = {
    "enabled": True,
    "cron": "* * * * *",
    "timezone": "UTC",
    "input": {"scheduled": True},
}
schedule = request("PUT", f"/adapters/{task_id}/schedule", schedule_payload)
assert schedule["enabled"] is True
assert request("GET", f"/adapters/{task_id}")["runtime_locked"] is True
assert_locked(save(task_id, v2_code, expected=409))
changed_schedule = dict(schedule_payload, cron="*/2 * * * *")
assert_locked(request("PUT", f"/adapters/{task_id}/schedule", changed_schedule, expected=409))

# Schedule mode keeps an independent Run Once action. It uses the latest
# Revision and must not mutate the Schedule cursor or enabled state.
schedule_before_manual = request("GET", f"/adapters/{task_id}/schedule")
manual_in_schedule_mode = create_execution(task_id, {"manual_in_schedule_mode": True})
manual_finished = wait_terminal(manual_in_schedule_mode["id"])
assert manual_finished["trigger"] == "manual" and manual_finished["version_id"] == v2["id"]
schedule_after_manual = request("GET", f"/adapters/{task_id}/schedule")
assert schedule_after_manual["enabled"] is True
assert schedule_after_manual["next_run_at"] == schedule_before_manual["next_run_at"]

scheduled_finished = wait_schedule_execution(task_id)
assert scheduled_finished["status"] == "succeeded", scheduled_finished
assert scheduled_finished["trigger"] == "schedule"
assert scheduled_finished["version_id"] == v2["id"]
assert scheduled_finished["target_worker_id"] == runtime_worker_id
assert "任务开始" in scheduled_finished["stdout"]
assert "任务结束" in scheduled_finished["stdout"]
disabled_schedule = dict(schedule_payload, enabled=False)
assert request("PUT", f"/adapters/{task_id}/schedule", disabled_schedule)["enabled"] is False

clone = request(
    "POST",
    f"/adapters/{task_id}/clone",
    {"name": "smoke-m542-task-clone"},
    expected=201,
)
assert (
    clone["adapter_type"] == "task"
    and clone["run_mode"] == "schedule"
    and clone["runtime_worker_id"] == runtime_worker_id
)
clone_versions = request("GET", f"/adapters/{clone['id']}/versions")
assert len(clone_versions) == 1 and clone_versions[0]["seq"] == 1
assert request("GET", f"/adapters/{clone['id']}/executions")["items"] == []
clone_schedule = request("GET", f"/adapters/{clone['id']}/schedule")
assert clone_schedule["enabled"] is False and clone_schedule["next_run_at"] is None

# Removed lifecycle routes cannot participate in new business behavior.
assert request("POST", f"/adapters/{task_id}/versions/{v2['id']}/publish", expected=404)
assert request("POST", f"/adapters/{task_id}/production/start", expected=404)
assert request("POST", f"/adapters/{task_id}/production/stop", {}, expected=404)

request("DELETE", f"/adapters/{task_id}", expected=204)
deleted = request("GET", f"/adapters/{task_id}")
assert deleted["archived_at"] is not None and deleted["latest_version_id"] == v2["id"]
assert len(request("GET", f"/adapters/{task_id}/versions")) == 2
deleted_history = request("GET", f"/adapters/{task_id}/executions")["items"]
assert len(deleted_history) == 4
assert [item["trigger"] for item in deleted_history].count("manual") == 3
assert [item["trigger"] for item in deleted_history].count("schedule") == 1
deleted_save = save(task_id, v2_code, expected=409)
assert deleted_save["detail"]["code"] == "adapter_deleted", deleted_save
with SessionLocal() as session:
    assert session.scalar(select(AdapterVersion).where(AdapterVersion.id == v1["id"])) is not None
    assert session.scalar(select(Execution).where(Execution.id == run1["id"])) is not None

# Final Webhook model: random stopped path, first-save Worker/Token gates,
# readable path, immediate Stop without cancelling active work, and Clone
# upgrade takeover while the public URL remains unchanged.
webhook_adapter = create_adapter("smoke-m543-webhook", "python", "webhook")
webhook_id = webhook_adapter["id"]
initial_webhook = request("GET", f"/adapters/{webhook_id}/webhook")
assert initial_webhook["enabled"] is False
assert initial_webhook["credential_id"] is None
assert len(initial_webhook["public_id"]) == 16
choose_worker(webhook_id, runtime_worker_id)
webhook_token = "smoke-webhook-token"
webhook_credential = request(
    "POST",
    "/credentials",
    {
        "name": "smoke-webhook-credential",
        "type": "token",
        "fields": {"token": webhook_token},
    },
    expected=201,
)
webhook_path = "receive-sys1-data"
webhook_config = request(
    "PUT",
    f"/adapters/{webhook_id}/webhook",
    {
        "enabled": False,
        "public_id": webhook_path,
        "credential_id": webhook_credential["id"],
    },
)
assert webhook_config["public_id"] == webhook_path
webhook_v1 = save(
    webhook_id,
    "import time\n\n"
    "def handle(context, input):\n"
    "    context.logger.info('收到 Webhook 请求')\n"
    "    try:\n"
    "        time.sleep(3)\n"
    "        return {'received': True, 'data': input}\n"
    "    finally:\n"
    "        context.logger.info('处理完 Webhook 请求')\n",
)
started = request(
    "PUT",
    f"/adapters/{webhook_id}/webhook",
    {
        "enabled": True,
        "public_id": webhook_path,
        "credential_id": webhook_credential["id"],
    },
)
assert started["enabled"] is True
assert request("GET", f"/adapters/{webhook_id}/executions")["items"] == []
unauthorized = hook(webhook_path, "wrong-token", {}, expected=401)
assert unauthorized["detail"]["code"] == "unauthorized", unauthorized
accepted1 = hook(webhook_path, webhook_token, {"event": 1})
busy_hook = hook(webhook_path, webhook_token, {"event": "duplicate"}, expected=409)
assert busy_hook["detail"]["code"] == "adapter_busy", busy_hook
assert_locked(save(webhook_id, "# blocked\n", expected=409))
assert_locked(
    request(
        "PUT",
        f"/adapters/{webhook_id}/webhook",
        {
            "enabled": True,
            "public_id": "blocked-path-change",
            "credential_id": webhook_credential["id"],
        },
        expected=409,
    )
)
webhook_metadata = request(
    "PATCH",
    f"/adapters/{webhook_id}",
    {"description": "metadata remains editable"},
)
assert webhook_metadata["runtime_locked"] is True
stopped_while_active = request(
    "PUT",
    f"/adapters/{webhook_id}/webhook",
    {
        "enabled": False,
        "public_id": webhook_path,
        "credential_id": webhook_credential["id"],
    },
)
assert stopped_while_active["enabled"] is False
assert request("GET", f"/adapters/{webhook_id}")["runtime_locked"] is True
assert hook(webhook_path, webhook_token, {}, expected=404)["detail"]["code"] == "webhook_not_found"
webhook_finished1 = wait_terminal(accepted1["execution_id"])
assert webhook_finished1["status"] == "succeeded", webhook_finished1
assert webhook_finished1["version_id"] == webhook_v1["id"]
assert webhook_finished1["target_worker_id"] == runtime_worker_id
assert webhook_finished1["output"] == {"received": True, "data": {"event": 1}}
assert "收到 Webhook 请求" in webhook_finished1["stdout"]
assert "处理完 Webhook 请求" in webhook_finished1["stdout"]
assert request("GET", f"/adapters/{webhook_id}")["runtime_locked"] is False
call_history = request("GET", f"/adapters/{webhook_id}/executions")["items"]
assert len(call_history) == 1 and call_history[0]["trigger"] == "webhook"

# Re-enable A, clone it to stopped B with the same URL/Token/Worker/current
# Revision, then prove only the running owner can receive until Stop A.
request(
    "PUT",
    f"/adapters/{webhook_id}/webhook",
    {
        "enabled": True,
        "public_id": webhook_path,
        "credential_id": webhook_credential["id"],
    },
)
webhook_clone = request(
    "POST",
    f"/adapters/{webhook_id}/clone",
    {"name": "smoke-m543-webhook-clone"},
    expected=201,
)
clone_webhook = request("GET", f"/adapters/{webhook_clone['id']}/webhook")
assert clone_webhook["public_id"] == webhook_path
assert clone_webhook["credential_id"] == webhook_credential["id"]
assert clone_webhook["enabled"] is False
assert webhook_clone["runtime_worker_id"] == runtime_worker_id
assert request("GET", f"/adapters/{webhook_clone['id']}/executions")["items"] == []
clone_v2 = save(
    webhook_clone["id"],
    "def handle(context, input):\n"
    "    context.logger.info('收到 Webhook 请求')\n"
    "    try:\n"
    "        return {'clone': True, 'data': input}\n"
    "    finally:\n"
    "        context.logger.info('处理完 Webhook 请求')\n",
)
assert clone_v2["seq"] == 2
clone_conflict = request(
    "PUT",
    f"/adapters/{webhook_clone['id']}/webhook",
    {
        "enabled": True,
        "public_id": webhook_path,
        "credential_id": webhook_credential["id"],
    },
    expected=409,
)
assert clone_conflict["detail"]["code"] == "webhook_path_in_use", clone_conflict
request(
    "PUT",
    f"/adapters/{webhook_id}/webhook",
    {
        "enabled": False,
        "public_id": webhook_path,
        "credential_id": webhook_credential["id"],
    },
)
request(
    "PUT",
    f"/adapters/{webhook_clone['id']}/webhook",
    {
        "enabled": True,
        "public_id": webhook_path,
        "credential_id": webhook_credential["id"],
    },
)
accepted2 = hook(webhook_path, webhook_token, {"event": 2})
webhook_finished2 = wait_terminal(accepted2["execution_id"])
assert webhook_finished2["version_id"] == clone_v2["id"]
assert webhook_finished2["output"] == {"clone": True, "data": {"event": 2}}
request(
    "PUT",
    f"/adapters/{webhook_clone['id']}/webhook",
    {
        "enabled": False,
        "public_id": webhook_path,
        "credential_id": webhook_credential["id"],
    },
)

wrong_schedule = request(
    "PUT",
    f"/adapters/{webhook_id}/schedule",
    schedule_payload,
    expected=409,
)
assert wrong_schedule["detail"]["code"] == "adapter_type_mismatch", wrong_schedule
wrong_webhook = request(
    "PUT",
    f"/adapters/{clone['id']}/webhook",
    {
        "enabled": False,
        "public_id": webhook_path,
        "credential_id": webhook_credential["id"],
    },
    expected=409,
)
assert wrong_webhook["detail"]["code"] == "adapter_type_mismatch", wrong_webhook

# Three-language runtime remains intact under the simplified lifecycle.
language_cases = {
    "javascript": (
        "export async function handle(context, input) {\n"
        "  context.logger.info('任务开始');\n"
        "  try { return {language: 'javascript', input}; }\n"
        "  finally { context.logger.info('任务结束'); }\n"
        "}\n"
    ),
    "java": (
        "import java.util.Map;\n"
        "public class Adapter {\n"
        "  public Object handle(Context context, Object input) {\n"
        '    context.logger.info("任务开始");\n'
        "    try {\n"
        '      return Map.of("language", "java", "input", input);\n'
        "    } finally {\n"
        '      context.logger.info("任务结束");\n'
        "    }\n"
        "  }\n"
        "}\n"
    ),
}
for language, code in language_cases.items():
    adapter = create_adapter(f"smoke-m541-{language}", language, "task")
    choose_worker(adapter["id"], runtime_worker_id)
    revision = save(adapter["id"], code)
    execution = create_execution(adapter["id"], {"language": language})
    assert execution["version_id"] == revision["id"]
    finished = wait_terminal(execution["id"], timeout=150)
    assert finished["status"] == "succeeded", finished
    assert finished["output"]["language"] == language, finished
    assert "任务开始" in finished["stdout"] and "任务结束" in finished["stdout"]

# AI remains browser-candidate-only and cannot mutate lifecycle facts.
setting = {
    "provider": "custom_openai_compatible",
    "base_url": AI_FAKE_BASE_URL,
    "model": "dlr-smoke-model",
    "credential_id": None,
    "reasoning_mode": "default",
    "reasoning_effort": None,
}
request("PUT", "/ai/settings", setting)
models = request(
    "POST",
    "/ai/models/refresh",
    {
        "provider": setting["provider"],
        "base_url": setting["base_url"],
        "credential_id": None,
    },
)
assert setting["model"] in models["models"], models
assert request("POST", "/ai/settings/test", setting)["ok"] is True

ai_adapter = create_adapter("smoke-m541-ai", "python", "task")
choose_worker(ai_adapter["id"], runtime_worker_id)
working_copy = {
    "code": "def handle(context, input):\n    return input\n",
    "requirements": "",
    "runtime_config": {"before_ai": True},
}
ai_revision = save(ai_adapter["id"], working_copy["code"], runtime_config={"before_ai": True})
before = request("GET", f"/adapters/{ai_adapter['id']}")
before_versions = request("GET", f"/adapters/{ai_adapter['id']}/versions")
assisted = request(
    "POST",
    f"/adapters/{ai_adapter['id']}/ai/assist",
    {
        "message": "Generate the deterministic python smoke Candidate.",
        "working_copy": working_copy,
        "recent_messages": [],
        "base_version_id": ai_revision["id"],
    },
)
assert assisted["candidate"] is not None, assisted
assert "SMOKE_REASONING_MUST_NOT_REACH_BROWSER" not in json.dumps(assisted)
after = request("GET", f"/adapters/{ai_adapter['id']}")
after_versions = request("GET", f"/adapters/{ai_adapter['id']}/versions")
assert after["latest_version_id"] == before["latest_version_id"]
assert after["runtime_worker_id"] == before["runtime_worker_id"]
assert after_versions == before_versions
assert request("GET", f"/adapters/{ai_adapter['id']}/executions")["items"] == []

# M5.5.2：模型刷新与连接测试相互独立；不支持的 /v1/models 有专属可行动中文错误。
disabled_models = request(
    "POST",
    "/ai/models/refresh",
    {
        "provider": setting["provider"],
        "base_url": AI_FAKE_DISABLED_BASE_URL,
        "credential_id": None,
    },
    expected=502,
)
assert disabled_models["detail"]["code"] == "ai_models_not_supported", disabled_models
assert "无法自动获取模型列表" in disabled_models["detail"]["message"], disabled_models
assert "可手工填写模型 ID" in disabled_models["detail"]["message"], disabled_models

# Test Connection 走 chat/completions，不因模型列表接口缺失而被判为不可用。
disabled_test = request(
    "POST",
    "/ai/settings/test",
    {
        **setting,
        "base_url": AI_FAKE_DISABLED_BASE_URL,
        "model": "manual-smoke-model",
    },
)
assert disabled_test["ok"] is True, disabled_test

# 刷新失败不影响手工 Model ID 路径：保存手工模型后仍可正常 assist。
manual = {
    **setting,
    "base_url": AI_FAKE_DISABLED_BASE_URL,
    "model": "manual-smoke-model",
}
request("PUT", "/ai/settings", manual)
assert request("GET", "/ai/settings")["model"] == "manual-smoke-model"
manual_assist = request(
    "POST",
    f"/adapters/{ai_adapter['id']}/ai/assist",
    {
        "message": "Generate the deterministic python smoke Candidate with the manual model id.",
        "working_copy": working_copy,
        "recent_messages": [],
        "base_version_id": ai_revision["id"],
    },
)
assert manual_assist["candidate"] is not None, manual_assist
assert manual_assist["model"] == "manual-smoke-model", manual_assist

print("M5.4.4 compose smoke passed")
