#!/usr/bin/env bash
# Focused static audit for the opt-in Linux Worker Compose override.
set -euo pipefail

cd "$(dirname "$0")/.."

example_parent=/system.slice/dlr-worker-sandbox-example.service
example_path=/sys/fs/cgroup${example_parent}
runbook=docs/zh-CN/issue130-sandbox-deployment.md
sandbox_source=backend/src/dlr/worker/sandbox.py
consumer_source=backend/src/dlr/worker/consumer.py
executor_source=backend/src/dlr/worker/executor.py
runtime_tests=backend/tests/test_issue130_b3_runtime.py

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
  DLR_SANDBOX_CGROUP_SOURCE="$example_path" \
  DLR_SANDBOX_CGROUP_PATH=/run/dlr-cgroup \
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
require_literal "      - SETUID"
require_literal "      - SETGID"
require_literal "      - ALL"
require_literal "      - no-new-privileges:true"
require_literal "      - apparmor=unconfined"
expected_cap_add=$'      - SYS_ADMIN\n      - SETUID\n      - SETGID'
actual_cap_add=$(awk '
  /^    cap_add:$/ { in_block=1; next }
  in_block && /^    [[:alnum:]_]+:/ { exit }
  in_block && /^      - / { print }
' docker-compose.sandbox.yml)
if [[ "$actual_cap_add" != "$expected_cap_add" ]]; then
  echo "Worker cap_add must be exactly SYS_ADMIN, SETUID, SETGID" >&2
  exit 1
fi
if grep -Fq 'seccomp=unconfined' docker-compose.sandbox.yml || grep -Fq 'seccomp=unconfined' <<<"$rendered"; then
  echo "seccomp must remain Docker's default profile" >&2
  exit 1
fi
if grep -Eq '^[[:space:]]+group_add:' docker-compose.sandbox.yml; then
  echo "group_add is not needed with the root-owned exact delegated parent" >&2
  exit 1
fi
require_literal "    cgroup: private"
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

require_runbook_literal '--property=Delegate=yes'
require_runbook_literal '--property=TasksMax=infinity'
require_runbook_literal "--property='CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SETUID CAP_SETGID'"
require_runbook_literal '--expand-environment=no'
if grep -Fq -- 'AmbientCapabilities=' "$runbook"; then
  echo "keeper must not require AmbientCapabilities on the root-owned delegated unit" >&2
  exit 1
fi
require_runbook_literal 'root:root'
require_runbook_literal 'owner access is sufficient'
require_runbook_literal 'AGENT="$CONTROL_GROUP/agent"'
require_runbook_literal 'mkdir -p "$AGENT"'
require_runbook_literal 'printf "%s\\n" "$$" > "$AGENT/cgroup.procs"'
require_runbook_literal 'test -z "$(cat "$CONTROL_GROUP/cgroup.procs")"'
require_runbook_literal 'grep -qx "$$" "$AGENT/cgroup.procs"'
require_runbook_literal 'test -w "$CONTROL_GROUP/cgroup.procs"'
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
require_runbook_literal 'test "$(stat -c '\''%u:%g'\'' "$PARENT")" = 0:0'
require_runbook_literal 'test "$(stat -c '\''%u:%g'\'' "$PARENT/cgroup.procs")" = 0:0'
require_runbook_literal 'test -w "$PARENT/cgroup.procs"'
require_runbook_literal 'test -n "$(cat "$PARENT/attempt/cgroup.procs")"'
require_runbook_literal 'test -w "$PARENT/attempt/$interface"'
require_runbook_literal 'NO_NEW_PRIVS=$(awk '\''$1 == "NoNewPrivs:" { print $2; exit }'\'' /proc/self/status)'
require_runbook_literal 'test "$NO_NEW_PRIVS" = 1'
require_runbook_literal 'docker info --format '\''CgroupDriver={{.CgroupDriver}} CgroupVersion={{.CgroupVersion}}'\'''
require_runbook_literal 'cgroup2fs'
require_runbook_literal 'read_blocked'
require_runbook_literal 'write_blocked'
require_runbook_literal 'docker-default'
require_runbook_literal 'apparmor=unconfined'
require_runbook_literal 'phase=mount_namespace_private'
require_runbook_literal 'errno=13'
require_runbook_literal 'adapter_mount_blocked'
require_runbook_literal '0711'
require_runbook_literal 'workspace_ownership'
require_runbook_literal 'follow_symlinks=False'
require_runbook_literal 'output.json'
require_runbook_literal 'temp` fill'
require_runbook_literal '所有 descendant 路径'
require_runbook_literal 'recursive-bind'
require_runbook_literal 'task-owned `/tmp/.dlr-sandbox-*`'
require_runbook_literal 'fork 出来的 payload PID'
require_runbook_literal 'helper 也始终留在 Worker'
require_runbook_literal 'cgroup.kill` 只终止 Attempt payload'

require_source_literal() {
  local literal=$1
  if ! grep -Fq -- "$literal" "$sandbox_source"; then
    echo "missing recovery authorization guard: $literal" >&2
    exit 1
  fi
}

require_source_literal 'def _derived_recovery_mount(runtime_root: Path, name: str, execution_id: int) -> Path:'
require_source_literal 'return root / "workspaces" / f"attempt-{int(parts[2])}" / ".dlr-sandbox-mount"'
require_source_literal 'if mount != expected_mount:'
require_source_literal 'expected_mount = _derived_recovery_mount(runtime_root, name, execution_id)'
require_source_literal 'CGROUP2_SUPER_MAGIC'
require_source_literal 'def _filesystem_magic(path: Path) -> int:'
require_source_literal 'def _validated_hidden_cgroup_path('
require_source_literal 'configured cgroup path is required'
require_source_literal 'MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC'
require_source_literal 'hidden_mounts.append(str(exact_cgroup_mount))'
require_source_literal 'for target in reversed(hidden_mounts):'
require_source_literal 'class HelperDiagnostic:'
require_source_literal 'def _parse_helper_diagnostic(value: bytes | str) -> HelperDiagnostic | None:'
require_source_literal 'DLR_SANDBOX_HELPER_DIAGNOSTIC phase='
require_source_literal '"--diagnostic-fd"'
require_source_literal 'os.set_inheritable(diagnostic_fd, False)'
require_source_literal 'adapter_mount_blocked'
require_source_literal 'def mount_blocked():'
require_source_literal 'Adapter mount unexpectedly succeeded'
require_source_literal 'DLR_PREFLIGHT_PAYLOAD_UID'
require_source_literal 'CAP_SETGID = 6'
require_source_literal 'CAP_SETUID = 7'
require_source_literal 'CAP_SYS_ADMIN = 21'
require_source_literal 'SUPERVISOR_CAPABILITY_MASK'
require_source_literal "cap_prm=int(field('CapPrm') or '0',16)"
require_source_literal "cap_inh=int(field('CapInh') or '0',16)"
require_source_literal "cap_bnd=int(field('CapBnd') or '0',16)"
require_source_literal "cap_amb=int(field('CapAmb') or '0',16)"
require_source_literal 'assert cap_bnd & ~allowed_caps == 0'
require_source_literal 'assert cap_inh==0'
require_source_literal 'def _filesystem_identity(uid: int, gid: int) -> Iterator[None]:'
require_source_literal 'setfsuid = getattr(libc, "setfsuid", None)'
require_source_literal 'setfsgid = getattr(libc, "setfsgid", None)'
require_source_literal 'def _copy_tree_as_owner(source: Path, target: Path, uid: int, gid: int) -> None:'
require_source_literal 'def _replace_workspace(command: list[str], source: Path, target: Path) -> list[str]:'
require_source_literal 'relative = item_path.relative_to(source_path)'
require_source_literal 'os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))'
require_source_literal 'os.symlink(link, target_path)'
require_source_literal 'if entry.is_dir(follow_symlinks=False):'
require_source_literal 'stage = "workspace_ownership"'
require_source_literal 'f"size={tmp_bytes},mode=0711"'
require_source_literal 'MS_BIND | MS_REC'
require_source_literal 'payload_root = Path("/tmp")'
require_source_literal 'created_payload_root = False'
require_source_literal 'payload_outer_fd'
require_source_literal 'payload_root.rmdir()'
require_source_literal 'os.fchdir(payload_outer_fd)'
require_source_literal 'payload_workspace = Path(f"/proc/self/fd/{inner_fd}/{host_workspace.name}")'
require_source_literal 'parser.add_argument("--attempt-cgroup", required=True)'
require_source_literal 'Path(args.attempt_cgroup)'
require_source_literal 'self._payload_pid: int | None = None'
require_source_literal 'return self._payload_pid'
require_source_literal 'details["helper_outside_attempt"]'
require_source_literal 'attempt.payload_pid'

if ! grep -Fq 'prevalidated_profile' "$consumer_source" \
  || ! grep -Fq 'raw_payload.get("resource_profile")' "$consumer_source" \
  || ! grep -Fq 'V3TaskPayload.model_validate(raw_payload)' "$consumer_source"; then
  echo "Consumer must validate the raw Resource Profile before Pydantic/model side effects" >&2
  exit 1
fi
if ! grep -Fq 'sandbox_diagnostic' "$executor_source" \
  || ! grep -Fq 'helper_diagnostic' "$executor_source"; then
  echo "Executor must preserve helper phase/errno diagnostics in the sandbox receipt" >&2
  exit 1
fi

if grep -Fq 'if exact_cgroup_mount.is_dir()' "$sandbox_source"; then
  echo "exact configured cgroup hide must not silently skip a missing target" >&2
  exit 1
fi
if ! grep -Fq 'if not cgroup_mount.is_dir()' "$sandbox_source"; then
  echo "canonical cgroup mount must be a required hide target" >&2
  exit 1
fi
if ! grep -Fq 'configured cgroup path overlaps workspace' "$sandbox_source"; then
  echo "configured cgroup hide target must be disjoint from workspace" >&2
  exit 1
fi

if ! grep -Fq '"/run/dlr-cgroup"' "$runtime_tests" \
  || ! grep -Fq 'read_blocked' "$runtime_tests" \
  || ! grep -Fq 'write_blocked' "$runtime_tests"; then
  echo "runtime tests must probe read/write visibility at the exact bind target" >&2
  exit 1
fi
if ! grep -Fq 'test_helper_diagnostic_keeps_syscall_phase_and_errno_path_free' "$runtime_tests" \
  || ! grep -Fq 'test_supervisor_capability_mask_is_exactly_the_approved_three' "$runtime_tests" \
  || ! grep -Fq 'test_recovery_marker_cannot_authorize_an_unrelated_mount' "$runtime_tests" \
  || ! grep -Fq 'test_consumer_reports_intrinsic_profile_error_before_ceiling_or_model_validation' "$runtime_tests" \
  || ! grep -Fq 'test_copied_tmpfs_workspace_allows_payload_output_but_not_managed_input' "$runtime_tests"; then
  echo "runtime tests must cover helper diagnostics, forged-marker recovery, profile ordering, and workspace ownership" >&2
  exit 1
fi

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
