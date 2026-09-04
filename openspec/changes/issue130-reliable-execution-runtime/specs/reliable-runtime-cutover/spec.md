## Purpose

定义从 legacy PostgreSQL long-poll 与 Worker v1/v2 迁移到 RabbitMQ Worker v3 的 additive schema、dark launch、Sandbox Gate、最终 Cutover、Rollback、Post-cutover invariant 与单 PR 交付合同，避免双 Claim、旧 Worker 误执行和不可恢复降级。

## ADDED Requirements

### Requirement: 迁移先增不删并显式标记 Backend
第一阶段 migration SHALL 扩展 Execution 状态并集、回填所有历史行为 `dispatch_backend=legacy`，增加 generation/Outbox/Idempotency/Admission/Attempt/Slot/policy snapshot，并扩展 Worker protocol schema/config 允许 3。该阶段 MUST 保留 legacy Claim、当前 minimum protocol 与 `uq_executions_active_adapter`，且 RabbitMQ 新流量 gate 默认关闭。

#### Scenario: 从固定 main schema 升级
- **WHEN** fresh/upgrade migration 对当前 main 数据库执行
- **THEN** 历史 Execution 全部可明确归为 legacy，新表/字段完整，旧 Control/Worker 仍不会看到 rabbitmq 行

#### Scenario: 重跑 Backfill
- **WHEN** migration inventory/backfill 因中断再次执行
- **THEN** 计数一致、backend 不翻转、不重复创建 Outbox/Attempt/Slot

### Requirement: Legacy 与 RabbitMQ Claim 路径严格隔离
Legacy Claim SHALL 只选择 `dispatch_backend=legacy` 的旧状态；v3 Control Claim SHALL 只接受 `dispatch_backend=rabbitmq` 且 protocol/capability 合法的 generation。Worker v1/v2 遇到新 backend、状态或 payload MUST fail closed，禁止 silent fallback。

#### Scenario: v2 Worker 调用 legacy Claim
- **WHEN** 数据库同时有 legacy pending 与 rabbitmq queued
- **THEN** v2 只能领取 legacy pending，永不返回 rabbitmq row

#### Scenario: v3 Claim 指向 Legacy Execution
- **WHEN** RabbitMQ message 错误引用 legacy Execution
- **THEN** Control 不创建 Attempt并把它作为不可解释 infrastructure dispatch 处置

### Requirement: Batch 2 只允许 v3 Dark Launch
Batch 2 SHALL 实现并验证 v3 Consumer、Claim/Attempt/Lease/Fencing、Retry/Dead Letter 与迁移工具，但 MUST 只对隔离 canary/测试 Execution 开启 RabbitMQ backend。Batch 2 结束时不得提高 minimum protocol、drop legacy active index、关闭 legacy Claim 或把全部新流量不可逆切换。

#### Scenario: Batch 2 Candidate Gate
- **WHEN** v3 canary 与故障注入通过
- **THEN** 证据仍证明关闭 RabbitMQ ingress gate 后 legacy 新流量可继续，schema 无破坏性回退需求

#### Scenario: Sandbox 尚未通过
- **WHEN** v3 queue/Attempt 功能全绿但 Resource Sandbox Gate 未通过
- **THEN** 不允许进入最终 Cutover，不能以功能 smoke 代替隔离验收

### Requirement: Sandbox Gate 是最终 Cutover 前置条件
所有继续服务的 Worker SHALL 同时报告 protocol v3 与完整 cgroup v2/namespace/tmpfs capability，并在目标 Linux Compose/CI 通过 OOM、fork、log flood、temp disk 与 timeout 故障注入；任一缺失 MUST 阻止新流量 Cutover。

#### Scenario: 一个 Worker 缺少 tmpfs hard limit
- **WHEN** inventory 中继续服务的固定 Worker 未报告 tmpfs_hard_limit
- **THEN** Cutover command fail closed，不提高 minimum protocol或修改旧索引

### Requirement: Legacy Running 与 Pending 使用显式处理
Cutover SHALL 让 legacy running 按旧合同完成，不得执行中途转换。Legacy pending MUST 二选一：由 legacy Worker drain，或在事务中转换为 queued/rabbitmq、generation 与唯一 Outbox；转换 MUST 保留 Version/Input/timeout/locale/Credential/InputArtifact Lease 等不可变事实。

#### Scenario: Pending migration 在提交前崩溃
- **WHEN** 工具在事务提交前中断
- **THEN** 原 row 仍为 legacy pending且无新 Outbox，重跑安全

#### Scenario: Pending migration 在提交后响应丢失
- **WHEN** row 与 Outbox 已提交但工具未记录本地成功
- **THEN** 重跑通过 backend/generation/唯一约束报告 already converted，不创建重复 Outbox

#### Scenario: Legacy Running 存在
- **WHEN** preflight 发现仍有 legacy running
- **THEN** 最终 Cutover Gate 停止并等待 drain 或显式人工终止，不把它转换为 Attempt

### Requirement: Final Cutover 使用不可交换的顺序
Sandbox Gate 通过后，系统 MUST 依次执行 preflight/backup-restore 证据、drain/migrate legacy、切新流量到 RabbitMQ、验证 Slot 并发防线、提高 minimum protocol 为 3、drop `uq_executions_active_adapter`、在 legacy pending/running 清零后关闭 legacy Claim。任一步失败 MUST 阻止后续步骤。

#### Scenario: Slot 并发测试未通过
- **WHEN** 同 Adapter 并发 Claim 仍可能形成两个 active Attempt
- **THEN** 不得 drop 旧索引或宣告 Cutover 完成

#### Scenario: 仍有 v2 Worker
- **WHEN** 需要继续服务的 Worker protocol distribution 包含 v2
- **THEN** 不得设置 minimum=3或关闭 legacy Claim

#### Scenario: Legacy rows 清零
- **WHEN** legacy pending/running 均为 0、新流量为 rabbitmq且所有 Worker v3+isolated
- **THEN** 才允许关闭 legacy execution Claim；历史 terminal legacy 行继续可读

### Requirement: 二进制降级不是新数据的安全 Rollback
一旦已创建 RabbitMQ Execution/Attempt或提高 minimum protocol，系统 MUST 不把启动旧 Control/Worker 二进制作为安全 rollback。Rollback SHALL 保留 additive schema，通过兼容 Control drain/repair 或显式、审计化 reverse migration；旧 Worker 对新合同始终 fail closed。

#### Scenario: Cutover 后尝试启动旧 Worker
- **WHEN** v1/v2 Worker 注册或 Claim
- **THEN** Control 明确返回 protocol incompatible，不降低校验让其 silent execute

#### Scenario: Cutover 前关闭 Gate
- **WHEN** 仍处于 additive/dark-launch 且无生产 RabbitMQ Execution 需要旧代码理解
- **THEN** 可关闭新 ingress gate继续 legacy，新 schema/审计事实保留且不破坏性 downgrade

### Requirement: Post-cutover Invariant 自动验证
Cutover 工具 SHALL 以数据库/Broker 权威事实断言：legacy pending/running 为 0；每个 queued RabbitMQ Execution 有合法 generation 与 pending/published Outbox或明确 Incident；每个 running Execution 有且仅有一个 active Attempt/Slot；不存在双 backend Claim、重复 active Attempt、orphan Outbox 或无主 DLQ。

#### Scenario: Orphan Outbox
- **WHEN** 验证发现 Outbox 无对应 Execution/generation
- **THEN** Gate 失败并列出非敏感 ID，禁止关闭 legacy path或标记完成

#### Scenario: 全部不变量成立
- **WHEN** Cutover 后重复执行验证工具
- **THEN** 结果幂等全绿且计数稳定，不修改业务数据

### Requirement: 一个 Change 与一个最终 PR
Issue #130 SHALL 在一个 OpenSpec change、一个功能分支内按 Batch 1、2、3 串行实施。每个 Batch MUST 有 checkpoint Candidate SHA、相关自动/故障证据与 Sol exact-SHA 只读审计，但 LOCAL_FAST checkpoint MUST 不创建 PR或冒充 AO 官方 Review。全部本地 Gate 通过后才可创建一个非 Draft REMOTE_RELEASE PR。

#### Scenario: Batch 1 Gate 失败
- **WHEN** Outbox/Admission/RabbitMQ Gate 未通过
- **THEN** 不进入 Batch 2、不创建 PR、不勾选失败任务

#### Scenario: 最终 PR Review 修复
- **WHEN** AO 官方 Claude Review 要求修改且产生新 head SHA
- **THEN** 受影响本地 Gate、Hosted CI 与 exact-head AO Review 全部重跑，旧 SHA 证据只保留为历史

### Requirement: 自动证据与用户验收分离
最终 PR head 同时通过 Hosted CI 与 AO 官方 Claude Review后，系统 SHALL 只标记 `READY_FOR_USER_ACCEPTANCE`；merge、release 与用户视觉/业务 PASS MUST 分别记录，任何自动 Gate 不得代替用户决定。

#### Scenario: PR Review 与 CI 全绿
- **WHEN** exact-head Hosted CI 和 AO Review 均 PASS
- **THEN** 交付状态为待用户验收，不自动声称已合并、已发布或用户 PASS
