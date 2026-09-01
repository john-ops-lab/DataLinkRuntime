## Purpose

定义 RabbitMQ 4.3.5 固定 Worker 派发拓扑、Transactional Outbox、Publisher Confirm、队列边界、延迟重投与 Infrastructure DLQ 合同，使 Broker 故障、Confirm 歧义和重复投递都不会让 PostgreSQL 业务事实静默丢失或产生并行业务 Attempt。

## ADDED Requirements

### Requirement: RabbitMQ 版本与镜像不可浮动
部署 SHALL 以 RabbitMQ 4.3.5 行为为基线，Compose MUST 使用精确版本并在发布证据中记录不可变 image digest；禁止使用 `latest`。单节点单成员 Quorum Queue MUST 明确标注只满足功能语义而不提供 Broker HA。

#### Scenario: Compose 镜像审计
- **WHEN** 运行发布 Gate
- **THEN** 证据包含实际 RabbitMQ version、image digest、启用 feature flags 与单节点非 HA 声明

#### Scenario: 运行版本与配置不符
- **WHEN** Control/bootstrap 发现 Broker 版本或必需 feature flag 不符合冻结基线
- **THEN** RabbitMQ ingress gate 保持关闭且 health 返回稳定配置错误

### Requirement: 固定 Worker 使用 durable bounded topology
系统 SHALL 使用 durable direct dispatch exchange、每个固定 Worker 一条 durable non-auto-delete Quorum Queue、独立 infrastructure DLX 与 durable Quorum DLQ。Queue MUST 配置 `reject-publish`、显式 length/bytes、有限 delivery limit、at-least-once dead lettering 与 consumer timeout；禁止 `drop-head`。

#### Scenario: Worker 暂时没有 Consumer
- **WHEN** 固定 Worker 离线但其 Queue 已 bootstrap
- **THEN** 已确认的 persistent dispatch 保留在 durable Queue，不自动删除或 reroute

#### Scenario: Queue 达到硬上限
- **WHEN** publish 会超过 length 或 bytes 上限
- **THEN** Broker 拒绝 publish，Relay 保持 Outbox pending 并触发 backpressure/告警，不丢弃旧消息

#### Scenario: Topology 漂移
- **WHEN** 已存在 Queue 的类型、overflow、DLX、delivery limit 或 bounds 与冻结配置不同
- **THEN** bootstrap 不静默覆盖不兼容事实，health Gate 失败并输出非敏感差异

### Requirement: Dispatch Message 只携带最小非敏感事实
Dispatch Message SHALL 只携带 schema version、message ID、execution ID、dispatch generation、adapter ID、language、resource class 与 target worker ID；MUST 使用 persistent delivery，并不得包含 Adapter Code、Secret、完整 Runtime Config、大 Input、文件内容、Artifact storage key、宿主路径或 Claim/Cleanup Token。

#### Scenario: Message schema 安全审计
- **WHEN** 测试捕获已发布 message body 与 headers
- **THEN** 只存在允许字段，且 Token、Credential、用户内容和内部路径扫描均为空

#### Scenario: 未知 schema version
- **WHEN** Worker 收到不支持的 dispatch schema
- **THEN** 它不得调用 Adapter 或猜测字段，而是按 `REJECT_DLQ` 形成可关联 Infrastructure Incident

### Requirement: Outbox 与 Execution generation 一一对应
系统 SHALL 为每个需要派发的 `(execution_id, dispatch_generation)` 创建至多一条 Outbox，且每条 Outbox 使用唯一 message ID、冻结 routing key/body/bytes、`pending|published` 状态、available time、publish lease、attempt count、last error 与 published time。Retry MUST 通过 generation 加一和新 Outbox 表达，不得改写旧 published generation。

#### Scenario: Ingress 事务重复执行
- **WHEN** 同一 Execution generation 的事务逻辑因并发或重试再次创建 Outbox
- **THEN** 数据库唯一约束阻止重复行并使调用方得到原责任事实

#### Scenario: Retry 产生下一代
- **WHEN** retry_wait 到期且 Dispatcher 成功重派
- **THEN** Execution generation 单调加一并创建新的 pending Outbox，旧 generation 保持审计不可变

### Requirement: Relay 不持数据库行锁等待 Broker
Relay SHALL 在短事务中以数据库时间领取 due pending 或过期 publish lease，提交后在事务外使用 `mandatory` 与 Publisher Confirm 发布，再在短事务中标记 published 或释放 lease/记录 bounded backoff。任何网络等待 MUST 有时限，且 PostgreSQL row lock 不得跨 publish/confirm 持有。

#### Scenario: Broker Confirm 正常成功
- **WHEN** mandatory publish 未 return 且收到 confirm ack
- **THEN** Relay 仅在仍拥有 publish lease 时把 Outbox 标记 published 并记录 published_at

#### Scenario: Unroutable mandatory return
- **WHEN** Broker return 消息或 confirm nack
- **THEN** Outbox 保持 pending、记录稳定错误并按有界退避重试，Execution 不被误标终态

#### Scenario: Relay 在 Confirm 后崩溃
- **WHEN** Broker 已持久确认但 Relay 在 DB published mark 前崩溃
- **THEN** lease 到期后允许重复 publish，Consumer/Claim 以 generation 和 active Attempt 吸收重复

#### Scenario: Relay 卡在网络等待
- **WHEN** Broker publish/confirm 超过配置时限
- **THEN** Relay 释放资源并让 lease 后续可重领，不长期占用数据库连接或行锁

### Requirement: Consumer timeout 只覆盖 Claim/ACK 握手
Worker Queue consumer timeout SHALL 默认为 300000 ms，并 MUST 大于 delivery、Control Claim、Worker 私有 journal 与 ACK 的最大合法总预算；业务 Execution 运行时间不得计入 Unacked 窗口，Consumer timeout 不需要大于最长 Execution timeout。

#### Scenario: 24 小时 Execution
- **WHEN** 合法 Execution 的运行 timeout 远大于 5 分钟
- **THEN** Worker 仍在 durable Claim/journal 后立即 ACK，长任务不因 Broker consumer timeout 被重投

### Requirement: Claim 前 defer 使用有界延迟而非热循环
Adapter slot 暂不可用、任务尚未到 due time或可恢复 Claim 冲突 SHALL 使用 RabbitMQ 4.3 delayed retry 与 bounded jitter；该 defer MUST 不增加业务 attempt count，禁止无限即时 `nack(requeue=true)`。

#### Scenario: 同 Adapter 多条消息同时送达
- **WHEN** 一个 Attempt 已占用 Adapter Slot，后续 generation 合法但暂不可 Claim
- **THEN** Worker/Control 返回 `DEFER`，消息延迟后重试且 Broker/CPU 不形成热循环

### Requirement: Infrastructure DLQ 与 Business Dead Letter 分离
消息 schema/routing 永久错误或 Broker delivery-limit SHALL 进入 Infrastructure DLQ；Adapter 最终执行失败 MUST 只通过 PostgreSQL `Execution.dead_letter` 表达。Infrastructure DLQ Reconciler SHALL 关联 Execution/generation，形成可见 Incident 并决定重派或人工处置，禁止只有 Broker 孤儿消息而 DB 永久非终态。

#### Scenario: Poison dispatch 到达 delivery limit
- **WHEN** 一条 message 因基础设施原因达到有限 delivery limit
- **THEN** at-least-once DLX 把它送入 Infrastructure DLQ，Reconciler 在 DB 记录可关联 Incident 与告警

#### Scenario: Adapter 业务错误耗尽
- **WHEN** Attempt 业务失败达到 max_attempts
- **THEN** Execution 进入 Business Dead Letter，原 dispatch 不依赖 Infrastructure DLQ 表达业务结果

### Requirement: Broker 凭据和管理面保持平台边界
RabbitMQ Credential SHALL 只提供给 Control Relay、bootstrap 与 Worker Agent，并 MUST 不注入 Adapter；默认 guest MUST 禁用，管理端口不得暴露到不受信网络。连接错误与审计不得记录密码、URI userinfo 或 TLS 私钥。

#### Scenario: Adapter 环境审计
- **WHEN** 三语言 Adapter 枚举其可见环境变量与挂载
- **THEN** 不存在 RabbitMQ Credential、管理端口凭据或 Broker TLS 私钥
