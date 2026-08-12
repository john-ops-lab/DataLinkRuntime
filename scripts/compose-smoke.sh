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
# tearing down only the smoke project.
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

cleanup() {
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

# --- start -> stop(terminate) -> cancelled --------------------------------------
started = request("POST", f"/adapters/{adapter_id}/production/start", expected=202)
prod_execution_id = started["id"]
check(started["trigger"] == "production", "start creates a production execution")
check(started["version_id"] == version_id, "start pins the published version")
check(started["target_worker_id"] == worker_id, "start targets the production worker")
running_adapter = request("GET", f"/adapters/{adapter_id}")
check(running_adapter["production_state"] == "running", "start opens the production entry")
check(running_adapter["running_execution_id"] == prod_execution_id, "running execution is derived")
check(running_adapter["running_version_id"] == version_id, "running version is derived")

duplicate = request("POST", f"/adapters/{adapter_id}/production/start", expected=409)
check(
    duplicate["detail"]["code"] == "production_already_running",
    f"a second start must be rejected, got {duplicate}",
)

# Wait until the worker actually claims it and enters the sleep(6) body, so
# the cancel exercises the running-path (cancel_requested) instead of the
# pending-path (immediate cancelled).
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    state = request("GET", f"/executions/{prod_execution_id}")
    if state["status"] == "running":
        break
    time.sleep(1)
check(state["status"] == "running", f"production execution must become running: {state['status']}")
cancelled_direct = request("POST", f"/executions/{prod_execution_id}/cancel")
check(cancelled_direct["cancel_requested"] is True, "cancel marks a running execution")
stopped = request("POST", f"/adapters/{adapter_id}/production/stop", {"mode": "terminate"})
check(stopped["production_state"] == "stopped", "terminate closes the production entry")
cancelled = wait_terminal(prod_execution_id)
check(cancelled["status"] == "cancelled", f"terminated execution must end cancelled, got {cancelled['status']}")

# --- start again: bound secret reaches the worker --------------------------------
started = request("POST", f"/adapters/{adapter_id}/production/start", expected=202)
prod_execution_id = started["id"]
succeeded = wait_terminal(prod_execution_id)
check(
    succeeded["status"] == "succeeded",
    f"production run must succeed, got {succeeded['status']}: {succeeded['error']} / {succeeded['stderr']}",
)
check(
    succeeded["output"] == {"stage": "m3-2-smoke", "secret_digest": EXPECTED_DIGEST},
    "the bound credential reaches the worker (digest round-trip)",
)
check(SMOKE_SECRET not in json.dumps(succeeded), "raw bound secret must not leak into responses")

history = request("GET", f"/adapters/{adapter_id}/executions?limit=50")
check(
    sum(1 for item in history["items"] if item["trigger"] == "production") >= 2,
    "history distinguishes production runs",
)

stopped = request("POST", f"/adapters/{adapter_id}/production/stop", {"mode": "wait"})
check(stopped["production_state"] == "stopped", "wait closes the production entry")

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
    started = request("POST", f"/adapters/{adapter_id}/production/start", expected=202)
    started = wait_succeeded(started["id"])
    assert started["output"] == {"language": language}, started
    request("POST", f"/adapters/{adapter_id}/production/stop", {"mode": "wait"})

print("M3.3 three-language lifecycle smoke passed")
PY

echo "==> compose smoke test passed"
