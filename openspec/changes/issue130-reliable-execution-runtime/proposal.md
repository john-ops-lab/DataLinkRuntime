## Why

DLR 现有 PostgreSQL 长轮询与“每个 Adapter 最多一个 `pending/running` Execution”只能完成基本异步执行，无法在 Worker busy/offline、Broker 短时故障或调用方重试时同时提供可靠接收、有界排队、可恢复 Attempt 和明确背压；现有子进程也缺少 CPU、Memory、PID 与临时磁盘硬隔离。Issue #130 需要在单 Control、单 PostgreSQL、单 RabbitMQ 节点和固定 Worker 的最小部署中冻结并交付一套可迁移、可回滚、可审计的可靠运行时。

## What Changes

- 新增 PostgreSQL 权威的统一逻辑 Execution 队列：202 前同事务提交 Execution、Idempotency、Admission 与 Transactional Outbox；Adapter/Global Admission 与 Outbox backlog 的 count/bytes/age 是精确业务保护线，RabbitMQ 只负责持久派发与短时缓冲，`messages_ready` 仅作运维指标。
- 新增 RabbitMQ 4.3.5 Quorum Queue、Publisher Confirm、Persistent Message、Manual ACK、delayed retry、Infrastructure DLQ 与有界 topology；`max-length`/`max-length-bytes` + `reject-publish` 只作为允许 bounded in-flight overshoot 的近似、最终拒绝第二道保护，不宣称精确硬上限；ACK 改为 Control durable Claim 与 Worker journal 落盘后确认，不等待业务终态。
- 新增 ExecutionAttempt、Adapter Slot、Lease、Fencing、有限 Retry、Business Dead Letter 与 Replay；同一 Adapter 在 #130 固定一个 active Attempt，但允许多个 `queued/retry_wait`。
- 新增显式 Schedule misfire policy、bounded catch-up 与逐点或可验证聚合的审计结果。
- 新增 Linux cgroup v2 + namespace + bounded tmpfs 的 Resource Sandbox，并把 log/output/workspace/dependency preparation 变为运行期有界；能力不足时 fail closed。
- 修改现有输入、文件 Lease、Worker 协议、Web 与兼容发布合同，使 v1/v2 legacy Execution 与 v3 RabbitMQ Execution 在迁移期可共存且互不误 Claim。
- 收敛运行状态的信息架构：首页只显示绿/黄/红的系统汇总（加载或未知为中性灰），Control 与 Worker 的协议、隔离、队列和预检详情移入管理员“系统设置 / 系统状态”，复用既有健康与 Worker API，不新增监控存储或告警框架。
- **BREAKING**：最终 Cutover 后新 Execution 使用 `queued/running/retry_wait/succeeded/dead_letter/cancelled/expired` 状态、Worker minimum protocol 提升为 3，并在新 Adapter Slot 防线验证后退役 legacy Claim 与 `uq_executions_active_adapter`；历史 legacy 行及其旧终态继续兼容读取。
- 采用一个功能分支与一个最终非 Draft PR；B1/B2/B3 只形成串行 checkpoint，最终 PR head 才运行 Hosted CI 与 AO 官方 Claude Review。

## Capabilities

### New Capabilities

- `reliable-execution-ingress`: 定义可靠 202、原子 Admission、Idempotency、Execution 新状态与固定 Worker 离线入队合同。
- `rabbitmq-dispatch-outbox`: 定义 RabbitMQ 版本/topology、Transactional Outbox、Confirm、queue bounds、Infrastructure DLQ 与恢复合同。
- `execution-attempt-lifecycle`: 定义 Attempt/Adapter Slot、Control Claim decision、ACK-on-durable-claim、Lease/Fencing 与取消/恢复合同。
- `execution-retry-dead-letter`: 定义有限 Retry、错误分类、Business Dead Letter、Replay 与 Managed File Hold 合同。
- `schedule-queue-policy`: 定义三种 Schedule misfire policy、bounded catch-up、cursor 与审计结果。
- `execution-resource-isolation`: 定义 cgroup v2/namespace/tmpfs Resource Sandbox、capability preflight、fail-closed 与运行期有界合同。
- `reliable-runtime-cutover`: 定义 additive migration、v3 dark launch、Sandbox Gate、最终 Cutover、Rollback、Post-cutover invariant 与单 PR Gate。

### Modified Capabilities

- `adapter-input-config`: 运行入口不再要求固定 Worker 当下 effective-online；queued/retry_wait 使用不可变快照且不扩张用户态 Runtime Lock。
- `execution-input-snapshot`: RabbitMQ Execution 固化 retry/resource/backend/generation 事实，并由 Attempt lease recovery 替代 legacy stale 收敛语义。
- `input-compatibility-rollout`: Worker v1/v2 兼容窗口扩展到 v3，发布顺序改为 B1/B2 dark launch/B3 cutover 与一个最终 PR。
- `managed-input-lifecycle`: 文件 Lease 保护覆盖 RabbitMQ 非终态，Business Dead Letter 通过有界 Artifact Hold 保证 Replay 窗口。
- `managed-input-web`: Web 增加 queued/retry_wait/dead_letter、Schedule policy、Admission/Retry 与 isolation capability 的双语权威状态。
- `worker-input-protocol`: Worker 协议新增 v3 Consumer/Claim/Attempt journal/Lease/Fencing/Sandbox 合同，并保持 v1/v2 fail-closed 兼容。

## Impact

- Backend：Execution/Worker/Schedule/InputArtifact 模型与 Alembic、Control ingress/claim/reconciler/retention、Outbox Relay、RabbitMQ client、Worker Agent/executor/workspace、三语言 runtime 与故障注入测试。
- Web：Execution 状态、运行按钮与队列反馈、Schedule policy 表单、Dead Letter/Replay、系统能力与双语错误映射；继续使用 React 19、Ant Design 5.29.3、ProComponents 2.8.10 的固定版本。
- 部署：新增固定 RabbitMQ 4.3.5 service、凭据/TLS/topology/health 配置，以及 Linux cgroup v2 delegation、namespace/tmpfs 能力门禁；单节点 Quorum Queue 不声明 HA。
- 数据与兼容：schema 先增不删，历史行回填 `dispatch_backend=legacy`；v1/v2 只处理 legacy，v3 RabbitMQ 路径默认关闭，Sandbox Gate 通过后才执行不可逆 Cutover。回滚保留 additive schema，禁止用旧二进制 silent execute 新合同数据。
- 安全：Worker 仍不持 PostgreSQL 凭据；RabbitMQ/Claim/Cleanup 凭据不进入 Adapter、消息正文、URL 或日志；Resource Sandbox 是资源 containment，不升级为不可信多租户承诺。
- 交付：#127 六份 delta specs 先同步为主规范但保持 change active；#130 使用单一 OpenSpec change、串行 B1/B2/B3、一个最终 PR 和 exact-head Hosted CI/AO Review。
