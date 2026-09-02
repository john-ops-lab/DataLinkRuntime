# Issue #130 Batch 3 Sandbox 部署

本文描述 Linux cgroup v2 的 Worker host/Compose 前置和 v3 Sandbox 运行期合同；默认
Compose 路径仍是 legacy/诊断路径，不会因为本 override 自动把普通流量切到 v3。真实
Sandbox Gate 必须在 Linux 上完成；macOS、Docker Desktop 等环境不能用静态配置代替
真实 probe。

## 精确 delegated subtree

host 必须由 system manager 创建一个新的 transient service。不得使用 `--user` manager，
不得使用已有 `app.slice` 或其他非 delegated 路径。unit creator 可以是 root，但实际
unit payload 使用非 root Worker 用户；下面的 `king` 仅为部署示例，实际 gid
必须从 `Group=king` 与目标上的 `id -g king` 读取，不固定为 `501`：

```sh
UNIT=dlr-worker-sandbox-$(hostname -s)-$(date +%s).service

sudo -n systemd-run \
  --unit="$UNIT" \
  --description='DLR Issue 130 Worker Sandbox' \
  --property=User=king \
  --property=Group=king \
  --property=Delegate=yes \
  --property=TasksMax=infinity \
  --property=CapabilityBoundingSet=CAP_SYS_ADMIN \
  --property=AmbientCapabilities=CAP_SYS_ADMIN \
  --property=NoNewPrivileges=yes \
  --expand-environment=no \
  --service-type=exec \
  --remain-after-exit \
  /bin/bash -c '
    set -euo pipefail
    CGROUP_REL=$(awk -F: '\''$1 == "0" { print $3; exit }'\'' /proc/self/cgroup)
    CONTROL_GROUP=/sys/fs/cgroup$CGROUP_REL
    AGENT="$CONTROL_GROUP/agent"
    mkdir -p "$AGENT"

    # Move the shell out of the unit parent before enabling domain controllers.
    printf "%s\\n" "$$" > "$AGENT/cgroup.procs"
    test -z "$(cat "$CONTROL_GROUP/cgroup.procs")"
    grep -qx "$$" "$AGENT/cgroup.procs"
    # Docker's root supervisor has no DAC override after cap_drop=ALL; grant
    # only the known Group=king to the exact delegated parent.
    chmod 0770 "$CONTROL_GROUP"

    printf "+cpu +memory +pids\\n" > "$CONTROL_GROUP/cgroup.subtree_control"
    SUBTREE_CONTROL=$(cat "$CONTROL_GROUP/cgroup.subtree_control")
    for controller in cpu memory pids; do
      case " $SUBTREE_CONTROL " in
        *" $controller "*) ;;
        *) exit 1 ;;
      esac
    done
    ATTEMPT="$CONTROL_GROUP/attempt"
    mkdir -p "$ATTEMPT"
    test "$(dirname "$ATTEMPT")" = "$CONTROL_GROUP"
    for interface in cpu.max memory.max memory.swap.max pids.max; do
      test -r "$ATTEMPT/$interface"
      test -w "$ATTEMPT/$interface"
    done
    printf "100000 100000\\n" > "$ATTEMPT/cpu.max"
    printf "67108864\\n" > "$ATTEMPT/memory.max"
    printf "0\\n" > "$ATTEMPT/memory.swap.max"
    printf "64\\n" > "$ATTEMPT/pids.max"
    test "$(cat "$ATTEMPT/cpu.max")" = "100000 100000"
    test "$(cat "$ATTEMPT/memory.max")" = "67108864"
    test "$(cat "$ATTEMPT/memory.swap.max")" = "0"
    test "$(cat "$ATTEMPT/pids.max")" = "64"

    # The workload is a sibling of agent; the keeper remains in agent.
    (
      printf "%s\\n" "$BASHPID" > "$ATTEMPT/cgroup.procs"
      exec /bin/sleep infinity
    ) &
    WORKLOAD_PID=$!
    for _ in $(seq 1 50); do
      grep -qx "$WORKLOAD_PID" "$ATTEMPT/cgroup.procs" && break
      sleep 0.05
    done
    grep -qx "$WORKLOAD_PID" "$ATTEMPT/cgroup.procs"
    test -z "$(cat "$CONTROL_GROUP/cgroup.procs")"
    grep -qx "$$" "$AGENT/cgroup.procs"
    NO_NEW_PRIVS=$(awk '$1 == "NoNewPrivs:" { print $2; exit }' /proc/self/status)
    test "$NO_NEW_PRIVS" = 1

    exec /bin/sleep infinity
  '

CONTROL_GROUP=$(sudo -n systemctl show "$UNIT" -p ControlGroup --value)
test "$CONTROL_GROUP" = "/system.slice/$UNIT"
PARENT=/sys/fs/cgroup$CONTROL_GROUP
KEEPER_PID=$(sudo -n systemctl show "$UNIT" -p MainPID --value)
test -z "$(cat "$PARENT/cgroup.procs")"
grep -qx "$KEEPER_PID" "$PARENT/agent/cgroup.procs"
test -s "$PARENT/attempt/cgroup.procs"
SUBTREE_CONTROL=$(cat "$PARENT/cgroup.subtree_control")
for controller in cpu memory pids; do
  case " $SUBTREE_CONTROL " in
    *" $controller "*) ;;
    *) exit 1 ;;
  esac
done
for interface in cpu.max memory.max memory.swap.max pids.max; do
  test -r "$PARENT/attempt/$interface"
  test -w "$PARENT/attempt/$interface"
done
export DLR_SANDBOX_CGROUP_PARENT="$CONTROL_GROUP"
export DLR_SANDBOX_CGROUP_PATH="/sys/fs/cgroup$CONTROL_GROUP"
```

`DLR_SANDBOX_CGROUP_PARENT` 与 `DLR_SANDBOX_CGROUP_PATH` 必须来自同一个
`ControlGroup`，不能手写成 `/sys/fs/cgroup`，也不能指向 `app.slice`。上面的
`keeper` 只创建单层 `agent` 并把自身直接移入其中；只有确认 parent 的
`cgroup.procs` 为空后，才在 parent 写入并读回 `+cpu +memory +pids`。随后确认
parent 的 `subtree_control` 已委派三控制器；四个限额文件只允许在与 `agent`
平级的 sibling `attempt` child 上写入并读回，绝不能写 `agent` 或 systemd 管理的
unit parent。这样遵守 cgroup v2
no-internal-process 约束，同时保留 ControlGroup 作为 Compose `cgroup_parent` 与精确
bind source；本 provisioning smoke 创建与 `agent` 平级的 `attempt` sibling，把
workload 放入其中并将四个限额写/read 于 `/attempt`。keeper/MainPID 只驻留
`/agent`，不进入 Attempt；停止 transient unit 时由 systemd 一并删除 `attempt`
与 `agent`。`Delegate=yes` 是让该 unit ControlGroup 成为 Worker 可管理 subtree
的必要条件。由于 Docker 对非 root `--user` 不会在 NNP 下保留 effective
`CAP_SYS_ADMIN`，Compose override 只让 Worker supervisor 保持已批准的 root
身份与 `SYS_ADMIN`；`group_add`/parent `0770` 仅解决精确 delegated 目录的遍历与
创建权限，不能证明 controller interface 可写。每次 Worker 都必须创建全新的
task-owned Attempt child 并实际完成四个 limits 的 write/read；若该验证失败，必须
保持 v3 gate 关闭，不能复用 provisioning smoke 预建的 `attempt`。不增加
`CAP_DAC_OVERRIDE`。

## Docker cgroup driver 与路径检查

当前 Colima `default` 的只读检查应为：

```sh
docker info --format 'CgroupDriver={{.CgroupDriver}} CgroupVersion={{.CgroupVersion}}'
# CgroupDriver=cgroupfs CgroupVersion=2
```

本 override 针对当前 `cgroupfs` + cgroup v2 目标，要求 `ControlGroup` 作为带前导
`/` 的绝对 cgroup parent，例如 `/system.slice/$UNIT`；不要去掉前导 `/`，否则
Docker 会把 parent 解析到 daemon cgroup 之下。渲染后的 Compose 必须保持：

```text
cgroup_parent: /system.slice/$UNIT
source: /sys/fs/cgroup/system.slice/$UNIT
target: /run/dlr-cgroup
cgroup: host
group_add: ["1000"]
```

driver 或 cgroup version 不匹配时，停止部署并回到 target provisioning review；不在
本 change 中修改 Colima/Docker 配置，也不把静态 Compose 渲染当作真实 startup probe。

使用后仅停止这个精确的 transient unit：

```sh
sudo -n systemctl stop "$UNIT"
sudo -n systemctl reset-failed "$UNIT"
```

## Compose override

默认 `docker-compose.yml` 保持 legacy/诊断可用。Linux Sandbox 部署必须显式叠加
`docker-compose.sandbox.yml`：

```sh
docker compose -f docker-compose.yml -f docker-compose.sandbox.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.sandbox.yml up -d worker
```

override 对 Worker 固定声明：

| 项目 | 合同 |
| --- | --- |
| privilege | `privileged: false` |
| capability | 只增加 `SYS_ADMIN`，同时 `cap_drop: ALL` |
| privilege escalation | `no-new-privileges:true` |
| cgroup namespace | `host`，仅为让 Worker 管理 bind 的 delegated subtree；Adapter 运行期隐藏 cgroupfs |
| delegated subtree access | exact parent 的目录权限仅用于 child 创建；每个 fresh Attempt 仍须自行 limits write/read，不增加 DAC capability |
| cgroup parent | 精确的 systemd transient `ControlGroup` |
| cgroup mount | 只读写 bind 一个精确 subtree 到 `/run/dlr-cgroup`；不覆盖 Docker 的 cgroup root |
| Docker control plane | 不挂 Docker socket |

Docker 的 `cap_add` 对显式非 root container 不会形成可用的 effective
`CAP_SYS_ADMIN`；因此 override 不设置 `user`，只让 Worker supervisor 以默认 root 身份
持有这一个 capability。它不是 Adapter workload：helper 在 exec 前固定执行
`setgid(1000)`/`setuid(501)`、`NoNewPrivileges=1` 和全 capability drop；Adapter 本身始终
是非 root、无 capability。若部署不能接受该 supervisor 例外，应保持 v3 gate 关闭。

这项 capability 只供受信任 Worker 进行后续 namespace/tmpfs 操作；Adapter 在实际
Attempt 启动前仍必须 drop capability 并设置 `NoNewPrivileges`。资源 containment 不
构成不可信多租户安全边界，平台凭据、Worker token 与 cgroup 控制面不能进入 Adapter。

## Worker v3 运行期合同（11.3–11.7）

启用此 override 的 Worker 在注册前只对 v3 执行一次真实 disposable preflight。它读取
同一 transient unit 的 delegated ControlGroup，创建 task-owned probe child，实测
cgroup v2/limits/namespace/tmpfs/`cgroup.kill`/cleanup；注册提交实际 capability
matrix。任一 probe 或 cleanup 断言失败时，matrix 的 `preflight_passed` 为 `false`，
Control 保持 `rabbitmq_execution_v3=false`，不会把 v3 delivery 当作普通 subprocess
执行。

v3 TaskPayload 的 Resource Profile 在 Attempt journal、Workspace、依赖准备或 Adapter
启动前校验。Profile 是 Control 在排队时冻结的快照：Worker 只验证快照仍在自身声明的
有界 capability ceiling 内，不以当前部署默认值替换快照。每个 Attempt 在 delegated
unit parent 下创建自己的 sibling `attempt-*` child；Agent/keeper 只在 unit 外部的
`agent` child（部署 smoke）或 Worker 自身 cgroup 中运行，四个 cgroup limits 只写入
Attempt child，绝不写 parent 或 `agent`。helper 只有在 child limits readback 和
membership 成功后才释放 Adapter。

Adapter Workspace 在 private mount/PID namespace 的 size-bounded tmpfs 中运行；Adapter
进程使用 profile 的 `RLIMIT_NOFILE`，设置并读回 `NoNewPrivileges=1`，清空 capabilities，
并以 `/sys/fs/cgroup` 与 `/run/secrets` 的私有空 tmpfs 隐藏 cgroup control plane 和平台
credential mount。Worker 通过 bounded ready pipe 启动 helper；cancel、wall timeout 或
crash 使用 child `cgroup.kill`，随后确认进程为空、卸载 tmpfs、删除 child 和 workspace。
无法立即完成时只写入精确 task-owned recovery marker，startup scanner 只接受严格命名、
0600、字段闭合的 marker。`mount_path` 本身不是删除授权：scanner 根据 marker 的
`cgroup_name`、`execution_id` 和 `runtime_root` 派生唯一的 task-owned
`.dlr-sandbox-mount`（Attempt 为 `runtime_root/workspaces/attempt-<attempt>/`，
preflight 为其专属 token 目录），只有路径与派生值完全一致才会删除；伪造的 0600
marker 指向 runtime root 内的其他目录时必须保留 marker 和该目录。

v1/v2 legacy 与普通流量保持原有路径；minimum protocol 仍不是 3，RabbitMQ/v3 只有在
完整 preflight matrix 通过后才可由 Control 视为可用。更广的 dependency/cache、log/spool
和 output 运行期预算属于后续 12.x，不由本阶段静默扩大范围。

## 验证边界

`scripts/issue130-b3-compose-audit.sh` 使用匿名占位值渲染 override，并断言上述
security/cgroup 条件，同时拒绝 `privileged: true`、Docker socket 和 cgroup root
broad mount。Compose 配置审计不能替代目标 Linux 上的真实 startup probe；真实运行期
receipt 应记录 target kernel、ControlGroup、child limits、membership、namespace、
cleanup 和 residue=0，且只清理本次精确 task-owned 资源。
