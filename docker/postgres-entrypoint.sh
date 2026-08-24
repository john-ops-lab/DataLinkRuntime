#!/bin/sh
set -eu

log_dir="${DLR_PLATFORM_LOG_ROOT:-/var/lib/dlr/platform-logs}/postgres"

if [ ! -d "$log_dir" ]; then
    echo "PostgreSQL startup blocked: platform log directory does not exist: $log_dir" >&2
    exit 1
fi

if ! gosu postgres test -w "$log_dir"; then
    echo "PostgreSQL startup blocked: platform log directory is not writable by postgres: $log_dir" >&2
    exit 1
fi

exec /usr/local/bin/docker-entrypoint.sh "$@"
