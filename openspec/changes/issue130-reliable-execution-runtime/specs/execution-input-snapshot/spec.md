## MODIFIED Requirements

### Requirement: Execution 固化完整输入快照
每个新建且可由 Worker claim 的 Execution（包括 Task manual/Schedule/run-now 与 Webhook）SHALL 在创建事务中固化 `input_source_type`、`input_config_revision`、不可变 `input_snapshot`、Adapter timeout、claim/recovery/Workspace cleanup 超时快照和 `claim_deadline_at`；RabbitMQ Execution MUST 同时固化 `dispatch_backend=rabbitmq`、初始 dispatch generation、target Worker、logical input bytes、Retry Policy、Resource Profile、Credential binding 引用与适用 Schedule policy。协议 v1/v2 的 legacy claim 继续按旧合同固化 execution deadline/Token；v3 MUST 在 Attempt Claim 时签发 Attempt-scoped deadline/Lease/Fencing 与 Token。公开 `input_snapshot` 顶层对所有来源固定使用 `source_type` 与 `revision`；只有 `managed_files` 额外包含 `artifacts`，其他来源不得携带该键。

#### Scenario: JSON 输入快照
- **WHEN** 当前配置为 `json`
- **THEN** `Execution.input` 原样保存 JSON，snapshot 标记 source/revision，且后续配置保存不改变该 Execution

#### Scenario: None 或文件输入快照
- **WHEN** 当前配置为 `none` 或 `managed_files`
- **THEN** `Execution.input` 为 JSON `null`，类型与文件展示事实只存在于不可变 snapshot

#### Scenario: 文件摘要最小字段
- **WHEN** 创建 managed_files Execution
- **THEN** snapshot 文件项包含原始展示名、content type、size bytes、SHA-256，不包含 Artifact ID、storage key、Control 路径或 Worker 路径

#### Scenario: 快照顶层键保持封闭
- **WHEN** 序列化 none、json、managed_files 或 remote_files 的公开 Execution snapshot
- **THEN** 顶层只出现合同允许的 `source_type`、`revision` 与 managed_files 专属 `artifacts`，不得增加 Artifact ID、Binding、Lease、路径或 Token 键

#### Scenario: Webhook 创建 Execution
- **WHEN** 合法 Webhook 请求创建 `trigger=webhook` Execution
- **THEN** 系统保留完整 JSON body 的 Webhook ingress 语义，并与 Task 创建路径共享数据库时间、timeout/recovery/cleanup、backend/generation/retry/resource 快照、`workspace_cleanup_status=pending` 与第一条 Outbox；不得把 Webhook body 解释为 Task per-run override

#### Scenario: 排队后部署配置变化
- **WHEN** RabbitMQ Execution queued 后管理员滚动修改 Retry、Resource、Worker 或 timeout 配置
- **THEN** 既有 Execution/Attempt 使用原快照，新建 Execution 使用新合法值

### Requirement: stale pending Execution 在 Control 侧收敛
仅 `dispatch_backend=legacy,status=pending` 且超过 `claim_deadline_at` 仍未 claim 的 Execution SHALL 进入 `failed`、`error_code=worker_unavailable`，记录 cleanup `completed`、清空 cleanup error 并释放 Lease；legacy 系统 MUST 不自动重跑。RabbitMQ `queued` 不使用该 stale-pending 终态合同，MUST 由 Admission、Outbox/Incident、显式 expiry/cancel 与 Attempt 状态机承担。

#### Scenario: Worker 永未领取
- **WHEN** legacy pending Execution 超过 claim deadline
- **THEN** reconciler 原子写入 ended_at、业务错误、cleanup 状态和 Lease 释放，晚到 legacy claim 不再成功

#### Scenario: RabbitMQ queued 超过 legacy claim timeout
- **WHEN** rabbitmq queued Execution 的等待时间超过旧 `claim_deadline_at` 语义但未触发显式 expiry
- **THEN** 系统不得把它改为 legacy failed，必须继续保留可靠责任并暴露 queue/outbox/worker 原因

### Requirement: stale running Execution 在 Control 侧收敛
`dispatch_backend=legacy,status=running` 超过 `execution_deadline_at + recovery_grace_seconds_snapshot` SHALL 根据 Worker 健康事实进入 `timeout` 或 `failed/worker_lost`，记录 cleanup `deferred/workspace_cleanup_unknown` 并释放 Lease；legacy 系统 MUST 不自动重跑。RabbitMQ running MUST 由 active Attempt lease/fence、Resource timeout 与 Retry Policy 收敛，不得由 legacy reconciler直接写 `failed/timeout`。

#### Scenario: Worker 仍健康但执行超限
- **WHEN** legacy deadline 加 grace 已过且 Worker 仍 effective-online
- **THEN** Execution 原子进入 timeout、释放 Lease，并保留 cleanup unknown 供 Worker 后续回执

#### Scenario: Worker 已离线
- **WHEN** legacy deadline 加 grace 已过且 Worker 已确认离线
- **THEN** Execution 原子进入 failed、`error_code=worker_lost`、释放 Lease且不自动重跑

#### Scenario: RabbitMQ Attempt lease 过期
- **WHEN** rabbitmq running 的 active Attempt lease 过期
- **THEN** Attempt Reconciler 以 fence 写入 worker_lost，并依据快照策略把 Execution 置为 retry_wait 或 dead_letter，不使用 legacy timeout/failed 状态
