## Purpose

定义 Execution 创建时不可变输入事实、文件 Lease、Schedule 阻塞游标和 Worker 丢失后的终态收敛，使后续配置或文件治理不能改变已创建运行。

## ADDED Requirements

### Requirement: Execution 固化完整输入快照
每个 Task Execution SHALL 在创建事务中固化 `input_source_type`、`input_config_revision`、不可变 `input_snapshot`、Adapter timeout、claim/recovery/Workspace cleanup 超时快照和 `claim_deadline_at`；协议 v2 claim 时再固化 `execution_deadline_at` 和 Token 哈希。公开 `input_snapshot` 顶层对所有来源固定使用 `source_type` 与 `revision`；只有 `managed_files` 额外包含 `artifacts`，其他来源不得携带该键。

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

### Requirement: 文件 Execution 使用运行期 Lease 固定具体集合
managed_files Execution 创建时 SHALL 在持有 Artifact 锁的同一事务中创建带 `created_at` 的有序 Lease；Lease MUST 只授权该 Execution 的 Worker 下载并阻止 Blob 在 Execution 为 pending/running 时被删除。新建 Task Execution 的 Workspace cleanup 状态 SHALL 为 `pending`，并只可由终态、stale reconciler 或合法 cleanup receipt 收敛为 `completed/deferred`；迁移前历史行可保持 NULL。

#### Scenario: 保存后立即替换配置
- **WHEN** Execution 已创建后用户替换当前 Binding
- **THEN** 新 Execution 使用新集合，既有 pending/running Execution 继续通过原 Lease 使用旧集合

#### Scenario: 终态释放 Lease
- **WHEN** Execution 进入稳定终态
- **THEN** 业务终态、cleanup 状态和 Lease 释放在同一 Control 受锁流程中提交，且 Lease 不以独立 TTL 猜测释放

### Requirement: 超时快照有范围与不变量
Control SHALL 以数据库时间计算 deadline，并在 Execution 创建时固化部署值；配置与 TaskPayload MUST 满足 `cleanup_attempt <= cleanup_total < recovery_grace` 以及 Issue 指定范围，非法部署配置阻止相关服务启动，非法 TaskPayload 不启动 Adapter。

#### Scenario: 滚动修改部署超时
- **WHEN** 管理员修改环境变量并滚动服务
- **THEN** 既有 Execution 继续使用原快照，新建 Execution 使用新合法值

#### Scenario: 非法清理预算
- **WHEN** attempt 大于 total 或 total 不小于 recovery grace
- **THEN** Control 启动校验失败，Worker 领取到非法 payload 时也拒绝启动 Adapter并返回稳定错误

### Requirement: Schedule 输入失效必须消费计划点而不热循环
Scheduler SHALL 在锁定同一 Schedule 行的事务中记录 due point、顶层 `last_blocked_reason=input_invalid`、非本地化结构化 invalid detail、阻塞时间与已处理计划点，并按现有 Cron/timezone/DST 规则把 `next_run_at` 推进到当前时间之后；不得创建 Execution 或补跑失效期间计划点。

#### Scenario: 到期文件导致计划点阻塞
- **WHEN** Schedule due 且当前 managed_files 无有效文件
- **THEN** 系统不创建 Execution，保持顶层 reason 为 `input_invalid`，另行持久化 `managed_files_empty`、`artifact_expired` 等结构化 detail，并推进到下一个未来点

#### Scenario: 多 Scheduler 竞争阻塞点
- **WHEN** 多个 Control 同时处理同一无效 due point
- **THEN** Schedule 行锁与已处理计划点保证该点只消费一次且不会每轮重复告警

#### Scenario: 用户修复输入
- **WHEN** 用户停用 Schedule、保存有效输入并重新启用
- **THEN** Scheduler 从新的未来游标恢复，不补跑输入失效期间的积压

### Requirement: stale pending Execution 在 Control 侧收敛
超过 `claim_deadline_at` 仍未 claim 的 pending Execution SHALL 进入 `failed`、`error_code=worker_unavailable`，记录 cleanup `completed`、清空 cleanup error 并释放 Lease；系统 MUST 不自动重跑。

#### Scenario: Worker 永未领取
- **WHEN** pending Execution 超过 claim deadline
- **THEN** reconciler 原子写入 ended_at、业务错误、cleanup 状态和 Lease 释放，晚到 claim 不再成功

### Requirement: stale running Execution 在 Control 侧收敛
running Execution 超过 `execution_deadline_at + recovery_grace_seconds_snapshot` SHALL 根据 Worker 健康事实进入 `timeout` 或 `failed/worker_lost`，记录 cleanup `deferred/workspace_cleanup_unknown` 并释放 Lease；系统 MUST 不自动重跑。

#### Scenario: Worker 仍健康但执行超限
- **WHEN** deadline 加 grace 已过且 Worker 仍 effective-online
- **THEN** Execution 原子进入 timeout、释放 Lease，并保留 cleanup unknown 供 Worker 后续回执

#### Scenario: Worker 已离线
- **WHEN** deadline 加 grace 已过且 Worker 已确认离线
- **THEN** Execution 原子进入 failed、`error_code=worker_lost`、释放 Lease且不自动重跑

### Requirement: 终态与晚到报告幂等
Execution 一旦进入业务终态 MUST 不再被 progress、Result、stale reconciler 或晚到 Worker 改写；同一拥有者重复上报相同终态可幂等成功，清理回执只能修改 cleanup 字段。

#### Scenario: Control 已收敛后晚到成功 Result
- **WHEN** Worker 在 Execution 已因 stale 收敛后上报 succeeded
- **THEN** Control 返回幂等终态事实，不改变业务状态、output、error code、ended_at，不触发重跑

#### Scenario: 非拥有 Worker 上报
- **WHEN** 其他 Worker 对该 Execution 上报 progress、Result 或下载
- **THEN** Control 拒绝操作，即使 Execution 已终态也不泄露可操作数据

### Requirement: 业务结果与 Workspace 清理结果独立
Execution SHALL 分别保存业务 `status/error_code` 与 `workspace_cleanup_status/error_code`；清理失败 MUST 不把成功业务结果改成失败，也不得阻止业务终态和 Lease 释放。

#### Scenario: 业务成功但同步清理失败
- **WHEN** Adapter 成功且 Worker 在清理总预算内未删除 Workspace
- **THEN** Execution 保持 succeeded，cleanup 为 `deferred/workspace_cleanup_failed`

#### Scenario: Result 报告 deferred 缺少原因
- **WHEN** Worker Result 提交 `workspace_cleanup_status=deferred` 但未携带受支持的 cleanup error code
- **THEN** Control 返回稳定 schema/domain 校验错误且不写入含糊的 cleanup 状态

#### Scenario: 从未创建 Workspace
- **WHEN** stale pending 从未 claim
- **THEN** cleanup 直接为 completed，且不生成虚假 Worker 文件系统错误

### Requirement: Execution 历史只暴露不可操作输入摘要
详情 SHALL 只读展示 `none`、当次 JSON 或文件名/类型/大小/SHA-256；历史 API/UI MUST 不提供 Artifact ID、内部路径、下载、复用、恢复配置、再次运行或可操作 Lease。

#### Scenario: 查看历史文件输入
- **WHEN** 用户打开 managed_files Execution 详情
- **THEN** 响应与页面只显示不可变摘要，即使当前 Artifact 已删除也保持审计事实

#### Scenario: 列表分页
- **WHEN** 用户仅请求 Execution 历史列表
- **THEN** 列表继续使用轻量摘要，不携带 JSON input、文件摘要、output 或日志大字段

### Requirement: 稳定错误码跨组件一致
Control、Worker、Web 与发布文档 SHALL 复用 Issue 定义的稳定 code 集；`input_invalid` MUST 通过非本地化结构化 reason 区分 `managed_files_empty`、`artifact_expired` 等原因，用户文案可本地化但 code 不得翻译。

#### Scenario: 校验失败跨层传播
- **WHEN** Worker 下载校验发现 SHA-256 不匹配
- **THEN** Worker 与 Control 记录 `input_artifact_checksum_mismatch`，Web 映射双语消息且不展示原始路径或 Token
