## MODIFIED Requirements

### Requirement: Worker 注册显式协商协议版本
Worker 注册 SHALL 携带 `protocol_version`；缺失或显式 JSON `null` 按 v1 兼容，支持 Claim/Cleanup Token 与持久清理日志的 legacy Worker 使用 v2，支持 RabbitMQ Consumer、Attempt Claim/Lease/Fencing、durable Attempt journal 和完整 Resource Sandbox 的 Worker 使用 v3。TaskPayload 中非空版本 MUST 是整数 `1`、`2` 或 `3`，不得把 bool、float 或数字字符串强制转换为协议版本。Control MUST 保存版本与 v3 isolation capability matrix，并在 backend/dispatch/cutover判断中使用。

#### Scenario: 旧 Worker 注册
- **WHEN** Worker 注册未携带 protocol_version
- **THEN** Control 将其视为 v1，并只允许领取符合最低协议的 legacy none/json Execution

#### Scenario: v1 领取文件 Execution
- **WHEN** v1 Worker 尝试领取 managed_files Execution
- **THEN** Control 返回 `worker_protocol_incompatible` 且 Execution 保持可由合格 v2/v3 Worker 领取

#### Scenario: 非整数协议版本
- **WHEN** TaskPayload 携带 bool、float、数字字符串或未知整数协议版本
- **THEN** Worker 在 journal、Workspace、依赖准备和 Adapter 进程副作用前返回 `worker_protocol_payload_invalid`

#### Scenario: v2 看到 RabbitMQ Backend
- **WHEN** v2 Worker 尝试 Claim 或解释 `dispatch_backend=rabbitmq` 的 payload
- **THEN** 它 fail closed 返回 protocol incompatible，不把 queued 状态降级为 legacy pending执行

#### Scenario: v3 缺少 Isolation Capability
- **WHEN** protocol_version=3 但 startup preflight 未证明全部必需 capability
- **THEN** Worker 注册仍可用于诊断，但 `rabbitmq_execution_v3=false`，Control 不向其切流

## ADDED Requirements

### Requirement: v3 Worker 使用 RabbitMQ Dispatch 与 Control Attempt Claim
v3 Worker SHALL 主动消费分配给自身的 durable Queue，使用 `prefetch=execution_slots` 与本地 Semaphore，并只通过 Control v3 Claim API 获取 Attempt/TaskPayload/Token。Worker MUST 校验 message target/schema 与 Control decision，且不得直接访问 PostgreSQL。

#### Scenario: Local Slots 已满
- **WHEN** 所有本地 execution slots 已占用
- **THEN** Worker 不继续无界拉取 message，Broker ready 保持背压且其他内部线程可续租/取消

#### Scenario: Target Worker 不匹配
- **WHEN** message routing queue 与 body target_worker_id 不等于当前 Worker
- **THEN** Worker 不启动 Adapter，按永久 routing 错误进入 REJECT_DLQ/Incident

### Requirement: v3 Attempt Payload 在副作用前完整校验
v3 TaskPayload SHALL 至少包含 execution/attempt ID、attempt no、fencing token、lease/renew interval、Claim/Cleanup Token、不可变 code/input/credential references、Resource Profile、cleanup deadlines 与 controlled managed-file facts。Worker MUST 在持久 journal、创建 Workspace、依赖准备或 Adapter 进程前验证类型、范围、交叉不变量与 capability；非法 payload 返回稳定 `worker_protocol_payload_invalid`。

#### Scenario: Lease interval 不合法
- **WHEN** renew interval 不小于 lease或时间字段不是合法有界数值
- **THEN** Worker 在任何本地执行副作用前拒绝 payload

#### Scenario: Resource Profile 不完整
- **WHEN** v3 payload 缺少 memory/pids/tmpfs/output 任一必需限制
- **THEN** Worker fail closed，不回退到 v2 普通 subprocess

### Requirement: v3 Durable Journal 先于 ACK 和 Sandbox
Worker SHALL 在 Control Claim commit 后把 execution/attempt、fence、受控 Workspace 计划和恢复所需 Claim/Cleanup 凭据原子写入 Workspace 外的 `0700/0600` 私有 journal，再 ACK RabbitMQ并创建 Sandbox。Journal 写入 MUST 使用同目录临时文件、fsync 与原子 rename，且内容不得含 Adapter Code、JSON input、用户文件名、Secret 或 RabbitMQ Credential。

#### Scenario: Claim 后 Journal 成功
- **WHEN** v3 Worker 收到 EXECUTE并成功持久 journal
- **THEN** 它 ACK dispatch 后才开始 Sandbox prepare，后续崩溃可由 journal/Lease恢复

#### Scenario: Claim 后 Journal 失败
- **WHEN** journal 无法安全落盘
- **THEN** Worker 不 ACK 后继续执行、不创建 Workspace；它报告 prepare failure或关闭 channel等待 Lease Recovery

### Requirement: v3 续租失败必须停止信任自身所有权
v3 Worker SHALL 以 Attempt ID、Claim Token 与 fencing token 续租；连续无法在 lease 到期前获得 Control 成功确认时 MUST 终止/冻结当前 Sandbox并停止提交业务结果。它不得因本地时钟认为仍有效而继续作为权威拥有者。

#### Scenario: Control 网络分区
- **WHEN** Adapter 仍运行但 Worker 无法确认 lease renewal直到 deadline
- **THEN** Worker 终止当前 Sandbox，保留 journal等待权威恢复，迟到 output不覆盖新 fence

### Requirement: v3 复用 v2 文件与 Cleanup 安全边界
v3 managed-files Attempt SHALL 继续使用 Execution Lease、受控下载、size/SHA-256 校验、分权 Claim/Cleanup Token、不可变 Context 文件 API 与 Workspace cleanup receipt；Resource Sandbox MUST 包围下载后的文件使用和 dependency/Adapter进程，但不得把 ArtifactStore 路径或 Token加入 RabbitMQ Message。

#### Scenario: v3 Managed Files 执行
- **WHEN** v3 Worker Claim 合法 managed-files Execution
- **THEN** 它按原 Lease/Token合同准备只读输入并在 Sandbox内执行，RabbitMQ body始终不含文件内容或存储路径
