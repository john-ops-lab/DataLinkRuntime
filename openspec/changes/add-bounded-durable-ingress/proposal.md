# Proposal

## Why

DLR 已有可靠事件接收和批量启停，但尚无证据证明在固定单机资源下能接管并处理一万条独立消息的突发。[Issue #222](https://github.com/john-ops-lab/DataLinkRuntime/issues/222) 需要先测清接收、调度和真实 Worker 执行的成本，再建设独立的 HTTP 持久接收及必要优化，使接收成功、业务成功和故障恢复都可逐条核验。

本稿依据 2026-10-26 的 Issue 正文和 `main@1158e413e55d92127d615d666c5d4e4bcc15514f`。**状态：用户授权的 B0 真实链路测量与针对性验证已完成，结果见 [b0-results.md](b0-results.md)；B1–B5 产品实现和验证已获用户授权，当前按批次推进，实际完成状态以 tasks 和阶段证据为准；OCR 审查通过后停在提交 PR 之前。** 合同数值不是当前性能承诺，B0 与正式容量结果分别登记。

## What Changes

- 先完成 B0：已有 RabbitMQ 业务来源、一个 Python Adapter、200 条独立 2 KiB JSON、真实 Worker/沙箱、独立幂等目标。资源、输入、计时、观测、判据与停止条件固定在 [B0 测量计划](b0-measurement-plan.md)。
- 增加显式选择的 HTTP 持久接收来源，复用 Subscription/Activation、Receipt/Delivery 和统一执行链路；接收后返回 `receipt_id`，区别于创建执行或业务完成。原 Webhook `accepted/completed` 保持原合同。
- 增加部署级容量上限，保留入口/订阅配额、同事务记账、低水位恢复及历史/失败责任保护。接收资源与管理、恢复资源分别预算。
- 根据测量优化就绪选择、完成确认、Outbox 发布与重复准备成本；继续固定 Worker、每 Adapter 一个 active Attempt 和真实沙箱。一万条首轮接管超时后，按 D7.1/D7.2 仅为显式多条模式增加 HTTP/RabbitMQ 最多16条/1MiB的事务外收集及同事务接管复用，额外等待分别25ms/5ms（初始HTTP4条的首败保留）。D7.10已ready重包和D7.11实际机会支持D7.12的最小后继设计：有合法机会时先整体提前封collector为ready，再由原claim裁决，不直接借未封组项；逐条身份、提交后确认和资源上限保持。D7.12已在`c9da9bb931225a00017e4ca98a355ac3220369d3`实现，并完成36项纯检查、真实collector＋ready三项PG及HTTP20/实际native200/忙闲代表链（E85–E86）；原正式万条与完整观察门仍待过，不将小样本变化归因为吞吐收益。
- 正式HTTP R11仍失败（E87）。D7.13已在`17f74ee482a40fedb4be3293b0a9b27885284c6d`实施：仅在batchhint正文完整读完后归还未使用的raw预留，完整256KiB Envelope额度及原32请求/8MiB/8native上限保持；14项纯代表、GPT独立审查和同候选新HTTP20/native200/忙闲代表链支持针对性功能资格（E88/E90）。不以收集尺寸估计替代内存额度，不把可容纳小请求数变化或小链当成容量通过或已证瓶颈；原正式标准、完整600秒观察及最终统一OCR仍待。
- 首轮采用参考 Adapter 中显式、有时限的目标数据库重试，验证目标暂时不可用 60 秒后自动继续；不更改普通业务错误的默认平台分类。
- B2 真实单条测量触发业务小批次专项合同：HTTP/RabbitMQ 显式选择，仍逐条身份、结果、幂等与配额；共用一次 Execution 不把整批执行结果当作每条业务成功。
- 正式分别验收 HTTP 与 RabbitMQ 的 10,000 条突发：首次到达/发布窗口 ≤5 秒、全部持久接管 ≤60 秒、最后接管后全部处理 ≤600 秒；同时核验资源、管理面、恢复和独立目标结果。

非目标：#129 的 Worker Pool、跨主机 HA、同 Adapter 多并发、默认常驻 Adapter、DAG、自研 CDC、额外消息基础设施、持续每秒一万条、任意外部系统 exactly-once，以及无持久重试能力的 HTTP 发送方可靠性承诺。

## Capabilities

### New Capabilities

- `durable-http-ingress`：HTTP 接收身份、响应与状态查询、持久接管、输入与处理绑定、入口资源限制及旧 Webhook 兼容。
- `business-event-batch`：有界业务批次、不可变成员与共享执行、严格逐成员结果、重放/取消和整组历史保护；默认逐条不变。
- `ingress-capacity-validation`：固定资源下的完整链路测量、分阶段证据判定、一万条突发与故障验收；测量完成不能替代容量达标。

### Modified Capabilities

- `event-delivery`：正式可配置的接收容量、分层释放、就绪与完成确认的有界公平推进、受限发布及显式恢复策略，保留旧来源身份、顺序和失败责任。
- `event-subscription-management`：登记被动 HTTP 来源并沿用适配器内的配置/接收记录；容量配置权限、生效与积压保护规则明确，不增加独立一级菜单。

## Impact

- 后续涉及接收 API、来源注册、Subscription/Receipt 模型与迁移、容量配置、T1/T2、维护调度、Outbox、观测和适配器运行设置；Worker 协议、Admission、Slot 与凭据真值管理继续复用。B0 只允许隔离测量夹具和必要的观测性补充，不优化业务路径。
- 新 HTTP 路径和能力默认关闭、显式启用；旧来源身份/哈希、旧 Webhook、手动/定时运行、Kubernetes 完成与人工放弃合同保持。来源开关、容量等待、配置失效不变成坏消息。
- 只新增必要的接收字段/索引和配置审计，不另建平行消息权威。迁移先备份、验证旧责任与账本；回退先停止新接收并核对旧责任能否由目标版本读取，不能通过删除新责任或数据库降级强行回退。
- 复用 PostgreSQL、现有 RabbitMQ 和沙箱，无默认新增平台依赖。参考 Adapter 的数据库依赖显式锁定，不能改变平台依赖基线。
- 分批计划见 [tasks.md](tasks.md)。每批按改动范围验证和审查；发布前需要全部正式验收。可提前完成的文档、正式规格同步和归档随对应功能 PR；合并后证据补原 PR/Release，依照根目录 AGENTS.md 执行。
