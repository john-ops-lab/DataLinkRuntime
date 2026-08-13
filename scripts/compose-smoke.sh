#!/usr/bin/env bash
# Minimal compose smoke test.
#
# Runs in an ISOLATED compose project (dlr-smoke-*) with a dedicated host
# port, so it never shares containers or volumes with the normal development
# stack and cleanup can only touch the smoke project.
#
# Steps: build & start all services, run the Alembic migration as soon as
# PostgreSQL accepts connections (the worker healthcheck needs the M2 tables
# to register), wait until every service is healthy, verify the web page,
# the /api/health proxy path and 401 auth rejection, run the full M1 Adapter
# management chain with the admin token, then run the M2/M3 execution loop
# (Manual Execution -> worker claim -> venv -> subprocess -> live logs over
# SSE -> succeeded -> history list/detail), then run the M3.2 production
# lifecycle chain (credentials/package sources -> production worker ->
# publish gate -> publish -> start -> stop(terminate) -> cancelled ->
# start again -> succeeded with a bound secret -> archive/restore) before
# the M3.3 three-language lifecycle. Finally, it starts a temporary local
# OpenAI-compatible fake Provider outside the production Compose topology and
# proves the M4 settings/models/assist chain cannot change lifecycle facts,
# before tearing down only the smoke project.
set -euo pipefail

cd "$(dirname "$0")/.."

SERVICES=(postgres control worker web)
TIMEOUT_SECONDS=${COMPOSE_SMOKE_TIMEOUT:-240}

# Isolated compose project: unique on CI via GITHUB_RUN_ID, locally via PID.
export COMPOSE_PROJECT_NAME=${COMPOSE_SMOKE_PROJECT:-dlr-smoke-${GITHUB_RUN_ID:-$$}}
# Dedicated host port for the smoke web service, so a running dev stack
# (default 8080) cannot conflict with the smoke run.
export DLR_WEB_HOST_PORT=${COMPOSE_SMOKE_WEB_PORT:-8880}

# Smoke credentials: generated per run unless the caller provides them, so the
# compose file never needs hardcoded production-usable tokens.
export DLR_ADMIN_TOKEN=${DLR_ADMIN_TOKEN:-smoke-admin-token-$$}
export DLR_WORKER_TOKEN=${DLR_WORKER_TOKEN:-smoke-worker-token-$$}
export DLR_SECRET_SMOKE=${DLR_SECRET_SMOKE:-smoke-secret-$$}
# M3.2 Secret Store master key: Control derives the Fernet key from it
# (HKDF-SHA256); without it the credential APIs answer 503.
export DLR_MASTER_KEY=${DLR_MASTER_KEY:-smoke-master-key-$$}

# Filled only if the M4 one-off fake Provider starts. Keeping its exact ID lets
# cleanup remove only the container created by this smoke run.
AI_FAKE_CONTAINER_ID=""

cleanup() {
  if [ -n "$AI_FAKE_CONTAINER_ID" ]; then
    docker rm -f "$AI_FAKE_CONTAINER_ID" >/dev/null 2>&1 || true
  fi
  # Only touch the smoke project; never the default development project.
  docker compose -p "$COMPOSE_PROJECT_NAME" down --volumes --remove-orphans
}
trap cleanup EXIT

echo "==> smoke project: $COMPOSE_PROJECT_NAME (web host port: $DLR_WEB_HOST_PORT)"

echo "==> docker compose build"
docker compose build

echo "==> docker compose up -d"
docker compose up -d

echo "==> waiting for postgres to accept connections"
elapsed=0
while true; do
  pg_container=$(docker compose ps -q postgres)
  pg_health=""
  if [ -n "$pg_container" ]; then
    pg_health=$(docker inspect --format '{{.State.Health.Status}}' "$pg_container" 2>/dev/null || echo "")
  fi
  if [ "$pg_health" = "healthy" ]; then
    break
  fi
  if [ "$elapsed" -ge "$TIMEOUT_SECONDS" ]; then
    echo "ERROR: postgres not healthy within ${TIMEOUT_SECONDS}s" >&2
    docker compose ps
    docker compose logs --tail 30 postgres
    exit 1
  fi
  sleep 5
  elapsed=$((elapsed + 5))
done

echo "==> running alembic upgrade head against the smoke PostgreSQL"
# Must run before the healthy-wait: the worker's healthcheck needs a
# successful registration, which requires the M2 tables to exist.
docker compose run --rm control alembic upgrade head

echo "==> waiting for all services to become healthy (timeout ${TIMEOUT_SECONDS}s)"
elapsed=0
while true; do
  healthy_count=0
  for service in "${SERVICES[@]}"; do
    container_id=$(docker compose ps -q "$service")
    health=""
    if [ -n "$container_id" ]; then
      health=$(docker inspect --format '{{.State.Health.Status}}' "$container_id" 2>/dev/null || echo "")
    fi
    if [ "$health" = "healthy" ]; then
      healthy_count=$((healthy_count + 1))
    fi
  done
  if [ "$healthy_count" -eq "${#SERVICES[@]}" ]; then
    break
  fi
  if [ "$elapsed" -ge "$TIMEOUT_SECONDS" ]; then
    echo "ERROR: services not healthy within ${TIMEOUT_SECONDS}s" >&2
    docker compose ps
    for service in "${SERVICES[@]}"; do
      echo "--- logs: $service ---"
      docker compose logs --tail 30 "$service"
    done
    exit 1
  fi
  sleep 5
  elapsed=$((elapsed + 5))
done
echo "all services healthy"

echo "==> checking web page"
curl -fsS "http://localhost:${DLR_WEB_HOST_PORT}/" | grep -q "DataLinkRuntime"
echo "web page ok"

echo "==> checking /api/health via web/nginx"
health_response=$(curl -fsS "http://localhost:${DLR_WEB_HOST_PORT}/api/health")
echo "health response: $health_response"
echo "$health_response" | grep -q '"status":"ok"'
echo "$health_response" | grep -q '"database":true'
echo "control + database ok"

echo "==> checking protected APIs reject missing and wrong tokens with 401"
no_token_status=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${DLR_WEB_HOST_PORT}/api/adapters")
[ "$no_token_status" = "401" ] || { echo "ERROR: expected 401 without token, got $no_token_status" >&2; exit 1; }
wrong_token_status=$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer ${DLR_WORKER_TOKEN}" "http://localhost:${DLR_WEB_HOST_PORT}/api/adapters")
[ "$wrong_token_status" = "401" ] || { echo "ERROR: worker token must not access admin API, got $wrong_token_status" >&2; exit 1; }
echo "auth 401 checks ok"

echo "==> running M4.1 stale Worker effective-online smoke"
# Register a synthetic Worker with no backing Agent, then backdate its heartbeat
# directly in PostgreSQL. This proves the timeout contract without shortening the
# deployment timeout or sleeping through a real heartbeat interval.
docker compose exec -T -e DLR_ADMIN_TOKEN -e DLR_WORKER_TOKEN control python - <<'PY'
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from sqlalchemy import func, select

from dlr.common.config import settings
from dlr.control.db import SessionLocal
from dlr.control.models import Execution, Worker

BASE = "http://web/api"
ADMIN_TOKEN = os.environ["DLR_ADMIN_TOKEN"]
WORKER_TOKEN = os.environ["DLR_WORKER_TOKEN"]


def request(method, path, payload=None, expected=200, token=ADMIN_TOKEN):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read()
    body = json.loads(raw) if raw else None
    assert status == expected, f"{method} {path}: expected {expected}, got {status}: {body}"
    return body


def check(condition, message):
    assert condition, message


worker = request(
    "POST",
    "/workers/register",
    {"name": "smoke-m41-stale-worker", "capabilities": ["python"]},
    token=WORKER_TOKEN,
)
worker_id = worker["id"]

with SessionLocal.begin() as session:
    stored_worker = session.get(Worker, worker_id)
    check(stored_worker is not None, "synthetic Worker must be persisted")
    database_now = session.scalar(select(func.clock_timestamp()))
    check(isinstance(database_now, datetime), "database must return a current timestamp")
    stored_worker.status = "online"
    stored_worker.last_heartbeat = database_now - timedelta(
        seconds=settings.worker_heartbeat_timeout_seconds + 1
    )

workers = request("GET", "/workers")
effective_worker = next(item for item in workers if item["id"] == worker_id)
check(
    effective_worker["status"] == "offline",
    f"stale stored-online Worker must be exposed as offline, got {effective_worker}",
)

with SessionLocal() as session:
    stored_worker = session.get(Worker, worker_id)
    check(stored_worker is not None, "synthetic Worker must still exist")
    check(
        stored_worker.status == "online",
        "effective status serialization must not rewrite the stored Worker status",
    )

adapter = request(
    "POST",
    "/adapters",
    {"name": "smoke-m41-stale-adapter", "language": "python"},
    expected=201,
)
adapter_id = adapter["id"]
version = request(
    "POST",
    f"/adapters/{adapter_id}/versions",
    {
        "code": "def handle(context, input):\n    return input\n",
        "requirements": "",
        "runtime_config": {"stage": "m4.1-smoke"},
    },
    expected=201,
)
configured = request(
    "PATCH",
    f"/adapters/{adapter_id}",
    {"production_worker_id": worker_id},
)
check(
    configured["production_worker_id"] == worker_id,
    "an effective-offline compatible Worker must remain configurable",
)

blocked = request(
    "POST",
    f"/adapters/{adapter_id}/executions",
    {"version_id": version["id"], "input": {"smoke": "m4.1"}},
    expected=409,
)
check(
    blocked["detail"]["code"] == "worker_offline",
    f"stale configured Worker must block Manual Test with worker_offline, got {blocked}",
)
history = request("GET", f"/adapters/{adapter_id}/executions?limit=50")
check(history["items"] == [], "blocked Manual Test must not create execution history")

with SessionLocal() as session:
    execution_count = session.scalar(
        select(func.count(Execution.id)).where(Execution.adapter_id == adapter_id)
    )
    check(execution_count == 0, "blocked Manual Test must not persist an Execution")

print("M4.1 stale Worker effective-online smoke passed")
PY

echo "==> running M1 adapter management chain (via web/nginx, real PostgreSQL)"
# Uses the Python standard library inside the control container, so the host
# needs no extra tooling (no jq, no extra python packages).
docker compose exec -T -e DLR_ADMIN_TOKEN control python - <<'PY'
import json
import os
import urllib.error
import urllib.request

BASE = "http://web/api"
ADMIN_TOKEN = os.environ["DLR_ADMIN_TOKEN"]


def request(method, path, payload=None, expected=200, token=ADMIN_TOKEN):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read()
    body = json.loads(raw) if raw else None
    assert status == expected, f"{method} {path}: expected {expected}, got {status}: {body}"
    return body


def check(condition, message):
    assert condition, message


adapter = request(
    "POST", "/adapters", {"name": "smoke-adapter", "description": "smoke"}, expected=201
)
adapter_id = adapter["id"]
check(
    adapter["latest_version_id"] is None and adapter["published_version_id"] is None,
    "new adapter must start without version pointers",
)

fetched = request("GET", f"/adapters/{adapter_id}")
check(fetched["name"] == "smoke-adapter", "get adapter returns the created adapter")

patched = request("PATCH", f"/adapters/{adapter_id}", {"description": "smoke updated"})
check(patched["description"] == "smoke updated", "patch updates adapter metadata")

v1 = request(
    "POST",
    f"/adapters/{adapter_id}/versions",
    {
        "code": "def handle(context, input):\n    return input\n",
        "requirements": "",
        "runtime_config": {"stage": "v1"},
    },
    expected=201,
)
v2 = request(
    "POST",
    f"/adapters/{adapter_id}/versions",
    {
        "code": "def handle(context, input):\n    return {'v': 2}\n",
        "requirements": "requests",
        "runtime_config": {"stage": "v2"},
    },
    expected=201,
)
check(v1["seq"] == 1 and v2["seq"] == 2, "version seq increments from 1")

adapter = request("GET", f"/adapters/{adapter_id}")
check(adapter["latest_version_id"] == v2["id"], "latest points to the newest version")

# M3.2 contract change: publish is now gate-enforced server-side. Without a
# production worker the gate locks, which is exactly what M1 asserts here; the
# full gate -> publish chain is exercised by the M3.2 smoke chain below.
gated = request("POST", f"/adapters/{adapter_id}/versions/{v1['id']}/publish", expected=409)
check(
    gated["detail"]["code"] == "publish_gate_locked",
    f"publish without a production worker must be gated, got {gated}",
)
check(
    request("GET", f"/adapters/{adapter_id}")["published_version_id"] is None,
    "a rejected publish must not move the published pointer",
)

versions = request("GET", f"/adapters/{adapter_id}/versions")
check([version["seq"] for version in versions] == [2, 1], "version list sorted by seq desc")

detail = request("GET", f"/adapters/{adapter_id}/versions/{v2['id']}")
check(
    detail["runtime_config"] == {"stage": "v2"} and detail["requirements"] == "requests",
    "version detail returns the immutable snapshot (JSONB round-trip)",
)

request("DELETE", f"/adapters/{adapter_id}", expected=204)
request("GET", f"/adapters/{adapter_id}", expected=404)

print("M1 smoke chain passed")
PY

echo "==> running M2/M3 execution loop (manual execution -> worker -> SSE -> succeeded)"
# The smoke adapter uses only the Python standard library so CI does not
# depend on public PyPI availability. The secret is consumed as a SHA-256
# digest: it is genuinely usable via context.secrets, but the raw value can
# never appear in responses or logs.
m2_output=$(docker compose exec -T \
  -e DLR_ADMIN_TOKEN -e DLR_SECRET_SMOKE control python - <<'PY'
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

BASE = "http://web/api"
ADMIN_TOKEN = os.environ["DLR_ADMIN_TOKEN"]
SMOKE_SECRET = os.environ["DLR_SECRET_SMOKE"]
EXPECTED_DIGEST = hashlib.sha256(SMOKE_SECRET.encode()).hexdigest()

ADAPTER_CODE = (
    "import hashlib\n"
    "import time\n"
    "\n"
    "\n"
    "def handle(context, input):\n"
    "    secret = context.secrets.get('SMOKE') or ''\n"
    "    print('smoke step 1: starting', flush=True)\n"
    "    time.sleep(3)\n"
    "    print('smoke step 2: finishing', flush=True)\n"
    "    context.logger.info('secret available, reporting digest only')\n"
    "    return {\n"
    "        'echo': input,\n"
    "        'stage': context.config.get('stage'),\n"
    "        'secret_digest': hashlib.sha256(secret.encode()).hexdigest(),\n"
    "    }\n"
)


def request(method, path, payload=None, expected=200):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {ADMIN_TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read()
    body = json.loads(raw) if raw else None
    assert status == expected, f"{method} {path}: expected {expected}, got {status}: {body}"
    return body


def check(condition, message):
    assert condition, message


adapter = request(
    "POST", "/adapters", {"name": "smoke-exec-adapter", "description": "m2"}, expected=201
)
adapter_id = adapter["id"]
version = request(
    "POST",
    f"/adapters/{adapter_id}/versions",
    {"code": ADAPTER_CODE, "requirements": "", "runtime_config": {"stage": "m2-smoke"}},
    expected=201,
)
version_id = version["id"]

execution = request(
    "POST",
    f"/adapters/{adapter_id}/executions",
    {"input": {"n": 7}},
    expected=202,
)
execution_id = execution["id"]
check(execution["status"] == "pending", "a new execution starts pending")
check(execution["version_id"] == version_id, "execution pins the latest version")

# M3: the SSE stream needs the admin token like every other admin API.
sse_unauth = urllib.request.Request(BASE + f"/executions/{execution_id}/events")
try:
    urllib.request.urlopen(sse_unauth, timeout=10)
    raise AssertionError("SSE without token must be rejected with 401")
except urllib.error.HTTPError as error:
    check(error.code == 401, f"SSE without token must return 401, got {error.code}")

# M3: observe the execution over SSE until the server closes the stream at a
# terminal status; live logs must be visible while the adapter is running.
sse_req = urllib.request.Request(BASE + f"/executions/{execution_id}/events")
sse_req.add_header("Authorization", f"Bearer {ADMIN_TOKEN}")
sse_req.add_header("Accept", "text/event-stream")
sse_events = []
with urllib.request.urlopen(sse_req, timeout=180) as response:
    buffer = b""
    while True:
        chunk = response.read(4096)
        if not chunk:
            break
        buffer += chunk
        while b"\n\n" in buffer:
            raw_block, buffer = buffer.split(b"\n\n", 1)
            event_name = "message"
            data_lines = []
            for line in raw_block.decode().split("\n"):
                if line.startswith("event: "):
                    event_name = line[len("event: "):]
                elif line.startswith("data: "):
                    data_lines.append(line[len("data: "):])
            if data_lines:
                sse_events.append((event_name, json.loads("\n".join(data_lines))))

execution_events = [data for name, data in sse_events if name == "execution"]
log_events = [data for name, data in sse_events if name == "log"]
check(len(execution_events) >= 1, "SSE sends execution state events")
check(
    any(event["status"] == "running" for event in execution_events),
    "SSE observes the running state",
)
check(len(log_events) >= 1, "SSE streams at least one live log event")
check(
    any("smoke step" in event["chunk"] for event in log_events),
    "SSE log events carry the adapter's live output",
)
check(execution_events[-1]["status"] == "succeeded", "SSE ends with the terminal state")

deadline = time.monotonic() + 180
current = request("GET", f"/executions/{execution_id}")
while current["status"] in ("pending", "running") and time.monotonic() < deadline:
    time.sleep(2)
    current = request("GET", f"/executions/{execution_id}")
check(
    current["status"] == "succeeded",
    f"execution must succeed, got {current['status']}: {current['error']} / {current['stderr']}",
)
check(current["version_id"] == version_id, "version stays pinned through execution")
check(current["input"] == {"n": 7}, "input round-trips unchanged")
check(current["worker_id"] is not None, "a worker claimed the execution")
check(current["duration_ms"] is not None and current["duration_ms"] >= 0, "duration recorded")
check(
    current["output"]
    == {"echo": {"n": 7}, "stage": "m2-smoke", "secret_digest": EXPECTED_DIGEST},
    "output carries input echo, runtime_config and a usable secret digest",
)
check("secret available" in current["stdout"], "context.logger output is collected")

# M3: history lists this execution newest-first and the summary rows never
# carry the large payload fields.
history = request("GET", f"/adapters/{adapter_id}/executions?limit=50")
history_ids = [item["id"] for item in history["items"]]
check(execution_id in history_ids, "history lists this execution")
check(
    history_ids == sorted(history_ids, reverse=True),
    "history is ordered newest first",
)
summary = next(item for item in history["items"] if item["id"] == execution_id)
check(summary["status"] == "succeeded", "history row shows the terminal status")
check(summary["version_seq"] == version["seq"], "history row enriches the version seq")
check(
    all(field not in summary for field in ("input", "output", "stdout", "stderr")),
    "history summary carries no large fields",
)

# M3: the worker list admin API shows the registered smoke worker.
workers = request("GET", "/workers")
check(len(workers) >= 1, "worker list returns the registered worker")
check(
    any(worker["status"] == "online" for worker in workers),
    "the smoke worker reports online",
)

# The raw secret value must never leak into any persisted field.
blob = json.dumps(current)
check(SMOKE_SECRET not in blob, "raw secret must not appear in execution responses")

deleted = request("DELETE", f"/adapters/{adapter_id}", expected=409)
check(
    deleted["detail"]["code"] == "adapter_has_executions",
    "adapters with execution history cannot be deleted",
)

print(f"SMOKE_EXEC_ADAPTER_ID={adapter_id}")
print(f"SMOKE_EXEC_VERSION_ID={version_id}")
print("M2/M3 smoke chain passed")
PY
)
echo "$m2_output"

smoke_adapter_id=$(echo "$m2_output" | sed -n 's/^SMOKE_EXEC_ADAPTER_ID=//p')
smoke_version_id=$(echo "$m2_output" | sed -n 's/^SMOKE_EXEC_VERSION_ID=//p')
[ -n "$smoke_adapter_id" ] && [ -n "$smoke_version_id" ] \
  || { echo "ERROR: could not parse smoke execution ids" >&2; exit 1; }

echo "==> checking the worker built a version-scoped venv (.venv/.ready)"
docker compose exec -T worker \
  test -d "/var/lib/dlr/runtime/adapters/${smoke_adapter_id}/versions/${smoke_version_id}/.venv"
docker compose exec -T worker \
  test -f "/var/lib/dlr/runtime/adapters/${smoke_adapter_id}/versions/${smoke_version_id}/.ready"
echo "worker runtime venv ok"

echo "==> running M3.2 production lifecycle chain (credentials -> gate -> publish -> start/stop -> secrets)"
# Bound secrets travel as SHA-256 digests for the same reason as DLR_SECRET_*:
# usable end-to-end, but the raw value can never surface in responses/logs.
docker compose exec -T -e DLR_ADMIN_TOKEN -e DLR_SECRET_SMOKE control python - <<'PY'
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

BASE = "http://web/api"
ADMIN_TOKEN = os.environ["DLR_ADMIN_TOKEN"]
SMOKE_SECRET = os.environ["DLR_SECRET_SMOKE"]
EXPECTED_DIGEST = hashlib.sha256(SMOKE_SECRET.encode()).hexdigest()

PROD_CODE = (
    "import hashlib\n"
    "import time\n"
    "\n"
    "\n"
    "def handle(context, input):\n"
    "    secret = context.secrets.get('SMOKE_CRED') or ''\n"
    "    print('prod step 1: starting', flush=True)\n"
    "    time.sleep(6)\n"
    "    print('prod step 2: finishing', flush=True)\n"
    "    return {\n"
    "        'stage': context.config.get('stage'),\n"
    "        'secret_digest': hashlib.sha256(secret.encode()).hexdigest(),\n"
    "    }\n"
)


def request(method, path, payload=None, expected=200):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {ADMIN_TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read()
    body = json.loads(raw) if raw else None
    assert status == expected, f"{method} {path}: expected {expected}, got {status}: {body}"
    return body


def check(condition, message):
    assert condition, message


def wait_terminal(execution_id, deadline_seconds=180):
    deadline = time.monotonic() + deadline_seconds
    current = request("GET", f"/executions/{execution_id}")
    while current["status"] in ("pending", "running") and time.monotonic() < deadline:
        time.sleep(2)
        current = request("GET", f"/executions/{execution_id}")
    check(
        current["status"] not in ("pending", "running"),
        f"execution {execution_id} did not reach a terminal state: {current['status']}",
    )
    return current


# --- M3.2 Secret Store: credentials ------------------------------------------
credential = request(
    "POST",
    "/credentials",
    {
        "name": "smoke-credential",
        "type": "password",
        "fields": {"username": "smoke-user", "password": SMOKE_SECRET},
    },
    expected=201,
)
credential_id = credential["id"]
refetched = request("GET", f"/credentials/{credential_id}")
for blob in (json.dumps(credential), json.dumps(refetched), json.dumps(request("GET", "/credentials"))):
    check(SMOKE_SECRET not in blob, "credential responses must never carry plaintext")
    check("ciphertext" not in blob, "credential responses must not expose ciphertext")

# --- M3.2 package sources ------------------------------------------------------
source = request(
    "POST",
    "/package-sources",
    {"name": "smoke-source", "index_url": "http://web/api/health", "is_default": True},
    expected=201,
)
reachability = request("POST", f"/package-sources/{source['id']}/test")
check(reachability["ok"] is True, f"default package source must be reachable: {reachability}")

# --- production worker + publish gate -------------------------------------------
workers = request("GET", "/workers")
online_workers = [worker for worker in workers if worker["status"] == "online"]
check(online_workers, "an online worker is required for the production chain")
worker_id = online_workers[0]["id"]

adapter = request(
    "POST",
    "/adapters",
    {"name": "smoke-prod-adapter", "description": "m3.2"},
    expected=201,
)
adapter_id = adapter["id"]
check(adapter["production_state"] == "idle", "a fresh adapter starts in the idle state")
version = request(
    "POST",
    f"/adapters/{adapter_id}/versions",
    {"code": PROD_CODE, "requirements": "", "runtime_config": {"stage": "m3-2-smoke"}},
    expected=201,
)
version_id = version["id"]

patched = request("PATCH", f"/adapters/{adapter_id}", {"production_worker_id": worker_id})
check(patched["production_worker_id"] == worker_id, "patch sets the production worker")

gated = request("POST", f"/adapters/{adapter_id}/versions/{version_id}/publish", expected=409)
check(
    gated["detail"]["code"] == "publish_gate_locked",
    f"publish before a successful target test must be gated, got {gated}",
)
gate = request("GET", f"/adapters/{adapter_id}/versions/{version_id}/publish-gate")
check(gate["allowed"] is False, "the gate must be locked before any test run")
check(gate["reason"] == "not_tested_on_production_worker", f"unexpected gate reason: {gate}")

# A manual test run targets the production worker automatically once it is set.
test_run = request(
    "POST", f"/adapters/{adapter_id}/executions", {"input": {"warmup": True}}, expected=202
)
check(test_run["trigger"] == "manual", "test runs stay manual triggers")
check(test_run["target_worker_id"] == worker_id, "test runs target the production worker")
test_run = wait_terminal(test_run["id"])
check(
    test_run["status"] == "succeeded",
    f"gate test run must succeed, got {test_run['status']}: {test_run['error']} / {test_run['stderr']}",
)

gate = request("GET", f"/adapters/{adapter_id}/versions/{version_id}/publish-gate")
check(gate["allowed"] is True, f"the gate must open after a succeeded test run: {gate}")
check(gate["last_test"]["execution_id"] == test_run["id"], "gate reports the last test run")
published = request("POST", f"/adapters/{adapter_id}/versions/{version_id}/publish")
check(published["published_version_id"] == version_id, "publish sets the published pointer")

# --- credential binding ---------------------------------------------------------
bindings = request(
    "PUT",
    f"/adapters/{adapter_id}/credential-bindings",
    {"bindings": [{"env_key": "SMOKE_CRED", "credential_id": credential_id, "field": "password"}]},
)
check(len(bindings) == 1 and bindings[0]["env_key"] == "SMOKE_CRED", "binding is stored")
check(bindings[0]["credential_name"] == "smoke-credential", "binding enriches credential metadata")

# --- M5.1 start -> stop lifecycle: Start opens entry and locks version --------
started = request("POST", f"/adapters/{adapter_id}/production/start", expected=202)
# M5.1: Start returns an AdapterResponse (not an Execution).
check(started["production_state"] == "running", "start opens the production entry")
check(started["production_version_id"] == version_id, "start locks the production version")
check(started["production_version_seq"] is not None, "production version seq is populated")

duplicate = request("POST", f"/adapters/{adapter_id}/production/start", expected=409)
check(
    duplicate["detail"]["code"] == "production_already_running",
    f"a second start must be rejected, got {duplicate}",
)

# M5.1: Stop clears production_version_id. No Execution to cancel.
stopped = request("POST", f"/adapters/{adapter_id}/production/stop", {"mode": "terminate"})
check(stopped["production_state"] == "stopped", "terminate closes the production entry")
check(stopped["production_version_id"] is None, "stop clears production_version_id")

# --- start again: verify version re-locked, then test bound secret via manual exec -
started2 = request("POST", f"/adapters/{adapter_id}/production/start", expected=202)
check(started2["production_state"] == "running", "re-start opens the production entry")
check(started2["production_version_id"] == version_id, "re-start re-locks the production version")

# M5.1: Start no longer creates an Execution. Test the bound credential via a
# manual Execution (the worker still exercises the secret binding).
manual_exec = request(
    "POST", f"/adapters/{adapter_id}/executions",
    {"version_id": version_id, "input": {"stage": "m3-2-smoke"}},
    expected=202,
)
succeeded = wait_terminal(manual_exec["id"])
check(
    succeeded["status"] == "succeeded",
    f"manual production run must succeed, got {succeeded['status']}: {succeeded['error']} / {succeeded['stderr']}",
)
check(
    succeeded["output"] == {"stage": "m3-2-smoke", "secret_digest": EXPECTED_DIGEST},
    "the bound credential reaches the worker (digest round-trip)",
)
check(SMOKE_SECRET not in json.dumps(succeeded), "raw bound secret must not leak into responses")

history = request("GET", f"/adapters/{adapter_id}/executions?limit=50")
check(
    sum(1 for item in history["items"] if item["trigger"] == "manual") >= 1,
    "history records the manual smoke run",
)

stopped = request("POST", f"/adapters/{adapter_id}/production/stop", {"mode": "wait"})
check(stopped["production_state"] == "stopped", "wait closes the production entry")
check(stopped["production_version_id"] is None, "wait stop also clears production_version_id")

# --- clone / archive / restore -----------------------------------------------------
clone = request(
    "POST", f"/adapters/{adapter_id}/clone", {"name": "smoke-prod-clone"}, expected=201
)
check(clone["published_version_id"] is None, "a clone starts unpublished")
check(clone["production_state"] == "idle", "a clone starts not running")
check(clone["latest_version_id"] is not None, "a clone copies the working copy as v1")
clone_bindings = request("GET", f"/adapters/{clone['id']}/credential-bindings")
check(len(clone_bindings) == 1, "a clone copies the binding references")

archived = request("POST", f"/adapters/{adapter_id}/archive")
check(archived["archived_at"] is not None, "archive stamps archived_at")
blocked_save = request(
    "POST",
    f"/adapters/{adapter_id}/versions",
    {"code": PROD_CODE, "requirements": "", "runtime_config": {}},
    expected=409,
)
check(blocked_save["detail"]["code"] == "adapter_archived", "archived adapters are read-only")
blocked_start = request("POST", f"/adapters/{adapter_id}/production/start", expected=409)
check(blocked_start["detail"]["code"] == "adapter_archived", "archived adapters cannot start")
restored = request("POST", f"/adapters/{adapter_id}/restore")
check(restored["archived_at"] is None, "restore clears archived_at")

in_use = request("DELETE", f"/credentials/{credential_id}", expected=409)
check(
    in_use["detail"]["code"] == "credential_in_use",
    "bound credentials cannot be deleted",
)

print("M3.2 smoke chain passed")
PY

echo "==> running M3.3 Python + JavaScript + Java lifecycle smoke"
docker compose exec -T -e DLR_ADMIN_TOKEN control python - <<'PY'
import json
import os
import time
import urllib.error
import urllib.request

BASE = "http://web/api"
TOKEN = os.environ["DLR_ADMIN_TOKEN"]


def request(method, path, payload=None, expected=200):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    body = json.loads(raw) if raw else None
    assert status == expected, f"{method} {path}: expected {expected}, got {status}: {body}"
    return body


def wait_succeeded(execution_id):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        execution = request("GET", f"/executions/{execution_id}")
        if execution["status"] not in ("pending", "running"):
            assert execution["status"] == "succeeded", execution
            return execution
        time.sleep(1)
    raise AssertionError(f"execution {execution_id} did not finish")


codes = {
    "python": "def handle(context, input):\n    return {'language': 'python'}\n",
    "javascript": (
        "export async function handle(context, input) {\n"
        "  return { language: 'javascript' };\n"
        "}\n"
    ),
    "java": (
        "import java.util.Map;\n"
        "public class Adapter {\n"
        "  public Object handle(Context context, Object input) {\n"
        "    return Map.of(\"language\", \"java\");\n"
        "  }\n"
        "}\n"
    ),
}

workers = request("GET", "/workers")
worker = next(item for item in workers if item["status"] == "online")
assert set(codes) <= set(worker["capabilities"]), worker

for language, code in codes.items():
    adapter = request(
        "POST",
        "/adapters",
        {"name": f"smoke-m33-{language}", "language": language},
        expected=201,
    )
    adapter_id = adapter["id"]
    version = request(
        "POST",
        f"/adapters/{adapter_id}/versions",
        {"code": code, "requirements": "", "runtime_config": {}},
        expected=201,
    )
    request("PATCH", f"/adapters/{adapter_id}", {"production_worker_id": worker["id"]})
    tested = request(
        "POST",
        f"/adapters/{adapter_id}/executions",
        {"version_id": version["id"], "input": {"smoke": True}},
        expected=202,
    )
    tested = wait_succeeded(tested["id"])
    assert tested["output"] == {"language": language}, tested
    request("POST", f"/adapters/{adapter_id}/versions/{version['id']}/publish")
    # M5.1: Start opens the production entry and locks the version; no Execution.
    started = request("POST", f"/adapters/{adapter_id}/production/start", expected=202)
    assert started["production_state"] == "running", started
    assert started["production_version_id"] == version["id"], started
    request("POST", f"/adapters/{adapter_id}/production/stop", {"mode": "wait"})

print("M3.3 three-language lifecycle smoke passed")
PY

echo "==> starting temporary local OpenAI-compatible fake Provider"
AI_FAKE_CONTAINER_NAME="${COMPOSE_PROJECT_NAME}-ai-fake"
export AI_FAKE_BASE_URL="http://${AI_FAKE_CONTAINER_NAME}:18080"
# Reuse the already-built Control image only for its Python runtime. The fake
# is an external Provider boundary and must not inherit Control's database or
# platform credentials, even when the smoke caller supplied real-looking values.
AI_FAKE_CONTAINER_ID=$(docker compose run -d --no-deps \
  --name "$AI_FAKE_CONTAINER_NAME" \
  --env DATABASE_URL= \
  --env DLR_ADMIN_TOKEN= \
  --env DLR_WORKER_TOKEN= \
  --env DLR_MASTER_KEY= \
  --volume "$PWD/scripts/ai-fake-provider.py:/tmp/dlr-ai-fake-provider.py:ro" \
  --entrypoint python \
  control /tmp/dlr-ai-fake-provider.py --port 18080)

echo "==> waiting for local fake Provider"
elapsed=0
while ! docker compose exec -T -e AI_FAKE_BASE_URL control python -c \
  'import os, urllib.request; urllib.request.urlopen(os.environ["AI_FAKE_BASE_URL"] + "/healthz", timeout=2).read()' \
  >/dev/null 2>&1; do
  if [ "$elapsed" -ge 60 ]; then
    echo "ERROR: local fake Provider not ready within 60s" >&2
    docker logs --tail 30 "$AI_FAKE_CONTAINER_ID"
    exit 1
  fi
  sleep 2
  elapsed=$((elapsed + 2))
done

echo "==> running M4 AI settings + models + three-language assist smoke"
docker compose exec -T -e DLR_ADMIN_TOKEN -e AI_FAKE_BASE_URL control python - <<'PY'
import json
import os
import urllib.error
import urllib.request

BASE = "http://web/api"
TOKEN = os.environ["DLR_ADMIN_TOKEN"]
FAKE_BASE_URL = os.environ["AI_FAKE_BASE_URL"]
MODEL_ID = "dlr-smoke-model"
REASONING_SENTINEL = "SMOKE_REASONING_MUST_NOT_REACH_BROWSER"


def request(method, path, payload=None, expected=200):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    body = json.loads(raw) if raw else None
    assert status == expected, f"{method} {path}: expected {expected}, got {status}: {body}"
    return body


setting_payload = {
    "provider": "custom_openai_compatible",
    "base_url": FAKE_BASE_URL,
    "model": MODEL_ID,
    "credential_id": None,
    "reasoning_mode": "default",
    "reasoning_effort": None,
}
saved_setting = request("PUT", "/ai/settings", setting_payload)
assert saved_setting["provider"] == setting_payload["provider"], saved_setting
assert saved_setting["base_url"] == FAKE_BASE_URL, saved_setting
assert saved_setting["model"] == MODEL_ID, saved_setting
assert saved_setting["credential_id"] is None, saved_setting
assert saved_setting["reasoning_mode"] == "default", saved_setting
assert "api_key" not in json.dumps(saved_setting).lower(), saved_setting

refetched_setting = request("GET", "/ai/settings")
assert refetched_setting["model"] == MODEL_ID, refetched_setting
assert "api_key" not in json.dumps(refetched_setting).lower(), refetched_setting

models = request(
    "POST",
    "/ai/models/refresh",
    {
        "provider": "custom_openai_compatible",
        "base_url": FAKE_BASE_URL,
        "credential_id": None,
    },
)
assert MODEL_ID in models["models"], models

connection = request("POST", "/ai/settings/test", setting_payload)
assert connection["ok"] is True, connection

working_copies = {
    "python": {
        "code": "def handle(context, input):\n    return input\n",
        "requirements": "",
        "runtime_config": {"before_ai": "python"},
    },
    "javascript": {
        "code": (
            "export async function handle(context, input) {\n"
            "  return input;\n"
            "}\n"
        ),
        "requirements": "",
        "runtime_config": {"before_ai": "javascript"},
    },
    "java": {
        "code": (
            "public class Adapter {\n"
            "  public Object handle(Context context, Object input) throws Exception {\n"
            "    return input;\n"
            "  }\n"
            "}\n"
        ),
        "requirements": "",
        "runtime_config": {"before_ai": "java"},
    },
}

for language, working_copy in working_copies.items():
    adapter = request(
        "POST",
        "/adapters",
        {"name": f"smoke-m4-{language}", "language": language},
        expected=201,
    )
    adapter_id = adapter["id"]
    version = request(
        "POST",
        f"/adapters/{adapter_id}/versions",
        working_copy,
        expected=201,
    )

    before_adapter = request("GET", f"/adapters/{adapter_id}")
    before_versions = request("GET", f"/adapters/{adapter_id}/versions")
    before_executions = request("GET", f"/adapters/{adapter_id}/executions?limit=50")
    lifecycle_before = {
        "latest_version_id": before_adapter["latest_version_id"],
        "published_version_id": before_adapter["published_version_id"],
        "production_state": before_adapter["production_state"],
    }

    assisted = request(
        "POST",
        f"/adapters/{adapter_id}/ai/assist",
        {
            "message": f"Generate the deterministic {language} smoke Candidate.",
            "working_copy": working_copy,
            "recent_messages": [
                {"role": "user", "content": "Explain the current Adapter briefly."},
                {"role": "assistant", "content": "Ready for the requested change."},
            ],
            "base_version_id": version["id"],
        },
    )
    candidate = assisted["candidate"]
    assert candidate is not None, assisted
    assert candidate["runtime_config"] == {"ai_smoke": language}, candidate
    assert candidate["required_secret_keys"] == [], candidate
    assert language in candidate["summary"], candidate
    if language == "python":
        assert "def handle(context, input)" in candidate["code"], candidate
    elif language == "javascript":
        assert "export async function handle(context, input)" in candidate["code"], candidate
    else:
        assert "public Object handle" in candidate["code"], candidate
    assert REASONING_SENTINEL not in json.dumps(assisted), assisted

    after_adapter = request("GET", f"/adapters/{adapter_id}")
    after_versions = request("GET", f"/adapters/{adapter_id}/versions")
    after_executions = request("GET", f"/adapters/{adapter_id}/executions?limit=50")
    lifecycle_after = {
        "latest_version_id": after_adapter["latest_version_id"],
        "published_version_id": after_adapter["published_version_id"],
        "production_state": after_adapter["production_state"],
    }
    assert len(after_versions) == len(before_versions), (before_versions, after_versions)
    assert len(after_executions["items"]) == len(before_executions["items"]), (
        before_executions,
        after_executions,
    )
    assert lifecycle_after == lifecycle_before, (lifecycle_before, lifecycle_after)

with urllib.request.urlopen(FAKE_BASE_URL + "/_smoke/metrics", timeout=5) as response:
    metrics = json.load(response)
assert metrics["models"] >= 1, metrics
assert metrics["chat_completions"] >= 4, metrics

print("M4 local-provider three-language AI assist smoke passed")
PY

echo "==> compose smoke test passed"
