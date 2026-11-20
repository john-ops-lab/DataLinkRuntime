# 双 Control 共享文件

W3 第三批为 Managed Files、内置依赖材料和 AI 临时附件提供独立的共享 POSIX 命名空间。每类单独配置、验证和启用。单机部署默认继续使用本地文件后端。

| 功能 | 后端选项 | 命名空间 ID | 挂载目录 |
| --- | --- | --- | --- |
| Managed Files | DLR_ARTIFACT_STORE_BACKEND | DLR_ARTIFACT_STORE_NAMESPACE_ID | DLR_ARTIFACT_STORE_ROOT |
| 内置依赖材料 | DLR_BUILTIN_PACKAGE_BACKEND | DLR_BUILTIN_PACKAGE_NAMESPACE_ID | DLR_BUILTIN_PACKAGE_ROOT |
| AI 临时附件 | DLR_AI_ATTACHMENT_BACKEND | DLR_AI_ATTACHMENT_NAMESPACE_ID | DLR_AI_ATTACHMENT_ROOT |

后端选项可填 `local` 或 `shared_posix`。启用共享后端时，两节点配置同一个命名空间 UUID，分别挂载同一存储中的私有目录。三个目录独立且互不嵌套。以 Control 的同一服务 UID 访问，目录权限为 0700，文件权限为 0600。运行期间保持挂载、命名空间和锁文件身份稳定。Worker 经受权 Control API 下载，不挂载共享目录。

## 挂载与预检

在存储供应侧创建目录。使用候选 Control 镜像和实际挂载，以运行 Control 的 UID 执行一次初始化。两节点分别运行同一个新探针 UUID 的预检。

```shell
python -m dlr.control.services.shared_posix initialize "$PURPOSE" "$MOUNT" "$CLUSTER_ID" "$NAMESPACE_ID"
python -m dlr.control.services.shared_posix prove "$PURPOSE" "$MOUNT" "$CLUSTER_ID" "$NAMESPACE_ID" --probe "$PROBE_ID" --slot a
python -m dlr.control.services.shared_posix prove "$PURPOSE" "$MOUNT" "$CLUSTER_ID" "$NAMESPACE_ID" --probe "$PROBE_ID" --slot b
```

A 和 B 在预检时同时在线。每轮读取对端的新挑战、确认原子发布与 fsync，并实测跨节点独占锁的排斥和交接。证明记录绑定两个 Linux boot ID 和各自挂载目录身份。Control 首次启用某个后端时，将证明登记到当前 Control 实例和运行时挂载。换 Control 实例、宿主重启或重新挂载后运行新一轮预检，存活节点可以继续使用仍有效的证明。挂载、预检或数据库绑定错误时，相应功能拒绝依赖它的新请求。

部署存储需要支持跨客户端一致的 flock、同一文件系统内的原子 rename 和持久 fsync。仅客户端本地的锁配置不适用于双 Control。跨节点访问演练可以使用单服务器 NFS。验收使用 NFSv4.2、跨客户端远端锁、`lookupcache=none` 和 `actimeo=1`，并在实际挂载上验证新名称的可见性。存储服务的故障域验收见后续 HA 操作合同。

## 升级和文件迁移

从 W3 第二批 `1fc2ca9483a32d607a27cc9942a1a92a29ba68d3` 的 `0095_issue129_takeover` 以原数据库 owner 执行受控停机升级到 `0096_issue129_shared_storage`。按[镜像转换流程](control-ha-takeover.md)验证备份和实际进程停止，保留身份、权限和转换链。在前一转换已使用的参数后追加 `--takeover-source` 和 `--takeover-intent`，用 `check`、`apply`、`observe` 确认当前版本已提交。

迁移时保持运行角色 NOLOGIN，确认两节点的写者、读者和 GC 已停止，源挂载保留。对 Managed Files 和内置材料分别选择已完成预检的目标。

```shell
python -m dlr.control.storage_migration "$PURPOSE" "$MOUNT" "$NAMESPACE_ID" \
    --source "$OLD_ROOT" --expected-generation 0 --manifest "$PRIVATE_MANIFEST"
python -m dlr.control.storage_migration "$PURPOSE" "$MOUNT" "$NAMESPACE_ID" --observe
```

清单逐项记录内部对象键、part 标记、物理大小和 SHA-256，并保存在私有目录。只有全部校验和独立读回成功才配置新后端并恢复服务。中断后的同源同目标可重跑，使用新的清单文件名，已存在的目标仍逐项复验。数据库提交结果未知时先运行 `--observe`。若源缺少冻结内容、目标与预期不符或原物理责任未结，保留源和账本，查清后再继续。

迁移不会改写原 Artifact、Binding、Lease、Dead-letter Hold、内置依赖快照或上传预留。首次切换前可以使用原后端恢复。已接受新写入后，从当前权威挂载向另一个独立、已验证的目标反向迁移，并把 `--expected-generation` 设为读回的当前值。原先的副本必须对账，存在冲突的目标不能成为权威。

## 处理活动责任

Worker 下载在接收节点重新校验身份、Attempt、Claim 与冻结内容。同一 Attempt 对每个冻结文件的下载资格由数据库只授予一次。流持有读锁到实际 fd 关闭。Managed Files 删除和内置材料结账在物理操作成功后完成。内置上传取消先撤销发布许可，写者仍持锁时继续保留预留。切换时不要删除或替换锁文件来解除忙状态。

Managed Files 的孤儿审计只隔离超过宽限且缺少权威引用的合法对象。正常删除和后台审计使用持久分页进度或租约。挂载失联、底层 I/O 仍未结束、提交未知时保留对应责任，由可用节点按原冻结引用继续核对。

## AI 附件与数据库恢复

AI 附件的命名空间单独选择，迁移命令使用 `ai_attachments`，省略 `--source`。两节点使用相同的 TTL 和总量设置，并配置支持实例校验及实际进程停止回执的私有转换服务。两节点共用一个转换器时使用相同 URL。分别提供转换器时，使用 `DLR_AI_DOCUMENT_CONVERTER_URL` 设置本节点端点，以 `DLR_AI_ATTACHMENT_PEER_CONVERTER_URL` 设置对端端点。端点按固定槽位绑定。维护命令采用相同配置。附件文件名、内容和解析产物只存在于该临时挂载。数据库只保存授权、绝对过期时间、大小、摘要、解析代次和资源账。

关闭页面范围会记录有期限的关闭事实。授权失效、过期或范围关闭后，迟到的解析不能发布。未证明停止的解析保留预算。连接中断后，人工 Retry 可以使用仍有效的冻结附件，过期会要求重新上传。

在恢复数据库备份前保持附件服务停用。使用 `--revoke-temporary` 在旧挂载写入独立于数据库的撤销记录。恢复后先用 `--retire-temporary` 核对旧挂载和任务归属，在实际读者、写者和转换器都已停止后清理旧临时内容和容量账。无法确认实际停止时保持原责任。之后选择全新命名空间并重新预检，再恢复附件入口。保留旧挂载的撤销记录，先前的引用和页面状态不会因恢复而重新授权。
