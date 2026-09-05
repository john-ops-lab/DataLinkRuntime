# Sandbox Deployment and Troubleshooting

[简体中文](../zh-CN/issue130-sandbox-deployment.md) · **English**

Every execution uses PostgreSQL Outbox, RabbitMQ, Worker Agent, and the Linux
Sandbox. There is no legacy executor, canary switch, or manual Cutover. The base
Compose file includes isolation; `docker-compose.sandbox.yml` is an empty override
retained only for callers that still name that file.

## 1. Prepare the Real Linux Host

Run preparation on the **Linux host running the Docker daemon**. On macOS this
means the Colima or equivalent Linux VM, not a similarly named macOS directory.

Requirements: systemd, unified cgroup v2, Docker's `cgroupfs` driver, delegated
`cpu memory pids` controllers, and sufficient real CPU/RAM. The script checks the
host; it does not reconfigure Docker or resize a VM.

From the repository directory:

```sh
sudo bash scripts/prepare-sandbox-host.sh
```

Colima example (replace the absolute path with a repository path visible in the VM):

```sh
colima ssh -- sudo bash /ABSOLUTE/PATH/DataLinkRuntime/scripts/prepare-sandbox-host.sh
```

The default unit is `dlr-worker-sandbox.service` with a finite **3 CPU / 3 GiB**
envelope. The script moves its keeper into an `agent` child, leaves the parent
without internal processes, enables controllers, and verifies actual child
CPU/memory/swap/PID limits, process migration, `cgroup.kill`, and cleanup.
It never changes an existing unit with different ownership/configuration.
For another deployment, choose a distinct name:

```sh
sudo bash scripts/prepare-sandbox-host.sh \
  --unit dlr-my-deployment.service --cpu-quota 300% --memory-max 3G
```

Copy the two printed settings into this deployment's `.env`. The default unit
prints the following values. If you used `--unit`, copy that command's actual
output instead of substituting these default names:

```dotenv
DLR_SANDBOX_CGROUP_PARENT=/system.slice/dlr-worker-sandbox.service
DLR_SANDBOX_CGROUP_SOURCE=/sys/fs/cgroup/system.slice/dlr-worker-sandbox.service
```

Profile arithmetic cannot create host capacity. The default is
`DLR_WORKER_EXECUTION_SLOTS=2`, with additional resources reserved for the Agent. Increasing
slots or execution profiles requires a matching real envelope and a fresh passing
preflight. This is a transient systemd unit: rerun preparation after a host reboot
before starting the Worker.

## 2. Start Services

Configure credentials and log paths in `.env` as described in the README, then run:

```sh
bash scripts/issue130-b3-compose-audit.sh
docker compose up -d --wait postgres rabbitmq
docker compose run --rm --no-deps control alembic upgrade head
docker compose up -d --build
docker compose ps
docker compose logs --tail 100 worker
```

The configuration audit proves structure only, not host readiness. A fresh
database must be migrated before Control starts. RabbitMQ is required without
an execution enablement switch.

## 3. Verify Execution Readiness

At startup the Worker reads the real cgroup resource envelope and runs isolation
preflight. It may accept executions only after all required capabilities pass.
An existing directory, a healthy container, or `/health` alone is not Sandbox
acceptance.

Inspect this deployment's Worker with an authenticated `GET /api/workers`, matching
its `id`/`name`. This example uses the administrator Token already set in the current
shell; replace `8080` if you changed the Web port:

```sh
curl -fsS -H "Authorization: Bearer ${DLR_ADMIN_TOKEN}" \
  http://localhost:8080/api/workers
```

The target Worker must have both `status="online"` and
`isolation_preflight_status="passed"`. Each of the following **18 required keys**
in `isolation_capabilities` must be the boolean `true`; counting returned keys
alone is insufficient:

```text
cgroup_v2, cgroup_namespace_private, mount_namespace, pid_namespace,
memory_hard_limit, pids_hard_limit, tmpfs_hard_limit, bounded_output,
preflight_passed, resource_envelope_verified, cpu_hard_limit, swap_hard_limit,
nofile_hard_limit, no_new_privileges, cgroup_kill, adapter_control_plane_hidden,
adapter_mount_blocked, sandbox_cleanup
```

A missing/false capability or an offline Worker is not execution-ready.
Product UI reports Worker availability and actionable failure
reasons without exposing internal protocol versions as a user choice.

The default boundary is:

- `privileged: false`, `cgroup: private`, no Docker socket or entire host cgroup root.
- The supervisor drops `ALL` capabilities, then adds exactly `SYS_ADMIN`, `SETUID`,
  and `SETGID`, with `no-new-privileges`. Worker-only `apparmor=unconfined` permits
  mount namespace operations; it is not a complete AppArmor confinement policy.
- Only the prepared parent is bound to `/run/dlr-cgroup`; automatic source-directory
  creation is disabled.
- Adapter payloads use the configured non-root UID/GID, clear capabilities, remove
  the delegated cgroup mount, and run in a child cgroup and bounded tmpfs workspace.

Inside a private cgroup namespace, `/proc/self/cgroup` showing `0::/` is expected.
**Do not switch to `cgroup: host` to expose a full path.** The kernel represents the
exact ancestor bind's mount root as `/..`; preflight checks both facts and real
child operations.

## 4. Common Failures

| Symptom | Action |
| --- | --- |
| Bind source missing | Rerun preparation on the Linux Docker daemon host; match `.env` to that unit |
| Unsupported Docker driver or missing controllers | Fix actual host prerequisites; the script does not reconfigure Docker or fake success |
| `sandbox_private_cgroup_namespace_required` | Check the actual private namespace and exact parent/source match |
| cgroup write, process migration, or namespace mount failure | Inspect this unit's journal and Worker preflight; do not bypass with privileged/host namespace |
| Resource envelope or slots rejected | Provide real host resources, or lower slots/profiles and reverify |
| `resource_exceeded_disk` during tmpfs probe | Expected exhaustion probe output; judge the complete preflight result |

The isolated Compose smoke prepares a unique unit on local Linux Docker and stops
it after its containers exit. For macOS or remote Docker, export the two settings
returned by host preparation first. Smoke never stops an operator-supplied unit.

## 5. Stop and Clean Up

Stop this Compose deployment before its unit. Stopping a unit terminates its whole
cgroup subtree, so do not share it with another deployment:

```sh
docker compose down
sudo systemctl stop dlr-worker-sandbox.service
```

This matches the default preparation command. If you used `--unit`, replace the
second command's unit with the actual unit named by this deployment's
`DLR_SANDBOX_CGROUP_PARENT` in `.env`; do not stop another deployment's unit.
On macOS, run the second command inside the VM via `colima ssh --` as well.
These commands preserve database volumes. Do not use global Docker prune.

The [Issue #130 migration record](issue130-reliable-runtime-migrations.md) is
historical only; its legacy/canary/Cutover APIs and settings do not apply today.
