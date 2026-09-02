#!/usr/bin/env bash
# Focused static audit for the opt-in Linux Worker Compose override.
set -euo pipefail

cd "$(dirname "$0")/.."

example_parent=/system.slice/dlr-worker-sandbox-example.service
example_path=/sys/fs/cgroup${example_parent}
runbook=docs/zh-CN/issue130-sandbox-deployment.md

docker_cgroup_driver=$(docker info --format '{{.CgroupDriver}}')
docker_cgroup_version=$(docker info --format '{{.CgroupVersion}}')
if [[ "$docker_cgroup_driver" != cgroupfs || "$docker_cgroup_version" != 2 ]]; then
  echo "expected Colima target Docker cgroupfs v2, got driver=$docker_cgroup_driver version=$docker_cgroup_version" >&2
  exit 1
fi

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
if grep -Eq '^[[:space:]]+user:' docker-compose.sandbox.yml; then
  echo "Worker user override would remove effective CAP_SYS_ADMIN under Docker" >&2
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
require_literal "    group_add:"
if ! grep -Eq "^      - (\\\"1000\\\"|'1000'|1000)$" <<<"$rendered"; then
  echo "missing Compose contract: group_add gid 1000" >&2
  exit 1
fi
require_literal "    cgroup: host"
require_literal "      DLR_SANDBOX_CGROUP_PATH: /run/dlr-cgroup"
require_literal "    cgroup_parent: ${example_parent}"
require_literal "        source: ${example_path}"
require_literal "        target: /run/dlr-cgroup"

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

require_runbook_literal() {
  local literal=$1
  if ! grep -Fq -- "$literal" "$runbook"; then
    echo "missing runbook provisioning contract: $literal" >&2
    exit 1
  fi
}

require_runbook_literal '--property=User=king'
require_runbook_literal '--property=Group=king'
require_runbook_literal '--property=Delegate=yes'
require_runbook_literal '--property=TasksMax=infinity'
require_runbook_literal '--expand-environment=no'
require_runbook_literal 'AGENT="$CONTROL_GROUP/agent"'
require_runbook_literal 'mkdir -p "$AGENT"'
require_runbook_literal 'printf "%s\\n" "$$" > "$AGENT/cgroup.procs"'
require_runbook_literal 'test -z "$(cat "$CONTROL_GROUP/cgroup.procs")"'
require_runbook_literal 'grep -qx "$$" "$AGENT/cgroup.procs"'
require_runbook_literal 'chmod 0770 "$CONTROL_GROUP"'
require_runbook_literal 'printf "+cpu +memory +pids\\n" > "$CONTROL_GROUP/cgroup.subtree_control"'
require_runbook_literal 'SUBTREE_CONTROL=$(cat "$CONTROL_GROUP/cgroup.subtree_control")'
require_runbook_literal 'for controller in cpu memory pids; do'
require_runbook_literal 'ATTEMPT="$CONTROL_GROUP/attempt"'
require_runbook_literal 'mkdir -p "$ATTEMPT"'
require_runbook_literal 'test "$(dirname "$ATTEMPT")" = "$CONTROL_GROUP"'
require_runbook_literal 'for interface in cpu.max memory.max memory.swap.max pids.max; do'
require_runbook_literal 'test -r "$ATTEMPT/$interface"'
require_runbook_literal 'test -w "$ATTEMPT/$interface"'
require_runbook_literal 'printf "100000 100000\\n" > "$ATTEMPT/cpu.max"'
require_runbook_literal 'printf "67108864\\n" > "$ATTEMPT/memory.max"'
require_runbook_literal 'printf "0\\n" > "$ATTEMPT/memory.swap.max"'
require_runbook_literal 'printf "64\\n" > "$ATTEMPT/pids.max"'
require_runbook_literal 'test "$(cat "$ATTEMPT/cpu.max")" = "100000 100000"'
require_runbook_literal 'test "$(cat "$ATTEMPT/memory.max")" = "67108864"'
require_runbook_literal 'test "$(cat "$ATTEMPT/memory.swap.max")" = "0"'
require_runbook_literal 'test "$(cat "$ATTEMPT/pids.max")" = "64"'
require_runbook_literal 'printf "%s\\n" "$BASHPID" > "$ATTEMPT/cgroup.procs"'
require_runbook_literal 'WORKLOAD_PID=$!'
require_runbook_literal 'grep -qx "$WORKLOAD_PID" "$ATTEMPT/cgroup.procs"'
require_runbook_literal 'test -z "$(cat "$PARENT/cgroup.procs")"'
require_runbook_literal 'grep -qx "$KEEPER_PID" "$PARENT/agent/cgroup.procs"'
require_runbook_literal 'test -s "$PARENT/attempt/cgroup.procs"'
require_runbook_literal 'test -w "$PARENT/attempt/$interface"'
require_runbook_literal 'NO_NEW_PRIVS=$(awk '\''$1 == "NoNewPrivs:" { print $2; exit }'\'' /proc/self/status)'
require_runbook_literal 'test "$NO_NEW_PRIVS" = 1'
require_runbook_literal 'docker info --format '\''CgroupDriver={{.CgroupDriver}} CgroupVersion={{.CgroupVersion}}'\'''

if grep -Eq 'agent/keeper|KEEPER=' "$runbook"; then
  echo "runbook must not create a second keeper cgroup" >&2
  exit 1
fi

if grep -Eq '\$(CONTROL_GROUP|AGENT|PARENT)/(cpu\.max|memory\.max|memory\.swap\.max|pids\.max)' "$runbook"; then
  echo "runbook must write controller limits only in the attempt child" >&2
  exit 1
fi

if grep -Eq '^[[:space:]]+/bin/sleep infinity$' "$runbook"; then
  echo "runbook must not leave the keeper directly in the unit parent" >&2
  exit 1
fi

echo "issue130-b3-compose-audit=PASS"
