#!/usr/bin/env bash
# Current deployment audit; retained filename for existing automation callers.
set -euo pipefail
cd "$(dirname "$0")/.."
export DLR_RABBITMQ_USER=EXAMPLE_RABBITMQ_USER
export DLR_RABBITMQ_PASSWORD=EXAMPLE_RABBITMQ_PASSWORD
export DLR_ADMIN_TOKEN=EXAMPLE_ADMIN_TOKEN
export DLR_WORKER_TOKEN=EXAMPLE_WORKER_TOKEN
export DLR_SANDBOX_CGROUP_PARENT=/system.slice/dlr-compose-audit.service
export DLR_SANDBOX_CGROUP_SOURCE=/sys/fs/cgroup/system.slice/dlr-compose-audit.service
docker compose --env-file /dev/null -f docker-compose.yml config --format json \
  | python3 scripts/check-runtime-deployment.py
docker compose --env-file /dev/null -f docker-compose.yml -f docker-compose.sandbox.yml config --format json \
  | python3 scripts/check-runtime-deployment.py
