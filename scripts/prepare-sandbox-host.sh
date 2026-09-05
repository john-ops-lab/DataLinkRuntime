#!/usr/bin/env bash
# Run on the Linux Docker daemon host. Only creates the named DLR unit/subtree.
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 1; }

unit=dlr-worker-sandbox.service
cpu_quota=300%
memory_max=3G
keeper=false
status_only=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --unit) unit=${2:?missing unit}; shift 2 ;;
    --cpu-quota) cpu_quota=${2:?missing CPU quota}; shift 2 ;;
    --memory-max) memory_max=${2:?missing memory limit}; shift 2 ;;
    --keeper) keeper=true; shift ;;
    --status) status_only=true; shift ;;
    --help)
      printf '%s\n' 'Usage: sudo bash scripts/prepare-sandbox-host.sh [--unit dlr-NAME.service] [--cpu-quota 300%] [--memory-max 3G] [--status]'
      exit 0 ;;
    *) fail "Unknown option: $1" ;;
  esac
done
[[ "$unit" =~ ^dlr-[a-zA-Z0-9][a-zA-Z0-9_.-]*\.service$ ]] || fail 'Unit must be a task-specific dlr-*.service name'
[[ "$cpu_quota" =~ ^[1-9][0-9]*%$ ]] || fail 'CPU quota must be a positive integer percentage'
[[ "$memory_max" =~ ^[1-9][0-9]*[KMG]?$ ]] || fail 'Memory limit must be bytes or an integer K/M/G value'
[ "$(uname -s)" = Linux ] || fail 'Run this command on the Linux Docker daemon host (inside the VM on macOS)'
[ "$(id -u)" = 0 ] || fail 'Root is required to create the named system-manager delegated unit'
[ "$(stat -fc %T /sys/fs/cgroup)" = cgroup2fs ] || fail 'A unified cgroup v2 host is required'

expected_group="/system.slice/$unit"
parent="/sys/fs/cgroup$expected_group"

if [ "$keeper" = true ]; then
  trap 'printf "Sandbox keeper failed at line %s\n" "$LINENO" >&2' ERR
  actual_group=$(awk -F: '$1 == "0" { print $3; exit }' /proc/self/cgroup)
  [ "$actual_group" = "$expected_group" ] || fail 'Keeper is outside its named systemd unit'
  mkdir "$parent/agent"
  printf '%s\n' "$$" > "$parent/agent/cgroup.procs"
  [ -z "$(<"$parent/cgroup.procs")" ] || fail 'Delegated parent has internal processes'
  printf '+cpu +memory +pids\n' > "$parent/cgroup.subtree_control"

  probe="$parent/dlr-host-probe"
  probe_pid=
  cleanup_probe() {
    if [ -n "$probe_pid" ]; then
      kill "$probe_pid" 2>/dev/null || true
      wait "$probe_pid" 2>/dev/null || true
    fi
    if [ -d "$probe" ]; then
      rmdir "$probe" 2>/dev/null || true
    fi
  }
  trap cleanup_probe EXIT INT TERM
  mkdir "$probe"
  printf '100000 100000\n' > "$probe/cpu.max"
  printf '67108864\n' > "$probe/memory.max"
  printf '0\n' > "$probe/memory.swap.max"
  printf '64\n' > "$probe/pids.max"
  [ "$(<"$probe/cpu.max")" = '100000 100000' ]
  [ "$(<"$probe/memory.max")" = 67108864 ]
  [ "$(<"$probe/memory.swap.max")" = 0 ]
  [ "$(<"$probe/pids.max")" = 64 ]
  (
    printf '%s\n' "$BASHPID" > "$probe/cgroup.procs"
    exec sleep infinity
  ) &
  probe_pid=$!
  for ((attempt=0; attempt<100; attempt++)); do
    grep -qx "$probe_pid" "$probe/cgroup.procs" && break
    sleep 0.05
  done
  grep -qx "$probe_pid" "$probe/cgroup.procs"
  printf '1\n' > "$probe/cgroup.kill"
  wait "$probe_pid" 2>/dev/null || true
  probe_pid=
  [ -z "$(<"$probe/cgroup.procs")" ]
  rmdir "$probe"
  trap - EXIT INT TERM
  exec sleep infinity
fi

for tool in docker systemctl systemd-run numfmt; do
  command -v "$tool" >/dev/null || fail "Missing host command: $tool"
done
[ "$(docker info --format '{{.CgroupDriver}}:{{.CgroupVersion}}')" = cgroupfs:2 ] || fail 'This deployment requires Docker cgroupfs with cgroup v2; the script does not reconfigure Docker'
cpu_percent=${cpu_quota%%%}
[ "$cpu_percent" -le "$(( $(nproc) * 100 ))" ] || fail 'CPU quota exceeds the host CPU capacity'
memory_bytes=$(numfmt --from=iec "$memory_max")
host_memory_bytes=$(awk '/MemTotal:/ {printf "%.0f", $2 * 1024}' /proc/meminfo)
[ "$memory_bytes" -le "$host_memory_bytes" ] || fail 'Memory limit exceeds host RAM'
description="DataLinkRuntime Sandbox $unit CPU=$cpu_quota Memory=$memory_max"
keeper_dir=
cleanup_staged_keeper() {
  if [ -n "$keeper_dir" ]; then
    rm -f "$keeper_dir/keeper.sh"
    rmdir "$keeper_dir"
  fi
}
trap cleanup_staged_keeper EXIT
load_state=$(systemctl show "$unit" -p LoadState --value)
if [ "$load_state" = not-found ]; then
  [ "$status_only" = false ] || fail 'Named Sandbox unit has not been prepared'
  script_path=$(readlink -f "$0")
  # The capability-bounded root service cannot bypass a user's private home
  # directory permissions. Stage only this script in a root-owned runtime
  # directory, without widening repository permissions or service capabilities.
  keeper_dir=$(mktemp -d /run/dlr-sandbox-keeper.XXXXXX)
  install -m 0700 "$script_path" "$keeper_dir/keeper.sh"
  systemd-run --unit="$unit" --description="$description" --collect \
    --property=Delegate=yes --property="CPUQuota=$cpu_quota" \
    --property="MemoryMax=$memory_max" --property=TasksMax=infinity \
    --property='CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SETUID CAP_SETGID' \
    --property=NoNewPrivileges=yes --service-type=exec \
    /bin/bash "$keeper_dir/keeper.sh" --unit "$unit" --keeper
else
  [ "$(systemctl show "$unit" -p Description --value)" = "$description" ] || fail 'Existing unit has different ownership/configuration; use a new task-specific unit name'
fi

for ((attempt=0; attempt<100; attempt++)); do
  main_pid=$(systemctl show "$unit" -p MainPID --value)
  if [ "$main_pid" != 0 ] && [ -r "/proc/$main_pid/comm" ] \
      && [ "$(<"/proc/$main_pid/comm")" = sleep ]; then
    break
  fi
  if [ "$(systemctl show "$unit" -p ActiveState --value)" = failed ]; then
    journalctl --unit "$unit" --no-pager -n 30 >&2 || true
    fail 'Sandbox host probe failed; inspect the unit log above'
  fi
  sleep 0.05
done
if [ "$(systemctl show "$unit" -p ActiveState --value)" != active ]; then
  journalctl --unit "$unit" --no-pager -n 30 >&2 || true
  fail 'Sandbox unit did not become active'
fi
[ "$(systemctl show "$unit" -p Delegate --value)" = yes ] || fail 'systemd did not delegate the unit'
[ "$(systemctl show "$unit" -p ControlGroup --value)" = "$expected_group" ] || fail 'Unexpected systemd ControlGroup'
[ "$main_pid" != 0 ] && [ "$(<"/proc/$main_pid/comm")" = sleep ] || fail 'Sandbox host probe did not finish'
[ -z "$(<"$parent/cgroup.procs")" ] || fail 'Delegated parent is not empty'
grep -qx "$main_pid" "$parent/agent/cgroup.procs"
[ ! -e "$parent/dlr-host-probe" ] || fail 'Sandbox host probe left a child behind'
[ "$(stat -c '%u:%g' "$parent")" = 0:0 ] || fail 'Unexpected delegated parent owner'
[ -w "$parent/cgroup.procs" ] || fail 'Delegated parent cannot move processes'
for controller in cpu memory pids; do
  [[ " $(<"$parent/cgroup.subtree_control") " == *" $controller "* ]] || fail "Missing delegated controller: $controller"
done
[ "$(awk '{print $1}' "$parent/cpu.max")" != max ] || fail 'CPU envelope is not finite'
[ "$(<"$parent/memory.max")" = "$memory_bytes" ] || fail 'Memory envelope differs from the requested limit'

printf 'DLR_SANDBOX_CGROUP_PARENT=%s\nDLR_SANDBOX_CGROUP_SOURCE=%s\n' "$expected_group" "$parent"
printf 'Prepared %s; actual Worker isolation preflight must pass before execution.\n' "$unit" >&2
