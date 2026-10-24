# Proposal

## Why

[Issue #219](https://github.com/john-ops-lab/DataLinkRuntime/issues/219) 需要把 Kafka、Kubernetes 等长期接收来源纳入 DLR，同时保持 Adapter 按次执行、业务逻辑由代码负责的产品边界。先固定接收责任、恢复、版本、顺序与容量合同，才能避免把“连接正常”或“已入队”误当作目标处理成功。

本稿已获用户批准，**已按 B0 → B5 完成实现、本地验证与独立审查材料**。源码基线为 2026-10-23 核对的 `origin/main`：`eedda36f9ed1e3ab4f84b0a76532d3916f8ca6d5`。2026-10-23 用户授权每批验证与审查通过后继续，最终统一提交一个 PR 审查，不自动合并；`dlr-preview` 改作开发测试环境。任务与证据以实际完成记录为准。

2026-10-24 用户批准适配器中心的界面及创建流程示意图，并授权先更新本变更，再按 B6 → B8 实现、验证和审查，更新现有 PR #221 与同一本地开发测试环境。取消独立事件订阅一级菜单；事件触发作为适配器运行方式呈现，配置、记录与异常处置均回到适配器内。该修订不改变 B0–B5 的接收、版本、顺序、处置和容量合同。

## What Changes

2026-10-24 后续授权：在本变更内增加 **RabbitMQ、RocketMQ** 两种订阅来源，先固定公共扩展规则，再按 B9–B13 实现、针对性验证和审查，更新现有私有开发测试环境。本阶段仅本地分批提交，不推送、不新建或更新 PR，不自动合并；原 B0–B8 的验证结论只适用于当时版本。本轮 B9–B13 的实施、针对性验证、自审与开发交付已完成，见 [B13 审查记录](b13-queue-delivery-review.md)。

- 来源注册集中声明配置、稳定身份、消费组/队列独占范围、事件内容校验、运行器、容量和恢复能力。新增来源继续使用原 Inbox、Execution、版本、权限及受限监听预算；未知来源默认拒绝。
- RabbitMQ 使用现有锁定 `pika`，接收已有专用队列，手动确认、一条未确认预取；要求生产方为每条逻辑消息设置稳定且唯一的 `message_id`。
- RocketMQ 首期使用 5.x gRPC Proxy / SimpleConsumer 与手动 ACK，只支持普通消息和专用消费组；不宣称兼容 4.x Remoting、FIFO、事务、Lite 或各云厂商专有协议。客户端安装、资源/关闭边界及真实 Broker 必须通过 B9/B11 门槛后才开放。
- 新队列来源按 DLR 实际持久接收顺序串行处理，不声称跨消费者、重投或重启的 Broker 全局顺序；来源确认不等于业务成功，来源确认后的失败仍由持久责任恢复。

- 新增独立 EventSubscription；首期一条订阅绑定一个普通 Task/manual Adapter，一个 Adapter 至多绑定一条未删除订阅。订阅启用或排空期间禁止其他触发入口混入。
- 监听器位于 Control 的受限后台服务，独立于普通 Execution/Attempt；PostgreSQL 租约与 generation 防止重复接管，网络调用不占用数据库事务。
- 统一使用有界 Inbox 保存接收责任，再复用 Admission、Execution、Attempt、Outbox 与 Worker；事务提交前不确认外部来源，不让 Inbox 绕过执行容量。
- 固定 `dlr.event.v1` Envelope、稳定源身份、内容哈希、配置版本、原始内容和人工重处理关联；接收重投与显式重处理分别定义。
- 固定停用只停止新接收、已接收责任继续排空；普通配置/代码切换必须经过停用和排空。失败屏障提供显式受限的修复 Revision 流程，不改旧责任快照。已创建 Execution 的输入、Revision 与运行目标不可变。
- 先交付 Kafka：逐条消息、分区内顺序屏障、失败阻塞、显式补处理/跳过，以及 offset/rebalance 恢复。
- 后交付 Kubernetes：默认保留逐条已接收变化，显式选择 `state_sync` 才可合并；LIST/WATCH、410 重同步、UID 和批次完成屏障共同防止错误对账。正常路径要求 completed 业务成功；另提供用户已批准的专用“放弃完成确认”，在停用、原生关闭和无非终态执行后明确结清业务结果未确认的完成责任，不伪造成功或同批失败。两种模式都不承诺恢复已过期历史。
- 新增来源到 Execution 的追踪、独立接收/处理状态、容量与留存界限；中英文、现有权限与凭据脱敏贯穿管理和历史页面。放弃完成确认绑定批次、完成 Receipt、最新 Delivery 及其执行关联快照，采用已有预算内的单份有界审计；旧失败终态、历史和冻结事实保留，旧 completed 永不再获新执行许可。

2026-10-23 用户明确选择专用“放弃完成确认”，替代无法保证满额退出的纯成功屏障；这是 Kubernetes 正常完成流程之外的唯一人工结账例外，目标可能已部分执行且无法由该动作回滚。仅解除完成屏障，其他失败仍须修复/处置，旧 WATCH 按原序排空；不自动启用或新建 LIST，旧责任结清后再显式启用新批次。用户另已明确选择：已成功确认且闭合的 completed 拒绝新 replay，原请求仅只读回原结果，重新全量对账使用新完整批次；普通对象/WATCH 的历史补处理不因此受限。B0–B5 的实现、本地验证与独立审查材料已完成；最终精确 HEAD CI、实际部署和 Worker 烟测作为统一 PR 的独立退出门禁，实施及交付状态以 tasks、各批审查记录和 PR 检查项为准。

非目标：HA/Worker 池（#129）、DAG、完整流计算、任意常驻用户代码、通用连接器市场、平台内置 CMDB/Sink、全局业务 exactly-once、带积压修改限额的新增 API 或恢复信用方案；除 RabbitMQ AMQP 0-9-1 外的其他 AMQP 产品、MQTT、Redis、办公平台、轮询采集、文件/对象存储及 CDC 均不进入 B9–B13。B0–B8 的 PR #221 保持原状，B9–B13 暂不对外提交；发布另行授权，不预定版本或日期。

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
- Web：沿用“适配器、模板广场”顶部导航及“编辑、运行设置、执行记录、实时日志”四页签；在运行设置选择事件触发、配置来源并启停，在执行记录中查看接收记录和处置异常，原适配器列表增加运行方式与需要处理筛选。旧订阅详情链接归入所属适配器。未完成来源不可启用。
- 依赖/部署：复用现有 Control、PostgreSQL、RabbitMQ 和 Worker；Kafka/Kubernetes 客户端只用于平台连接器。候选库及精确版本、来源支持版本、预算验证在 B0 固定，未通过前不开始连接器实现；不新增默认必需服务。
- 兼容：不增加 Adapter 类型或修改普通 Task InputConfig 枚举；事件入口走受信任内部接收方法，不开放任意 per-run 输入覆盖。已有手动、定时、Webhook 数据不迁移为订阅。
- 迁移：向前增加表、索引与 `subscription` trigger 合同；功能默认关闭，现有对象无订阅。退回旧应用前必须关闭新接收并处理/保全未完责任，不能靠数据库降级或恢复旧备份抹去已确认消息；具体顺序见 [design.md](design.md)。
- 审查入口：[design.md](design.md) 固定规则与取舍，[tasks.md](tasks.md) 划分 B0–B8，[validation.md](validation.md) 定义证据与停止条件。勾选与证据区分历史 B0–B5 和新增 B6–B8，不代表界面修订已交付。
