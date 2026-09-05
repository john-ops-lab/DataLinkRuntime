#!/usr/bin/env bash
# Isolated end-to-end Compose smoke for the current DLR lifecycle contract.
set -euo pipefail

cd "$(dirname "$0")/.."

SERVICES=(postgres rabbitmq control worker web account-web)
TIMEOUT_SECONDS=${COMPOSE_SMOKE_TIMEOUT:-240}
export COMPOSE_PROJECT_NAME=${COMPOSE_SMOKE_PROJECT:-dlr-smoke-${GITHUB_RUN_ID:-$$}}
export DLR_WEB_HOST_PORT=${COMPOSE_SMOKE_WEB_PORT:-8880}
export DLR_ACCOUNT_WEB_HOST_PORT=${COMPOSE_SMOKE_ACCOUNT_WEB_PORT:-8881}
# Compose binds platform logs from the host.  A fresh CI runner cannot rely on
# /var/lib/dlr being writable by PostgreSQL's unprivileged container user, so
# keep the smoke run isolated under the runner temp directory (or an ignored
# repository-local directory for Docker Desktop, whose file-sharing layer does
# not preserve mode bits for arbitrary macOS temp paths).  Production
# deployments still use their explicitly configured DLR_PLATFORM_LOG_ROOT.
SMOKE_PLATFORM_LOG_ROOT="${RUNNER_TEMP:-${PWD}/.tmp-platform-logs}/${COMPOSE_PROJECT_NAME}-platform-logs"
export DLR_PLATFORM_LOG_ROOT="${DLR_PLATFORM_LOG_ROOT:-$SMOKE_PLATFORM_LOG_ROOT}"
mkdir -p "$DLR_PLATFORM_LOG_ROOT"/{control,worker,web,account-web,postgres}
if [ "$DLR_PLATFORM_LOG_ROOT" = "$SMOKE_PLATFORM_LOG_ROOT" ]; then
  # PostgreSQL uses its own UID; the capability-bounded Worker cannot bypass
  # runner-owned directory permissions even as UID 0. These are disposable
  # smoke directories only, never operator-supplied production paths.
  chmod 0777 "$DLR_PLATFORM_LOG_ROOT/postgres" "$DLR_PLATFORM_LOG_ROOT/worker"
fi
export DLR_ADMIN_TOKEN=${DLR_ADMIN_TOKEN:-smoke-admin-token-$$}
export DLR_WORKER_TOKEN=${DLR_WORKER_TOKEN:-smoke-worker-token-$$}
export DLR_RABBITMQ_USER=${DLR_RABBITMQ_USER:-dlr_smoke}
export DLR_RABBITMQ_PASSWORD=${DLR_RABBITMQ_PASSWORD:-smoke-rabbitmq-password-$$}
export DLR_SECRET_SMOKE=${DLR_SECRET_SMOKE:-smoke-env-secret-$$}
export DLR_MASTER_KEY=${DLR_MASTER_KEY:-smoke-master-key-$$}
export SMOKE_STORED_SECRET=${SMOKE_STORED_SECRET:-smoke-stored-secret-$$}
# Force deterministic, bounded in-process rotation for the isolated smoke.
# Dedicated COMPOSE_SMOKE_* overrides avoid inheriting production-sized host
# values that would make rotation impractical to exercise in this short run.
export DLR_AI_TOOL_AUDIT_MAX_BYTES=${COMPOSE_SMOKE_AI_TOOL_AUDIT_MAX_BYTES:-2048}
export DLR_AI_TOOL_AUDIT_BACKUP_COUNT=${COMPOSE_SMOKE_AI_TOOL_AUDIT_BACKUP_COUNT:-3}
# M5.5.5: the selected code sent to the fake Provider must never reach
# service logs (requests are not logged; this assertion pins that contract).
export SMOKE_SELECTED_TEXT=${SMOKE_SELECTED_TEXT:-smoke-selected-sentinel-$$}
# M5.5.13: the masked live-log snippet sent to the fake Provider is also
# request-only: never logged, and only browser-visible masked text travels.
export SMOKE_LOG_TEXT=${SMOKE_LOG_TEXT:-smoke-log-sentinel-$$}
# M5.7 Wave B2: attachment body text sent to the fake Provider is request-only
# too: never logged, never persisted, never echoed back to the browser.
export SMOKE_ATTACH_TEXT=${SMOKE_ATTACH_TEXT:-smoke-attach-sentinel-$$}
# M5.7 Wave C2: the fake official ima service credentials (Client ID / API
# Key). The knowledge tools resolve them through a DLR access_key Credential
# (Secret Store); the fake echoes the API Key inside read content on purpose
# so the smoke proves by-value redaction end to end.
export SMOKE_IMA_TOKEN=${SMOKE_IMA_TOKEN:-smoke-ima-token-$$}
export SMOKE_IMA_CLIENT_ID=${SMOKE_IMA_CLIENT_ID:-smoke-ima-client-$$}
# M5.7 Wave C2: the read-only KnowledgeSource deployment config. Exported
# before `docker compose up -d` so the Control service (which handles the
# assist requests) boots with them via the docker-compose env passthrough.
# The endpoint points at the fake official ima service on the private smoke
# network; DLR_IMA_ALLOW_HTTP is the explicit test/smoke escape hatch.
IMA_FAKE_CONTAINER_NAME="${COMPOSE_PROJECT_NAME}-ima-fake"
export DLR_IMA_ENDPOINT="http://${IMA_FAKE_CONTAINER_NAME}:18081"
export DLR_IMA_ALLOWED_HOSTS="ima.qq.com,${IMA_FAKE_CONTAINER_NAME}"
export DLR_IMA_ALLOW_HTTP=1
export DLR_IMA_CREDENTIAL_NAME="smoke-ima"
AI_FAKE_CONTAINER_ID=""
AI_FAKE_DISABLED_CONTAINER_ID=""
IMA_FAKE_CONTAINER_ID=""
AO_DOCKER_LABEL="ao.session=${AO_SESSION_ID:-compose-smoke}"
ACCOUNT_COOKIE_FILE=""
ACCOUNT_STALE_COOKIE_FILE=""
SMOKE_SANDBOX_UNIT=""

cleanup() {
  smoke_status=$?
  if [ "$smoke_status" -ne 0 ]; then
    docker compose -p "$COMPOSE_PROJECT_NAME" logs --no-color --tail 100 control worker >&2 || true
  fi
  if [ -n "$AI_FAKE_CONTAINER_ID" ]; then
    docker rm -f "$AI_FAKE_CONTAINER_ID" >/dev/null 2>&1 || true
  fi
  if [ -n "$AI_FAKE_DISABLED_CONTAINER_ID" ]; then
    docker rm -f "$AI_FAKE_DISABLED_CONTAINER_ID" >/dev/null 2>&1 || true
  fi
  if [ -n "$IMA_FAKE_CONTAINER_ID" ]; then
    docker rm -f "$IMA_FAKE_CONTAINER_ID" >/dev/null 2>&1 || true
  fi
  if [ -n "$ACCOUNT_COOKIE_FILE" ]; then
    rm -f "$ACCOUNT_COOKIE_FILE" "$ACCOUNT_STALE_COOKIE_FILE"
  fi
  docker compose -p "$COMPOSE_PROJECT_NAME" down --volumes --remove-orphans
  if [ -n "$SMOKE_SANDBOX_UNIT" ]; then
    # --collect may already have removed a failed transient unit. Preserve
    # the original smoke failure rather than replacing it with stop exit 5.
    sudo -n systemctl stop "$SMOKE_SANDBOX_UNIT" || true
  fi
  if [ "$DLR_PLATFORM_LOG_ROOT" = "$SMOKE_PLATFORM_LOG_ROOT" ]; then
    rm -rf "$DLR_PLATFORM_LOG_ROOT"
  fi
}
trap cleanup EXIT

echo "==> smoke project: $COMPOSE_PROJECT_NAME (Token port: $DLR_WEB_HOST_PORT; account port: $DLR_ACCOUNT_WEB_HOST_PORT)"
if [ -z "${DLR_SANDBOX_CGROUP_PARENT:-}" ] || [ -z "${DLR_SANDBOX_CGROUP_SOURCE:-}" ]; then
  docker_endpoint=${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}
  if [ "$(uname -s)" != Linux ] || [[ "$docker_endpoint" != unix://* ]]; then
    echo 'ERROR: run scripts/prepare-sandbox-host.sh on the Linux Docker daemon host, then export both returned DLR_SANDBOX_CGROUP_* values before this smoke.' >&2
    exit 1
  fi
  # This unit is unique to this smoke. Never stop an operator-supplied parent.
  smoke_unit_candidate="dlr-compose-smoke-$(date +%s)-$$.service"
  if [ "$(systemctl show "$smoke_unit_candidate" -p LoadState --value)" != not-found ]; then
    echo 'ERROR: smoke Sandbox unit name is already in use' >&2
    exit 1
  fi
  SMOKE_SANDBOX_UNIT=$smoke_unit_candidate
  sandbox_host_config=$(sudo -n bash scripts/prepare-sandbox-host.sh --unit "$SMOKE_SANDBOX_UNIT" --cpu-quota "${COMPOSE_SMOKE_CPU_QUOTA:-300%}")
  while IFS='=' read -r setting value; do
    case "$setting" in
      DLR_SANDBOX_CGROUP_PARENT) export DLR_SANDBOX_CGROUP_PARENT="$value" ;;
      DLR_SANDBOX_CGROUP_SOURCE) export DLR_SANDBOX_CGROUP_SOURCE="$value" ;;
      *) echo 'ERROR: unexpected Sandbox host preparation output' >&2; exit 1 ;;
    esac
  done <<< "$sandbox_host_config"
fi
docker compose config --format json | python3 scripts/check-runtime-deployment.py
# M5.5.8: the optional DNS override file must always parse, and the default
# compose config must stay valid without any DNS-related .env variables.
docker compose -f docker-compose.yml config -q
docker compose -f docker-compose.yml -f docker-compose.dns.example.yml config -q
# M5.5.8: default DNS fallback is present and overridable/disableable via .env.
docker compose -f docker-compose.yml config | grep -q "1.1.1.1"
docker compose -f docker-compose.yml config | grep -q "127.0.0.11"
if DLR_DNS_FALLBACK_1= DLR_DNS_FALLBACK_2= docker compose -f docker-compose.yml config \
  | grep -q "1.1.1.1"; then
  echo "ERROR: disabled DNS fallback still in config" >&2
  exit 1
fi
echo "==> running isolated PostgreSQL init/health regression"
./scripts/compose-postgres-init-health.sh
# M5.5.8: the README-standard `cp .env.example .env` path must keep the public
# DNS fallback. .env.example deliberately ships no active DLR_DNS_FALLBACK_*
# assignments because an empty-but-set value overrides the compose default.
env_example_check=$(mktemp -d)
cp .env.example "$env_example_check/.env"
if ! docker compose --env-file "$env_example_check/.env" -f docker-compose.yml config \
  | grep -q "1.1.1.1"; then
  echo "ERROR: .env.example copied per README loses the public DNS fallback" >&2
  rm -rf "$env_example_check"
  exit 1
fi
rm -rf "$env_example_check"
docker compose build
docker compose up -d postgres rabbitmq

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
docker compose run --rm --no-deps control alembic upgrade head

# Start Control only after the schema exists.  Web and Worker require a
# healthy Control, whose health endpoint queries the migrated runtime tables.
docker compose up -d

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
curl -fsS "http://localhost:${DLR_ACCOUNT_WEB_HOST_PORT}/" | grep -q "DataLinkRuntime"
curl -fsS "http://localhost:${DLR_ACCOUNT_WEB_HOST_PORT}/api/health" | grep -q '"database":true'
no_token_status=$(curl -s -o /dev/null -w '%{http_code}' \
  "http://localhost:${DLR_WEB_HOST_PORT}/api/adapters")
[ "$no_token_status" = "401" ]
wrong_token_status=$(curl -s -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer ${DLR_WORKER_TOKEN}" \
  "http://localhost:${DLR_WEB_HOST_PORT}/api/adapters")
[ "$wrong_token_status" = "401" ]

echo "==> checking the account entry, forced password change and logout"
ACCOUNT_COOKIE_FILE=$(mktemp)
ACCOUNT_STALE_COOKIE_FILE=$(mktemp)
curl -fsS -c "$ACCOUNT_COOKIE_FILE" -b "$ACCOUNT_COOKIE_FILE" \
  "http://localhost:${DLR_ACCOUNT_WEB_HOST_PORT}/api/auth/account/csrf" >/dev/null
account_csrf=$(awk '$6 == "dlr_account_csrf" { print $7 }' "$ACCOUNT_COOKIE_FILE")
[ -n "$account_csrf" ]

echo "==> checking account login throttling and proxy-header boundary"
for attempt in 1 2 3 4 5; do
  throttle_status=$(curl -sS -o /dev/null -w '%{http_code}' \
    -c "$ACCOUNT_COOKIE_FILE" -b "$ACCOUNT_COOKIE_FILE" \
    -H "Content-Type: application/json" -H "X-CSRF-Token: $account_csrf" \
    -H "X-Forwarded-For: 198.51.100.$attempt" \
    -H "X-Real-IP: 203.0.113.$attempt" \
    -d '{"username":"throttle-probe","password":"wrong-password"}' \
    "http://localhost:${DLR_ACCOUNT_WEB_HOST_PORT}/api/auth/account/login")
  [ "$throttle_status" = "401" ]
done
throttle_status=$(curl -sS -o /dev/null -w '%{http_code}' \
  -c "$ACCOUNT_COOKIE_FILE" -b "$ACCOUNT_COOKIE_FILE" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $account_csrf" \
  -H "X-Forwarded-For: 192.0.2.200" -H "X-Real-IP: 192.0.2.200" \
  -d '{"username":"throttle-probe","password":"wrong-password"}' \
  "http://localhost:${DLR_ACCOUNT_WEB_HOST_PORT}/api/auth/account/login")
[ "$throttle_status" = "429" ]

account_login_body=$(curl -fsS -c "$ACCOUNT_COOKIE_FILE" -b "$ACCOUNT_COOKIE_FILE" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $account_csrf" \
  -d '{"username":"admin","password":"admin123"}' \
  "http://localhost:${DLR_ACCOUNT_WEB_HOST_PORT}/api/auth/account/login")
echo "$account_login_body" | grep -q '"must_change_password":true'
cp "$ACCOUNT_COOKIE_FILE" "$ACCOUNT_STALE_COOKIE_FILE"
forced_status=$(curl -s -o /dev/null -w '%{http_code}' \
  -b "$ACCOUNT_COOKIE_FILE" \
  "http://localhost:${DLR_ACCOUNT_WEB_HOST_PORT}/api/adapters")
[ "$forced_status" = "403" ]
account_csrf=$(awk '$6 == "dlr_account_csrf" { print $7 }' "$ACCOUNT_COOKIE_FILE")
change_status=$(curl -s -o /dev/null -w '%{http_code}' \
  -c "$ACCOUNT_COOKIE_FILE" -b "$ACCOUNT_COOKIE_FILE" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $account_csrf" \
  -d '{"current_password":"admin123","new_password":"wave-a-new-admin-123"}' \
  "http://localhost:${DLR_ACCOUNT_WEB_HOST_PORT}/api/auth/account/change-password")
[ "$change_status" = "200" ]
stale_status=$(curl -s -o /dev/null -w '%{http_code}' \
  -b "$ACCOUNT_STALE_COOKIE_FILE" \
  "http://localhost:${DLR_ACCOUNT_WEB_HOST_PORT}/api/auth/account/me")
[ "$stale_status" = "401" ]
curl -fsS -c "$ACCOUNT_COOKIE_FILE" -b "$ACCOUNT_COOKIE_FILE" \
  "http://localhost:${DLR_ACCOUNT_WEB_HOST_PORT}/api/auth/account/csrf" >/dev/null
account_csrf=$(awk '$6 == "dlr_account_csrf" { print $7 }' "$ACCOUNT_COOKIE_FILE")
account_login_body=$(curl -fsS -c "$ACCOUNT_COOKIE_FILE" -b "$ACCOUNT_COOKIE_FILE" \
  -H "Content-Type: application/json" -H "X-CSRF-Token: $account_csrf" \
  -d '{"username":"admin","password":"wave-a-new-admin-123"}' \
  "http://localhost:${DLR_ACCOUNT_WEB_HOST_PORT}/api/auth/account/login")
echo "$account_login_body" | grep -q '"must_change_password":false'
account_csrf=$(awk '$6 == "dlr_account_csrf" { print $7 }' "$ACCOUNT_COOKIE_FILE")
curl -fsS -c "$ACCOUNT_COOKIE_FILE" -b "$ACCOUNT_COOKIE_FILE" \
  -H "X-CSRF-Token: $account_csrf" -X POST \
  "http://localhost:${DLR_ACCOUNT_WEB_HOST_PORT}/api/auth/account/logout" >/dev/null
logout_status=$(curl -s -o /dev/null -w '%{http_code}' \
  -b "$ACCOUNT_COOKIE_FILE" \
  "http://localhost:${DLR_ACCOUNT_WEB_HOST_PORT}/api/auth/account/me")
[ "$logout_status" = "401" ]

echo "==> verifying the default DNS fallback wiring (host-side)"
for service in control worker; do
  container_id=$(docker compose ps -q "$service")
  dns_list=$(docker inspect --format '{{.HostConfig.Dns}}' "$container_id")
  echo "$dns_list" | grep -q "127.0.0.11" || { echo "ERROR: $service missing embedded DNS" >&2; exit 1; }
  echo "$dns_list" | grep -q "1.1.1.1" || { echo "ERROR: $service missing DNS fallback 1.1.1.1" >&2; exit 1; }
  echo "$dns_list" | grep -q "8.8.8.8" || { echo "ERROR: $service missing DNS fallback 8.8.8.8" >&2; exit 1; }
done

echo "==> starting isolated local OpenAI-compatible fake Provider"
AI_FAKE_CONTAINER_NAME="${COMPOSE_PROJECT_NAME}-ai-fake"
export AI_FAKE_BASE_URL="http://${AI_FAKE_CONTAINER_NAME}:18080"
CONTROL_CONTAINER_ID=$(docker compose ps -q control)
CONTROL_IMAGE=$(docker inspect --format '{{.Config.Image}}' "$CONTROL_CONTAINER_ID")
CONTROL_NETWORK=$(docker inspect --format '{{range $network, $_ := .NetworkSettings.Networks}}{{println $network}}{{end}}' \
  "$CONTROL_CONTAINER_ID" | head -n 1)
AI_FAKE_CONTAINER_ID=$(docker run -d \
  --label "$AO_DOCKER_LABEL" \
  --name "$AI_FAKE_CONTAINER_NAME" \
  --network "$CONTROL_NETWORK" \
  -e SMOKE_SELECTED_TEXT \
  -e SMOKE_LOG_TEXT \
  -e SMOKE_ATTACH_TEXT \
  -e SMOKE_IMA_TOKEN \
  -e SMOKE_IMA_CLIENT_ID \
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
  --label "$AO_DOCKER_LABEL" \
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

echo "==> starting isolated fake official ima-compatible knowledge service (M5.7 Wave C2)"
IMA_FAKE_CONTAINER_ID=$(docker run -d \
  --label "$AO_DOCKER_LABEL" \
  --name "$IMA_FAKE_CONTAINER_NAME" \
  --network "$CONTROL_NETWORK" \
  -e SMOKE_IMA_TOKEN \
  -e SMOKE_IMA_CLIENT_ID \
  -e SMOKE_IMA_BASE_URL="$DLR_IMA_ENDPOINT" \
  --volume "$PWD/scripts/ima-fake-service.py:/tmp/dlr-ima-fake-service.py:ro" \
  --entrypoint python \
  "$CONTROL_IMAGE" /tmp/dlr-ima-fake-service.py --port 18081)

elapsed=0
while ! docker compose exec -T -e DLR_IMA_ENDPOINT control python -c \
  'import os, urllib.request; urllib.request.urlopen(os.environ["DLR_IMA_ENDPOINT"] + "/healthz", timeout=2).read()' \
  >/dev/null 2>&1; do
  if [ "$elapsed" -ge 60 ]; then
    docker logs --tail 50 "$IMA_FAKE_CONTAINER_ID"
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
  -e SMOKE_LOG_TEXT \
  -e SMOKE_ATTACH_TEXT \
  -e AI_FAKE_BASE_URL \
  -e AI_FAKE_DISABLED_BASE_URL \
  -e SMOKE_IMA_TOKEN \
  -e SMOKE_IMA_CLIENT_ID \
  control python - < scripts/compose-smoke.py

echo "==> verifying secrets did not enter service logs"
if docker compose logs control worker web account-web | grep -F "$SMOKE_STORED_SECRET" >/dev/null; then
  echo "ERROR: stored secret appeared in service logs" >&2
  exit 1
fi
# M5.5.5/5.13: the administrator-selected code and the masked log snippet
# are request payload, never log lines.
if docker compose logs control worker web account-web | grep -F "$SMOKE_SELECTED_TEXT" >/dev/null; then
  echo "ERROR: selected code appeared in service logs" >&2
  exit 1
fi
if docker compose logs control worker web account-web | grep -F "$SMOKE_LOG_TEXT" >/dev/null; then
  echo "ERROR: log snippet appeared in service logs" >&2
  exit 1
fi
# M5.7 Wave B2: attachment body text never enters service logs either.
if docker compose logs control worker web account-web | grep -F "$SMOKE_ATTACH_TEXT" >/dev/null; then
  echo "ERROR: attachment text appeared in service logs" >&2
  exit 1
fi
# M5.7 Wave C2: the ima credential truth must never enter service logs
# (the fake ima service echoes the API Key inside the read content on purpose).
if docker compose logs control worker web account-web | grep -F "$SMOKE_IMA_TOKEN" >/dev/null; then
  echo "ERROR: ima credential appeared in service logs" >&2
  exit 1
fi
if docker compose logs control worker web account-web | grep -F "$SMOKE_IMA_CLIENT_ID" >/dev/null; then
  echo "ERROR: ima client id appeared in service logs" >&2
  exit 1
fi

echo "==> compose smoke test passed"
