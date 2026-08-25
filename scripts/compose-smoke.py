"""M5.4.4 end-to-end smoke assertions executed inside the Control container."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, inspect, select, update

from dlr.common.config import settings
from dlr.control.db import SessionLocal
from dlr.control.models import AdapterSchedule, AdapterVersion, Execution, Worker

BASE = "http://web/api"
ADMIN_TOKEN = os.environ["DLR_ADMIN_TOKEN"]
WORKER_TOKEN = os.environ["DLR_WORKER_TOKEN"]
STORED_SECRET = os.environ["SMOKE_STORED_SECRET"]
AI_FAKE_BASE_URL = os.environ["AI_FAKE_BASE_URL"]
AI_FAKE_DISABLED_BASE_URL = os.environ["AI_FAKE_DISABLED_BASE_URL"]
AUDIT_PROMPT_SENTINEL = "SMOKE_AUDIT_PROMPT_MUST_NOT_PERSIST"

# M5.7 Wave B2: a real 1x1 PNG (magic bytes are sniffed server-side).
PNG_1PX_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8"
    "AAAAASUVORK5CYII="
)


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

# M5.5.8: the fresh deployment must keep Docker internal service-name
# resolution intact while the default DNS fallback is configured.
with open("/etc/resolv.conf", encoding="utf-8") as handle:
    resolv = handle.read()
assert "127.0.0.11" in resolv, f"embedded resolver missing from resolv.conf:\n{resolv}"
for host, port in (("postgres", 5432), ("control", 8000)):
    socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)

# M5.5.15: a fresh deployment starts with domestic + official sources for each
# kind, with the domestic source selected; deleting one and restoring is safe.
defaults = request("GET", "/package-sources/defaults")
assert defaults["pypi"]["index_url"] == "https://mirrors.aliyun.com/pypi/simple/"
assert defaults["npm"]["index_url"] == "https://registry.npmmirror.com/"
assert defaults["maven"]["index_url"] == "https://maven.aliyun.com/repository/public"
sources = request("GET", "/package-sources")
assert len(sources) == 6, sources
for kind in ("pypi", "npm", "maven"):
    kind_sources = [source for source in sources if source["kind"] == kind]
    assert len(kind_sources) == 2, kind_sources
    assert sum(source["is_default"] for source in kind_sources) == 1, kind_sources
    assert next(source for source in kind_sources if source["is_default"])["index_url"] == defaults[kind]["index_url"]
removed = next(source for source in sources if source["kind"] == "pypi" and source["is_default"])
request("DELETE", f"/package-sources/{removed['id']}", expected=204)
assert len(request("GET", "/package-sources")) == 5
restored = request("POST", f"/package-sources/defaults/{removed['kind']}")
assert restored["index_url"] == defaults[removed["kind"]]["index_url"], restored
assert restored["is_default"] is True, restored
assert len(request("GET", "/package-sources")) == 6

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

# M5.5.7: the demo Credentials are bootstrapped on fresh deployments with
# random values, metadata-only APIs, and brand-new Task Adapters default-bind
# PASSWORD to the demo Credential. Webhook entry authentication is a separate
# Bearer Token configuration and is never injected into code Credential
# bindings. The Control background bootstrap retries until the migrated schema
# exists, so poll for the rows instead of assuming they are present on the
# first request.
deadline = time.monotonic() + 60
credential_names: dict[str, dict[str, Any]] = {}
while time.monotonic() < deadline:
    credentials = request("GET", "/credentials")
    credential_names = {credential["name"]: credential for credential in credentials}
    if {"demo-passwd", "demo-token"} <= set(credential_names):
        break
    time.sleep(2)
assert {"demo-passwd", "demo-token"} <= set(credential_names), credential_names
assert credential_names["demo-passwd"]["type"] == "password"
assert credential_names["demo-token"]["type"] == "token"
for credential in credentials:
    assert "ciphertext" not in credential
    assert set(credential) == {"id", "name", "type", "created_at", "updated_at"}
demo_task = create_adapter("smoke-m557-demo-task", "python", "task")
demo_task_bindings = request("GET", f"/adapters/{demo_task['id']}/credential-bindings")
assert demo_task_bindings == [
    {
        "env_key": "PASSWORD",
        "credential_id": credential_names["demo-passwd"]["id"],
        "field": "password",
        "credential_name": "demo-passwd",
        "credential_type": "password",
    }
], demo_task_bindings
demo_webhook = create_adapter("smoke-m557-demo-webhook", "python", "webhook")
demo_webhook_bindings = request("GET", f"/adapters/{demo_webhook['id']}/credential-bindings")
assert demo_webhook_bindings == [], demo_webhook_bindings
demo_webhook_row = request("GET", f"/adapters/{demo_webhook['id']}/webhook")
assert demo_webhook_row["credential_id"] is None, demo_webhook_row
# The default demo binding really resolves at claim time: the Starter Code
# contract (context.secrets.get("PASSWORD")) reads the bound 32-hex value,
# and the Worker redaction contract keeps it out of stdout/output.
choose_worker(demo_task["id"], runtime_worker_id)
save(
    demo_task["id"],
    "import os\n\n"
    "def handle(context, input):\n"
    "    password = context.secrets.get('PASSWORD')\n"
    "    print('demo password: ' + str(password), flush=True)\n"
    "    return {'bound': password is not None, 'length': len(password or ''), "
    "'leaked': password}\n",
)
demo_run = create_execution(demo_task["id"], {})
demo_finished = wait_terminal(demo_run["id"])
assert demo_finished["status"] == "succeeded", demo_finished
assert demo_finished["output"]["bound"] is True
assert demo_finished["output"]["length"] == 32, demo_finished
assert demo_finished["output"]["leaked"] == "[REDACTED]", demo_finished
assert "demo password: [REDACTED]" in demo_finished["stdout"], demo_finished

# Task foundation: immutable Revisions, fixed runtime Worker, latest execution,
# unified active lock, metadata exception, clone and permanent delete.
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

# M5.5.11: Adapter-level single-run execution timeout. A short timeout really
# kills the user-code process and marks the Execution timeout; the timeout is
# copied by Clone and shared by manual and schedule runs.
timeout_task = create_adapter("smoke-m5511-timeout", "python", "task")
assert timeout_task["timeout_seconds"] == 300, timeout_task
request("PATCH", f"/adapters/{timeout_task['id']}", {"timeout_seconds": 2})
assert request("GET", f"/adapters/{timeout_task['id']}")["timeout_seconds"] == 2
choose_worker(timeout_task["id"], runtime_worker_id)
save(
    timeout_task["id"],
    "import time\n\n"
    "def handle(context, input):\n"
    "    time.sleep(60)\n"
    "    return {'never': True}\n",
)
timeout_run = create_execution(timeout_task["id"], {})
# Runtime lock semantics: the timeout is runtime configuration and cannot
# change while the Execution is pending/running.
assert_locked(
    request(
        "PATCH",
        f"/adapters/{timeout_task['id']}",
        {"timeout_seconds": 300},
        expected=409,
    )
)
timed_out = wait_terminal(timeout_run["id"], timeout=30)
assert timed_out["status"] == "timeout", timed_out
assert "timed out after 2s" in timed_out["error"], timed_out
assert timed_out["ended_at"] is not None and timed_out["duration_ms"] is not None
# Clone copies the authoritative timeout.
timeout_clone = request(
    "POST",
    f"/adapters/{timeout_task['id']}/clone",
    {"name": "smoke-m5511-timeout-clone"},
    expected=201,
)
assert timeout_clone["timeout_seconds"] == 2, timeout_clone
# Task schedule runs share the same Adapter-level timeout.
request("PATCH", f"/adapters/{timeout_task['id']}", {"run_mode": "schedule"})
request(
    "PUT",
    f"/adapters/{timeout_task['id']}/schedule",
    {"enabled": True, "cron": "* * * * *", "timezone": "UTC", "input": {}},
)
with SessionLocal() as session:
    session.execute(
        update(AdapterSchedule)
        .where(AdapterSchedule.adapter_id == timeout_task["id"])
        .values(next_run_at=func.now() - timedelta(seconds=1))
    )
    session.commit()
scheduled_timeout = wait_schedule_execution(timeout_task["id"], timeout=40)
assert scheduled_timeout["status"] == "timeout", scheduled_timeout
assert scheduled_timeout["trigger"] == "schedule", scheduled_timeout
assert "timed out after 2s" in scheduled_timeout["error"], scheduled_timeout
request(
    "PUT",
    f"/adapters/{timeout_task['id']}/schedule",
    {"enabled": False, "cron": "* * * * *", "timezone": "UTC", "input": {}},
)

# Removed lifecycle routes cannot participate in new business behavior.
assert request("POST", f"/adapters/{task_id}/versions/{v2['id']}/publish", expected=404)
assert request("POST", f"/adapters/{task_id}/production/start", expected=404)
assert request("POST", f"/adapters/{task_id}/production/stop", {}, expected=404)

request("DELETE", f"/adapters/{task_id}", expected=204)
assert request("GET", f"/adapters/{task_id}", expected=404)["detail"]["code"] == "adapter_not_found"
deleted_save = save(task_id, v2_code, expected=404)
assert deleted_save["detail"]["code"] == "adapter_not_found", deleted_save
with SessionLocal() as session:
    assert session.scalar(select(AdapterVersion).where(AdapterVersion.id == v1["id"])) is None
    assert session.scalar(select(AdapterVersion).where(AdapterVersion.id == v2["id"])) is None
    assert session.scalar(select(Execution).where(Execution.id == run1["id"])) is None

# Final Webhook model: random stopped path, saved Revision decoupled from the
# Worker/Token start gate, readable path, immediate Stop without cancelling
# active work, and Clone upgrade takeover while the public URL remains
# unchanged.
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
# M5.5.13：上下文以有序多片段快照（代码 + 已脱敏日志文本，各带 source 与
# 1-based 行范围）随请求发送；日志片段只携带浏览器可见的 [REDACTED] 文本。
selected_text = os.environ.get(
    "SMOKE_SELECTED_TEXT", "def handle(context, input):"
)
log_text = os.environ.get(
    "SMOKE_LOG_TEXT",
    "[2026-08-17 10:21:03] [ERROR] token [REDACTED] failed\n"
    "[2026-08-17 10:21:08] retry ok\n",
)
assisted = request(
    "POST",
    f"/adapters/{ai_adapter['id']}/ai/assist",
    {
        "message": "Generate the deterministic python smoke Candidate.",
        "working_copy": working_copy,
        "recent_messages": [],
        "base_version_id": ai_revision["id"],
        "context_snippets": [
            {
                "source": "code",
                "text": selected_text,
                "start_line": 1,
                "end_line": 1,
            },
            {
                "source": "log",
                "text": log_text,
                "start_line": 1,
                "end_line": 2,
            },
        ],
    },
)
assert assisted["candidate"] is not None, assisted
# 选区块与脱敏日志片段真实到达 Provider（fake 在 message 中回显确认），且
# Provider 的 hidden reasoning 哨兵永不返回浏览器。
assert "with selected context" in assisted["message"], assisted
assert "with log snippet" in assisted["message"], assisted
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
# 反向判别：不带上下文片段的请求不得触发 fake 的 "with selected context"
# 回显，证明检测只对真实携带 context_snippets 的请求生效。
assert "with selected context" not in manual_assist["message"], manual_assist

# M5.7 Wave B2：附件服务端合同。附件正文只进入本轮 Provider 请求（fake
# 回显确认），绝不进入浏览器响应、服务日志或数据库；图片只在能力表明确
# 支持时走 Provider 原生 payload，否则给稳定可行动错误，绝不伪装 OCR。
attach_text = os.environ.get("SMOKE_ATTACH_TEXT", "smoke-attach-sentinel")
attach_assisted = request(
    "POST",
    f"/adapters/{ai_adapter['id']}/ai/assist",
    {
        "message": "Generate the deterministic python smoke Candidate with the attachment.",
        "working_copy": working_copy,
        "recent_messages": [],
        "base_version_id": ai_revision["id"],
        "attachments": [
            {
                "filename": "smoke-notes.txt",
                "content_type": "text/plain",
                "data_base64": base64.b64encode(attach_text.encode()).decode(),
            }
        ],
    },
)
assert attach_assisted["candidate"] is not None, attach_assisted
# fake 回显 "with attachment" 证明解析后的附件文本真实到达 Provider。
assert "with attachment" in attach_assisted["message"], attach_assisted
# 附件原文与 hidden reasoning 哨兵永不返回浏览器。
assert attach_text not in json.dumps(attach_assisted), attach_assisted
assert "SMOKE_REASONING_MUST_NOT_REACH_BROWSER" not in json.dumps(attach_assisted)

# custom_openai_compatible 能力表不支持图片：必须稳定拒绝且不调用 Provider。
image_blocked = request(
    "POST",
    f"/adapters/{ai_adapter['id']}/ai/assist",
    {
        "message": "Look at the image.",
        "working_copy": working_copy,
        "recent_messages": [],
        "attachments": [
            {
                "filename": "smoke.png",
                "content_type": "image/png",
                "data_base64": PNG_1PX_BASE64,
            }
        ],
    },
    expected=422,
)
assert image_blocked["detail"]["code"] == "ai_attachment_image_unsupported", image_blocked
assert "更换支持图片的模型" in image_blocked["detail"]["message"], image_blocked

# openai 能力表明确支持原生图片：fake 收到 content 数组形式的 image_url 部
# 分并回显 "with native image"；随后恢复 custom 设置。
openai_setting = {
    "provider": "openai",
    "base_url": AI_FAKE_BASE_URL,
    "model": "dlr-smoke-model",
    "credential_id": None,
    "reasoning_mode": "default",
    "reasoning_effort": None,
}
request("PUT", "/ai/settings", openai_setting)
image_assisted = request(
    "POST",
    f"/adapters/{ai_adapter['id']}/ai/assist",
    {
        "message": "Describe the image.",
        "working_copy": working_copy,
        "recent_messages": [],
        "attachments": [
            {
                "filename": "smoke.png",
                "content_type": "image/png",
                "data_base64": PNG_1PX_BASE64,
            }
        ],
    },
)
assert image_assisted["candidate"] is not None, image_assisted
assert "with native image" in image_assisted["message"], image_assisted
assert PNG_1PX_BASE64 not in json.dumps(image_assisted), image_assisted
assert "SMOKE_REASONING_MUST_NOT_REACH_BROWSER" not in json.dumps(image_assisted)
request("PUT", "/ai/settings", setting)

# M5.7 Wave C1: the controlled read-only Tool Call chain. The fake Provider
# only enters a tool scenario when the DLR whitelist really reached it and the
# user message carries the scenario marker; each scenario proves one part of
# the bounded loop: tool call -> DLR executes -> sanitized tool result returns
# on the same non-streaming chain -> final strict AiModelOutput.
tool_single = request(
    "POST",
    f"/adapters/{ai_adapter['id']}/ai/assist",
    {
        "message": (
            f"SMOKE_TOOL_SINGLE {AUDIT_PROMPT_SENTINEL} "
            "Generate the deterministic python smoke Candidate."
        ),
        "working_copy": working_copy,
        "recent_messages": [],
        "base_version_id": ai_revision["id"],
    },
)
assert tool_single["candidate"] is not None, tool_single
assert "with tool result" in tool_single["message"], tool_single
assert "with docs source" in tool_single["message"], tool_single
assert len(tool_single["tool_calls"]) == 1, tool_single
single_summary = tool_single["tool_calls"][0]
assert single_summary["tool_name"] == "dlr_docs_list", single_summary
assert single_summary["status"] == "success", single_summary
assert single_summary["error_code"] is None, single_summary
assert single_summary["source"] == "dlr-docs:v1:runtime-contract-python", single_summary
assert "dlr-docs:v1" in single_summary["result_summary"], single_summary
assert single_summary["result_truncated"] is False, single_summary
# hidden reasoning 哨兵与附件正文/Secret 永不进入工具摘要或浏览器响应。
assert "SMOKE_REASONING_MUST_NOT_REACH_BROWSER" not in json.dumps(tool_single)

tool_multi = request(
    "POST",
    f"/adapters/{ai_adapter['id']}/ai/assist",
    {
        "message": "SMOKE_TOOL_MULTI Generate the deterministic python smoke Candidate.",
        "working_copy": working_copy,
        "recent_messages": [],
        "base_version_id": ai_revision["id"],
    },
)
assert tool_multi["candidate"] is not None, tool_multi
assert len(tool_multi["tool_calls"]) == 2, tool_multi
assert [item["tool_name"] for item in tool_multi["tool_calls"]] == [
    "dlr_docs_list",
    "dlr_docs_search",
], tool_multi
assert all(item["status"] == "success" for item in tool_multi["tool_calls"]), tool_multi
assert tool_multi["tool_calls"][1]["source"] == "dlr-docs:v1:secrets-and-bindings", tool_multi

# 非白名单工具被安全拒绝：错误摘要返回浏览器，模型仍产出最终合法 JSON。
tool_unknown = request(
    "POST",
    f"/adapters/{ai_adapter['id']}/ai/assist",
    {
        "message": "SMOKE_TOOL_UNKNOWN Generate the deterministic python smoke Candidate.",
        "working_copy": working_copy,
        "recent_messages": [],
        "base_version_id": ai_revision["id"],
    },
)
assert tool_unknown["candidate"] is not None, tool_unknown
assert "with rejected tool" in tool_unknown["message"], tool_unknown
assert tool_unknown["tool_calls"][0]["tool_name"] == "not_registered_tool", tool_unknown
assert tool_unknown["tool_calls"][0]["status"] == "error", tool_unknown
assert tool_unknown["tool_calls"][0]["error_code"] == "ai_tool_unknown", tool_unknown

# 写操作风格工具同样被拒绝（不在白名单，永不执行）。
tool_write = request(
    "POST",
    f"/adapters/{ai_adapter['id']}/ai/assist",
    {
        "message": "SMOKE_TOOL_WRITE Generate the deterministic python smoke Candidate.",
        "working_copy": working_copy,
        "recent_messages": [],
        "base_version_id": ai_revision["id"],
    },
)
assert tool_write["candidate"] is not None, tool_write
assert "with write tool rejected" in tool_write["message"], tool_write
assert tool_write["tool_calls"][0]["status"] == "error", tool_write
assert tool_write["tool_calls"][0]["error_code"] == "ai_tool_unknown", tool_write

# 循环模型每轮使用不同的合法查询，证明第 8 轮完成后会禁用工具并强制最终化，
# 不再沿用旧合同以 ai_tool_limit_exceeded 502 结束。
tool_round_budget = request(
    "POST",
    f"/adapters/{ai_adapter['id']}/ai/assist",
    {
        "message": "SMOKE_TOOL_LOOP Generate the deterministic python smoke Candidate.",
        "working_copy": working_copy,
        "recent_messages": [],
        "base_version_id": ai_revision["id"],
    },
)
assert tool_round_budget["candidate"] is not None, tool_round_budget
assert len(tool_round_budget["tool_calls"]) == 8, tool_round_budget
assert all(
    item["tool_name"] == "dlr_docs_search" and item["status"] == "success"
    for item in tool_round_budget["tool_calls"]
), tool_round_budget

# 每轮四个不同查询，在第 16 次实际调用后触发调用预算并正常最终化；不得执行
# 第 17 次调用，也不得把预算触顶单独返回为 502。
tool_call_budget = request(
    "POST",
    f"/adapters/{ai_adapter['id']}/ai/assist",
    {
        "message": "SMOKE_TOOL_CALL_BUDGET Generate the deterministic python smoke Candidate.",
        "working_copy": working_copy,
        "recent_messages": [],
        "base_version_id": ai_revision["id"],
    },
)
assert tool_call_budget["candidate"] is not None, tool_call_budget
assert len(tool_call_budget["tool_calls"]) == 16, tool_call_budget
assert all(
    item["tool_name"] == "dlr_docs_search" and item["status"] == "success"
    for item in tool_call_budget["tool_calls"]
), tool_call_budget

# 工具调用不产生任何生命周期副作用。
after_tools = request("GET", f"/adapters/{ai_adapter['id']}")
assert after_tools["latest_version_id"] == before["latest_version_id"], after_tools
assert request("GET", f"/adapters/{ai_adapter['id']}/executions")["items"] == []

# M5.7 Wave C2: the read-only KnowledgeSource chain against the fake official
# ima service implementing the OFFICIAL ima OpenAPI contract (base path
# /openapi/wiki/v1, auth headers ima-openapi-clientid / ima-openapi-apikey,
# envelope {code, msg, data}). The credential is stored through the normal
# DLR Secret Store API as an access_key Credential (access_key_id -> Client
# ID, access_key_secret -> API Key); the fake rejects requests without the
# exact headers and echoes the API Key inside the read content on purpose, so
# the smoke proves: list -> search -> read -> final AiModelOutput, the ima:v1
# source identifiers, and by-value credential redaction (token never reaches
# the browser response or the model chain). The target knowledge base is
# matched by NAME only ("DLR接口库"); its id/content are never recorded.
ima_token = os.environ["SMOKE_IMA_TOKEN"]
ima_client_id = os.environ["SMOKE_IMA_CLIENT_ID"]
ima_credential = request(
    "POST",
    "/credentials",
    {
        "name": "smoke-ima",
        "type": "access_key",
        "fields": {
            "access_key_id": ima_client_id,
            "access_key_secret": ima_token,
        },
    },
    expected=201,
)
assert ima_credential["name"] == "smoke-ima", ima_credential
knowledge = request(
    "POST",
    f"/adapters/{ai_adapter['id']}/ai/assist",
    {
        "message": "SMOKE_KNOWLEDGE Generate the deterministic python smoke Candidate.",
        "working_copy": working_copy,
        "recent_messages": [],
        "base_version_id": ai_revision["id"],
        "knowledge_search_enabled": True,
    },
)
assert knowledge["candidate"] is not None, knowledge
assert "with knowledge result" in knowledge["message"], knowledge
assert len(knowledge["tool_calls"]) == 3, knowledge
assert [item["tool_name"] for item in knowledge["tool_calls"]] == [
    "list_knowledge_bases",
    "search_knowledge",
    "read_knowledge",
], knowledge
assert all(item["status"] == "success" for item in knowledge["tool_calls"]), knowledge
# The test knowledge base is matched by NAME ("DLR接口库"); only the name is
# asserted here, never its id or content.
assert knowledge["tool_calls"][0]["source"] == "ima:v1:dlr-interface-lib", knowledge
assert "DLR接口库" in knowledge["tool_calls"][0]["result_summary"], knowledge
# The "secrets" query returns the notes-backed credential-safety item.
assert knowledge["tool_calls"][1]["source"] == "ima:v1:kb-item-2", knowledge
# The read goes through the official notes branch (get_doc_content).
assert knowledge["tool_calls"][2]["source"] == "ima:v1:kb-item-2", knowledge
# The read content echoed the credential token; the tools layer redacted it
# by value before it could reach the browser.
serialized = json.dumps(knowledge, ensure_ascii=False)
assert ima_token not in serialized, knowledge
assert ima_client_id not in serialized, knowledge
assert "[REDACTED]" in serialized, knowledge
# hidden reasoning 哨兵与附件正文/Secret 永不进入知识工具摘要或浏览器响应。
assert "SMOKE_REASONING_MUST_NOT_REACH_BROWSER" not in serialized
# 知识工具调用同样不产生任何生命周期副作用。
after_knowledge = request("GET", f"/adapters/{ai_adapter['id']}")
assert after_knowledge["latest_version_id"] == before["latest_version_id"], after_knowledge
assert request("GET", f"/adapters/{ai_adapter['id']}/executions")["items"] == []

# The dedicated audit stream is persisted in Control's platform-log mount.
# Exercise both the current and rotated JSONL files with the small smoke-only
# rotation limit, then pin the non-content schema and literal secrecy boundary.
audit_current = (
    Path(os.environ["DLR_PLATFORM_LOG_ROOT"]) / "control" / "ai-tool-audit.jsonl"
)
audit_files = sorted(audit_current.parent.glob(f"{audit_current.name}*"))
assert audit_current in audit_files, audit_files
assert any(path != audit_current for path in audit_files), audit_files

audit_common_fields = {
    "timestamp",
    "schema_version",
    "event_type",
    "request_id",
    "conversation_id",
    "adapter_id",
    "round",
    "call_index",
    "tool",
    "args_summary",
    "status",
    "duration_ms",
    "result_size",
    "result_truncated",
    "error_code",
    "stop_reason",
}
audit_event_fields = {
    "tool_attempt": audit_common_fields,
    "guard": audit_common_fields,
    "request_terminal": audit_common_fields
    | {"successful_calls", "failed_calls", "blocked_calls"},
}
audit_statuses = {
    "tool_attempt": {"success", "error", "blocked"},
    "guard": {"blocked"},
    "request_terminal": {"success", "stopped", "error"},
}
audit_records: list[dict[str, Any]] = []
audit_text = ""
request_conversations: dict[str, str] = {}
for audit_path in audit_files:
    assert audit_path.is_file(), audit_path
    raw = audit_path.read_text(encoding="utf-8")
    audit_text += raw
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise AssertionError(f"{audit_path}:{line_number}: invalid JSONL") from error
        assert isinstance(record, dict), (audit_path, line_number, record)
        event_type = record.get("event_type")
        assert event_type in audit_event_fields, (audit_path, line_number, record)
        assert set(record) == audit_event_fields[event_type], (
            audit_path,
            line_number,
            record,
        )
        assert record["schema_version"] == 1, record
        assert isinstance(record["timestamp"], str) and record["timestamp"].endswith("Z"), record
        assert type(record["adapter_id"]) is int and record["adapter_id"] > 0, record
        assert type(record["round"]) is int and record["round"] >= 0, record
        assert type(record["call_index"]) is int and record["call_index"] >= 0, record
        assert isinstance(record["args_summary"], dict), record
        assert record["status"] in audit_statuses[event_type], record
        assert type(record["duration_ms"]) is int and record["duration_ms"] >= 0, record
        assert type(record["result_size"]) is int and record["result_size"] >= 0, record
        assert isinstance(record["result_truncated"], bool), record
        assert record["error_code"] is None or isinstance(record["error_code"], str), record
        assert record["stop_reason"] is None or isinstance(record["stop_reason"], str), record
        if event_type == "tool_attempt":
            assert isinstance(record["tool"], str) and record["tool"], record
        else:
            assert record["tool"] is None, record

        for identifier_field in ("request_id", "conversation_id"):
            identifier = record[identifier_field]
            assert isinstance(identifier, str), record
            parsed_identifier = uuid.UUID(identifier)
            assert parsed_identifier.version == 4 and str(parsed_identifier) == identifier, record
        request_id = record["request_id"]
        conversation_id = record["conversation_id"]
        previous_conversation = request_conversations.setdefault(request_id, conversation_id)
        assert previous_conversation == conversation_id, record

        if event_type == "request_terminal":
            for count_field in ("successful_calls", "failed_calls", "blocked_calls"):
                assert type(record[count_field]) is int and record[count_field] >= 0, record
        audit_records.append(record)

assert audit_records, audit_files
assert any(record["event_type"] == "request_terminal" for record in audit_records), audit_records

audit_sensitive_values = {
    "admin token": ADMIN_TOKEN,
    "worker token": WORKER_TOKEN,
    "stored credential": STORED_SECRET,
    "selected code": selected_text,
    "log snippet": log_text,
    "attachment body": attach_text,
    "ima API key": ima_token,
    "ima client ID": ima_client_id,
    "Provider reasoning": "SMOKE_REASONING_MUST_NOT_REACH_BROWSER",
    "Assist Prompt": AUDIT_PROMPT_SENTINEL,
}
for sensitive_name, sensitive_value in audit_sensitive_values.items():
    assert sensitive_value not in audit_text, f"{sensitive_name} leaked into AI tool audit"

print("M5.4.4 compose smoke passed")
