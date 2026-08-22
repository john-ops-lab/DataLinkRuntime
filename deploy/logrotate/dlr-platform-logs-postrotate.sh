#!/bin/sh
# Reopen services after deploy/logrotate/dlr-platform-logs.conf renames files.
# Configure DLR_COMPOSE_PROJECT and DLR_COMPOSE_FILE in
# /etc/default/dlr-platform-logs (or the environment of logrotate).
set -eu

env_file=${DLR_PLATFORM_LOG_ENV_FILE:-/etc/default/dlr-platform-logs}
if [ -r "$env_file" ]; then
    # shellcheck disable=SC1090
    . "$env_file"
fi

project=${DLR_COMPOSE_PROJECT:-}
compose_file=${DLR_COMPOSE_FILE:-}
if [ -z "$project" ] || [ -z "$compose_file" ]; then
    echo "dlr-platform-logs-postrotate: set DLR_COMPOSE_PROJECT and DLR_COMPOSE_FILE" >&2
    exit 1
fi
if [ ! -f "$compose_file" ]; then
    echo "dlr-platform-logs-postrotate: compose file not found: $compose_file" >&2
    exit 1
fi

docker compose -f "$compose_file" -p "$project" exec -T web nginx -s reopen >/dev/null
docker compose -f "$compose_file" -p "$project" exec -T account-web nginx -s reopen >/dev/null

postgres_user=${DLR_POSTGRES_USER:-dlr}
postgres_db=${DLR_POSTGRES_DB:-dlr}
docker compose -f "$compose_file" -p "$project" exec -T postgres \
    psql -U "$postgres_user" -d "$postgres_db" -Atqc 'select pg_rotate_logfile();' >/dev/null
