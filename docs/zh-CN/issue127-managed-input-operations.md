# Issue #127 Managed Input 运维说明

本文是 Managed Input 的 API、存储归属、迁移兼容和非破坏回滚 runbook。文中的
Token、数据库 URL 和宿主机目录均为占位符；不要把真实值写入文档、日志或命令历史。

## API 与权限边界

业务用户只读 `GET /api/system/managed-input-capability`，响应只包含
`managed_files_enabled`、`ready`、`default_retention_seconds`、
`max_custom_retention_seconds` 与 `allow_manual_delete`；这些字段只供业务表单判定，
不含用量或部署信息。管理员才能读写
`/api/system/managed-input-settings`；该资源包含策略和 usage，但不返回部署路径、
storage key、Token、Secret 或密码。

Adapter 输入使用下列边界：

| 操作 | 方法与路径 | 权限/结果 |
| --- | --- | --- |
| 上传 | `POST /api/adapters/{id}/input-artifacts` | Bearer 管理员或账号 Cookie + CSRF；multipart 单文件 |
| staged 列表 | `GET /api/adapters/{id}/input-artifacts` | Adapter edit |
| 保存当前输入 | `PUT /api/adapters/{id}/input-config` | `expected_revision` 乐观并发控制 |
| staged/READY 删除 | `DELETE /api/adapters/{id}/input-artifacts/{artifact}` | Adapter edit；READY 必须带 `expected_revision` |
| Artifact 删除重试 | `POST /api/system/managed-input-artifacts/{artifact}/retry-delete` | 管理员；只接受已达告警阈值的 `DELETE_FAILED` |
| deletion job 重试 | `POST /api/system/managed-input-deletion-jobs/{job}/retry-delete` | 管理员；只接受已达告警阈值的 `DELETE_FAILED` |

客户端按结构化 `detail.code` 处理错误，不按 message 分支。`409` 的
`adapter_busy`、`input_config_revision_conflict` 或 quota 错误应保留草稿并刷新，
`input_source_not_available` 表示发布开关关闭；上传进度只显示已接收字节。
上传 writer 由 Control 在后台周期续租，不提供浏览器可调用的 renew API。低水位与
配额冲突返回 `409`，客户端保留草稿且不得显示存储路径。

## 单 Control 与 LocalFileArtifactStore

`LocalFileArtifactStore` 只由 Control 进程持有并写入。Compose 中唯一的
`dlr_artifact_store` 挂载属于 Control；Worker 不挂载该卷，只能用带 claim token 的
内部下载接口取得当前 Execution 被 Lease 授权的文件。浏览器和 Worker 永远不接触
storage key、Control 路径或删除凭据。

所有新 Control/Worker 使用 v2 protocol 后，才能开放 managed-files 执行；Worker 通过
`DLR_WORKER_PROTOCOL_VERSION=2` 声明协议。v1 Worker
继续处理 `none`/`json`，遇到 managed-files 必须被协议门禁拒绝。扩容时仍只增加
Worker，不增加 ArtifactStore 写入者；GC 先锁 Artifact/Lease，再在锁外做文件 I/O。

Worker 只通过 canonical
`POST /api/workers/executions/{execution_id}/workspace-cleanup` 上报延迟清理完成，
并使用独立 Cleanup Token；`deferred` Result 只接受稳定
`workspace_cleanup_failed` 原因。业务成功与 Workspace 清理状态相互独立。

## 迁移与旧协议兼容

升级使用固定顺序，并在目标部署上记录 Alembic head：

```sh
docker compose run --rm control alembic upgrade head
docker compose ps
```

迁移从旧 Adapter/Schedule 数据确定性回填 AdapterInputConfig：手动 Task 变为
`json` `{}`，Schedule 的旧 `input` 回填为同值；Webhook 不创建 Task 输入配置。
回填必须可重复、冲突即 fail-fast，并输出 adapter 数量和 source type 计数。旧的
`adapter_schedules.input` 列在兼容窗口保留并由写入事务镜像更新；Scheduler 只读取
新的 AdapterInputConfig。Managed Input 的新表、Blob 和 deletion job 都是加法迁移。
Alembic `0026`～`0029` 的 `downgrade()` 仅供隔离测试清理，生产回滚禁止调用，
因为它会丢弃输入权威事实或 Execution 快照。

## 非破坏回滚演练

回滚是部署回滚，不是数据库降级。先冻结新增 managed upload 和新 managed
Execution，停用 Schedule/Webhook admission，等待 `pending/running` 归零并确认
Lease 已释放；已有 history、Blob、new tables、deletion jobs 和旧列全部保留。

```sh
# 使用部署编排文件和受保护的 secret store；下列值仅为占位符
export DLR_MANAGED_FILES_ENABLED=false
export DLR_ARTIFACT_DELETE_ALERT_THRESHOLD=5
docker compose up -d --force-recreate control web account-web
curl -fsS -H 'Authorization: Bearer <ADMIN_TOKEN>' \
  http://<control>/api/system/managed-input-capability
# 必须至少显示 managed_files_enabled=false 与 ready=false
# managed upload/config 写入必须返回 input_source_not_available
```

回滚期间不能执行 `alembic downgrade`、删除 ArtifactStore 卷、清空旧列或删除
deletion jobs。保留双协议 Worker 和数据表，必要时只读旧 `none/json` 流程。确认
计数、历史和 Blob 未变化后恢复新版：

```sh
export DLR_MANAGED_FILES_ENABLED=true
docker compose up -d --force-recreate control worker web account-web
docker compose ps
```

恢复后再次检查 capability 为 `true/true`、旧配置和 history 仍可读，随后让 GC/Lease
治理继续运行。若 active Execution 无法排空，应保持开关关闭并升级处理，不强杀进程、
不释放未知 Lease。

## 保留与审计

`system_default`、custom 和永久（`manual_delete`）保留策略均受后台
`max_custom_retention_seconds`、quota 与低水位限制；过期文件由 GC 标记、检查
Lease 后删除，并释放 capacity。管理员 settings 页面可见策略、usage、quota 和
`over_quota`，普通用户只能看到 capability。`expires_at=NULL` 只表示当前状态由
`manual_delete` 或尚未确定的 staged 生命周期管理，不代表文件永久安全或可绕过治理。
连续删除失败达到阈值后停止自动重试并记录 ERROR，必须由管理员调用上述 retry API。
未达阈值的失败继续遵守有限 backoff，管理员 retry 不会提前跳过；达阈值后普通业务删除
返回 `input_artifact_retry_not_allowed`，不能替代管理员治理。capability 同时返回有序
`allowed_extensions`，浏览器的 accept 与预校验只由该安全字段派生，服务端校验始终是最终权威。
Execution 历史允许下载业务 stdout/stderr 日志，但 Managed Input 历史摘要永远不提供
输入文件下载、复用或恢复。日志只记录安全错误码和计数，禁止记录
Bearer、Cookie、CSRF、storage key、宿主机绝对路径和文件内容。
