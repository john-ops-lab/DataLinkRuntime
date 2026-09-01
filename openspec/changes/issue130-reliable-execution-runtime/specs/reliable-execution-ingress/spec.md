## Purpose

定义 Manual、API、MCP、Schedule 与 Webhook 在固定 Worker 单节点部署中的可靠接收、原子容量准入、请求幂等和逻辑 Execution 状态合同，使 202、背压与最终责任具有可验证且不依赖 RabbitMQ 瞬时可用性的统一含义。

## ADDED Requirements

### Requirement: 202 只在 DLR 已持久承担责任后返回
Manual、API、MCP 与 Webhook ingress SHALL 仅在同一 PostgreSQL 事务提交不可变 Execution、适用的 Idempotency 记录、Adapter/Global Admission 占用和第一条 Transactional Outbox 后返回 `202 Accepted`；Schedule 成功创建也 MUST 使用同一事务边界。Ingress MUST 不在提交前直接发布 RabbitMQ，也不得先返回 202 再补写责任事实。

#### Scenario: RabbitMQ 短时不可用但容量安全
- **WHEN** PostgreSQL 可提交、业务容量和 Outbox backlog 未触发保护线，但 RabbitMQ 暂时不可用
- **THEN** 系统仍提交完整责任事实并返回 `202 + execution_id`，由 Outbox Relay 在 Broker 恢复后发布

#### Scenario: 事务提交失败
- **WHEN** Execution、Idempotency、Admission 或 Outbox 任一写入导致事务回滚
- **THEN** 系统不得返回 202，也不得遗留部分占用、孤儿 Outbox 或 RabbitMQ Message

#### Scenario: 响应在提交后丢失
- **WHEN** PostgreSQL 已提交但 202 响应在调用方收到前丢失
- **THEN** 原 Execution 保持已接受，调用方可用同一 Idempotency-Key 安全重试

### Requirement: Business Admission 原子且有界
系统 SHALL 对 `queued/running/retry_wait` RabbitMQ Execution 维护 Adapter 与 Global 两级 outstanding count/bytes；创建时 MUST 在受锁事务中检查并递增，Execution 首次进入任一终态时 MUST 条件递减一次。系统 MUST 提供从 Execution 权威状态重算并修正 counter 漂移的 Reconciler，且不得把同一 Execution 与 Outbox 重复计费。

#### Scenario: 两个请求竞争最后一个 Adapter 名额
- **WHEN** 两个并发 ingress 共同会超过 Adapter outstanding count 或 bytes 上限
- **THEN** 最多一个事务成功，另一个返回 `429 adapter_queue_full` 与 `Retry-After`

#### Scenario: Global 容量已满
- **WHEN** Adapter 仍有容量但 Global count 或 bytes 达到保护线
- **THEN** 请求返回 `503 runtime_capacity_full` 与 `Retry-After`，不创建 Execution 或 Outbox

#### Scenario: 终态上报重复
- **WHEN** 同一个 Execution 的终态上报、取消或 Reconciler 并发重试
- **THEN** Admission 占用只释放一次，counter 不得变为负数

#### Scenario: Counter 漂移
- **WHEN** 审计发现 counter 与非终态 Execution 权威集合不一致
- **THEN** Reconciler 记录漂移量并以幂等受锁更新恢复一致，不删除或改变业务 Execution

### Requirement: 逻辑输入字节使用统一算法
Admission `logical_input_bytes` SHALL 对 `none` 取 0、对 JSON/Webhook 取规范化 JSON 的 UTF-8 字节数、对 `managed_files` 取不可变快照内 Artifact `size_bytes` 之和；计算 MUST 在 Execution 创建前完成并固化，后续配置或 Artifact 治理不得追溯改变该数值。

#### Scenario: Managed Files 集合排队后被当前配置替换
- **WHEN** 一个 managed-files Execution 已按文件快照计入 bytes，用户之后合法替换 Adapter 当前文件集合
- **THEN** 既有 Execution 的 logical bytes 与 Admission 占用保持不变，新 Execution 使用新集合计算

### Requirement: Idempotency-Key 具有封闭规范
Manual/API/MCP/Webhook SHALL 接受可选、大小写敏感、1 至 128 个可见 ASCII 字符的 `Idempotency-Key`；系统 MUST 不保存或记录 Key 原文，只保存 SHA-256。Payload hash MUST 由 trigger 与 RFC 8785/JCS 规范化 JSON 计算，无 body 使用显式 JSON `null`，唯一范围为 Adapter 与 Key hash。

#### Scenario: 同 Key 同 payload 重试
- **WHEN** 同一 Adapter 在去重窗口内收到相同 Key 与相同 payload hash
- **THEN** 系统返回原 `execution_id` 与原接收结果，不重复占用 Admission、不创建新 Outbox

#### Scenario: 同 Key 不同 payload
- **WHEN** 同一 Adapter 在去重窗口内收到相同 Key 但 payload hash 不同
- **THEN** 系统返回 `409 idempotency_conflict` 且不泄露原请求 body

#### Scenario: Key 格式非法
- **WHEN** Key 为空、超过 128 字符或含不可见/非 ASCII 字符
- **THEN** 系统返回稳定校验错误且不创建 Idempotency、Execution 或 Outbox

#### Scenario: Adapter 保存发生在重试之间
- **WHEN** 原请求已接受，之后 Adapter Version、InputConfig 或 Worker 绑定发生合法变化，再以同 Key/同 body 重试
- **THEN** 系统仍返回原 Execution，不用当前 Adapter 状态重新计算 payload conflict

### Requirement: 幂等记录具有最小保留窗口
Idempotency 记录 SHALL 默认至少保留 24 小时，并 MUST 不早于关联 Execution 进入终态被清理；Execution retention 与 Idempotency cleanup MUST 协调，不能在仍承诺返回原 Execution 的窗口内删除该 Execution。

#### Scenario: 非终态任务超过 24 小时
- **WHEN** Execution 在 24 小时去重窗口结束时仍为非终态
- **THEN** Idempotency 记录继续保留，直到该 Execution 终态后再按策略清理

### Requirement: RabbitMQ Execution 使用新状态并兼容历史状态
系统 SHALL 以 `dispatch_backend=legacy|rabbitmq` 显式区分派发合同。Legacy 行 MUST 继续支持 `pending/running/succeeded/failed/timeout/cancelled`；RabbitMQ 行 MUST 只使用 `queued/running/retry_wait/succeeded/dead_letter/cancelled/expired`。`failed/timeout` MUST 保留为 legacy 历史终态，不得作为新 RabbitMQ Execution 的最终状态。

#### Scenario: 读取历史 legacy timeout
- **WHEN** 客户端读取升级前 `dispatch_backend=legacy,status=timeout` 的 Execution
- **THEN** API 与 Web 继续按历史终态展示，不强制重写为 dead_letter

#### Scenario: RabbitMQ Attempt 可重试失败
- **WHEN** active Attempt 以可重试错误终止且仍有次数
- **THEN** Execution 进入 `retry_wait` 而不是 `failed`

#### Scenario: Retention 扫描混合状态
- **WHEN** retention 同时看到 legacy 与 rabbitmq Execution
- **THEN** 只选择各 backend 的终态，绝不删除 `pending/queued/running/retry_wait`

### Requirement: 固定目标 Worker 暂时离线不阻止可靠排队
Ingress SHALL 在目标 Worker 仍存在、绑定有效且声明兼容语言/协议的前提下允许其暂时 effective-offline 时创建 `queued` Execution；目标缺失、已删除、绑定为空或 capability 不兼容 MUST 在 Admission 前返回 `409 runtime_worker_invalid`。本 Issue MUST 不自动 reroute 到其他 Worker。

#### Scenario: 固定 Worker 心跳超时
- **WHEN** Adapter 的固定 Worker 配置有效但心跳暂时超时，且 Admission 可用
- **THEN** ingress 返回 202，Durable Worker Queue 保留派发，Worker 恢复后按原目标执行

#### Scenario: 固定 Worker 已删除
- **WHEN** Adapter 指向不存在或已删除的 Worker
- **THEN** ingress 返回 `409 runtime_worker_invalid`，不创建无人负责的 queued Execution

### Requirement: 默认容量值公开且配置非法时 fail fast
系统 SHALL 以每 Adapter 100 条/1 GiB、Global 1000 条/10 GiB、Outbox pending 2000 条/64 MiB/oldest 900 秒为默认保护值；部署 MAY 在有界范围内覆盖，但 Control MUST 在启动时验证 count/bytes/age 均为正且层级关系可满足，非法值阻止 ingress 服务健康。

#### Scenario: Outbox oldest age 超过保护线
- **WHEN** pending Outbox 最老记录超过 900 秒或部署值
- **THEN** 新外部 ingress 返回 `503 outbox_backlog_full + Retry-After`，既有责任记录继续由 Relay 恢复
