## Purpose

定义 RabbitMQ Execution 的有限重试、错误分类、退避、Business Dead Letter、Replay 和 Managed File Artifact Hold，使每个已接受任务都能进入可解释终态，且失败历史、原输入与后续重放不会被静默覆盖。

## ADDED Requirements

### Requirement: Retry Policy 在 Execution 创建时固化
每个 RabbitMQ Execution SHALL 固化 max attempts、initial/max backoff、multiplier、jitter 与错误分类策略；默认值 MUST 为 3 次（含首次 Attempt）、5 秒初始、2.0 倍、300 秒上限与 ±20% jitter。后续 Adapter 或部署策略变化不得追溯改变已创建 Execution。

#### Scenario: 排队期间修改 Adapter Retry Policy
- **WHEN** Execution 已 queued 后管理员修改 Adapter Retry Policy
- **THEN** 既有 Execution 继续使用原快照，新 Execution 使用新合法策略

#### Scenario: 非法 Retry Policy
- **WHEN** max attempts 小于 1、backoff 为负、multiplier 小于 1 或 jitter 超出有界范围
- **THEN** 保存/启动校验失败，不创建含糊的 Execution

### Requirement: 默认错误分类保守处理外部副作用
系统 SHALL 默认只自动重试 Worker lost、Control/Worker 暂时通信失败和明确的 Sandbox 平台瞬时启动错误。Adapter 业务异常、协议/schema 错误、Credential/Input 永久错误、resource exceeded 与 execution timeout MUST 默认不可重试；只有 Execution 创建前显式合法策略允许时才可改变。

#### Scenario: Adapter 已写下游后抛异常
- **WHEN** Adapter 业务代码以普通异常终止且未显式允许业务错误重试
- **THEN** Execution 直接进入 dead_letter，不自动重复潜在下游副作用

#### Scenario: Worker 心跳丢失
- **WHEN** Attempt 因 worker_lost 终止且仍有次数
- **THEN** Execution 进入 retry_wait 并按快照退避

#### Scenario: 内存超限
- **WHEN** Attempt 被 memory hard limit 终止且策略未显式允许
- **THEN** Attempt 为 resource_exceeded，Execution 进入 dead_letter 并保留稳定资源错误码

### Requirement: 业务 Retry 只由 PostgreSQL generation 驱动
可重试 Attempt terminal SHALL 原子使 Execution 进入 `retry_wait` 并写入 `next_attempt_at`；到期 Dispatcher MUST 通过数据库时间把 Execution 变为 queued、令 dispatch generation 单调加一并创建新 Outbox。RabbitMQ message defer/requeue MUST 不增加业务 attempt count，也不得替代该状态机。

#### Scenario: Retry Dispatcher 重复运行
- **WHEN** 多个 Dispatcher 并发处理同一到期 retry_wait Execution
- **THEN** 最多一个 generation 加一并创建唯一 Outbox，其他观察新状态后跳过

#### Scenario: Retry 尚未到期的消息
- **WHEN** stale/duplicate message 在 next_attempt_at 前到达
- **THEN** Control 不提前创建 Attempt，返回 ACK_NOOP 或有界 DEFER，并保持业务 attempt_count

### Requirement: 已接受 Execution 只能进入可解释终态
RabbitMQ Execution MUST 最终进入 `succeeded/cancelled/expired/dead_letter` 之一；系统不得因 queue age、Broker DLQ、Retry 耗尽、Worker lost 或 GC 静默删除/遗忘。`expired` 只允许由显式 queue-age 或 Schedule catch-up 策略产生并保存稳定原因。

#### Scenario: Retry 次数耗尽
- **WHEN** 最后一次允许的 Attempt 以失败终止
- **THEN** Execution 原子进入 dead_letter、释放 Business Admission 并保留所有 Attempt/error/backoff 历史

#### Scenario: Infrastructure DLQ 有消息
- **WHEN** dispatch 进入 Broker Infrastructure DLQ 但 DB Execution 仍非终态
- **THEN** Reconciler 形成 DB Incident 并重派或升级人工处置，不能靠 retention 删除该 Execution

### Requirement: Business Dead Letter 不改写 Attempt 历史
Execution 进入 dead_letter SHALL 保存最终稳定错误、attempt count、policy snapshot 与最后 generation；既有 Attempt、日志摘要、resource usage 和 cleanup 状态 MUST 保持不可变。Dead Letter 不计 Business Outstanding，但继续计入历史 retention 与适用 Artifact Hold。

#### Scenario: 查看死信详情
- **WHEN** 用户打开 dead_letter Execution
- **THEN** API/Web 显示最终原因、Attempt 时间线、是否可 Replay 与输入可用性，不把它展示为 legacy failed

### Requirement: Replay 创建新 Execution
Replay SHALL 通过新的 Admission 事务创建新 Execution、id、snapshot、retry policy 与 Outbox，并写入 `replay_of_execution_id`；旧 dead_letter MUST 不改回 queued，也不得复用旧 active Attempt、generation 或 Admission 占用。

#### Scenario: Replay 成功
- **WHEN** 原输入仍可用、目标配置合法且 Admission 有容量
- **THEN** 系统返回新 `execution_id`，旧 dead_letter 保持不可变并可追溯到新 Execution

#### Scenario: Replay 时容量满
- **WHEN** Adapter 或 Global Admission 达到保护线
- **THEN** Replay 返回相同 429/503 背压合同，不改变旧 dead_letter 或 Artifact Hold

#### Scenario: 对非 dead-letter 请求 Replay
- **WHEN** 客户端对 queued/running/succeeded/cancelled Execution 调用 Replay
- **THEN** 系统返回稳定 transition error 且不创建新 Execution

### Requirement: Managed File Dead Letter 使用有界 Artifact Hold
Managed-files Execution 进入 dead_letter SHALL 创建默认 7 天的 Artifact Hold，阻止原快照 Blob 在 Replay 窗口内被 GC。Hold MUST 只增加引用/治理事实而不重复计算 Blob 物理字节，并 SHALL 单独观测 held count/bytes。既有已接受 Execution 必须能够形成 Hold；达到保护线后 MUST 拒绝新的 managed-files Execution，而不是让 dead-letter 终态提交失败。

#### Scenario: 当前 Binding 已替换后任务死信
- **WHEN** queued managed-files Execution 的原 Artifact 已不在当前 Binding，随后进入 dead_letter
- **THEN** Dead Letter Hold 继续保护原 Blob 至 hold expiry，允许按原输入 Replay

#### Scenario: Hold 保护线已超
- **WHEN** dead-letter held bytes 已达到部署保护线
- **THEN** 新 managed-files ingress 返回稳定容量错误，既有 Attempt 仍可进入 dead_letter 并形成 Hold

#### Scenario: Hold 到期后 Replay
- **WHEN** Hold 已到期且原 Blob 已被 GC
- **THEN** 旧 dead-letter 审计仍可读取，但 Replay 返回 `dead_letter_input_expired`，不得使用当前 Adapter 文件冒充原输入

### Requirement: Dead Letter purge 有权限与审计边界
提前 purge Dead Letter Hold SHALL 需要明确管理员权限与目标 Execution，记录主体、原因、held bytes 与结果；purge MUST 不删除 Execution/Attempt 审计，也不得用宽泛路径或年龄选择非目标 Blob。

#### Scenario: 普通编辑者提前 purge
- **WHEN** 非管理员尝试提前释放仍在 Replay 窗口内的 Hold
- **THEN** 系统拒绝且 Hold/Blob/Execution 不变
