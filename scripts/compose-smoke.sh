#!/usr/bin/env bash
# Minimal compose smoke test.
#
# Runs in an ISOLATED compose project (dlr-smoke-*) with a dedicated host
# port, so it never shares containers or volumes with the normal development
# stack and cleanup can only touch the smoke project.
#
# Steps: build & start all services, wait until healthy, run the Alembic
# migration against the real PostgreSQL, verify the web page and the
# /api/health proxy path, run the full M1 Adapter management chain
# (create / patch / save v1+v2 / publish historical / list / detail / delete),
# then tear down only the smoke project.
set -euo pipefail

cd "$(dirname "$0")/.."

SERVICES=(postgres control worker web)
TIMEOUT_SECONDS=${COMPOSE_SMOKE_TIMEOUT:-240}

# Isolated compose project: unique on CI via GITHUB_RUN_ID, locally via PID.
export COMPOSE_PROJECT_NAME=${COMPOSE_SMOKE_PROJECT:-dlr-smoke-${GITHUB_RUN_ID:-$$}}
# Dedicated host port for the smoke web service, so a running dev stack
# (default 8080) cannot conflict with the smoke run.
export DLR_WEB_HOST_PORT=${COMPOSE_SMOKE_WEB_PORT:-8880}

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

echo "==> running alembic upgrade head against the smoke PostgreSQL"
docker compose run --rm control alembic upgrade head

echo "==> checking web page"
curl -fsS "http://localhost:${DLR_WEB_HOST_PORT}/" | grep -q "DataLinkRuntime"
echo "web page ok"

echo "==> checking /api/health via web/nginx"
health_response=$(curl -fsS "http://localhost:${DLR_WEB_HOST_PORT}/api/health")
echo "health response: $health_response"
echo "$health_response" | grep -q '"status":"ok"'
echo "$health_response" | grep -q '"database":true'
echo "control + database ok"

echo "==> running M1 adapter management chain (via web/nginx, real PostgreSQL)"
# Uses the Python standard library inside the control container, so the host
# needs no extra tooling (no jq, no extra python packages).
docker compose exec -T control python - <<'PY'
import json
import urllib.error
import urllib.request

BASE = "http://web/api"


def request(method, path, payload=None, expected=200):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
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

published = request("POST", f"/adapters/{adapter_id}/versions/{v1['id']}/publish")
check(published["published_version_id"] == v1["id"], "publish v1 sets the published pointer")
check(published["latest_version_id"] == v2["id"], "publish must not change latest")

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

echo "==> compose smoke test passed"
