## Purpose

定义 RabbitMQ dispatch 从 Control Claim 转换为唯一 ExecutionAttempt 的状态、Adapter Slot、Lease/Fencing、ACK 与恢复合同，使重复消息、Worker 崩溃、网络分区和迟到报告不会产生两个权威 active Attempt 或覆盖新结果。

## ADDED Requirements

### Requirement: Attempt 与逻辑 Execution 分离
每次 Worker 实际运行 SHALL 创建一个递增 `attempt_no` 的 ExecutionAttempt；Attempt MUST 使用 `claimed/running` active 状态和 `succeeded/failed/timed_out/cancelled/worker_lost/resource_exceeded` terminal 状态。Attempt failure 不得直接等同于 Execution 最终失败，Control MUST 依据快照 Retry Policy 决定 `retry_wait` 或 `dead_letter`。

#### Scenario: 第二次执行成功
- **WHEN** Attempt 1 以可重试错误终止且 Attempt 2 成功
- **THEN** Execution 最终为 succeeded，并保留两个 Attempt 的独立审计事实

#### Scenario: Attempt 终态重复上报
- **WHEN** 同一拥有者重复提交完全相同的 Attempt terminal result
- **THEN** Control 幂等返回权威状态，不重复释放 Slot、Admission 或创建下一 generation

### Requirement: Control Claim 原子创建 Attempt 与 Adapter Slot
RabbitMQ v3 Claim SHALL 在一个 PostgreSQL 事务中锁定 Execution 与目标 Adapter 的 `slot_no=0`，校验 backend、generation、target Worker、due time、协议/capability和状态，创建 `claimed` Attempt、签发 Lease/Fencing、绑定 Slot 并把 Execution 从 queued 转为 running。任何校验失败 MUST 不产生部分 Attempt 或 Slot 占用。

#### Scenario: 两个 message 并发 Claim 同一 Adapter
- **WHEN** 两个 Worker consumer delivery 同时为同一 Adapter 的不同 queued Execution 发起 Claim
- **THEN** 最多一个绑定 Slot 0 并得到 EXECUTE，另一个得到 DEFER 或权威 NOOP

#### Scenario: 同一 Execution 重复消息
- **WHEN** 相同 execution/generation 的重复 dispatch 并发 Claim
- **THEN** 数据库约束保证至多一个 active Attempt，另一请求不得启动第二个进程

#### Scenario: Stale generation
- **WHEN** message generation 小于 Execution 当前 generation
- **THEN** Control 返回 ACK_NOOP，不创建 Attempt、不回退 Execution generation

### Requirement: Adapter Slot 是数据库级并发权威
#130 SHALL 为每个 Adapter 提供且只使用 `slot_no=0`，一个 Slot 同时至多绑定一个 active Attempt；Claim、terminal 与 lease recovery MUST 通过同一受锁 Slot/fence 合同更新。该合同 MUST 允许 #129 未来通过增加 Slot 扩展并发，而无需改变 Execution 历史语义。

#### Scenario: 旧 Attempt 租约过期后重试
- **WHEN** Slot 仍引用已过期 Attempt
- **THEN** Recovery 先以 fence 条件终结旧 Attempt并释放 Slot，之后新的 Claim 才可绑定更高 fencing token

#### Scenario: 迟到旧 Attempt 释放 Slot
- **WHEN** 旧 Attempt 在新 Attempt 已绑定 Slot 后提交 terminal/cleanup
- **THEN** 条件更新拒绝旧 fence，Slot 与新 Attempt 保持不变

### Requirement: Claim decision 是封闭合同
Control SHALL 只返回 `EXECUTE`、`ACK_NOOP`、`DEFER`、`REJECT_DLQ` 或 `PAUSE_CONSUMER` 五类 decision，并为每类返回稳定 reason。Worker MUST 按 decision 执行固定 Broker disposition，不得从 HTTP 状态或 message 文本猜测。

#### Scenario: Execution 已取消
- **WHEN** 旧 dispatch 到达但 Execution 已 cancelled
- **THEN** Control 返回 `ACK_NOOP/cancelled`，Worker ACK 且不启动 Adapter

#### Scenario: Adapter Slot 暂忙
- **WHEN** message 合法但 Slot 0 被另一个未过期 Attempt 占用
- **THEN** Control 返回 `DEFER/adapter_slot_busy`，不增加 attempt_count

#### Scenario: Message 永久损坏
- **WHEN** execution ID、target/routing 或 schema 无法与权威事实一致解释
- **THEN** Control 返回 `REJECT_DLQ`，Worker reject `requeue=false` 并形成 Infrastructure Incident

#### Scenario: Control 系统性不可用
- **WHEN** Worker 无法认证或连续遇到 Control 服务级故障
- **THEN** Worker 暂停/关闭 Consumer 并有界退避重连，而不是逐条 message 热重投

### Requirement: ACK 在 durable Claim 与私有 journal 后发生
正常路径 MUST 为 `deliver → Control Claim commit → Worker 私有 Attempt journal 原子落盘 → ACK → sandbox execute`。ACK MUST 不等待 Attempt/Execution terminal。私有 journal 必须在 Adapter 副作用前保存恢复所需 execution/attempt、受控 workspace、Claim/Cleanup 凭据与 fence，且不得进入普通日志。

#### Scenario: Worker 在 ACK 后立即崩溃
- **WHEN** Claim 与 journal 已持久、message 已 ACK，但 Adapter 尚未启动时 Worker 崩溃
- **THEN** Attempt lease 到期后由 Control 收敛为 worker_lost 并按 Retry Policy 重派，不依赖原 message 重投

#### Scenario: ACK 响应丢失
- **WHEN** Worker 发送 ACK 后 channel 丢失且 Broker 重新投递同 generation
- **THEN** duplicate Claim 返回 ACK_NOOP 或 DEFER，不创建第二 active Attempt

#### Scenario: Journal 写入失败
- **WHEN** Control Claim 已提交但 Worker 无法安全持久化 journal
- **THEN** Worker 不启动 Adapter，优先报告 `attempt_prepare_failed` 后 ACK；无法报告时关闭 channel并等待 Lease Recovery，不把已 Claim 伪装成未 Claim

### Requirement: Lease 使用数据库时间并持续续租
Attempt Lease SHALL 默认 60 秒并由拥有 Worker 至少每 15 秒通过 heartbeat/progress 续租；Control MUST 使用数据库时间、Attempt ID、Claim Token 与 fencing token 条件更新。配置 MUST 保证 renew interval 明显小于 lease，非法组合阻止服务启动或 Worker capability healthy。

#### Scenario: 正常长任务续租
- **WHEN** Adapter 运行时间超过多个 lease 周期且 Worker/Control 连通
- **THEN** Attempt 保持 running，message 已 ACK，lease 每次只由当前 fence 延长

#### Scenario: Worker 与 Control 分区
- **WHEN** Worker 无法在 lease 到期前成功续租
- **THEN** Worker 必须 fail closed 终止/停止接受当前 Attempt 结果，Control Recovery 形成唯一权威终态

### Requirement: Lease Recovery 与迟到报告可收敛
Control SHALL 扫描 lease 已过期的 `claimed/running` Attempt，锁定 Attempt、Execution 与 Slot，以 fence 条件把 Attempt 置为 `worker_lost`，释放 Slot，并使 Execution 进入 retry_wait 或 dead_letter。旧 fence 的 progress/result MUST 被拒绝，且不得恢复 Admission 或覆盖 output。

#### Scenario: Recovery 与 Result 竞争
- **WHEN** Worker Result 与 Lease Reconciler 并发处理同一 Attempt
- **THEN** 取得受锁权威的一方形成唯一 terminal，另一方幂等观察或收到 stale-fence，不产生双重 Retry

#### Scenario: Claimed 但从未 started
- **WHEN** Attempt 在 claimed 状态 lease 过期
- **THEN** 它以 worker_lost/prepare-or-start-lost 收敛，Slot 释放且 Execution 依据 Retry Policy 继续

### Requirement: 取消跨 queued、retry_wait 与 active Attempt 一致
取消 queued/retry_wait Execution SHALL 原子进入 cancelled、使既有 generation 后续 Claim 为 ACK_NOOP、释放 Admission 与文件责任；取消 active Attempt SHALL 记录 cancel request，由 Worker 终止 sandbox，最终 Result/Recovery 通过同一 fence 收敛。终态 Execution 不得被再次取消改写。

#### Scenario: 取消尚未消费的 queued Execution
- **WHEN** 用户取消 queued Execution 且 dispatch 已在 Broker ready
- **THEN** DB 先进入 cancelled，稍后 delivery 得到 ACK_NOOP，不启动 Adapter

#### Scenario: 取消与成功竞争
- **WHEN** cancel 与当前 Attempt succeeded Result 并发
- **THEN** 行锁与 fence 只允许一个合法权威终态，Admission/Slot 各释放一次

### Requirement: Worker 不直接访问 PostgreSQL
Worker v3 SHALL 仅主动连接 RabbitMQ 与 Control HTTP/HTTPS，所有 Claim、Attempt、Lease、Result 与 cleanup DB 变更 MUST 由 Control 执行；Worker 不得持 PostgreSQL Credential 或开放入站执行端口。

#### Scenario: Worker 配置审计
- **WHEN** 检查 Worker 环境、网络与 Adapter 子进程
- **THEN** Worker/Adapter 不存在 PostgreSQL Credential，且没有 Control 到 Worker 的反向执行连接
