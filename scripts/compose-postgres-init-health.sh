#!/usr/bin/env bash
# Isolated Compose regression for PostgreSQL init-time log checks and target-db health.
set -euo pipefail

cd "$(dirname "$0")/.."

REPO_ROOT=$PWD
RUN_ID_SOURCE="${COMPOSE_POSTGRES_REGRESSION_ID:-${AO_SESSION_ID:-$$}}"
RUN_ID=$(printf '%s' "$RUN_ID_SOURCE" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]/-/g')
if [[ ! "$RUN_ID" =~ ^[a-z0-9_-]*$ ]]; then
  echo "Invalid normalized PostgreSQL regression RUN_ID: $RUN_ID" >&2
  exit 1
fi
WORK_PARENT="${COMPOSE_POSTGRES_WORK_ROOT:-$REPO_ROOT/.tmp-platform-logs}"
mkdir -p "$WORK_PARENT"
WORK_ROOT=$(mktemp -d "$WORK_PARENT/dlr-i117-postgres-${RUN_ID}.XXXXXX")
EVIDENCE_DIR="${COMPOSE_POSTGRES_EVIDENCE_DIR:-}"
COMPOSE_FILE="$WORK_ROOT/compose.yml"
PROJECTS=()

cleanup() {
  if [ "${#PROJECTS[@]}" -gt 0 ]; then
    for project in "${PROJECTS[@]}"; do
      docker compose --env-file /dev/null -f "$COMPOSE_FILE" -p "$project" down \
        --volumes --remove-orphans --rmi local >/dev/null 2>&1 || true
    done
  fi
  rm -f "$COMPOSE_FILE"
  rm -rf "$WORK_ROOT"
}
trap cleanup EXIT

if [ -n "$EVIDENCE_DIR" ]; then
  mkdir -p "$EVIDENCE_DIR"
  EVIDENCE_FILE="$EVIDENCE_DIR/compose-postgres-init-health.md"
  : > "$EVIDENCE_FILE"
  {
    echo "# Issue #117 Batch 1 PostgreSQL Compose evidence"
    echo
    echo "- Generated: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "- Image source: docker/postgres.Dockerfile"
    echo
  } >> "$EVIDENCE_FILE"
else
  EVIDENCE_FILE=""
fi

write_evidence() {
  if [ -n "$EVIDENCE_FILE" ]; then
    printf '%s\n' "$@" >> "$EVIDENCE_FILE"
  fi
}

run_scenario() {
  local name="$1"
  local database="$2"
  local log_mode="$3"
  local scenario_root="$WORK_ROOT/$name"
  local log_root="$scenario_root/logs"
  local project="dlr-i117-b1-${RUN_ID}-${name}"
  local up_status
  local postgres_id
  local postgres_state
  local health
  local control_state
  local data_init_state
  local relevant_log
  local relevant_log_line
  local logs_file="$scenario_root/postgres.log"
  local up_file="$scenario_root/up.log"
  local marker="$scenario_root/control-started"

  mkdir -p "$scenario_root"
  if [ "$log_mode" != "missing" ]; then
    mkdir -p "$log_root"
    if [ "$log_mode" = "unwritable" ]; then
      chmod 0555 "$log_root"
    else
      # Disposable fixture: this mode is needed because the container's
      # postgres UID is not the host user on all Docker Desktop platforms.
      chmod 0777 "$log_root"
    fi
  fi

  {
    echo "services:"
    echo "  postgres:"
    echo "    build:"
    echo "      context: $REPO_ROOT"
    echo "      dockerfile: docker/postgres.Dockerfile"
    echo "    labels:"
    echo "      ao.session: ${AO_SESSION_ID:-compose-smoke}"
    echo "    environment:"
    echo "      POSTGRES_USER: dlr"
    echo "      POSTGRES_PASSWORD: EXAMPLE_POSTGRES_PASSWORD"
    echo "      POSTGRES_DB: $database"
    echo "      DLR_PLATFORM_LOG_ROOT: /var/lib/dlr/platform-logs"
    echo "    command:"
    echo "      - postgres"
    echo "      - -c"
    echo "      - logging_collector=on"
    echo "      - -c"
    echo "      - log_directory=/var/lib/dlr/platform-logs/postgres"
    echo "      - -c"
    echo "      - log_filename=postgresql-%Y-%m-%d_%H%M%S.log"
    echo "    volumes:"
    echo "      - pg_data:/var/lib/postgresql/data"
    if [ "$log_mode" != "missing" ]; then
      echo "      - type: bind"
      echo "        source: $log_root"
      echo "        target: /var/lib/dlr/platform-logs/postgres"
    fi
    echo "    healthcheck:"
    echo "      test: [\"CMD-SHELL\", \"psql --no-psqlrc --quiet -w -v ON_ERROR_STOP=1 -U dlr -d dlr -c 'SELECT 1' >/dev/null\"]"
    echo "      interval: 1s"
    echo "      timeout: 2s"
    echo "      retries: 3"
    echo "      start_period: 1s"
    echo "  control:"
    echo "    image: postgres:16-alpine"
    echo "    labels:"
    echo "      ao.session: ${AO_SESSION_ID:-compose-smoke}"
    echo "    entrypoint: [\"/bin/sh\", \"-ec\"]"
    echo "    command: [\"touch /evidence/control-started && sleep 60\"]"
    echo "    volumes:"
    echo "      - type: bind"
    echo "        source: $scenario_root"
    echo "        target: /evidence"
    echo "    depends_on:"
    echo "      postgres:"
    echo "        condition: service_healthy"
    echo "volumes:"
    echo "  pg_data:"
  } > "$COMPOSE_FILE"

  PROJECTS+=("$project")
  docker compose --env-file /dev/null -f "$COMPOSE_FILE" -p "$project" config --quiet
  if docker compose --env-file /dev/null -f "$COMPOSE_FILE" -p "$project" up -d --build \
    >"$up_file" 2>&1; then
    up_status=0
  else
    up_status=$?
  fi

  postgres_id=$(docker compose --env-file /dev/null -f "$COMPOSE_FILE" -p "$project" ps -a -q postgres || true)
  if [ -n "$postgres_id" ]; then
    for _ in $(seq 1 30); do
      health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
        "$postgres_id" 2>/dev/null || true)
      postgres_state=$(docker inspect --format '{{.State.Status}}' "$postgres_id" 2>/dev/null || true)
      if [ "$name" = "missing-target-database" ] && [ "$health" = "unhealthy" ]; then
        break
      fi
      if [ "$name" != "missing-target-database" ] && [ "$postgres_state" = "exited" ]; then
        break
      fi
      if [ "$name" = "healthy" ] && [ "$health" = "healthy" ]; then
        break
      fi
      sleep 1
    done
    health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
      "$postgres_id" 2>/dev/null || true)
    postgres_state=$(docker inspect --format '{{.State.Status}}' "$postgres_id" 2>/dev/null || true)
  else
    health="none"
    postgres_state="absent"
  fi
  docker compose --env-file /dev/null -f "$COMPOSE_FILE" -p "$project" logs --no-color postgres \
    >"$logs_file" 2>&1 || true
  if [ -e "$marker" ]; then
    control_state=started
  else
    control_state=not-started
  fi
  relevant_log=$(grep -Ei 'startup blocked|permission denied|could not open log file|database .* does not exist' \
    "$logs_file" | tail -1 | tr -d '\r' || true)
  if [ -n "$relevant_log" ]; then
    relevant_log_line="- Relevant log: $relevant_log"
  else
    relevant_log_line="- Relevant log:"
  fi
  data_init_state=not-checked
  if [ "$name" = "missing-log-directory" ] || [ "$name" = "unwritable-log-directory" ]; then
    if docker run --rm --label "ao.session=${AO_SESSION_ID:-compose-smoke}" \
      -v "${project}_pg_data:/data:ro" --entrypoint sh postgres:16-alpine \
      -ec 'test -z "$(find /data -mindepth 1 -print -quit)"'; then
      data_init_state=not-initialized
    else
      data_init_state=initialized
    fi
  fi

  case "$name" in
    missing-log-directory)
      [ "$postgres_state" = "exited" ]
      [ "$health" != "healthy" ]
      grep -Fq "platform log directory does not exist" "$logs_file"
      [ ! -e "$marker" ]
      [ "$data_init_state" = "not-initialized" ]
      ;;
    unwritable-log-directory)
      [ "$postgres_state" = "exited" ]
      [ "$health" != "healthy" ]
      grep -Fq "platform log directory is not writable by postgres" "$logs_file"
      [ ! -e "$marker" ]
      [ "$data_init_state" = "not-initialized" ]
      ;;
    missing-target-database)
      [ "$postgres_state" = "running" ]
      [ "$health" = "unhealthy" ]
      if docker compose --env-file /dev/null -f "$COMPOSE_FILE" -p "$project" exec -T postgres \
        pg_isready -U dlr -d dlr >/dev/null 2>&1; then
        :
      else
        echo "pg_isready unexpectedly failed in missing-target-database scenario" >&2
        return 1
      fi
      if docker compose --env-file /dev/null -f "$COMPOSE_FILE" -p "$project" exec -T postgres \
        psql --no-psqlrc --quiet -w -v ON_ERROR_STOP=1 -U dlr -d dlr -c 'SELECT 1' >/dev/null 2>&1; then
        echo "target database query unexpectedly succeeded" >&2
        return 1
      fi
      [ ! -e "$marker" ]
      ;;
    healthy)
      [ "$postgres_state" = "running" ]
      [ "$health" = "healthy" ]
      for _ in $(seq 1 10); do
        [ -e "$marker" ] && break
        sleep 1
      done
      [ -e "$marker" ]
      docker compose --env-file /dev/null -f "$COMPOSE_FILE" -p "$project" exec -T postgres \
        psql --no-psqlrc --quiet -w -v ON_ERROR_STOP=1 -U dlr -d dlr -c 'SELECT 1' >/dev/null
      ;;
    *)
      echo "unknown scenario: $name" >&2
      return 1
      ;;
  esac

  write_evidence "## $name" "" \
    "- Compose up exit: $up_status" \
    "- PostgreSQL state: $postgres_state" \
    "- PostgreSQL health: $health" \
    "- Control marker: $control_state" \
    "- Data volume init: $data_init_state" \
    "$relevant_log_line" \
    ""
  printf 'scenario=%s compose_up=%s postgres_state=%s health=%s control=%s\n' \
    "$name" "$up_status" "$postgres_state" "$health" "$control_state"

  docker compose --env-file /dev/null -f "$COMPOSE_FILE" -p "$project" down \
    --volumes --remove-orphans --rmi local >/dev/null 2>&1 || true
}

run_scenario missing-log-directory postgres missing
run_scenario unwritable-log-directory dlr unwritable
run_scenario missing-target-database postgres writable
run_scenario healthy dlr writable

write_evidence \
  '## Verification commands' '' \
  '- `docker compose -f docker-compose.yml config --quiet`' \
  '- `docker compose -f docker-compose.yml -f docker-compose.dns.example.yml config --quiet`' \
  "- \`docker run --rm --label ao.session=... postgres:16-alpine sh -c 'command -v su-exec'\`" \
  "- \`COMPOSE_POSTGRES_REGRESSION_ID=${RUN_ID} ./scripts/compose-postgres-init-health.sh\` (normalized RUN_ID: \`${RUN_ID}\`)" \
  '- `./scripts/compose-smoke.sh`' '' \
  'The recorded scenarios contain no credentials or raw service payloads. The healthy scenario records the target `dlr` database query gate and Control start; the three failure scenarios record the expected PostgreSQL/Control states. The two init-time failure scenarios also assert that the PostgreSQL data volume remains uninitialized.'

echo "PostgreSQL init/health Compose regression passed"
