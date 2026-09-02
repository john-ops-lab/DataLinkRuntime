# Issue #130 Batch 3 Sandbox 部署

本文只描述 Linux cgroup v2 的 Worker host/Compose 前置，不启用 Attempt
Supervisor，也不改变默认的 legacy Compose 路径。真实 Sandbox Gate 必须在 Linux
上完成；macOS、Docker Desktop 等环境不能用静态配置代替真实 probe。

## 精确 delegated subtree

host 必须由 system manager 创建一个新的 transient service。不得使用 `--user` manager，
不得使用已有 `app.slice` 或其他非 delegated 路径。unit creator 可以是 root，但实际
unit payload 使用非 root Worker 用户；下面的 `king`/`501` 仅为部署示例：

```sh
UNIT=dlr-worker-sandbox-$(hostname -s).service

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
  --service-type=exec \
  --remain-after-exit \
  /bin/sleep infinity

CONTROL_GROUP=$(sudo -n systemctl show "$UNIT" -p ControlGroup --value)
test "$CONTROL_GROUP" = "/system.slice/$UNIT"
export DLR_SANDBOX_CGROUP_PARENT="$CONTROL_GROUP"
export DLR_SANDBOX_CGROUP_PATH="/sys/fs/cgroup$CONTROL_GROUP"
```

`DLR_SANDBOX_CGROUP_PARENT` 与 `DLR_SANDBOX_CGROUP_PATH` 必须来自同一个
`ControlGroup`，不能手写成 `/sys/fs/cgroup`，也不能指向 `app.slice`。unit 的
`Delegate=yes` 是让该 unit ControlGroup 成为 Worker 可管理 subtree 的必要条件；
后续 Attempt child 只能由 Worker 在该 subtree 内创建。

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
