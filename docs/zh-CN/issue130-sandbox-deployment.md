# Issue #130 Batch 3 Sandbox 部署

本文只描述 Linux cgroup v2 的 Worker host/Compose 前置，不启用 Attempt
Supervisor，也不改变默认的 legacy Compose 路径。真实 Sandbox Gate 必须在 Linux
上完成；macOS、Docker Desktop 等环境不能用静态配置代替真实 probe。

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
parent 的 `subtree_control` 已委派三控制器；四个限额文件必须只在单层 `agent`
child 的 sibling `attempt` 上写入并读回，不能写 `agent` 或 systemd 管理的 unit
parent。这样遵守 cgroup v2
no-internal-process 约束，同时保留 ControlGroup 作为 Compose `cgroup_parent` 与精确
bind source；本 provisioning smoke 创建与 `agent` 平级的 `attempt` sibling，把
workload 放入其中并将四个限额写/read 于 `/attempt`。keeper/MainPID 只驻留
`/agent`，不进入 Attempt；停止 transient unit 时由 systemd 一并删除 `attempt`
与 `agent`。`Delegate=yes` 是让该 unit ControlGroup 成为 Worker 可管理 subtree
的必要条件。

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
target: /sys/fs/cgroup/dlr
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
| cgroup parent | 精确的 systemd transient `ControlGroup` |
| cgroup mount | 只读写 bind 一个精确 subtree 到 `/sys/fs/cgroup/dlr` |
| Docker control plane | 不挂 Docker socket |

这项 capability 只供受信任 Worker 进行后续 namespace/tmpfs 操作；Adapter 在实际
Attempt 启动前仍必须 drop capability 并设置 `NoNewPrivileges`。资源 containment 不
构成不可信多租户安全边界，平台凭据、Worker token 与 cgroup 控制面不能进入 Adapter。

## 验证边界

`scripts/issue130-b3-compose-audit.sh` 使用匿名占位值渲染 override，并断言上述
security/cgroup 条件，同时拒绝 `privileged: true`、Docker socket 和 cgroup root
broad mount。Compose 配置审计不能替代目标 Linux 上的真实 startup probe；后续
11.3+ 才实现 Worker preflight、Attempt child 与运行期 cleanup。
