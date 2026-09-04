## MODIFIED Requirements

### Requirement: Worker v1/v2 滚动升级独立于文件 feature flag
Control SHALL 在 additive migration 后显式支持 protocol v1、v2 与 v3，并以 `DLR_MIN_WORKER_PROTOCOL_VERSION=1` 或当前兼容值部署；v1 只领取 legacy none/json，v2 可按 #127 合同领取 legacy none/json/managed_files，v3 才可消费 RabbitMQ dispatch并创建 Attempt。RabbitMQ ingress gate 默认关闭，且只有目标 Worker 同时为 v3、通过完整 isolation preflight并排空/处理 legacy active Execution 后，才能执行最终 Cutover。

#### Scenario: 混合 Worker 池
- **WHEN** v1/v2/v3 Worker 共存且 RabbitMQ ingress gate 关闭
- **THEN** v1/v2 只按原 capability 领取 legacy Execution，v3 canary 只处理明确 rabbitmq backend，任何 Worker 都不会跨 backend silent execute

#### Scenario: 提高最低协议
- **WHEN** #127 原兼容窗口满足连续 48 小时无 v1 claim且所有 v1 active Execution 已终态
- **THEN** 可在 RabbitMQ Cutover 前把最低版本提升到 2，旧 v1 claim 返回 `worker_protocol_incompatible`

#### Scenario: v3 Dark Launch
- **WHEN** v3 Consumer/Claim/Attempt 功能通过 canary但 Resource Sandbox Gate 尚未通过
- **THEN** minimum protocol 不得提高到 3，生产新流量保持 legacy，v1/v2 兼容边界不被破坏

#### Scenario: 提高最低协议到 3
- **WHEN** 所有继续服务 Worker 均为 v3且 isolation capability 全绿、legacy pending/running 已 drain或迁移、新流量已切 RabbitMQ并验证 Slot 防线
- **THEN** 才可设置 minimum=3；之后 v1/v2 注册/claim 明确拒绝且不 silent fallback

## ADDED Requirements

### Requirement: Issue 130 使用串行三 Batch 与单一远端 PR
Issue #130 SHALL 在一个 change/branch 中按 Batch 1 additive queue、Batch 2 v3 dark launch、Batch 3 resource isolation + final cutover 顺序实施；每批 Candidate SHA MUST 先通过相关自动/故障 Gate 与 Sol exact-SHA 只读审计。LOCAL_FAST checkpoint MUST 不创建远端 PR或标记 AO 官方 Review；全部本地 Gate 通过后只创建一个非 Draft REMOTE_RELEASE PR。

#### Scenario: Batch 2 尝试提前切流
- **WHEN** Worker v3 功能完成但 Sandbox Gate 未通过
- **THEN** Batch Gate 失败，不提高 minimum、不 drop 旧索引、不关闭 legacy Claim，也不创建最终 PR

#### Scenario: 最终 PR head 改变
- **WHEN** Hosted CI、AO Review、rebase 或修复改变最终 PR head SHA/tree
- **THEN** 旧证据只作为历史，新 head 重跑受影响 Gate、Hosted CI 与 AO 官方 exact-head Review

### Requirement: 发布状态标签必须真实区分
系统与交付记录 SHALL 区分 implementation、local checkpoint、exact-SHA audit、PR opened、Hosted CI、AO official Review、merge、release 与 user acceptance；最终机器 Gate 全绿只允许标记 `READY_FOR_USER_ACCEPTANCE`。

#### Scenario: AO Review 和 Hosted CI 通过
- **WHEN** 唯一 PR 的同一 head SHA 获得两个 PASS
- **THEN** 不自动勾选 merge/release/user PASS，用户仍独立完成最终业务与视觉验收
