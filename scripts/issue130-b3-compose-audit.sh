#!/usr/bin/env bash
# Focused static audit for the opt-in Linux Worker Compose override.
set -euo pipefail

cd "$(dirname "$0")/.."

example_parent=/system.slice/dlr-worker-sandbox-example.service
example_path=/sys/fs/cgroup${example_parent}

rendered=$(
  DLR_RABBITMQ_USER=EXAMPLE_RABBITMQ_USER \
  DLR_RABBITMQ_PASSWORD=EXAMPLE_RABBITMQ_PASSWORD \
  DLR_ADMIN_TOKEN=EXAMPLE_ADMIN_TOKEN \
  DLR_WORKER_TOKEN=EXAMPLE_WORKER_TOKEN \
  DLR_SANDBOX_CGROUP_PARENT="$example_parent" \
  DLR_SANDBOX_CGROUP_PATH="$example_path" \
  docker compose -f docker-compose.yml -f docker-compose.sandbox.yml config
)

if ! grep -Eq '^[[:space:]]+privileged:[[:space:]]+false$' docker-compose.sandbox.yml; then
  echo "missing explicit privileged=false in the Sandbox override" >&2
  exit 1
fi

require_literal() {
  local literal=$1
  if ! grep -Fqx -- "$literal" <<<"$rendered"; then
    echo "missing Compose contract: $literal" >&2
    exit 1
  fi
}

require_literal "      - SYS_ADMIN"
require_literal "      - ALL"
require_literal "      - no-new-privileges:true"
require_literal "    cgroup_parent: ${example_parent}"
require_literal "        source: ${example_path}"
require_literal "        target: /sys/fs/cgroup/dlr"

if ! grep -Eq '^[[:space:]]+read_only:[[:space:]]+false$' docker-compose.sandbox.yml; then
  echo "missing explicit writable exact cgroup bind" >&2
  exit 1
fi

if grep -Eq '^[[:space:]]+privileged:[[:space:]]*true$' <<<"$rendered"; then
  echo "forbidden privileged=true in Worker Compose config" >&2
  exit 1
fi
if grep -Fq '/var/run/docker.sock' <<<"$rendered" || grep -Fq '/run/docker.sock' <<<"$rendered"; then
  echo "forbidden Docker socket in Worker Compose config" >&2
  exit 1
fi
if grep -Eq '/sys/fs/cgroup([[:space:]]|$|:)' <<<"$rendered"; then
  echo "forbidden broad cgroup filesystem mount in Worker Compose config" >&2
  exit 1
fi

echo "issue130-b3-compose-audit=PASS"
