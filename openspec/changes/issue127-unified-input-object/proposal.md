## Why

Task Adapter 的手动运行与定时运行目前分别持有 JSON 输入，导致保存、校验、Execution 固化和 Worker 下发语义分叉，也无法安全承载用户上传文件。Issue #127 需要在保持三语言 `handle(context, input)` JSON 合同的前提下，引入唯一的 Adapter 级输入配置和受治理的 Managed Input Store，使配置变更、运行快照、文件生命周期与故障回收可审计、可并发、可回滚。

## What Changes

- 为每个 Task Adapter 建立唯一、单调 revision 的 `AdapterInputConfig`，统一 `none`、`json`、`managed_files`，并保留前后端均不可用的 `remote_files` 占位。
- 让 manual、Scheduler 与 schedule Adapter 的“立即运行一次”复用同一输入解析、有效性校验、不可变 Execution 快照和 Worker 下发服务；“立即运行一次”不修改 Schedule 游标或配置。
- **BREAKING**：过渡开关关闭后，`POST /api/adapters/{adapter_id}/executions` 不再接受 per-run `input`，旧 Schedule API 也不再接受独立 `input`；旧式请求返回稳定错误码。
- 建立数据库动态 Managed Input 设置、部署环境变量边界、LocalFileArtifactStore、容量预留、上传会话、Artifact、Binding、Lease、删除任务、配额、低水位、TTL、GC、审计与完整锁序。
- 固化 Execution 输入摘要、配置 revision、claim/recovery/Workspace 清理超时快照和稳定错误码；输入失效的 Schedule 消费 due point、推进未来游标但不创建 Execution。
- 引入 Worker protocol v1/v2 协商、Claim Token 与独立 Cleanup Token、受控下载、校验、持久清理日志、同步/延迟 Workspace 清理和 Control stale Execution 回收。
- 为 Python、JavaScript、Java Context 增加文件读取 API；文件二进制仅存在 ArtifactStore 与 Execution 期间的 Worker 临时副本，不进入 PostgreSQL 或 TaskPayload 路径字段。
- 在 Web 运行设置中提供独立输入对象区、上传/刷新恢复/替换/删除/保留策略、双语错误与运行门禁；在 Execution 历史中仅展示不可操作的输入摘要，并提供按语言生成、显式复制的文件读取示例。
- 迁移现有 Schedule JSON 和 manual 默认输入，保留有期限的旧字段镜像与 Worker 双协议窗口；按 Wave A-D 串行集成，并通过 feature flag 延后开放 `managed_files`。
- 根据本地 Round 1 独立审计收紧兼容写入、上传续租、删除重试、Worker 恢复、Web 权威能力与 exact-SHA 证据；AI 表格附件和登录 locale 作为同 PR 的独立伴随 change 管理，不计入本 Issue 的人工验收。

目标是形成一套当前输入、一条 Execution 固化路径和一套文件治理闭环。非目标包括真实 `remote_files`、通用文件库/历史下载与复用、跨 Adapter 共享或去重、PostgreSQL 二进制存储、多 Control LocalFileArtifactStore 写入、强 OS 只读隔离、新 overlap/misfire 策略、per-run 输入覆盖及自动写入示例代码。

## Capabilities

### New Capabilities

- `adapter-input-config`: Adapter 级唯一输入配置、revision、四类 source、统一运行解析、Runtime Lock、Schedule/run-now 与复制语义。
- `managed-input-lifecycle`: Managed Input 设置、上传预留、ArtifactStore、Artifact/Binding、配额/低水位/TTL、删除任务、GC、权限、锁序与审计。
- `execution-input-snapshot`: 不可变 Execution 输入摘要、Artifact Lease、Schedule 阻塞游标、stale Execution 收敛、历史只读边界与稳定错误。
- `worker-input-protocol`: Worker v1/v2 协议、Claim/Cleanup Token、受控下载、Workspace 清理日志、故障恢复及 Python/JavaScript/Java Context 文件 API。
- `managed-input-web`: 四类输入卡片、JSON/文件配置、上传恢复、保留策略、双语与响应式交互、示例复制、历史展示和服务端权威门禁。
- `input-compatibility-rollout`: 数据迁移、旧输入兼容开关、旧 Schedule 镜像、Worker 滚动升级、feature flag、Wave A-D 发布及非破坏性回滚。

### Modified Capabilities

无。当前仓库尚无已同步到 `openspec/specs/` 的现行 capability；历史 `docs/specs/` 仅作为已发布里程碑事实，不在本变更中改写。

## Impact

- 数据库：新增 InputConfig、Managed Input 设置、Artifact/Reservation/Binding/Lease/Deletion Job 与 Execution/Schedule/Worker 字段和约束；Alembic 必须同时覆盖 fresh install、固定基线升级、幂等回填、冲突失败与旧列保留窗口。
- Control：新增输入配置、管理员设置、上传/恢复/删除、Worker 下载和清理回执 API；重构 manual/Scheduler/run-now 创建路径；新增 GC、审计、stale Execution reconciler 与安全脱敏。
- Worker/Runtime：TaskPayload 与 Worker 注册升级到可协商 v2；Workspace 从无状态临时目录升级为带归属标记、manifest、持久清理日志和确定性清理预算的 Execution 目录；三语言 Context 增加文件元数据。
- Web：`TaskRunSettingsPanel`、系统设置、Execution 历史、API/types、独立 multipart client、i18n 资源与浏览器测试受影响；Ant Design 版本保持 5.29.3，不新增通用 UI 框架。
- 部署：Control 持久卷新增 ArtifactStore，Worker 持久卷新增清理日志；LocalFileArtifactStore 明确只允许单 Control 写入。数据库动态策略与部署环境变量保持单一权威，文件能力默认关闭。
- 兼容与回滚：新 Control 先兼容旧 Web/Worker，旧式调用连续 48 小时归零后再关兼容；回滚观察至少 7 天并覆盖每个启用 Schedule 两个真实计划点。回滚只关 feature flag、停止新增并排空活动执行，保留新表和 Blob，不做破坏性 schema downgrade。
- 安全：原始 Claim Token 不落库，Cleanup Token 只允许进入 Worker 私有 `0600` 清理日志；二者不进入 URL、浏览器响应、普通/审计日志。原始文件名不参与路径，下载必须同时校验 Worker、Execution、Lease、Token、大小与 SHA-256。
