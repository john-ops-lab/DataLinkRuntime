# Sandbox 部署与故障定位

**简体中文** · [English](../en/issue130-sandbox-deployment.md)

所有执行统一经过 PostgreSQL Outbox、RabbitMQ、Worker Agent 和 Linux Sandbox。
没有 legacy 执行、灰度开关或人工 Cutover。普通 Compose 已包含完整隔离配置；
`docker-compose.sandbox.yml` 只是兼容旧调用文件名的空覆盖文件。

## 1. 准备真实 Linux 宿主机

在 **Docker daemon 所在 Linux 主机**运行准备脚本。macOS 上应进入 Colima 等 Linux VM，
不能在 macOS 文件系统创建一个同名目录来代替 cgroup。

要求：systemd、统一 cgroup v2、Docker `cgroupfs` driver、`cpu memory pids` controllers，
以及足够的真实 CPU/RAM。脚本检查环境，不自动改 Docker driver 或 VM 资源。

从仓库目录运行：

```sh
sudo bash scripts/prepare-sandbox-host.sh
```

Colima 示例（把绝对路径替换为 VM 可读的实际仓库路径）：

```sh
colima ssh -- sudo bash /ABSOLUTE/PATH/DataLinkRuntime/scripts/prepare-sandbox-host.sh
```

脚本默认创建 `dlr-worker-sandbox.service`，并提供有限的 **3 CPU / 3 GiB** envelope。
它会把 keeper 移到 `agent` 子组、保持父组无内部进程、启用 controllers，并真实验证
临时子组的 CPU/内存/swap/PID limits、进程迁移、`cgroup.kill` 和清理。
已有同名但归属或配置不符的服务不会被修改；多套部署使用新名称：

```sh
sudo bash scripts/prepare-sandbox-host.sh \
  --unit dlr-my-deployment.service --cpu-quota 300% --memory-max 3G
```

将脚本输出的两个配置写入这套部署的 `.env`。默认 unit 的输出如下；若使用了
`--unit`，应照抄该次实际输出，不要替换成下面的默认名称：

```dotenv
DLR_SANDBOX_CGROUP_PARENT=/system.slice/dlr-worker-sandbox.service
DLR_SANDBOX_CGROUP_SOURCE=/sys/fs/cgroup/system.slice/dlr-worker-sandbox.service
```

不能仅改变 profile 数字来制造资源容量。默认 `DLR_WORKER_EXECUTION_SLOTS=2`，还需为 Agent 预留
资源；扩大 slots 或单次执行 profile 后，应准备匹配的真实 envelope 并重新通过 preflight。
该 systemd 服务是 transient unit，宿主机重启后须重新运行准备脚本，再启动 Worker。

## 2. 启动服务

按 README 配好 `.env` 中的凭据和日志路径，再运行：

```sh
bash scripts/issue130-b3-compose-audit.sh
docker compose up -d --wait postgres rabbitmq
docker compose run --rm --no-deps control alembic upgrade head
docker compose up -d --build
docker compose ps
docker compose logs --tail 100 worker
```

配置审计只证明 Compose 结构，不证明宿主机可用。新数据库必须先 migration，再启动 Control。
RabbitMQ 是必需服务，不需要额外启用执行开关。

## 3. 确认 Worker 真正可执行

Worker 启动时读取真实 cgroup resource envelope 并执行隔离 preflight。
只有全部必需能力通过，Worker 才可接收执行；目录存在、容器 healthy 或 `/health` 成功
不能单独当作 Sandbox 验收。

通过已认证的 `GET /api/workers` 检查本次部署的 Worker（按 `id`/`name` 匹配）。
下面使用已在当前 shell 设置的管理员 Token；若更改 Web 端口，请替换 `8080`：

```sh
curl -fsS -H "Authorization: Bearer ${DLR_ADMIN_TOKEN}" \
  http://localhost:8080/api/workers
```

目标 Worker 必须同时满足 `status="online"`、`isolation_preflight_status="passed"`，
且 `isolation_capabilities` 中以下 **18 项都为布尔值 `true`**（不能只看已返回项的数量）：

```text
cgroup_v2, cgroup_namespace_private, mount_namespace, pid_namespace,
memory_hard_limit, pids_hard_limit, tmpfs_hard_limit, bounded_output,
preflight_passed, resource_envelope_verified, cpu_hard_limit, swap_hard_limit,
nofile_hard_limit, no_new_privileges, cgroup_kill, adapter_control_plane_hidden,
adapter_mount_blocked, sandbox_cleanup
```

任何一项缺失、为 `false`，或 Worker 离线，都不能视为可执行节点。
产品界面只展示用户需要的 Worker 可用性与不可用原因，不要求用户理解内部协议版本。

默认隔离边界：

- `privileged: false`、`cgroup: private`，不挂载 Docker socket 或整个宿主机 cgroup 根。
- supervisor 先 `cap_drop: ALL`，只添加 `SYS_ADMIN`、`SETUID`、`SETGID`；
  `no-new-privileges` 始终开启。`apparmor=unconfined` 仅用于 Worker 的 mount namespace 操作，
  不能将其理解为完整 AppArmor 隔离策略。
- 仅将准备好的父组 bind 到 `/run/dlr-cgroup`，禁用自动创建 source 目录。
- Adapter payload 降为配置的非 root UID/GID，清空 capabilities，移除 delegated cgroup 挂载，
  在自己的子 cgroup 和有容量限制的 tmpfs 工作区运行。

在 private cgroup namespace 中，`/proc/self/cgroup` 的 `0::/` 是正常结果，
**不应改用 `cgroup: host` 让路径看起来完整**。内核把精确父组 bind 的 mount root
表示为 `/..`；preflight 同时验证这两个事实和真实子组操作。

## 4. 常见失败

| 现象 | 定位与处理 |
| --- | --- |
| bind source 不存在 | 在 Docker daemon 的 Linux 主机重跑准备脚本；确认 `.env` 指向该 unit |
| Docker driver 不符 / controllers 缺失 | 修复实际主机前提；脚本不会重配 Docker 或伪造成功 |
| `sandbox_private_cgroup_namespace_required` | 检查实际容器为 private namespace，parent 与 bind source 精确匹配 |
| cgroup 写入、进程迁移或 namespace mount 失败 | 查看该 unit 的 `journalctl` 和 Worker preflight；不要切换 privileged/host namespace 绕过 |
| resource envelope 或 slots 不满足 | 为宿主机提供真实资源，或降低 slots/profile 后重新验证 |
| tmpfs 探测出现 `resource_exceeded_disk` | 容量耗尽探测的预期结果；以整份 preflight 是否通过判定，不单看这一行 |

独立 Compose smoke 在本地 Linux Docker 上自动准备本次专属 unit，并在容器退出后停止它。
macOS 或远程 Docker 的 smoke 要求事先导出宿主机脚本返回的两个变量；不会自动停止用户提供的 unit。

## 5. 停止与清理

先停止这套 Compose，再停止对应 unit。停止 unit 会终止它的整个 cgroup 子树，
因此不要让其他部署共用该 unit：

```sh
docker compose down
sudo systemctl stop dlr-worker-sandbox.service
```

上例与默认准备命令一致。若曾使用 `--unit`，必须将第二条替换为这套 `.env`
中 `DLR_SANDBOX_CGROUP_PARENT` 对应的实际 unit，不要停止其他部署的 unit。
在 macOS 上，第二条同样应通过 `colima ssh --` 在 VM 内执行。
上述命令不删除持久数据库卷；不要使用全局 Docker prune。

历史迁移设计仅保留于 [Issue #130 历史记录](issue130-reliable-runtime-migrations.md)，
其中的 legacy/canary/Cutover API 和环境变量不适用于当前部署。
