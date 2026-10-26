# B3 Business Batch Contract Candidate

## Status and Decision Gate

**状态：B2 真实 200 条测量已触发，专项审查修正已纳入，正在实施；不代表容量通过。** 本文固定实施中的最小业务批次合同；一万条目标仍须完成独立容量验证。旧默认路径仍采用每条事件一个 Execution；Kubernetes 的 `SyncBatch` 是全量同步资格协议，不是本文的业务执行批次。

单条触发证据使用约定资源、真实 Worker/沙箱和独立目标，实际积压窗口与四方逐 ID 对账已记录于 design D9。已审查决议同步到 design、delta specs 和 tasks；不能用均值或 P50 的倒数代替真实完整节拍，也不提前放宽容量判据。

## Opt-In and Immutable Binding

- 首版仅 HTTP/RabbitMQ 可显式选择 `mode=business_batch`；旧 `per_event` 为默认，输入形状、身份、顺序和失败处理不变。Kafka、RocketMQ、Kubernetes 及其同步控制不得进入本批次路径。
- 改 mode 必须先停用、确认来源关闭并排空责任，形成新的 ConfigVersion/Activation。T1 冻结的 mode 随原 Activation 保存，不能按当前设置改造旧 pending 的输入。
- 相容成员必须具有同一 Adapter、subscription、source epoch、shard、**精确相同的有效 Activation ID 和 Revision override**。首版不按字段相似或策略哈希推断多个 Activation 等价，也不合并不同 replay/repair 绑定。
- 在同一顺序分片取可运行的未结清头部前缀；已审计终态的序号间隙不妨碍推进。遇失败、处理中、尚未到期的重试或不相容绑定即停止，不越过成员。不存在同 Adapter 多 active Attempt 的新特权。

## Persistence and Database Invariants

保留 `EventDelivery.execution_id` 及现有 `uq_delivery_execution` **原样**，普通单条执行仍由数据库保证一对一。新增结构仅表达不可变关联及其有界结算事实，不另设消息接管权威：

| 结构 | 最小合同 |
| --- | --- |
| `SubscriptionExecutionBatch` | `execution_id` 为主键并引用 Execution；固定 mode、subscription/epoch/shard、有效 Activation/Revision、完整有序 manifest 及其哈希、member_count、输入字节、结果/关联保留字节；member_count 在 1–100 内 |
| 业务批次成员表 | `delivery_id` 唯一外键，保存 receipt_id、execution_id（外键到本批次表）和 ordinal；`UNIQUE(execution_id,receipt_id)`、`UNIQUE(execution_id,ordinal)`，`CHECK 0<=ordinal<100` |
| `EventDelivery.batch_execution_id` | 可空批次外键；数据库 CHECK 禁止与单条 execution_id 同时有值；pending 尚未交接时两者皆空，交接后仅一条关联有效 |

成员表的 ordinal 是**从零开始的批次索引**；Receipt 的 Delivery ordinal 是从一开始的重放序号，两者不能混用。manifest 同时固定 receipt_id、delivery_id、delivery_ordinal、member_index 和原 Envelope 哈希。

数据库还 MUST 保证以下跨表不变量，不能只依赖两个独立唯一索引或逐行 CHECK：

1. 成员引用的 receipt_id、delivery_id 和 batch_execution_id 必须来自同一 Delivery；采用复合外键/唯一键或等价的数据库约束保护。
2. 同一 Execution 不能既有普通单条 Delivery 又登记为业务批次。批次登记及普通关联写入必须在同一 Execution 行锁下更新并核验不可变的 `single/batch` 判别，数据库守卫覆盖二者并产生真实写冲突；不能只靠互斥 EXISTS，让 Repeatable Read 的旧快照漏掉对方。
3. 实际成员集合必须与固定 count、manifest、连续索引 `0..count-1` 完全相等；不足、额外、重复 Receipt 或改写集合均拒绝提交。可用延迟约束触发器或等价的数据库约束实现，专项实现审查必须验证其事务与并发行为。
4. 已绑定的成员身份/索引/批次不得原地重写。replay 新建 Delivery ordinal，不能借更新旧关联替换原失败责任。合法历史清理须依保护和预算合同整体处理，不以级联删除绕过。

## Assembly and T2 Atomic Handoff

每批至多 **100 成员、完整 UTF-8 执行输入 256 KiB、最长组装等待 50 ms**。字节上限包含包装、成员标识和完整原 Envelope；100 是上限，不承诺 100 个 2 KiB 正文总能装入。已有积压或达到人数/字节上限时立即形成，不另加固定等待。不得为等待凑批持有数据库事务、来源连接名额或 Adapter 锁。

T1 在业务批次 mode 下必须验证单个成员连同必要包装可以装入，并预留未来输入副本、关联及安全结果的历史预算。无法装入的新消息明确拒绝接管或进入原来源可处置拒收路径；原已接管记录的同参读回先于新限额判断，不能使历史重试失效。

T2 MUST 在同一事务中：锁定 Adapter/Subscription 和原分片责任；复核相容前缀；创建一份 immutable Execution、Admission、Outbox、批次与全部成员关联；固定实际输入；将所有成员转 processing 并扣减各成员 pending 数量/字节。任一步失败全部回滚，原 N 个责任仍可恢复。Admission/Outbox 按真实一份执行计量，但不能因此免记 N 份原事件及新增持久副本。

Slot 0 及原 Claim/fencing/Attempt 协议继续是执行并发权威；一批一份 Execution、一个 Outbox 责任和至多一个 active Attempt。通知只作提示，重启或丢通知仍由持久关联恢复；处理扫描按共享 Execution 去重，一批 100 成员不能吃掉 100 个完成确认候选。

## Input and Result Protocols

输入为显式 `dlr.event.batch.v1`，含固定批次/订阅标识、manifest 哈希和有序 members；每个成员含数字 receipt_id、delivery_id、delivery_ordinal、member_index 及完整原 Envelope。不放原始 HTTP token、认证请求头或凭据真值。仅显式支持该协议的保存代码可用于此 mode；普通返回值不自动解释为成员结果。

结果为 `dlr.event.batch.result.v1`，完整结果上限为 `min(64 KiB, 512 + N×512 bytes)`，精确覆盖同一 manifest 的全部成员及顺序。成员状态为 `succeeded`、`failed`、`unknown`、`not_run`；安全码来自固定允许集合，不将任意用户错误文本写入外部 HTTP 状态。额外字段、错/重复/遗漏 ID、错索引、超限或不匹配 manifest 均无效。

合法业务结果只有两类：全部 succeeded；或 succeeded 前缀，随后恰一个 failed/unknown，余下全部 not_run。不得在失败后声称后继成功。取消前可证实从未启动的全体 not_run 是 Control 处置事实，不是用户代码可借来绕过合法结果规则的通道。

`finish_attempt` MUST 在终态提交及释放 Admission 之前验证：实际 owner/Claim Token/fencing/权威 Attempt、原成员集合、完整 schema/大小/顺序、Worker 原 `output_truncated` 及 Control 规范化后的截断状态。脱敏、预览或截断破坏的协议不可由日志补猜。当逻辑 Execution 最终结束或成功回报的协议无效时，无效/缺失结果使全体成员成为 unknown，不从不可信对象保留“成功前缀”。基础设施故障仍应进入 `retry_wait` 的 Attempt 不封存最终成员结果、不释放 Admission；原 processing 成员随固定 Execution 输入恢复。

可信成员结果、其权威 Attempt 和安全计数与共享 Execution 终态一起持久提交，重复回报仅读回原事实。全部成员成功才允许 Execution succeeded；任一非成功则为明确的业务失败终态及固定批次错误码，保留合法成员结果。不得先提交共享 succeeded、释放 Admission，再由维护循环事后翻案。原业务错误分类和基础设施恢复策略不全局改变。

## Member Settlement, Cancellation and Recovery

Receipt 仍使用兼容的 pending/processing/succeeded/failed 等状态；Delivery 另外保存受限 `member_outcome` 及安全码，明确区分实际失败、执行结果未知和未执行。failed/unknown/not_run 均保留原输入及未结清责任，不能记成功、误记消息损坏或丢弃。API/UI 的“未执行”不能显示为“已经执行但失败”。

成员结算是一个有界事务，明确成功者独立结清，其余责任保留。Checkpoint blocker 始终重算为最早未结清成员；结算、replay 完成及 skip 都不得沿用“单条成功直接清空 blocker”的旧假设。结算重入、重复 finish、重启和部分数据库异常不得重复释放或覆盖原结果。

- Worker/进程丢失、目标提交后未回报等不明情况，沿原 Execution 的固定输入、成员身份和平台恢复策略处理。重复外部效果由业务稳定键核验解决，不能把未知写成成功，也不将普通 business_error 改为自动 platform_transient。
- 正在运行的共享 Execution 取消影响全部未完成成员，界面显示范围。只保留 **Control 已校验并持久记录** 的成功事实；其余按可证事实为 unknown，明确从未启动才可为 not_run。目标写入或日志存在不等于平台已经确认。取消与 finish 竞态在原锁序内裁决；保留原取消优先级，因此取消请求与合法全成功结果同时到达时，Execution 可记为 cancelled，但已经确认的全部成员仍记为 succeeded，不因取消改写业务事实。反向约束仍成立：Execution 只有在全部成员成功时才可 succeeded。
- 首版保留逐成员 replay/repair/skip，不新增批量处置 API。每次操作保留原 Receipt 和旧失败 Delivery，新增 Delivery ordinal、独立审计及真实预算。批次成员即使单独 replay 仍使用单成员 batch 输入，不悄悄切回 per_event。
- `use_current_configuration` 跨 per_event/business_batch 模式明确返回 409；同 mode 也须遵守原停止/排空、预览与绑定门禁。repair 只处理明确选中的最早阻塞责任，不自动将失败后缀重新成功化。
- 手工恢复可能需要逐成员操作，交付文案与验收如实说明；不宣称普通业务失败后整批自动恢复。

## Reference Target and Shared Retry Budget

参考 PostgreSQL 目标仍对每成员的 `(run_id,message_id)`、完整业务哈希和值逐条幂等核验，并独立提交事务。允许在 **同一次 Invocation** 内复用连接，不允许跨 Attempt 常驻连接或用户进程，不用一个大事务冒充 100 个独立效果。

批次有 N 个必要的首次成员事务，N≤100；其外整个 Invocation **共享最多 19 次额外重试**，所以任意单个成员至多 20 次尝试。全部连接、事务、重连和退避共同受从 handle 开始的 **120 秒总 deadline** 约束，不能按成员或重开连接重置。每次实际连接/语句仍≤5秒并受剩余 deadline 约束，Execution 原300秒边界及取消保持。单条 per_event 示例仍最多20次总尝试，不把100个必要事务称为“总共20次”。

重连消耗共享额外重试预算；已确认成功成员不因重连重新写入。commit 结果未知时只用原稳定键/原文核验并恢复当前成员，不产生第二个有效写入。白名单仍限连接类、SQLSTATE 08/40001/40P01；权限、语法、数据或身份冲突明确停止于首失败，后继 not_run。目标尝试表只代表已提交事务，失败连接与其他重试由有界安全日志/输出另计。

## Capacity, Retention and Upgrade

复用 B1 的 pending/history/dedupe、控制预留及保留副本账本。T1 保守预留可序列化的单成员包装、成员关联和安全结果上限；T2 保守保持各成员已预付的 singleton 输入份额和每成员 8 KiB 批次结构/结果预算；份额合计须覆盖真实共享输入、manifest、关联及有界结果的双份保留。未退差额明确仍是预留，不虚报释放；整组合法清理后一次归还。批次固定包装、manifest、关联、结果均有明确字节上限和计费，须在实际有效 profile 下核对余量，不能为通过容量临时调大额度。

共享 `Execution.input` 的每成员副本以及批次元数据/结果必须有唯一、可审计的保留预算归属。Receipt 原文过期、一个成员清理或一个 replay 成功，都不能把其他成员仍需恢复的共享输入记为已释放。任何成员未结清或仍受恢复/留存引用时，整个共享 Execution、固定成员集合及 Activation 继续受保护。只有全部相关保护解除时才整体清理共享副本并幂等释放相应预算。

迁移新增可空列和独立表，保留旧 unique、旧 Receipt ID/哈希/Envelope 和执行输入。先验证新库及有 pending/failed/replay 的旧库升级；首次批次创建后，不理解批次的旧版本不能直接接管或执行旧 `observe_terminal`。启动、版本能力或回退预检须 fail closed；停止新接收、排空/保留兼容可读责任，不删除批次、降级抹账或强行恢复不相容约束。

## Required Caller Audit

| 路径 | 批次实施时必须核验的关联 |
| --- | --- |
| `subscription_delivery.dispatch_delivery/observe_terminal` | T2 前缀整体交接、共享完成去重、结果验证及 blocker 重算 |
| `subscription_delivery.replay_event/skip_event` | 新 ordinal、单成员 batch 形状、原绑定、跨 mode 409、审计和分层计费 |
| `subscription_delivery.execution_protected/cleanup_history/audit_quota` | single execution_id 与 batch_execution_id 的联合保护；禁止逐成员清空共享 input；配额实物重算含批次和成员 |
| `subscription_management._nonterminal`、运行锁/删除/修复 | 任一共享执行非终态均锁住相关操作；停用仍可排空 |
| `subscription_activation_policy.ensure_policy/freeze_legacy_policies` | 同时读取普通和批次关联，旧策略不因 join 漏项回退到当前配置 |
| `subscription_scheduler` | 共享 Execution 为完成单元、成员上限仍有界、不同订阅公平和丢通知补偿 |
| `attempt.finish_attempt`、准备失败/Worker 丢失/租约恢复 | 权威 Attempt 的协议验证和终态事实；同 Adapter Slot 0；原输入重试 |
| `execution_cancellation`、执行清理/Artifact 保护 | 共享取消范围、原锁序及 Admission 一次释放；任何成员保护阻止整体删除 |
| `api/subscriptions.serialize_event/replay`、`http_ingress._status` | 对外 execution_id 使用单条或批次有效关联；每 Receipt 安全成员状态，token 不获得管理权限 |
| `subscription_sync` 的执行存在、合并、完成及放弃检查 | Kubernetes 永不创建业务批次；存在判据不能把 batch_execution_id 误认“尚未执行”并合并覆盖。历史全量资格与当前业务批次不混同 |
| Adapter 内运行设置、接收/执行详情 | 显式“一次处理多条（代码需支持）”、输入变化、成员数及成功/失败/未知/未执行计数，双向关联和共享取消范围；不新增一级菜单 |

## Targeted Acceptance Before B3 Completion

1. 人数、完整256KiB输入、50ms等待、不同Activation/override/epoch不混批、顺序前缀、单成员无法装入与原去重读回；单条默认输入不变。
2. 真实数据库的部分写入/提交前中断、并发重复调度、故意混单条关联/批次、重复Receipt/索引/漏成员；证明全部T2写入整体提交或回滚、N成员计费准确。
3. 真实 Worker/沙箱的全成功、成功前缀+首失败/unknown+not_run、错/漏/重复ID、错manifest、输出过大/截断/脱敏破坏；Attempt身份与finish/取消竞态；不能由Execution成功数替代成员数。
4. 目标60秒不可用、提交结果不明、结果回报丢失与Worker重启：共享19次额外重试/120秒总预算可核验，原键幂等、无重叠active Attempt、无重复有效业务效果。
5. 成员replay/repair/skip、跨mode当前配置重放、部分TTL与整体清理、升级及拒绝不相容回退；HTTP安全查询/ACL、旧K8s全量完成/合并/放弃、旧per_event针对性兼容。
6. 同资源200条完整链路实测与独立目标对账，再分别验证HTTP/RabbitMQ正常10000条及约定故障轮次。实际批次数、Execution数、成员数分别记录；本候选或小样本通过都不代表正式容量通过。

本文件新增不标记 B3 或 B4/B5 任务完成，不替代主设计/delta 的后续同步和专项审查决议。
