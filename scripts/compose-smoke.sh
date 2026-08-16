#!/usr/bin/env bash
# Isolated end-to-end Compose smoke for the current DLR lifecycle contract.
set -euo pipefail

cd "$(dirname "$0")/.."

SERVICES=(postgres control worker web)
TIMEOUT_SECONDS=${COMPOSE_SMOKE_TIMEOUT:-240}
export COMPOSE_PROJECT_NAME=${COMPOSE_SMOKE_PROJECT:-dlr-smoke-${GITHUB_RUN_ID:-$$}}
export DLR_WEB_HOST_PORT=${COMPOSE_SMOKE_WEB_PORT:-8880}
export DLR_ADMIN_TOKEN=${DLR_ADMIN_TOKEN:-smoke-admin-token-$$}
export DLR_WORKER_TOKEN=${DLR_WORKER_TOKEN:-smoke-worker-token-$$}
export DLR_SECRET_SMOKE=${DLR_SECRET_SMOKE:-smoke-env-secret-$$}
export DLR_MASTER_KEY=${DLR_MASTER_KEY:-smoke-master-key-$$}
export SMOKE_STORED_SECRET=${SMOKE_STORED_SECRET:-smoke-stored-secret-$$}
# M5.5.5: the selected code sent to the fake Provider must never reach
# service logs (requests are not logged; this assertion pins that contract).
export SMOKE_SELECTED_TEXT=${SMOKE_SELECTED_TEXT:-smoke-selected-sentinel-$$}
AI_FAKE_CONTAINER_ID=""
AI_FAKE_DISABLED_CONTAINER_ID=""

cleanup() {
  if [ -n "$AI_FAKE_CONTAINER_ID" ]; then
    docker rm -f "$AI_FAKE_CONTAINER_ID" >/dev/null 2>&1 || true
  fi
  if [ -n "$AI_FAKE_DISABLED_CONTAINER_ID" ]; then
    docker rm -f "$AI_FAKE_DISABLED_CONTAINER_ID" >/dev/null 2>&1 || true
  fi
  docker compose -p "$COMPOSE_PROJECT_NAME" down --volumes --remove-orphans
}
trap cleanup EXIT

echo "==> smoke project: $COMPOSE_PROJECT_NAME (web port: $DLR_WEB_HOST_PORT)"
# M5.5.3: the optional DNS override file must always parse; default compose
# config must not depend on it (no machine-specific DNS hardcoding).
docker compose -f docker-compose.yml config -q
docker compose -f docker-compose.yml -f docker-compose.dns.example.yml config -q
docker compose build
docker compose up -d

echo "==> waiting for PostgreSQL"
elapsed=0
while true; do
  container_id=$(docker compose ps -q postgres)
  health=""
  if [ -n "$container_id" ]; then
    health=$(docker inspect --format '{{.State.Health.Status}}' "$container_id" 2>/dev/null || true)
  fi
  [ "$health" = "healthy" ] && break
  if [ "$elapsed" -ge "$TIMEOUT_SECONDS" ]; then
    docker compose ps
    docker compose logs --tail 50 postgres
    exit 1
  fi
  sleep 5
  elapsed=$((elapsed + 5))
done

echo "==> applying Alembic head to a fresh database"
docker compose run --rm control alembic upgrade head

echo "==> waiting for all services"
elapsed=0
while true; do
  healthy_count=0
  for service in "${SERVICES[@]}"; do
    container_id=$(docker compose ps -q "$service")
    health=""
    if [ -n "$container_id" ]; then
      health=$(docker inspect --format '{{.State.Health.Status}}' "$container_id" 2>/dev/null || true)
    fi
    [ "$health" = "healthy" ] && healthy_count=$((healthy_count + 1))
  done
  [ "$healthy_count" -eq "${#SERVICES[@]}" ] && break
  if [ "$elapsed" -ge "$TIMEOUT_SECONDS" ]; then
    docker compose ps
    for service in "${SERVICES[@]}"; do
      docker compose logs --tail 50 "$service"
    done
    exit 1
  fi
  sleep 5
  elapsed=$((elapsed + 5))
done

echo "==> checking web, health and authentication boundaries"
curl -fsS "http://localhost:${DLR_WEB_HOST_PORT}/" | grep -q "DataLinkRuntime"
curl -fsS "http://localhost:${DLR_WEB_HOST_PORT}/api/health" | grep -q '"database":true'
no_token_status=$(curl -s -o /dev/null -w '%{http_code}' \
  "http://localhost:${DLR_WEB_HOST_PORT}/api/adapters")
[ "$no_token_status" = "401" ]
wrong_token_status=$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer ${DLR_WORKER_TOKEN}" \
  "http://localhost:${DLR_WEB_HOST_PORT}/api/adapters")
[ "$wrong_token_status" = "401" ]

echo "==> starting isolated local OpenAI-compatible fake Provider"
AI_FAKE_CONTAINER_NAME="${COMPOSE_PROJECT_NAME}-ai-fake"
export AI_FAKE_BASE_URL="http://${AI_FAKE_CONTAINER_NAME}:18080"
CONTROL_CONTAINER_ID=$(docker compose ps -q control)
CONTROL_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$CONTROL_CONTAINER_ID")
CONTROL_NETWORK=$(docker inspect --format '{{range $network, $_ := .NetworkSettings.Networks}}{{println $network}}{{end}}' \
  "$CONTROL_CONTAINER_ID" | head -n 1)
AI_FAKE_CONTAINER_ID=$(docker run -d \
  --name "$AI_FAKE_CONTAINER_NAME" \
  --network "$CONTROL_NETWORK" \
  -e SMOKE_SELECTED_TEXT \
  --volume "$PWD/scripts/ai-fake-provider.py:/tmp/dlr-ai-fake-provider.py:ro" \
  --entrypoint python \
  "$CONTROL_IMAGE" /tmp/dlr-ai-fake-provider.py --port 18080)

elapsed=0
while ! docker compose exec -T -e AI_FAKE_BASE_URL control python -c \
  'import os, urllib.request; urllib.request.urlopen(os.environ["AI_FAKE_BASE_URL"] + "/healthz", timeout=2).read()' \
  >/dev/null 2>&1; do
  if [ "$elapsed" -ge 60 ]; then
    docker logs --tail 50 "$AI_FAKE_CONTAINER_ID"
    exit 1
  fi
  sleep 2
  elapsed=$((elapsed + 2))
done

echo "==> starting second fake Provider without /v1/models (M5.5.2 independence path)"
AI_FAKE_DISABLED_CONTAINER_NAME="${COMPOSE_PROJECT_NAME}-ai-fake-disabled"
export AI_FAKE_DISABLED_BASE_URL="http://${AI_FAKE_DISABLED_CONTAINER_NAME}:18080"
AI_FAKE_DISABLED_CONTAINER_ID=$(docker run -d \
  --name "$AI_FAKE_DISABLED_CONTAINER_NAME" \
  --network "$CONTROL_NETWORK" \
  -e SMOKE_DISABLE_MODELS=1 \
  --volume "$PWD/scripts/ai-fake-provider.py:/tmp/dlr-ai-fake-provider.py:ro" \
  --entrypoint python \
  "$CONTROL_IMAGE" /tmp/dlr-ai-fake-provider.py --port 18080)

elapsed=0
while ! docker compose exec -T -e AI_FAKE_DISABLED_BASE_URL control python -c \
  'import os, urllib.request; urllib.request.urlopen(os.environ["AI_FAKE_DISABLED_BASE_URL"] + "/healthz", timeout=2).read()' \
  >/dev/null 2>&1; do
  if [ "$elapsed" -ge 60 ]; then
    docker logs --tail 50 "$AI_FAKE_DISABLED_CONTAINER_ID"
    exit 1
  fi
  sleep 2
  elapsed=$((elapsed + 2))
done

echo "==> running M5.4.4 Task, Webhook, runtime-lock and Clone regression chain"
docker compose exec -T \
  -e DLR_ADMIN_TOKEN \
  -e DLR_WORKER_TOKEN \
  -e SMOKE_STORED_SECRET \
  -e SMOKE_SELECTED_TEXT \
  -e AI_FAKE_BASE_URL \
  -e AI_FAKE_DISABLED_BASE_URL \
  control python - < scripts/compose-smoke.py

echo "==> verifying secrets did not enter service logs"
if docker compose logs control worker web | grep -F "$SMOKE_STORED_SECRET" >/dev/null; then
  echo "ERROR: stored secret appeared in service logs" >&2
  exit 1
fi
# M5.5.5: the administrator-selected code is request payload, never a log line.
if docker compose logs control worker web | grep -F "$SMOKE_SELECTED_TEXT" >/dev/null; then
  echo "ERROR: selected code appeared in service logs" >&2
  exit 1
fi

echo "==> compose smoke test passed"
