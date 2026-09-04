# Issue #130 Batch 3 Sandbox Deployment

[简体中文](../zh-CN/issue130-sandbox-deployment.md) · **English**

This document defines the Linux cgroup v2 host/Compose prerequisites and Worker v3
Sandbox runtime contract. The default Compose path remains a legacy/diagnostic path;
this override does not enable ordinary v3 traffic. The production Sandbox Gate must
be proven on target Linux. macOS, Docker Desktop, static Compose rendering, and
host-only probes do not count.

## Exact Delegated Subtree

Create a fresh transient service with the system manager, never a `--user` manager,
an existing `app.slice`, or another non-delegated path. The unit must set
`Delegate=yes` and finite aggregate CPU/memory limits. The unit creator and keeper
remain `root:root`, matching the trusted root Worker supervisor; every Adapter
payload drops to the configured non-root identity before its first instruction.

The provisioning procedure must prove all of these facts before Compose starts:

1. Resolve the unit's actual `ControlGroup` and require an absolute path such as
   `/system.slice/<unit>`.
2. Move the keeper into an `agent` child so the delegated parent has no processes.
3. Enable exactly `cpu memory pids` in `cgroup.subtree_control`.
4. Create a disposable sibling child and write/read `cpu.max`, `memory.max`,
   `memory.swap.max`, and `pids.max` there, never on the parent or `agent` child.
5. Move a disposable process into that child, verify membership, then kill it and
   remove the child with zero residue.
6. Read back a finite parent CPU/memory envelope. When `TasksMax=infinity`, record
   the host `pid_max` as the finite PID ceiling. The configured Worker slots plus an
   independent Agent reserve must fit inside this envelope.

A minimal unit creation skeleton is:

```sh
UNIT=dlr-worker-sandbox-$(hostname -s)-$(date +%s).service
REVIEWED_PROVISIONING_SCRIPT=./provision-dlr-sandbox.sh

sudo -n systemd-run \
  --unit="$UNIT" \
  --description='DLR Issue 130 Worker Sandbox' \
  --property=Delegate=yes \
  --property=CPUQuota=500% \
  --property=MemoryMax=5G \
  --property=TasksMax=infinity \
  --property='CapabilityBoundingSet=CAP_SYS_ADMIN CAP_SETUID CAP_SETGID' \
  --property=NoNewPrivileges=yes \
  --expand-environment=no \
  --service-type=exec \
  --remain-after-exit \
  /bin/bash "$REVIEWED_PROVISIONING_SCRIPT"
```

The reviewed provisioning script must implement the six checks above and remain
alive in `agent`; replacing it with `sleep infinity` without the checks is not
evidence. Stop only the exact task-owned unit after use:

```sh
sudo -n systemctl stop "$UNIT"
sudo -n systemctl reset-failed "$UNIT"
```

## Docker Driver and Path Checks

Read Docker's effective mode:

```sh
docker info --format 'CgroupDriver={{.CgroupDriver}} CgroupVersion={{.CgroupVersion}}'
```

The current override targets `cgroupfs` with cgroup v2. A different driver/version
requires a new target-provisioning review. With a unit under `system.slice`, rendered
Compose must preserve this exact relationship:

```text
cgroup_parent: /system.slice/$UNIT
source: /sys/fs/cgroup/system.slice/$UNIT
target: /run/dlr-cgroup
cgroup: host
DLR_SANDBOX_CGROUP_PATH: /run/dlr-cgroup
unit parent owner: root:root
```

Do not remove the leading slash from `cgroup_parent`, bind the cgroup root, mount the
Docker socket, broaden permissions with `chmod`, or add `CAP_DAC_OVERRIDE`.

## Compose Override and Worker Exception

Set `DLR_SANDBOX_CGROUP_PARENT` and `DLR_SANDBOX_CGROUP_SOURCE` from the same freshly
provisioned unit, then render and start the explicit override:

```sh
docker compose -f docker-compose.yml -f docker-compose.sandbox.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.sandbox.yml up -d worker
```

The Worker-only contract is:

| Property | Required value |
|---|---|
| Privilege | `privileged: false` |
| Capabilities | `cap_drop: ALL`; add only `SYS_ADMIN`, `SETUID`, `SETGID` |
| Privilege escalation | `no-new-privileges:true` |
| AppArmor | `apparmor=unconfined` on the Sandbox Worker only; default seccomp remains |
| cgroup namespace | `host` |
| cgroup bind | One exact delegated subtree at `/run/dlr-cgroup` |
| Docker control plane | No Docker socket |

The Worker supervisor needs the three capabilities for private namespaces/tmpfs,
cgroup child management, and one-way identity drop. They are not granted to Adapter
code. Before Adapter exec, the helper must set empty supplementary groups,
`gid=1000`, `uid=501`, `NoNewPrivileges=1`, and
`CapPrm=CapEff=CapInh=CapAmb=0`. `CapBnd` is audit data and is not cleared by adding
`CAP_SETPCAP`. If the deployment cannot accept this narrowly scoped supervisor
exception, keep the v3 gate closed.

The Worker-only AppArmor exception exists because the target's `docker-default`
profile permits `unshare(CLONE_NEWNS)` but denies the required private-propagation and
task-owned tmpfs mounts. Preserve an A/B receipt: A uses the same topology without
the exception and observes `phase=mount_namespace_private`, `errno=13`; B changes
only to `apparmor=unconfined` and must then pass identity, mount, control-plane hiding,
three-language execution, and cleanup. Never apply this exception to Control or the
Docker daemon.

## Adapter Control-plane Hiding

Worker startup validates that `DLR_SANDBOX_CGROUP_PATH` is an absolute, symlink-free
`cgroup2fs` path with the required interfaces and no overlap with the runtime root or
`.dlr-sandbox-mount`. Inside the Adapter's private mount namespace, the helper places
read-only empty tmpfs over both canonical `/sys/fs/cgroup` and the configured
`/run/dlr-cgroup` bind. Python, JavaScript, and Java probes must independently show
read and write are blocked without relying on `DLR_SANDBOX_*` environment variables.

The helper reports fixed `phase`, `kind`, and numeric `errno` over a dedicated
diagnostic descriptor that Adapter code does not inherit. Diagnostics must never
contain host paths, Secrets, or user output. Missing or failed exact hiding is a
startup/Attempt failure, never a warning followed by execution.

## Worker v3 Runtime Contract

Before registering v3 capability, the actual `python -m dlr.worker.agent` entrypoint
must read the finite parent envelope and run one disposable startup preflight against
the same bind. Any failed probe or cleanup leaves `preflight_passed=false` and
`rabbitmq_execution_v3=false`; registration may remain visible for diagnosis but
cannot receive v3 traffic.

Each queued Execution freezes its Resource Profile. Worker validates the raw profile
for closed fields, schema/backend, numeric limits, cleanup, and output invariants
before comparing it with the verified capability ceiling. A malformed and
over-ceiling profile reports `resource_profile_invalid`, never the less fundamental
capability error.

Each Attempt receives a sibling `attempt-*` cgroup with CPU, memory/swap, PID, tmpfs,
file-descriptor, output, and wall-time limits. Only the payload enters that child;
the helper and Agent remain outside it. The workspace is a private, bounded tmpfs.
Managed input is copied without following symlinks and remains read-only; no host
workspace path, platform credential mount, cgroup control plane, Worker token, or
RabbitMQ credential reaches Adapter code.

Cancel, timeout, resource violation, or crash kills only the Attempt child with
`cgroup.kill`, verifies it is empty, unmounts tmpfs, and removes exact task-owned
state. Recovery markers are `0600`, closed-schema, and validated against a path
derived from the verified Attempt/preflight identity. A path stored in a marker is
never deletion authority; forged markers must preserve unrelated sentinels.

Dependency preparation runs inside the same Attempt resource boundary. Cache builds
hold a global byte reservation and low-watermark budget, stage on bounded tmpfs, and
promote atomically only with a closed identity/digest/byte-count, read-only `.ready`
marker. Lease or reservation loss terminates preparation and forbids promotion.
Adapter code cannot write the shared cache.

## Verification Boundary

Run `scripts/issue130-b3-compose-audit.sh` to validate the source, rendered Compose,
security/cgroup declarations, bounded runtime, cache, recovery, and multilingual
matrix. That static audit is necessary but not sufficient.

Count only a real target-Linux receipt from the actual Compose Worker entrypoint. It
must include target kernel/cgroup facts, exact unit `ControlGroup`, parent envelope,
child limits/readback/membership, namespace and identity facts, both hidden cgroup
paths, bounded log/output/dependency/cache behavior, CPU/OOM/PID/tmpfs/FD/timeout/
cancel/crash/recovery faults for Python/JavaScript/Java, Agent survival and renewals,
cleanup, and zero task-owned residue. A driver that directly execs an internal
runtime helper instead of creating work through Control/RabbitMQ is `NO_COUNT`.

This Sandbox is resource and process containment for trusted-administrator Adapter
code. It is not an untrusted multi-tenant security boundary. Final traffic Cutover
also requires the separate database backup/restore, migration, Slot, and invariant
gates in [Reliable Runtime migration notes](issue130-reliable-runtime-migrations.md).
