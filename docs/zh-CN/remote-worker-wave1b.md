# Wave 1-B：已提交结果后的受控恢复

本批在 Wave 1-A 的专用部署上增加显式恢复入口：单 Control、单固定 Worker、标准库 Python、none/JSON 输入、手动/定时任务。仅处理 **W1-B 在故障前已生成完整 prepared/stopped 检查点，且 Control 已提交匹配结果** 的任务。失败/超时结果遵循同一规则。恢复不重跑业务、不重发结果正文、不创建新 Execution/Attempt，不消耗业务重试。

实现与验收状态以阶段 PR 的精确代码/镜像及私有记录为准。本说明不是尚未执行场景的通过回执。#129 保持开放，完整 Wave 1、Pool 和 HA 仍未交付。

## 兼容与信任边界

**旧 W1-A 残留缺少新检查点时仍返回 `recovery_required`。** 不追认当时的物理停止，不补造检查点，不删 journal、换空卷或改数据库解锁。Claim/结果未知、通用撤销/取消恢复、宿主重启/断电、换盘/卷复制、跨宿主接管、文件/依赖/业务凭据及其他语言不在本批范围。

恢复必须在同一 Linux 宿主、同一 boot、原稳定身份、原 runtime volume 与 journal bind 上执行。整个旧容器及其后代已经退出、无其他 writer/挂载引用、无外借 FD/namespace，是可信宿主操作者核验的部署前提。Control 接受的是管理员绑定的运维断言；证据引用和摘要不能证明其真实。Worker 不访问 Docker socket，不自动探测整个宿主，也不能排除任意特权进程私藏的引用。发生过未知共享、导出或人工改卷时不能使用本流程。

Worker 独立检查其能观察的事实：原根锁和锚点、boot/存储/身份、完整冻结记录、当前证明、原对象及新 namespace 中的挂载。旧 cgroup 被运行时移除的事实须由宿主证据确认，不能用新容器看不到旧路径代替。旧 PID/mount namespace 绑定保持原值，新实例观察另行记录。

Linux 可在旧 namespace 释放后复用其 inode 数字，因此不以数字必须不同判断新实例。Worker 核验同 boot 下 PID 1 的启动 ticks 严格晚于原进程，同时保留两次真实 namespace 观察；完整旧容器退出、当前 Docker/cgroup 启动归属、原卷锁和独占前提仍分别核对。内核依据见 [namespace 分配/释放入口](https://github.com/torvalds/linux/blob/v6.12/include/linux/proc_ns.h) 与 [可回收的 inode 分配器](https://github.com/torvalds/linux/blob/v6.12/fs/proc/generic.c)。

## 事前建档

沿用 [W1-A 部署准备](remote-worker-wave1a.md) 的固定 Compose project/service 和私有 env；镜像固定版本。每次正常启动、故障前，保存以下原生命令的选择字段输出到私有部署记录，绑定 Worker ID/identity、instance/generation 及本次检查点。不要导出完整 `docker inspect` 的 Env。

```sh
# 在原 Worker Linux 宿主执行；使用部署实际的 project、配置路径。
docker compose -p "$WORKER_PROJECT" --env-file "$WORKER_ENV" \
  -f deploy/standalone-worker/compose.wave1a.yml ps -a -q worker

# OLD_CONTAINER_ID 必须为上一步得到并保存的完整容器 ID。
docker inspect --format \
  '{{.Id}} {{.Image}} {{json .Config.Labels}} {{json .State}} {{json .Mounts}} {{.HostConfig.PidMode}} {{.HostConfig.Privileged}}' \
  "$OLD_CONTAINER_ID"
cat /proc/sys/kernel/random/boot_id
cat "/proc/$OLD_HOST_PID/stat"
cat "/proc/$OLD_HOST_PID/cgroup"
readlink "/proc/$OLD_HOST_PID/ns/pid" "/proc/$OLD_HOST_PID/ns/mnt"
docker volume inspect "$RUNTIME_VOLUME"
stat -Lc '%d %i %a %u' "$JOURNAL_ANCHOR"
findmnt -T "$JOURNAL_ANCHOR"
```

`OLD_HOST_PID` 来自该完整容器的 `State.Pid`，须保存其启动身份；由实际 cgroup 映射确认 Docker 容器根与 Agent/Attempt 子树，保留对应设备/inode和路径。不能用相同 Compose 名称或复用的 PID 代替原身份。卷名/driver/mountpoint、bind 原路径和挂载来源一并保存。只有本部署唯一 Worker writer 可挂载这些目录；业务身份无 journal/root 写权限。

prepared 检查点在业务放行前写入两份冻结额度、Dispatch 和实际对象；stopped 检查点由真实监督链在收割进程、确认空 cgroup、收完输出及结束本地 IO 后写入，并绑定 result intent/MAC。存在 stopped 文件即不可再次启动该业务。管理员请求使用的 `prepared_sha256` 是 **解析 JSON 后按键排序、紧凑 ASCII JSON 的 SHA-256**，不含文件末尾换行；不是直接对文件字节执行 `sha256sum` 的值。通过既有私有运维读取检查点元数据，保持 Cleanup Token 和结果证明不出私有目录。

## 故障后停止与核验

1. 管理员调用既有 `POST /api/workers/{id}/v4/drain`，保存 request ID、expected version、原实例和未结 Attempt；重复同一请求只读回本次排空操作。
2. 核对原 Compose 仍指向已保存的完整旧容器 ID，执行以下既有命令。保留每次命令的状态、输出、时间和前后 boot；不能先删除容器再补造映射。

```sh
docker compose -p "$WORKER_PROJECT" --env-file "$WORKER_ENV" \
  -f deploy/standalone-worker/compose.wave1a.yml stop --timeout 30 worker
docker wait "$OLD_CONTAINER_ID"
docker inspect --format '{{.Id}} {{json .State}}' "$OLD_CONTAINER_ID"
cat /proc/sys/kernel/random/boot_id
docker ps -a --no-trunc
```

3. 确认旧容器非 running、PID 为 0，事前映射的整个 cgroup 无成员/无 populated 责任或已被该运行时清退。超时、D 态成员、身份不符或 boot 变化时停止。单独路径不存在不能证明停止。
4. 对全部容器逐个选择输出 Mounts/状态，核对没有其他容器持有原 runtime volume 或 journal bind；用 `findmnt` 核对实际卷/bind及传播。结束本部署曾启动的诊断 exec、writer 和 namespace 引用，维护期间禁止新增引用。Docker 列表不能枚举任意特权宿主 FD；不能确认从部署起符合独占前提时保持阻断。

## 显式恢复操作

管理员在原有认证入口调用 `POST /api/workers/{id}/v4/recovery-intent`。请求闭合字段如下；值均来自本次实际状态和私有证据，UUID/摘要不能用示例占位值代替：

```json
{
  "request_id": "NONZERO_UUID",
  "expected_version": 5,
  "binding": {
    "execution_id": 12,
    "attempt": {
      "attempt_id": 15,
      "fencing_token": 7,
      "worker_identity_id": "ORIGINAL_IDENTITY_UUID",
      "original_instance_id": "ORIGINAL_INSTANCE_UUID",
      "original_instance_generation": 1
    },
    "old_container_id": "FULL_64_LOWERCASE_HEX_CONTAINER_ID",
    "deployment_record_id": "PRIVATE_RECORD_UUID",
    "evidence_sha256": "64_LOWERCASE_HEX",
    "boot_id": "ORIGINAL_HOST_BOOT_UUID",
    "prepared_sha256": "CANONICAL_PREPARED_SHA256"
  }
}
```

`expected_version` 使用管理员 status 的当前值。完整旧实例期限和候选围栏未过、身份/责任不符或版本冲突时拒绝。相同 request ID 和相同内容只读回同一操作；改变内容须拒绝。成功返回 `host_evidence: operator_attestation` 和新 maintenance version，不直接激活业务。

将 request ID 作为现有 Agent 的 `--recovery-operation UUID`，或仅在本次 Compose 启动时设置 `DLR_WORKER_RECOVERY_OPERATION`：

```sh
DLR_WORKER_RECOVERY_OPERATION="$RECOVERY_REQUEST_ID" \
docker compose -p "$WORKER_PROJECT" --env-file "$WORKER_ENV" \
  -f deploy/standalone-worker/compose.wave1a.yml up -d --force-recreate worker
```

保持同一 project、原挂载、原运行凭据及版本匹配镜像。不要同时启动第二个服务或使用 `compose run` 增加 writer。下次正常停启清除该环境设置；不为清除设置提前重建正在运行的容器。

Agent 在注册前只读扫描原卷，并用当前合法运行凭据读回这条管理员意图；不接受文件中的布尔“已停止”绕过。显式恢复模式下空目录也拒绝，不能据此将空卷认作恢复完成；已确认完成的后续正常启动须清除恢复参数。通过后注册 **新** reconciling instance/generation。恢复期间没有 consumer、ready 或业务执行。凭据撤销/过期仍拒绝，不能使用旧 Claim Token/证明续活。

## 清理、结算和重新激活

顺序为：当前实例的签名历史结果观察 → 原对象清理与持久删除进度 → 当前授权下 cleanup stage/finalize → 独立读回 committed completion → 精确终结清单 → journal 逐项 unlink/fsync → runtime 阻断标记 → 最后删除终结记录。

缺失对象只在匹配的持久删除意图下重入；同名替换、额外目录、嵌套挂载、损坏或未知记录均保留责任。已提交 completion 只读返回，不能重新 stage 覆盖。提交或回执未知先观察稳定标记，再决定是否继续；不按 HTTP 失败推断回滚。结果/output/error/ended_at 和原 Attempt 保持不变。

两份原冻结占用一直保留在受保护记录中。新进程不调用旧内存句柄的 release，也不把旧额度加回新账；清理/网络实际占用结束、全部记录归零后，才重新验证实际 envelope/preflight，并建立一次新预算。正常同进程路径仍只释放原 receive/task 两个句柄各一次。阻塞的 IO 不因超时而释放资源或并发重试。

恢复 proof HTTP 使用既有有限响应/整次调用截止和 pidfd 收割路径，父子进程实际受整个 Worker 容器的 CPU/内存/PID 硬限及零 swap 约束。这条 proof 路径未单独设置地址空间上限；W1-A 结果上传子进程的 128 MiB 限制不能套用于此。记录实际 cgroup/进程限额，不能以响应字节数或一次峰值代替内存上界。

恢复完成后仍保持原 draining/disabled 管理状态。必要时重复原 drain request，使未结责任归零的 Worker 变为 disabled；读回当前 version，再以新 request ID 显式 activate。新 channel 对旧消息取得服务端权威 `ACK_NOOP` 后，才 ACK 当前 delivery tag；不保存或复用旧 tag。ACK 到达未知时允许重投，再次权威判定，不重复清理结账。最后必须实际执行一项新任务。

## 诊断与迁移

| 现象 | 处理 |
| --- | --- |
| `recovery_required` | 未指定显式恢复或本地记录不具备资格；保留原卷，不补造旧检查点 |
| 恢复意图 409 | 核对版本、原身份/Attempt、旧实例和候选期限；相同请求未知先读回 |
| `worker_recovery_unavailable` / 恢复阶段 503 | 当前凭据、证明、内容或 committed 标记尚不能确认；保持阻断 |
| Agent `worker_unavailable` | 查看日志的 `phase=recovery_scan/recovery_operation/recovery_registration/recovery_execution`；不输出私有记录正文 |
| `worker_cleanup_unsettled` | 独立核对结果、物理清理、服务端结算及本地清单；不能据此宣称 ACK |

迁移 `0086_issue129_recovery` 在既有 maintenance audit 中增加恢复绑定，不修改旧业务结果。旧行保持为空。存在任何 v4 Worker 或恢复 audit 时拒绝降级，不能删除审计/责任来回退。升级仍须遵循既有停写和 schema 门禁；旧 W1-A 检查点不被迁移成可恢复记录。
