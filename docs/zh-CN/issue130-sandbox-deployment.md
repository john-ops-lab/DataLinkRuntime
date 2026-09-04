# Issue #130 Batch 3 Sandbox 部署

本文描述 Linux cgroup v2 的 Worker host/Compose 前置和 v3 Sandbox 运行期合同；默认
Compose 路径仍是 legacy/诊断路径，不会因为本 override 自动把普通流量切到 v3。真实
Sandbox Gate 必须在 Linux 上完成；macOS、Docker Desktop 等环境不能用静态配置代替
真实 probe。

## 精确 delegated subtree

host 必须由 system manager 创建一个新的 transient service。不得使用 `--user` manager，
不得使用已有 `app.slice` 或其他非 delegated 路径。unit creator 与 provisioning keeper
均保持 `root:root`，使 exact delegated parent 的 owner 与 root Worker supervisor
一致；Adapter payload 仍必须在 exec 前降为非 root：

```sh
UNIT=dlr-worker-sandbox-$(hostname -s)-$(date +%s).service

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
  /bin/bash -c '
    set -euo pipefail
    CGROUP_REL=$(awk -F: '\''$1 == "0" { print $3; exit }'\'' /proc/self/cgroup)
    CONTROL_GROUP="$CGROUP_REL"
    CGROUPFS_ROOT=/sys/fs/cgroup
    PARENT="$CGROUPFS_ROOT$CONTROL_GROUP"
    AGENT="$PARENT/agent"
    mkdir -p "$AGENT"

    # Move the shell out of the unit parent before enabling domain controllers.
    printf "%s\\n" "$$" > "$AGENT/cgroup.procs"
    test -z "$(cat "$PARENT/cgroup.procs")"
    grep -qx "$$" "$AGENT/cgroup.procs"
    # The system-manager unit and Docker Worker supervisor are both root;
    # owner access is sufficient with the exact three supervisor capabilities.
    # Do not chmod any broad cgroup path or grant CAP_DAC_OVERRIDE.
    test -w "$PARENT/cgroup.procs"

    printf "+cpu +memory +pids\\n" > "$PARENT/cgroup.subtree_control"
    SUBTREE_CONTROL=$(cat "$PARENT/cgroup.subtree_control")
    for controller in cpu memory pids; do
      case " $SUBTREE_CONTROL " in
        *" $controller "*) ;;
        *) exit 1 ;;
      esac
    done
    ATTEMPT="$PARENT/attempt"
    mkdir -p "$ATTEMPT"
    test "$(dirname "$ATTEMPT")" = "$PARENT"
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

    # The workload is a sibling of agent; the keeper remains in agent. Keep
    # its cleanup armed before starting it so a timeout or failed assertion
    # cannot leave an Attempt child behind while the keeper survives.
    WORKLOAD_PID=
    cleanup_workload() {
      if test -n "${WORKLOAD_PID:-}"; then
        if kill -0 "$WORKLOAD_PID" 2>/dev/null; then
          kill "$WORKLOAD_PID" 2>/dev/null || true
        fi
        wait "$WORKLOAD_PID" 2>/dev/null || true
      fi
      if test -d "$ATTEMPT"; then
        for _ in $(seq 1 50); do
          if test -z "$(cat "$ATTEMPT/cgroup.procs" 2>/dev/null || true)"; then
            break
          fi
          sleep 0.05
        done
        if test -z "$(cat "$ATTEMPT/cgroup.procs" 2>/dev/null || true)"; then
          rmdir "$ATTEMPT" 2>/dev/null || true
        fi
      fi
      WORKLOAD_PID=
    }
    trap cleanup_workload EXIT INT TERM
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
    test -z "$(cat "$PARENT/cgroup.procs")"
    grep -qx "$$" "$AGENT/cgroup.procs"
    NO_NEW_PRIVS=$(awk '$1 == "NoNewPrivs:" { print $2; exit }' /proc/self/status)
    test "$NO_NEW_PRIVS" = 1

    # The provisioning workload is disposable. Tear it down before handing
    # this exact delegated parent to Compose; only the keeper remains in
    # agent, while the parent stays empty for cgroup v2 domain control.
    kill "$WORKLOAD_PID"
    wait "$WORKLOAD_PID" || true
    for _ in $(seq 1 50); do
      test -z "$(cat "$ATTEMPT/cgroup.procs")" && break
      sleep 0.05
    done
    test -z "$(cat "$ATTEMPT/cgroup.procs")"
    rmdir "$ATTEMPT"
    test ! -e "$ATTEMPT"
    grep -qx "$$" "$AGENT/cgroup.procs"
    test -z "$(cat "$PARENT/cgroup.procs")"

    WORKLOAD_PID=
    exec /bin/sleep infinity
  '

CONTROL_GROUP=$(sudo -n systemctl show "$UNIT" -p ControlGroup --value)
test "$CONTROL_GROUP" = "/system.slice/$UNIT"
CGROUPFS_ROOT=/sys/fs/cgroup
PARENT="$CGROUPFS_ROOT$CONTROL_GROUP"
KEEPER_PID=$(sudo -n systemctl show "$UNIT" -p MainPID --value)
test -z "$(cat "$PARENT/cgroup.procs")"
grep -qx "$KEEPER_PID" "$PARENT/agent/cgroup.procs"
test "$(stat -c '%u:%g' "$PARENT")" = 0:0
test "$(stat -c '%u:%g' "$PARENT/cgroup.procs")" = 0:0
test -w "$PARENT/cgroup.procs"
test ! -e "$PARENT/attempt"
SUBTREE_CONTROL=$(cat "$PARENT/cgroup.subtree_control")
for controller in cpu memory pids; do
  case " $SUBTREE_CONTROL " in
    *" $controller "*) ;;
    *) exit 1 ;;
  esac
done
export DLR_SANDBOX_CGROUP_PARENT="$CONTROL_GROUP"
export DLR_SANDBOX_CGROUP_SOURCE="$PARENT"
export DLR_SANDBOX_CGROUP_PATH="/run/dlr-cgroup"
```

CONTROL_GROUP 是 systemd 返回的逻辑绝对路径（例如 /system.slice/<unit>.service），
只用于 Compose 的 cgroup_parent。CGROUPFS_ROOT=/sys/fs/cgroup 与
PARENT="$CGROUPFS_ROOT$CONTROL_GROUP" 才是 host cgroupfs 路径；所有 host
读取/写入和 DLR_SANDBOX_CGROUP_SOURCE 都必须使用该 PARENT，不能把 cgroupfs
根拼进逻辑值本身。

`DLR_SANDBOX_CGROUP_PARENT` 与 `DLR_SANDBOX_CGROUP_SOURCE` 必须来自同一个
`ControlGroup`，不能手写成 `/sys/fs/cgroup`，也不能指向 `app.slice`；
Worker 内的 `DLR_SANDBOX_CGROUP_PATH` 必须精确为 bind 后的既有普通挂载点
`/run/dlr-cgroup`。上面的
`keeper` 只创建单层 `agent` 并把自身直接移入其中；只有确认 parent 的
`cgroup.procs` 为空后，才在 parent 写入并读回 `+cpu +memory +pids`。随后确认
parent 的 `subtree_control` 已委派三控制器；四个限额文件仅允许在 sibling
`attempt` child 上写入并读回，`parent` 与 `agent` 均绝不能写 limits，也绝不能写
systemd 管理的 unit parent。这样遵守 cgroup v2
no-internal-process 约束，同时保留 ControlGroup 作为 Compose `cgroup_parent` 与精确
bind source；本 provisioning smoke 创建与 `agent` 平级的 `attempt` sibling，把
workload 放入其中并将四个限额写/read 于 `/attempt`。keeper/MainPID 只驻留
`/agent`，不进入 Attempt。由于跨 sibling 迁移还需要 common ancestor 的
`cgroup.procs` 可写，provisioning 与 root Worker supervisor 必须共同使用 exact
unit parent 的 `root:root` owner 访问；不通过 chmod、`group_add` 或
`CAP_DAC_OVERRIDE` 扩大权限。Worker 仍不写 parent 的 controller interfaces，limits
只写 `/attempt`。停止 transient unit 时由 systemd 一并删除 `attempt`
与 `agent`。`Delegate=yes` 是让该 unit ControlGroup 成为 Worker 可管理 subtree
的必要条件。由于 Docker 对非 root `--user` 不会在 NNP 下保留 effective
`CAP_SYS_ADMIN`，Compose override 只让 Worker supervisor 保持已批准的 root
身份与 exact `SYS_ADMIN,SETUID,SETGID`；root-owned exact parent 仅解决精确 delegated 目录的遍历与
创建权限，不能证明 controller interface 可写。每次 Worker 都必须创建全新的
task-owned Attempt child 并实际完成四个 limits 的 write/read；若该验证失败，必须
保持 v3 gate 关闭，不能复用 provisioning smoke 预建的 `attempt`。不增加
`CAP_DAC_OVERRIDE`。

v3 多 slot 调度还要求 systemd unit 给出有限的 aggregate envelope：`CPUQuota` 与 `MemoryMax`
必须是可从 delegated parent 的 `cpu.max`、`memory.max` 读回的有限值；`TasksMax=infinity`
时记录宿主 `pid_max` 作为可验证的 PID 上界。Worker 在执行 disposable preflight 之前、
提交 v3 capability registration 之前读取这些事实，并按每个 configured slot 的 queued
Resource Profile 加上 Agent reserve 检查全部 slots 是否可容纳；不会再用
`profile × slots` 伪造 deployment capacity。tmpfs aggregate envelope 保守取
`memory.max`（tmpfs pages 计入 memory controller）。若 CPU 或 memory envelope 不有限、无法读回
或不足以留下 Agent reserve，Worker 必须把 `resource_envelope_verified` 与完整 isolation
matrix 保持为 false；Control 因此保持 `rabbitmq_execution_v3=false`，不会进入 v3 traffic。
通过的 finite envelope snapshot 会传入 Consumer，避免 eligibility 与后续第二次 cgroup 读取
不一致。

### F3 实际 Agent 压力入口

Compose Worker 的唯一生产入口必须保持为镜像中的 `python -m dlr.worker.agent`。F3
压力驱动是同一 Compose network 上另一个 task-owned、无持久状态的 probe container；它
只通过 Control API 创建带 `dispatch_backend=rabbitmq` 的 canary、读取 Attempt/Lease/Result
状态并采样 `/api/health`，不进入 Worker container，也不调用
`scripts/issue130-b3-real-runtime.py` 的 `run_one` 或本地 ledger。这样 Worker 自身才是
真实 RabbitMQ consumer，heartbeat、lease renew、cancel 和 result report 必须来自该 Agent
的实际生命周期。

driver 启动前，systemd transient unit 的 provisioning shell 从同一个 delegated parent
读出有限 CPU/memory/PID envelope，并以脱敏 JSON 传给 driver；Worker 则在自身注册前
独立读取同一 parent、运行 `ResourceBudget.from_verified_envelope`，随后才运行 disposable
preflight。Agent 日志中的
`v3 pre-registration finite resource envelope gate passed; starting sandbox preflight`
必须出现在 registration 前；envelope 不可用或不足时仍可注册供诊断，但完整 matrix 为
false 且 Control 必须保持 `rabbitmq_execution_v3=false`。F3 receipt 必须同时记录
Worker image entrypoint、driver container identity、`driver_not_worker_exec=true`、注册返回
的 `resource_envelope_verified=true`、压力期间的 heartbeat/renew/cancel/report 以及
Control、RabbitMQ、PostgreSQL health 样本；仅直接 `exec` 诊断脚本的结果为 `NO_COUNT`。

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
DLR_SANDBOX_CGROUP_PATH: /run/dlr-cgroup
unit parent owner: root:root
parent cgroup.procs: owner access, exact unit parent only
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

### Worker-only AppArmor 例外与负向 probe

Colima `default` 的 Docker daemon 保持默认 seccomp；本地实测 `docker-default` AppArmor
在 Worker 保持 `privileged:false`、exact `CAP_SYS_ADMIN,CAP_SETUID,CAP_SETGID`、`NNP=1`、host cgroup namespace
和 exact delegated bind 时，`unshare(CLONE_NEWNS)` 可以成功，但
`mount(NULL, "/", MS_REC|MS_PRIVATE)` 和 task-owned tmpfs mount 返回
`EACCES (errno=13)`。这是一个必须保留的 negative receipt，不能把 helper 的 `125`
当作泛化成功或失败理由；receipt 必须记录
`phase=mount_namespace_private`、`kind=os_error`、`errno=13`。

因此 Linux sandbox override 只对 `worker` service 显式使用
`apparmor=unconfined`；不修改 seccomp，不对 Control、legacy Worker 或 Docker daemon
全局设置该选项，也不增加 capability、privilege、host mount 或 Docker socket。它是
Worker helper 为建立 private mount namespace 所需的 container-level 例外，不是 Adapter
的安全授权：Adapter 仍必须在 exec 前完成 `uid=501,gid=1000`、`CapEff=0`、
`NoNewPrivs=1`，并证明不能 mount；同时在自己的 private mount namespace 中隐藏
`/run/dlr-cgroup` 与 `/sys/fs/cgroup`，且看不到 platform credentials。任一条件失败时
必须保持 `rabbitmq_execution_v3=false`；只有 receipt 明确包含
`adapter_mount_blocked=true`、`adapter_control_plane_hidden=true`、`uid=501`、
`gid=1000`、`CapEff=0` 和 `NoNewPrivs=1` 时，Worker 才能注册对应的 isolation
capability matrix。

最小 A/B 复核必须使用同一个 fresh root-owned system-manager delegated unit、同一
`cgroup_parent`、exact bind、`privileged:false`、`cap_drop: ALL`、exact
`cap_add: SYS_ADMIN,SETUID,SETGID`、
`NNP=1` 和默认 seccomp：A 不设置 AppArmor 选项并预期上述
`mount_namespace_private/13` negative；B 只增加
`--security-opt apparmor=unconfined`，再继续验证 payload identity、namespace、两处
cgroup control-plane read/write denial、三语言 Adapter 和完整 cleanup。A/B 任一失败都
不能生成 11.x Candidate。

override 对 Worker 固定声明：

| 项目 | 合同 |
| --- | --- |
| privilege | `privileged: false` |
| capability | 仅增加 `SYS_ADMIN`、`SETUID`、`SETGID`，同时 `cap_drop: ALL`；不得出现其他 capability |
| privilege escalation | `no-new-privileges:true` |
| AppArmor | 仅 sandbox Worker 使用 `apparmor=unconfined`；默认 seccomp 不变，其他 service 不继承该 override |
| cgroup namespace | `host`；只 bind 精确 delegated subtree 到既有 `/run/dlr-cgroup`，Adapter 运行期隐藏 cgroupfs |
| delegated subtree access | exact parent 与 root Worker supervisor 均为 `root:root`；仅使用 owner access 完成 child 创建和 common-ancestor migration check；不 chmod broad path、不增加 `CAP_DAC_OVERRIDE`，不开放 parent controller interfaces，每个 fresh Attempt 仍须自行 limits write/read |
| cgroup parent | 精确的 systemd transient `ControlGroup` |
| cgroup mount | 只读写 bind 一个精确 subtree 到 `/run/dlr-cgroup`；不覆盖 Docker 的 cgroup root |
| Docker control plane | 不挂 Docker socket |

Docker 的 `cap_add` 对显式非 root container 不会形成可用的 effective capability；因此
override 不设置 `user`，只让 Worker supervisor 以默认 root 身份持有这三个 capability。
它们只属于 trusted supervisor，不是 Adapter workload：helper 在 Adapter 第一行前固定执行
`setgroups([])`、`setgid(1000)`/`setuid(501)`、`NoNewPrivileges=1` 和全 capability drop，
并验证 `Groups` 为空、`CapPrm=0`、`CapEff=0`、`CapInh=0`、`CapAmb=0`；`CapBnd` 只记录
审计值，不作为清零 Adapter capability 的前置条件（不能为清零它而增加 `CAP_SETPCAP`）。
Adapter 本身始终是非 root、无 effective/permitted/inheritable/ambient capability。若部署不能接受
该 supervisor 例外，应保持 v3 gate 关闭。

### Design review implementation evidence

2026-09-03 的 design review 批准 trusted Worker supervisor 的 capability 集合严格为
`CAP_SYS_ADMIN`、`CAP_SETUID`、`CAP_SETGID`，用于 namespace/tmpfs/cgroup 操作和一次性
payload identity drop；`privileged:false`、`NoNewPrivileges=1`、无 Docker socket、exact
delegated bind 与 Worker-only `apparmor=unconfined` 保持不变。不得添加
`CAP_DAC_OVERRIDE`、`CAP_SETPCAP` 或任何其他 capability。真实 payload receipt 必须保留
`uid=501,gid=1000`、`CapPrm=0`、`CapEff=0`、`CapInh=0`、`CapAmb=0`，并证明 cgroup control-plane
write 与 mount 仍失败；`CapBnd` 可保留 container bounding mask，不能为清零它授予
`CAP_SETPCAP` 或其他额外 capability。

这三个 capability 只供受信任 Worker 进行后续 namespace/tmpfs 操作与 identity drop；Adapter 在实际
Attempt 启动前仍必须 drop capability 并设置 `NoNewPrivileges`。资源 containment 不
构成不可信多租户安全边界，平台凭据、Worker token 与 cgroup 控制面不能进入 Adapter。

### Adapter cgroup control plane 隐藏

Worker 启动时必须校验 `DLR_SANDBOX_CGROUP_PATH` 是无 symlink 的绝对路径、实际文件
系统为 `cgroup2fs`，且包含 delegated parent 所需的 cgroup interface；路径还必须与
受控 workspace 和 `.dlr-sandbox-mount` 完全不重叠。校验失败时不得启动 Adapter。

Adapter 的 private mount namespace 先设为 private propagation，再以只读空 tmpfs
overmount canonical `/sys/fs/cgroup`，并对实际配置的 bind 目标（当前为
`/run/dlr-cgroup`）执行第二个 exact overmount。目标不存在、不是 `cgroup2fs` 或与
workspace 重叠时必须 fail closed；不得静默跳过 exact hide，也不得把 overmount 目标
放到 workspace 或任意未验证路径。Adapter 三语言 probe 必须在不依赖
`DLR_SANDBOX_*` 环境变量的情况下分别证明两个路径的控制文件不可读、不可写；receipt
需为每个路径记录 `read_blocked=true` 与 `write_blocked=true`。Worker supervisor
仍需在 Adapter 启动前通过同一精确 bind 完成 child 创建和 limits write/read。helper
失败不得只记录 `returncode=125`：它必须通过不传给 Adapter 的专用诊断 FD 写入固定
`phase`、`kind` 和 numeric `errno`，并把它映射为稳定 `sandbox_*` error code；诊断不得
包含宿主路径、Secret 或用户输出。这样可以区分 `payload_setup` 的身份/权限 syscall、
namespace/tmpfs mount、exact cgroup hide 与 output staging 失败；无诊断时才按普通
Adapter return code 处理。

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
`agent` child（部署 smoke）或 Worker 自身 cgroup 中运行；helper 也始终留在 Worker
自身 cgroup/parent 外部，只有 fork 出来的 payload PID 在 child limits readback 前移入
与 `agent` 平级的 sibling `attempt-*` child。四个 cgroup limits 只写入 Attempt child，
绝不写 parent 或 `agent`；`cgroup.kill` 只终止 Attempt payload，helper 保留到 output
staging、unmount 和 cleanup 完成。helper 只有在 child limits readback 和 membership
成功后才释放 Adapter。

Adapter Workspace 在 private mount/PID namespace 的 size-bounded tmpfs 中运行；tmpfs
mount root 保持 supervisor 的 `root:root` ownership，只提供 `0711` search（不提供写入），
实际复制的 ephemeral workspace 在 outer mount root 下的 task-owned bounded tmpfs 内以
payload `uid:gid`、mode 为 `0700` 创建。helper 先以 supervisor 身份安全打开 source
descriptor，再用 Linux `setfsuid`/`setfsgid` 创建每个 destination node，因此不需要
`CAP_CHOWN`；每个节点使用
`follow_symlinks=False` 语义且不跟随 staged symlink，保留 managed input directory/file
的 `0555`/`0444` read-only modes。payload 通过 inherited workspace directory FD 的
`/proc/self/fd/<fd>/dlr-exec-*` absolute path 访问 workspace，仍可写 `output.json` 和
`temp` fill；命令行中的 workspace 根及其所有 descendant 路径（包括 Node harness）都
必须重写到该 `/proc/self/fd/<fd>/dlr-exec-*` tree，不能把 host workspace 路径传给
payload。为满足 Node/Java runtime 的真实路径解析，helper 只把 outer tmpfs recursive-bind
到由 Attempt workspace 名派生的 task-owned `/tmp/.dlr-sandbox-*` 临时目录；该目录不是
用户可配置路径，且必须在 unmount 后精确 `rmdir`，不能覆盖 workspace 或任意 host path。
ownership handoff 失败必须记录
`phase=workspace_ownership`/`sandbox_workspace_ownership_failed`
并在 Adapter 前 fail closed。Adapter 进程使用 profile 的 `RLIMIT_NOFILE`，设置并读回
`NoNewPrivileges=1`，清空 capabilities，并以 `/sys/fs/cgroup` 与 `/run/secrets` 的私有空
tmpfs 隐藏 cgroup control plane 和平台 credential mount。Worker 通过 bounded ready pipe 启动 helper；cancel、wall timeout 或
crash 使用 child `cgroup.kill`，随后确认进程为空、卸载 tmpfs、删除 child 和 workspace。
无法立即完成时只写入精确 task-owned recovery marker，startup scanner 只接受严格命名、
0600、字段闭合的 marker。`mount_path` 本身不是删除授权：scanner 根据 marker 的
`cgroup_name`、`execution_id` 和 `runtime_root` 派生唯一的 task-owned
`.dlr-sandbox-mount`，并先对 marker 字段做绝对路径 `normpath` 后与派生路径做精确比较；
scanner 不会把 marker 的路径当作授权来源。Attempt 只允许
`runtime_root/workspaces/attempt-<attempt_id>/.dlr-sandbox-mount`，其中
`<attempt_id>` 来自已校验的 `attempt-<execution_id>-<attempt_id>` cgroup 名；preflight
先生成并校验一个 `dlr-preflight-<nonce>` identity，再用同一 identity 创建目录和 cgroup；
只允许 `runtime_root/dlr-preflight-<nonce>/.dlr-sandbox-mount`，且该直接父目录必须是
Worker 按 nonce 创建、owner 为当前 Worker、mode 为 `0700` 的私有目录。伪造的 0600
marker 指向 runtime root 内其他目录，或指向不符合 preflight 目录约束的路径时，必须保留
marker、mount 和目录内 sentinel。

Profile 校验顺序也属于 fail-closed 合同：Worker 必须先从原始 queued snapshot 校验
字段闭合、schema/backend、数值与 cleanup/输出等 intrinsic invariants，再比较 Worker
capability ceiling；同一 profile 同时 malformed 且超上限时，必须返回
`resource_profile_invalid`，不能先返回 `resource_profile_exceeds_worker_capability`。

v1/v2 legacy 与普通流量保持原有路径；minimum protocol 仍不是 3，RabbitMQ/v3 只有在
完整 preflight matrix 通过后才可由 Control 视为可用。12.x 的运行期边界也必须沿用同一
Attempt：`.log` 与 progress pending queue 使用固定内存 ring 和硬字节上限；Output 使用
`stat` 加 `limit+1` 流式读取；Python/npm/Maven dependency preparation 在 Attempt 的
cgroup、tmpfs、RLIMIT_NOFILE、超时和脱敏日志边界内运行。version cache 只接受带闭合
identity、digest、byte count 和不可写 `.ready` 标记的原子 promotion，并在 staging 前
持有全局 byte reservation 与 low-watermark；cache miss 的全部构造写入同一 Attempt bounded
tmpfs，reservation 是整个 live build 的权威 byte budget。Worker 持续续租；若续租或
reservation 检查失败，依赖进程会被终止、staging 清理且禁止 promotion，不以 TTL 过期后
继续写入。Adapter 不得写共享 cache。memory/pids/disk/
timeout/sandbox-prepare 失败使用稳定 error code，receipt 只包含有界 resource usage，不
包含宿主路径。Worker 的每个 slot 还必须保留单独 Agent reserve，不能把全部 delegated
容量分配给 Attempts。

13.x 的真实 Linux matrix 必须对 Python、JavaScript、Java 分别覆盖 CPU、memory、pids/
fork、tmpfs、FD、wall timeout、log flood、超大 output、dependency timeout、cache
low-watermark、cancel、crash 与 recovery；一次故障只能 kill 对应 Attempt，其他 Attempt
和 Agent 继续工作。Recovery 仍只接受由 `runtime_root` 与已校验 Attempt/preflight
identity 派生的 workspace，伪造 marker 不得删除无关 sentinel。不能用 macOS、host-only
或模拟 cgroup 结果替代真实 Compose Worker；不可用环境必须保持 v3 fail closed。

## 验证边界

`scripts/issue130-b3-compose-audit.sh` 使用匿名占位值渲染 override，并断言上述
security/cgroup、bounded runtime、cache、recovery 与 multilingual matrix 条件，同时拒绝
`privileged: true`、Docker socket 和 cgroup root broad mount。Compose 配置审计不能替代
目标 Linux 上的真实 startup probe；真实运行期 receipt 应记录 target kernel、ControlGroup、
child limits、membership、namespace、bounded log/output/dependency/cache/fault facts、
cleanup 和 residue=0，且只清理本次精确 task-owned 资源。通过 audit 的源码和测试存在性
同样不能单独标记 OpenSpec 任务完成；只有最终 actual-Compose receipt 通过后才能更新
对应复选框。
