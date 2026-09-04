#!/usr/bin/env bash
# Focused static audit for the opt-in Linux Worker Compose override.
set -euo pipefail

cd "$(dirname "$0")/.."

example_parent=/system.slice/dlr-worker-sandbox-example.service
example_path=/sys/fs/cgroup${example_parent}
runbook=docs/zh-CN/issue130-sandbox-deployment.md
sandbox_source=backend/src/dlr/worker/sandbox.py
cache_source=backend/src/dlr/worker/cache.py
venv_source=backend/src/dlr/worker/venv.py
workspace_source=backend/src/dlr/worker/workspace.py
consumer_source=backend/src/dlr/worker/consumer.py
executor_source=backend/src/dlr/worker/executor.py
runtime_tests=backend/tests/test_issue130_b3_runtime.py
runtime_unit_tests=backend/tests/test_runtime.py
cache_tests=backend/tests/test_worker_cache.py
multilang_tests=backend/tests/test_multilang_runtime.py
real_runtime_source=scripts/issue130-b3-real-runtime.py
expected_session=${AO_SESSION_ID:-compose}

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
require_literal "      dlr.task: issue130-b3-20260902"
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
require_literal "    cgroup: host"
require_literal "      DLR_SANDBOX_CGROUP_PATH: /run/dlr-cgroup"
require_literal "    cgroup_parent: ${example_parent}"
require_literal "        source: ${example_path}"
require_literal "        target: /run/dlr-cgroup"
require_literal "      ao.session: ${expected_session}"
require_literal "      dlr.task: issue130-b3-20260902"

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
require_runbook_literal '--property=CPUQuota=500%'
require_runbook_literal '--property=MemoryMax=5G'
require_runbook_literal '--property=TasksMax=infinity'
require_runbook_literal "--property='CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SETUID CAP_SETGID'"
require_runbook_literal '--expand-environment=no'
if grep -Fq -- 'AmbientCapabilities=' "$runbook"; then
  echo "keeper must not require AmbientCapabilities on the root-owned delegated unit" >&2
  exit 1
fi
require_runbook_literal 'root:root'
require_runbook_literal 'owner access is sufficient'
require_runbook_literal 'CONTROL_GROUP="$CGROUP_REL"'
require_runbook_literal 'CGROUPFS_ROOT=/sys/fs/cgroup'
require_runbook_literal 'PARENT="$CGROUPFS_ROOT$CONTROL_GROUP"'
require_runbook_literal 'AGENT="$PARENT/agent"'
require_runbook_literal 'mkdir -p "$AGENT"'
require_runbook_literal 'printf "%s\\n" "$$" > "$AGENT/cgroup.procs"'
require_runbook_literal 'test -z "$(cat "$PARENT/cgroup.procs")"'
require_runbook_literal 'grep -qx "$$" "$AGENT/cgroup.procs"'
require_runbook_literal 'test -w "$PARENT/cgroup.procs"'
require_runbook_literal 'printf "+cpu +memory +pids\\n" > "$PARENT/cgroup.subtree_control"'
require_runbook_literal 'SUBTREE_CONTROL=$(cat "$PARENT/cgroup.subtree_control")'
require_runbook_literal 'for controller in cpu memory pids; do'
require_runbook_literal 'ATTEMPT="$PARENT/attempt"'
require_runbook_literal 'mkdir -p "$ATTEMPT"'
require_runbook_literal 'test "$(dirname "$ATTEMPT")" = "$PARENT"'
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
require_runbook_literal 'kill "$WORKLOAD_PID"'
require_runbook_literal 'wait "$WORKLOAD_PID" || true'
require_runbook_literal 'test -z "$(cat "$ATTEMPT/cgroup.procs")"'
require_runbook_literal 'rmdir "$ATTEMPT"'
require_runbook_literal 'test ! -e "$ATTEMPT"'
require_runbook_literal 'test -z "$(cat "$PARENT/cgroup.procs")"'
require_runbook_literal 'grep -qx "$KEEPER_PID" "$PARENT/agent/cgroup.procs"'
require_runbook_literal 'test "$(stat -c '\''%u:%g'\'' "$PARENT")" = 0:0'
require_runbook_literal 'test "$(stat -c '\''%u:%g'\'' "$PARENT/cgroup.procs")" = 0:0'
require_runbook_literal 'test -w "$PARENT/cgroup.procs"'
require_runbook_literal 'test ! -e "$PARENT/attempt"'
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
require_runbook_literal 'normpath'
require_runbook_literal 'runtime_root/workspaces/attempt-<attempt_id>/.dlr-sandbox-mount'
require_runbook_literal 'runtime_root/dlr-preflight-<nonce>/.dlr-sandbox-mount'
require_runbook_literal 'mode 为 `0700`'
require_runbook_literal '有限的 aggregate envelope'
require_runbook_literal '`profile × slots` 伪造 deployment capacity'
require_runbook_literal 'v3 Consumer 必须 fail closed'
if grep -Eq 'CONTROL_GROUP=/sys/fs/cgroup|^[[:space:]]*PARENT="\$CONTROL_GROUP"$' "$runbook"; then
  echo "runbook must keep logical ControlGroup separate from the host cgroupfs path" >&2
  exit 1
fi

require_source_literal() {
  local literal=$1
  if ! grep -Fq -- "$literal" "$sandbox_source"; then
    echo "missing recovery authorization guard: $literal" >&2
    exit 1
  fi
}

require_source_literal 'def _derived_recovery_mount(runtime_root: Path, name: str, execution_id: int) -> Path:'
require_source_literal 'ATTEMPT_CGROUP_NAME_PATTERN = re.compile('
require_source_literal 'PREFLIGHT_CGROUP_NAME_PATTERN = re.compile('
require_source_literal 'attempt_match = ATTEMPT_CGROUP_NAME_PATTERN.fullmatch(name)'
require_source_literal 'attempt_id = int(attempt_match.group("attempt_id"))'
require_source_literal 'return root / "workspaces" / f"attempt-{attempt_id}" / ".dlr-sandbox-mount"'
require_source_literal 'def _validate_preflight_recovery_parent('
require_source_literal 'preflight_directory = root / name'
require_source_literal 'stat.S_IMODE(info.st_mode) != 0o700'
require_source_literal 'def _validated_recovery_mount('
require_source_literal 'normalized_mount = Path(os.path.normpath(mount_path))'
require_source_literal 'normalized_expected = Path(os.path.normpath(os.fspath(expected_mount)))'
require_source_literal 'or normalized_mount != normalized_expected'
require_source_literal 'resolved_mount != normalized_expected'
require_source_literal 'expected_mount = _derived_recovery_mount(runtime_root, name, execution_id)'
require_source_literal 'name=name,'
require_source_literal 'execution_id=execution_id,'
require_source_literal 'def _new_preflight_identity() -> str:'
require_source_literal 'preflight_name = _new_preflight_identity()'
require_source_literal 'preflight_identity = cgroup_name or ('
require_source_literal 'workspace.name'
require_source_literal 'PREFLIGHT_CGROUP_NAME_PATTERN.fullmatch(preflight_identity)'
require_source_literal 'CGROUP2_SUPER_MAGIC'
require_source_literal 'class ResourceEnvelope:'
require_source_literal 'def read_verified_resource_envelope(config: SandboxConfig) -> ResourceEnvelope:'
require_source_literal 'def from_verified_envelope('
require_source_literal 'sandbox_resource_envelope_insufficient'
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
require_source_literal "cap_bnd=field('CapBnd')"
require_source_literal "cap_amb=int(field('CapAmb') or '0',16)"
require_source_literal "assert field('Groups') == ''"
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

require_source_literal 'or profile.output_preview_max_bytes > profile.output_max_bytes'
require_source_literal 'raise SandboxError("resource_profile_invalid")'

require_source_literal 'class ResourceBudget:'
require_source_literal 'agent_reserve_memory'
require_source_literal 'def try_reserve(self, limits: ResourceLimits) -> ResourceReservation | None:'
require_source_literal 'self._capacity[key] - self._reserve[key]'

for literal in \
  'class _BoundedByteRing:' \
  'class _BoundedLogWriter:' \
  'pending_stdout' \
  'stream.read(STREAM_READ_CHUNK_BYTES)' \
  'class DependencyExecutionContext' \
  'context.cgroup_path / "cgroup.procs"' \
  'context.cgroup_path / "cgroup.kill"' \
  'resource.setrlimit(resource.RLIMIT_NOFILE' \
  'class VerifiedVersionCache' \
  'fcntl.flock' \
  'DEFAULT_CACHE_LOW_WATERMARK_BYTES' \
  'os.replace(staging_path, target_path)' \
  '_direct_entry' \
  'cache_low_watermark' \
  'output_too_large' \
  'resource_exceeded_memory' \
  'resource_exceeded_pids' \
  'resource_exceeded_disk'; do
  if ! grep -Fq -- "$literal" "$executor_source" "$venv_source" "$cache_source" "$sandbox_source"; then
    echo "missing bounded runtime implementation: $literal" >&2
    exit 1
  fi
done
if grep -Fq 'read_bytes()' "$executor_source"; then
  echo "Executor output/log handling must not use unbounded read_bytes()" >&2
  exit 1
fi
if ! grep -Fq 'sandbox_diagnostic' "$executor_source" \
  || ! grep -Fq 'helper_diagnostic' "$executor_source"; then
  echo "Executor must preserve helper phase/errno diagnostics in the sandbox receipt" >&2
  exit 1
fi

require_runtime_literal() {
  local literal=$1
  if ! grep -Fq -- "$literal" "$real_runtime_source"; then
    echo "real runtime matrix must assert: $literal" >&2
    exit 1
  fi
}

require_runtime_literal 'root.mkdir(mode=0o711, parents=True, exist_ok=False)'
require_runtime_literal "'/run/dlr-cgroup', '/sys/fs/cgroup'"
require_runtime_literal 'cap_prm_zero'
require_runtime_literal 'cap_eff_zero'
require_runtime_literal 'cap_inh_zero'
require_runtime_literal 'cap_amb_zero'
require_runtime_literal 'groups_empty'
require_runtime_literal 'expected_payload_uid = int(os.environ.get("DLR_SANDBOX_PAYLOAD_UID", "501"))'
require_runtime_literal 'expected_payload_gid = int(os.environ.get("DLR_SANDBOX_PAYLOAD_GID", "1000"))'
require_runtime_literal 'assert output["hidden_cgroup"] == {'
require_runtime_literal 'runtime_root_removed'
require_runtime_literal 'supervisor_identity'
require_runtime_literal 'SUPERVISOR_CAPABILITY_MASK'
require_runtime_literal 'adapter_identity'
require_runtime_literal 'adapter_hidden_cgroup_paths'
require_runtime_literal 'adapter_mount'
require_runtime_literal 'CapBnd'
require_runtime_literal 'bounded'
require_runtime_literal 'log_flood'
require_runtime_literal 'output_too_large'
require_runtime_literal 'dependency_timeout'
require_runtime_literal 'cache_low_watermark'
require_runtime_literal 'ResourceBudget'
require_runtime_literal 'read_verified_resource_envelope'
require_runtime_literal 'concurrent_pressure_attempts'
require_runtime_literal 'all_slots_started'
require_runtime_literal 'control_healthy_during_pressure'
require_runtime_literal 'ThreadPoolExecutor'
require_runtime_literal 'managed_input_read_only'
require_runtime_literal 'positive_recovery'
require_runtime_literal 'forged_marker_rejected'
require_runtime_literal 'python'
require_runtime_literal 'javascript'
require_runtime_literal 'java'
require_runtime_literal 'fork'
require_runtime_literal 'tmpfs'
require_runtime_literal 'nofile'
require_runtime_literal 'timeout'
require_runtime_literal 'cancel'
require_runtime_literal 'crash'

for test_name in \
  test_wait_with_progress_caps_the_physical_log_file \
  test_dependency_preparation_uses_attempt_cgroup_and_bounded_log \
  test_sandbox_output_copy_is_prefix_bounded_and_preserves_original_size \
  test_dependency_build_stages_inside_attempt_tmpfs_until_promotion \
  test_live_version_build_renews_global_reservation_until_finish; do
  if ! grep -Fq -- "$test_name" "$runtime_unit_tests"; then
    echo "missing bounded runtime test: $test_name" >&2
    exit 1
  fi
done
for test_name in \
  test_promoted_entry_is_verified_read_only_and_tamper_detected \
  test_reservations_are_bounded_across_concurrent_misses \
  test_cache_rejects_leaf_symlinks_for_verify_and_cleanup; do
  if ! grep -Fq -- "$test_name" "$cache_tests"; then
    echo "missing verified cache test: $test_name" >&2
    exit 1
  fi
done
for test_name in \
  test_resource_budget_keeps_agent_reserve_when_all_slots_are_used \
  test_verified_resource_budget_uses_deployment_envelope_for_all_slots \
  test_verified_resource_budget_rejects_envelope_that_cannot_leave_all_slot_reserve \
  test_attempt_recovery_marker_removes_only_derived_mount \
  test_preflight_recovery_marker_removes_derived_mount; do
  if ! grep -Fq -- "$test_name" "$runtime_tests"; then
    echo "missing B3 recovery/budget test: $test_name" >&2
    exit 1
  fi
done
if ! grep -Fq 'test_dependency_logs_are_unified_and_ready_environments_skip_install' "$multilang_tests"; then
  echo "missing three-language dependency/cache regression" >&2
  exit 1
fi
for test_name in \
  test_worker_capability_is_hard_scheduling_constraint \
  test_execution_rejects_workers_without_language_capability \
  test_worker_registration_rejects_unknown_or_empty_capabilities \
  test_single_compatible_worker_is_adopted_and_multiple_require_selection; do
  if ! grep -Fq -- "$test_name" "$multilang_tests"; then
    echo "missing required PostgreSQL scheduling regression: $test_name" >&2
    exit 1
  fi
done

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
  || ! grep -Fq 'test_preflight_recovery_marker_cannot_authorize_an_unrelated_mount' "$runtime_tests" \
  || ! grep -Fq 'test_preflight_recovery_marker_removes_derived_mount' "$runtime_tests" \
  || ! grep -Fq 'test_preflight_recovery_marker_requires_private_worker_directory' "$runtime_tests" \
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

if grep -Fq 'assert cap_bnd & ~allowed_caps == 0' "$sandbox_source"; then
  echo "Adapter CapBnd must be recorded but not narrowed without CAP_SETPCAP" >&2
  exit 1
fi

echo "issue130-b3-compose-audit=PASS"
