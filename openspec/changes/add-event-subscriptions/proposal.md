# Proposal

## Why

[Issue #219](https://github.com/john-ops-lab/DataLinkRuntime/issues/219) 需要把 Kafka、Kubernetes 等长期接收来源纳入 DLR，同时保持 Adapter 按次执行、业务逻辑由代码负责的产品边界。先固定接收责任、恢复、版本、顺序与容量合同，才能避免把“连接正常”或“已入队”误当作目标处理成功。

本稿已获用户批准，**按 B0 → B5 实施中**。源码基线为 2026-10-23 核对的 `origin/main`：`eedda36f9ed1e3ab4f84b0a76532d3916f8ca6d5`。2026-10-23 用户授权每批验证与审查通过后继续，最终统一提交一个 PR 审查，不自动合并；`dlr-preview` 改作开发测试环境。任务与证据以实际完成记录为准。

## What Changes

- 新增独立 EventSubscription；首期一条订阅绑定一个普通 Task/manual Adapter，一个 Adapter 至多绑定一条未删除订阅。订阅启用或排空期间禁止其他触发入口混入。
- 监听器位于 Control 的受限后台服务，独立于普通 Execution/Attempt；PostgreSQL 租约与 generation 防止重复接管，网络调用不占用数据库事务。
- 统一使用有界 Inbox 保存接收责任，再复用 Admission、Execution、Attempt、Outbox 与 Worker；事务提交前不确认外部来源，不让 Inbox 绕过执行容量。
- 固定 `dlr.event.v1` Envelope、稳定源身份、内容哈希、配置版本、原始内容和人工重处理关联；接收重投与显式重处理分别定义。
- 固定停用只停止新接收、已接收责任继续排空；普通配置/代码切换必须经过停用和排空。失败屏障提供显式受限的修复 Revision 流程，不改旧责任快照。已创建 Execution 的输入、Revision 与运行目标不可变。
- 先交付 Kafka：逐条消息、分区内顺序屏障、失败阻塞、显式补处理/跳过，以及 offset/rebalance 恢复。
- 后交付 Kubernetes：默认保留逐条已接收变化，显式选择 `state_sync` 才可合并；LIST/WATCH、410 重同步、UID 和批次完成屏障共同防止错误对账。两种模式都不承诺恢复已过期历史。
- 新增来源到 Execution 的追踪、独立接收/处理状态、容量与留存界限；中英文、现有权限与凭据脱敏贯穿管理和历史页面。

非目标：HA/Worker 池（#129）、DAG、完整流计算、任意常驻用户代码、通用连接器市场、平台内置 CMDB/Sink、全局业务 exactly-once；AMQP、MQTT、文件/对象存储、CDC 为后续独立变更。Kafka 与 Kubernetes 分别验收，本轮统一在一个 PR 中提交审查；发布另行授权，不预定版本或日期。

## Capabilities

### New Capabilities

- `event-subscription-management`：绑定、启停、配置版本、监听所有权、管理权限与状态追踪。
- `event-delivery`：有界接收、去重、调度事务、重试/重处理、顺序屏障、容量和留存。
- `kafka-event-source`：Kafka 消息身份、消费组、分区位置、恢复与确认。
- `kubernetes-event-source`：LIST/WATCH、全量批次、历史失效恢复、对象身份和显式状态合并。

### Modified Capabilities

- `adapter-input-config`：增加订阅输入与已保存 Task 输入隔离、订阅运行锁和互斥触发规则；现有输入枚举与普通手动/定时行为不变。
- `execution-input-snapshot`：增加事件触发输入快照与追踪合同；沿用 JSON 快照形状和现有 Worker 输入协议，不把事件定义为 managed file。

## Impact

- Control：新增订阅/Inbox/来源进度/所有权/批次与处置记录；扩展 `trigger=subscription` 的数据库约束、API 筛选与展示；扩展中心运行锁、Admission 接入、保留策略和凭据引用检查。
- Web：Adapter 运行设置中的“事件订阅”配置、启停和追踪；全局订阅列表用于运维定位。未完成来源不可启用。
- 依赖/部署：复用现有 Control、PostgreSQL、RabbitMQ 和 Worker；Kafka/Kubernetes 客户端只用于平台连接器。候选库及精确版本、来源支持版本、预算验证在 B0 固定，未通过前不开始连接器实现；不新增默认必需服务。
- 兼容：不增加 Adapter 类型或修改普通 Task InputConfig 枚举；事件入口走受信任内部接收方法，不开放任意 per-run 输入覆盖。已有手动、定时、Webhook 数据不迁移为订阅。
- 迁移：向前增加表、索引与 `subscription` trigger 合同；功能默认关闭，现有对象无订阅。退回旧应用前必须关闭新接收并处理/保全未完责任，不能靠数据库降级或恢复旧备份抹去已确认消息；具体顺序见 [design.md](design.md)。
- 审查入口：[design.md](design.md) 固定规则与取舍，[tasks.md](tasks.md) 划分 B0–B5，[validation.md](validation.md) 定义证据与停止条件。上述均为目标设计，不代表已支持。
