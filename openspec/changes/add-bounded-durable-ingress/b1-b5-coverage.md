# B1–B5 需求与证据覆盖稿

**状态：实施与验收进行中，尚不能归档或提交 PR。** 本稿用于定位已实现路径、已经发生的验证以及剩余门禁，不代替 [tasks.md](tasks.md) 的完成判定。本文未修改任务勾选，不以测试文件存在、代码可构建或 OpenSpec 校验通过宣布功能或容量通过。

记录日期：2026-10-26。当前文档核对基线为 `68900ca7eebd5275239de4bf93fa554642b8d66b`；该候选已包含 RabbitMQ 有限 credit、前置原生预算、剩余收集窗口及接收记录分页一致性修复。下列正式 r2 证据属于更早的 `b97e976e2a260739af3c6490259505660999d44d`，不代表当前候选重验通过。最终报告须补齐实际交付候选的完整 SHA、对应回执及剩余实测，不能将不同候选的结果合并为一次最终通过。

## 证据口径

| 编号 | 已核实来源与范围 | 能支持的结论 | 不能支持的结论 |
| --- | --- | --- | --- |
| E0 | [B0 结果](b0-results.md)、[阶段统计](b0-stage-statistics.md) | 约定基线、隔离资源、逐条核验与瓶颈初判已有记录；B0 已完成 | 后续实现、业务批次或正式容量通过 |
| E1 | [B1/B2 实测记录](b1-b2-results.md)，候选 `0b258794eeca094a03bd128ff1ecb2fb949a61e5` | 旧单条路线的 HTTP/RabbitMQ 各 20 条真实链路；200 条单条比较、原文/目标/责任对账；触发 B3 的依据 | T1Unit、HTTP 收集或新 RabbitMQ 路线的实测通过 |
| E2 | 本机核心定向回执：26 passed / 12.65 s | 17 项 T1Unit 核心、8 项容量/并发/SQL 数量及 1 项旧 T1/业务批次代表路径。包含逐条计费、组内重复/冲突、MQ 前缀、HTTP 独立错误、两个 bulk INSERT、控制优先权、磁盘、租约、停止及事务回滚 | 接收吞吐、真实 SDK 限额、UI、正式故障恢复或全部回归通过 |
| E3 | 主 Agent 提供：HTTP 48 passed / 14.72 s；runtime/pool 14 passed、3 项既有 skip / 9.39 s | 对应 HTTP 接收/收集、取消、当前认证及运行器/共享池定向范围已有回执 | 被 skip 的路径通过；所有变更均完成验证。skip 的逐项原因仍须随最终回执核对 |
| E4 | 首轮 RabbitMQ 10,000 条，候选 `058d4f1294e06b4882072e05ac8d1c2f2eb19ad1`，主 Agent 提供完整核验结论 | 发布确认窗 1.129 s；目标与平台两个独立 oracle 各 10,000 条通过；最后接管至全部结清上界 2.017 s；管理采样 1,338 项均 HTTP 200 | **接管时限失败**：实际接管边界为 279.926–281.739 s，超过 60 s。发布和最终正确不抵消首败；该候选不能判容量通过 |
| E5 | E4 同轮的资源与排空后观察，主 Agent 提供 | 已报告 Control / Worker 根层峰值 347.16 / 181.15 MiB，含准备/预热的保守上界；排空后观察 142.984 s | 600 s 观察尚未完成；1,338 个 HTTP 200 不替代逐接口 p95、基线和超时/拒绝核对；旧轮资源不替代新 SDK/取消边界验证 |
| E6 | 候选 `37536bc5ab7f3e2f542e0d3c134b91a5577dacef` 的真实 HTTP 小样本，主 Agent 提供 | 20 条全部成功上界 2.051 s；200 条最后接管首次观测 5.153 s、全部成功 6.167 s，两个独立 oracle 各 200 条通过。首轮发送窗 0.403 s，100 个 202、100 个 429；最终 301 次 POST、200 条接管、101 次 429、未知结果 0 | 不是 HTTP 10,000 条正式轮次，不能按小样本外推 60 s 达标；首轮拒绝与重试必须保留披露 |
| E7 | 同一候选的独立有界观察器诊断，主 Agent 提供 | 确认 `_run_group_sync` / T1Unit 命中 136 次，200 个新项，Receipt/Delivery INSERT 各 136 次，未走旧 POST 接收路径；平均小组约 1.47 条，否定该轮“hint 未命中”的猜测 | 观察器有开销，诊断轮不是无诊断性能验收；不能推定 10,000 条必然满组或获得四倍收益 |
| E8 | 第二轮 RabbitMQ 正式 10,000 条（r2），候选 `b97e976e2a260739af3c6490259505660999d44d`；只读核对私有收尾报告与两个 oracle 摘要 | 平台及目标两个独立 oracle 均通过 10,000 条；Receipt/Delivery/业务成员各 10,000，目标有效写入 10,000，两个容量审计均无差异；最后接管至全部完成上界 65.034 s | **接管时限仍失败**：实际接管边界为 134.210–135.235 s，超过 60 s；最终全部成功不抵消超时，不能判正式容量通过 |
| E9 | E8 同轮管理、资源及排空后观察，只读核对收尾报告 | 227 个资源采样均无采样错误；669 次管理请求均 HTTP 200，未观察到超时或 5xx；已关闭来源并排空本轮 Broker | 其中 1 次接收记录分页响应违反 `total >= len(items)`，**管理一致性门禁失败**；排空后仅观察到上界 2.065 s，未达到 600 s。分页竞态已在 `68900ca7eebd5275239de4bf93fa554642b8d66b` 改为单语句快照；修复存在不等于新正式轮次已通过 |
| E10 | 主 Agent 提供：`b97e976e2a260739af3c6490259505660999d44d` 的 RabbitMQ 20/200 条真实链路 | 已核对 Broker 实际 prefetch 为 4；两个独立 oracle 在 20/200 条均通过；200 条全部接管首次观测 2.048 s、全部业务成功 6.135 s | 小样本不替代正式 r2；prefetch=4 不证明每个接收组都满 4 条，也不证明当前候选性能、UI 或 OCR 通过 |


| E11 | `68900ca7eebd5275239de4bf93fa554642b8d66b` 的独立 RabbitMQ 200 条诊断；两独立 oracle 通过 | 修正剩余收集窗口前，200 条分为 90 组、平均 2.22 条；修正后 50 组均为 4 条。T1Unit.receive 累计 inclusive 时间由 1,846.30 ms 降至 585.55 ms，观察器无错误且最终无 active/duplicate | 含观察器、仅 200 条，不是正式容量；累计时间不等于墙钟接管时间，不能外推一万条通过 |
| E12 | 同一 `68900ca7eebd5275239de4bf93fa554642b8d66b` 冻结代码的 HTTP 20/200 条真实链路，两个独立 oracle 各轮通过 | 从宿主启动发生器到观察全部成功的保守上界分别为 3.120 / 6.174 s；各轮正常停用来源 | 该上界包含启动、轮询等开销，不是精确 T_accept；不能代替正式轮次 |
| E13 | HTTP 首轮正式 10,000 条，仍为冻结 `68900ca7eebd5275239de4bf93fa554642b8d66b` | 发生器 5 秒内仅发起 375 条，9,625 条首轮未发送；77 次未知、1 次 408；最终 374 条接管且全部成功，来源已正常停用，证据保留 | **发生器首轮覆盖失败，DLR 一万条容量未测成**。两独立 oracle 按原 10,000 条判失败，不以实际 374 条改小期望；没有完成 600 s 观察，不重发补齐或重置 T0 |

E2 的核心文件 SHA-256 为 `cf4a2c134ee6217d08256333b46d0ce2a1522857c5118c2d682b22108cf65d4e`，测试文件为 `7e03b43d1e60520481d862aa090e61b3a4bbd821e536d80b87030f446cb7af48`。该回执绑定当时工作区内容；执行开始时未另取完整 HEAD，不冒充最终候选的精确 HEAD 审查。E2/E3 套件可能覆盖相同依赖，不能简单相加为独立覆盖数量。

本稿的测试名称只标示验证入口。没有关联已核实回执的项目，仍须按影响范围核对结果；不从函数名推断它已经运行或通过。完整私有回执与原始观测由主 Agent 保留，本文不复制实际接收地址、凭据或本机目录。

## 实现与验证位置索引

下列均为仓库相对位置。

- **HTTP**：`backend/src/dlr/control/api/ingress.py`，`services/http_ingress.py`、`http_ingress_group.py`、`subscription_http.py`；验证入口为 `test_issue222_http_ingress.py`、`test_issue222_http_groups.py`、`test_http_ingress_limits.py`。
- **容量、绑定与账本**：`services/ingress_capacity.py`、`subscription_activation_policy.py`、`subscription_accounting.py`、`subscription_delivery.py`、`subscription_sync.py`；验证入口为 `test_ingress_capacity*.py`、`test_issue222_capacity_queries.py`、`test_issue222_queue_capacity_profile.py`、`test_issue222_activation_policy.py`、`test_issue222_input_ledger.py`、`test_issue222_ingress_upgrade.py`。
- **推进与发布**：`services/subscription_scheduler.py`、`subscription_runtime.py`、`ingress_database.py`、`outbox.py`；验证入口为 `test_issue222_scheduler.py`、`test_issue222_intake_pool.py` 及相关既有运行器回归。
- **业务批次**：`models/subscription_business_batch.py`，`services/subscription_business_batch.py`、`subscription_batch_protocol.py`、`subscription_batch_retention.py`、`subscription_batch_view.py`，以及 `attempt.py`、`reliable_execution.py`、`execution.py`；验证入口为 `test_issue222_business_batch.py`、`test_issue222_batch_protocol.py`、`test_issue222_batch_lifecycle.py`、`test_issue222_batch_declarations.py`、`test_issue222_batch_guard_json.py`。
- **T1 合并**：`services/subscription_t1_unit.py`、HTTP 收集/共享池、`subscription_rabbitmq.py`、`subscription_rabbit_budget.py`、`subscription_rabbit_metadata.py`；验证入口为 `test_issue222_t1_unit.py`、HTTP groups、pool、`test_issue222_rabbit_native_budget.py`、`test_issue222_rabbit_micro_t1.py`。MicroT1 最多 4 条/原文加完整事件 1 MiB/额外收集等待 5 ms，保留逐条责任与提交后确认；T2 的业务批次上限为 100 条/完整输入 256 KiB，二者不等同。
- **迁移**：`backend/alembic/versions/0051_issue222_durable_http.py`、`0052_issue222_business_batches.py`、`0053_issue222_batch_guard_json.py`；旧单条关联唯一约束保留。后续优化不能以修改已发布迁移取代前向迁移。
- **界面**：`web/src/components/HttpSubscriptionConfig.tsx`、`EventBatchModeFields.tsx`、`EventBatchCodeDeclaration.tsx`、`SubscriptionWorkspace.tsx`、`ExecutionBatchNotice.tsx`，相关中英文 locale、schema/registry/management/ACL；验证入口为 `HttpSubscriptions.test.tsx`、`BusinessBatchSubscriptions.test.tsx`、`EventBatchCodeDeclaration.test.tsx` 及 App 测试。
- **独立夹具**：`tools/ingress/workload.py`、`target.sql`、`reference_adapter.py`、`management_probe.py`、`fault_probe.py` 及其测试/说明；参考目标与平台账本分别核验。工具存在、synthetic 自检或故障点配置不等于真实故障轮次发生。

## Tasks 2.1–6.3 覆盖表

| 任务 | 实现与必要验证映射 | 已发生的证据 | 当前缺口与判定 |
| --- | --- | --- | --- |
| 2.1 HTTP 登记、路由与升级 | HTTP source/schema/registry、`HttpIngressRoute`、0051–0053；旧 pending/failed/control 数据升级、不可复用标记和降级保护 | E1 记录旧责任升级范围；迁移与批次模型可静态定位 | 最终迁移链及回退预检须绑定最终候选复核；不能仅据迁移文件存在完成任务 |
| 2.2 当前 token、安全查询与 ACL | HTTP `_authenticated` / `receipt_status`、管理 API ACL；错/撤销 token、跨来源、旧地址、删除/重建、原 Webhook 两模式 | E1 的旧候选功能边界；E3 当前 HTTP 定向回执 | 原 Webhook 两模式及管理 ACL 的适用最终回执须逐项归集，当前用户流程仍需 UI 验证 |
| 2.3 持久接管与固定输入 | HTTP `receive` / `receive_group`、T1Unit、Receipt/Delivery/Activation、原始 bytes SHA；提交前后故障和重复 | E1 的真实 20 条；E2/E3 的原文、202/200/409、计费与提交不明回归；E6/E10 小样本、E8 Rabbit 10,000 条双 oracle | 当前候选及真实故障点仍待核验；正式 HTTP 10,000 条未通过；E8 接管时限失败 |
| 2.4 生命周期及运行锁 | `subscription_management`、runtime/lease、平台错误分类、冻结策略、replay/repair/skip | E1 记录旧责任/平台故障范围；E2 的 stop、binding、租约回滚；E3 runtime/pool 回执 | 新接收合并路径的完整生命周期与真实关闭/恢复证据尚须归集；3 项 skip 不算通过 |
| 2.5 部署容量与管理边界 | `ingress_capacity`、fingerprint/audit、管理 limits；无效配置、并发采用、下调保留、入口排空 | E1、E2；真实 READ COMMITTED 旧状态刷新拒绝及 global 锁串行采用已有定向回执 | 最终公开运维说明及全部权限/未排空拒绝场景须与候选一起复核；不据调大配置宣布容量通过 |
| 2.6 资源隔离与拒绝释放 | HTTP 32 请求/8 MiB/8 原生工作；HTTP 与原生来源共享 intake 8 连接、管理/恢复另 4 连接，均无 overflow；64 MiB 来源父预算与磁盘迟滞 | E2 逐项盘检查；E3 HTTP 限额/取消及共享池；E5 旧轮资源上界；E9 r2 的 227 个资源采样无错误 | SDK 前置预算、有限 credit 及关闭路径已有实现与 E10 小样本；当前候选混合峰值、实际释放及完整观察仍须绑定证据，E9 管理一致性未通过 |
| 2.7 完整计费、T2 与留存 | Receipt 原文/元数据、Delivery 未来输入/批次份额、控制预留、30 天整组 GC；原子 T2、回滚、审计 | E1 的历史计费与升级记录；E2 的完整 T1 账本/回滚和旧 T1/业务批次代表回归 | 全部 batch TTL/重放交叉引用/旧 K8s 完成预留回执须绑定最终候选归集；不能以当前计数为零替代恢复验证 |
| 2.8 两来源 20 条链路与本批审查 | `workload`、真实 Worker/目标、平台与目标独立 oracle、HTTP/事件文档 | E1：旧路线 HTTP/Rabbit 各 20；E6 新 HTTP 20 条；E10 Rabbit 接收合并 20/200 条 | 两来源结果属于不同候选；最终候选文档及 OCR 未完成，小样本不等于容量验收 |
| 3.1 依据实测选择优化 | B0/B1/B2 结果、调度/Outbox、容量重复读、0053 JSON 一次展开、D7.1 | E0/E1；E4/E8 两次正式接管失败；E7 为后续选择提供组大小事实 | 最终差异审查/OCR待完成；不得把旧瓶颈假设或诊断含观测开销的耗时当作收益 |
| 3.2 有界公平推进 | scheduler 的 pending/processing 分离、轮转、轻量头部、补偿通知；10 入口/阻塞页/重启场景 | E1 已记录单条推进改进；相关测试入口已核对 | 新业务批次与 T1 合并后的 10 入口两类 ≤2 s 门槛仍须绑定证据，不从调度测试文件存在推断正式通过 |
| 3.3 受限发布与确认 | `outbox` 的线程归属连接/channel 复用、确认窗口、return/nack/超时/丢连接；逐行未知恢复 | E1 的 200 条发布与责任对账；测试入口含复用、重建、逐次 deadline | 最终真实中断/部分确认证据待归集，已有测试定义不替代 Broker 故障轮次 |
| 3.4 单条完整链路比较 | 不变沙箱、执行策略/凭据快照、准备成本与参考 Adapter | E1：B0→B2 200 条比较及逐项 oracle，触发 B3 | 比较支持路线选择，不是最终批次吞吐结论；当前候选的必要回归/OCR待完成 |
| 3.5 参考目标有界恢复 | `reference_adapter`、单条/批次重试白名单、共享 deadline/额外重试预算、独立成员事务 | E1 支持已发生的单条链路；参考目标真实数据库测试位置已核对 | 60 秒真实不可用、提交结果不明、权限及预算耗尽的最终回执须明确归集；不能凭示例代码宣称任意 Adapter 自动恢复 |
| 3.6 B2 路线裁决 | `b1-b2-results`、D9、`b3-contract` 与业务批次 delta | E1 已用完整 200 条真实积压窗口触发 B3 | 路线依据已记录；最终代码/OCR和 B4 门禁未完成，不以分位数倒数裁决容量 |
| 3.7 T1 接管合并 | T1Unit、async HTTP collector、共享 intake 8 连接、Rabbit header 前置预算/最多 4 credit/受限剩余收集窗口 | E2/E3；E6 新 HTTP20/200；E7 确认实际命中；E10 Rabbit20/200；E8 正式 r2 逐条结果正确 | r2 的接管时限与管理一致性门禁失败；689 候选的新窗口/分页修复仍需对应复核和正式重验，不以预取数推定满组或吞吐通过 |
| 4.1 B3 专项合同 | [b3-contract](b3-contract.md)、business-event-batch delta、D9；精确 Activation/override、结果/取消/留存规则 | E1 的触发证据；专项合同及 delta 已存在且可核对 | 最终合同与代码/文档的审查仍须收齐；没有用户界面声明或整批进程成功替代成员证明的例外 |
| 4.2 有界执行与逐条结算 | Batch/Member 模型、0052/0053、strict finish、Slot/Attempt、replay/skip、整体 GC | E2 的旧 T1/业务批次代表回归；E4/E8 各轮两个 10,000 条 oracle、成员最终正确 | 完整部分成功/未知/取消/Worker 重启真实范围须归集；两轮接管 SLA 仍失败，不能整项打勾 |
| 4.3 批次说明与比较 | 中英文业务批次指南、版本 marker、输入/结果 schema、逐条投影与技术详情 | E4/E6/E8 的逐项正确结果；中英文 HTTP 指南已区分 MicroT1 与业务批次 | 最终前后对照、UI 实际操作及 OCR 未完；文档更新不代表用户流程或容量通过 |
| 5.1 界面与原操作习惯 | HTTP来源抽屉、处理方式及代码声明、接收记录、共享执行提示、中英文资源 | 实现及测试入口存在；本稿没有可绑定当前候选的浏览器流程回执 | 待真实创建/取消/保存/明确开启/停止排空/容量暂停、权限和中英文 UI 交互/视觉验证；构建成功不替代 |
| 5.2 正式夹具与预冻结 | `workload` / target / management probe、容量 profile、原文/业务哈希、空闲接口基线 | E0/E1 方法；E4 首轮发布及双 oracle；E6 HTTP发送及重试披露 | 新候选的 profile/正文真实账单、资源、空闲 p95 与运行清单须在正式运行前固定；禁止运行中加额 |
| 5.3 两来源各一万条 | 独立目标与平台 oracle；5/60/600 秒分别核验 | Rabbit r1 接管 279.926–281.739 s、r2 接管 134.210–135.235 s，均 **FAIL**；E4/E8 各轮双 oracle 最终 10,000 条通过；E6 仅 HTTP200 | 当前候选 HTTP10,000 待验，Rabbit10,000 待正式重验；保留两次失败，不重置 T0 或用最终成功覆盖超时 |
| 5.4 管理面、资源及10分钟 | management/resource probe、根 cgroup、并发/队列/账本、排空后600 s | r1 E4/E5：1,338 个200、142.984 s；r2 E9：669 个200但1次分页一致性失败、227 个资源采样无错误、排空后仅≤2.065 s | 两轮均未完成600 s，不能跨轮累加；689 已修分页快照竞态，当前候选的同步管理/资源及完整观察仍待重验，HTTP200不等于管理门禁通过 |
| 5.5 目标中断60秒 | 独立 RabbitMQ1,000 条故障轮次；失败/恢复/逐项幂等、无人工处置 | 夹具与参考重试规则已有实现位置 | 未取得本稿可确认的完整真实轮次回执，保持待验；正常轮次不替代 |
| 5.6 精确故障点 | HTTP/Rabbit T1 前后、代表性 Outbox、Claim/journal、目标提交后回报前；每点20条 | E2/E3 的事务/提交不明定向回归；故障探针代码存在 | 各准确触发点、120/600 s 恢复、成员/Slot/释放与独立效果的真实故障轮次待验；synthetic 不算真实发生 |
| 5.7 背压、权限与旧来源 | 容量/磁盘、身份/绑定、管理锁、旧来源 ACK/开关、K8s 完成与放弃 | E1 的旧责任兼容记录；E2 部分门禁；E3 已发生的定向范围 | 新接收路径的受影响兼容用例与权限回执仍须归集；只补受影响来源，不默认全矩阵 |
| 5.8 公开报告与复审 | 本稿、测量报告、中英文指南、运维/故障说明 | E1/E4–E10 保留首败、第二轮失败、正确性结果及未完成观察；中英文 HTTP 指南同步共享池/MicroT1 边界 | 最终正式容量/恢复报告与 OCR/复审尚未完成；未通过的门槛不能改写为完成 |
| 6.1 需求逐项闭合 | 本覆盖表、五能力 delta、Issue 验收与实际证据 | 本稿提供了可审查映射和明确缺口 | 仍为 draft；正式两来源容量、故障、观察、UI及审查未闭合，不申请 Issue 完结 |
| 6.2 规格/文档与检查 | active change、HTTP/业务批次指南、配置/迁移/回退、必要静态检查 | E2 静态检查及既有报告；本次三份文档的 whitespace/链接/结构/脱敏自查和本 change 的 OpenSpec strict 校验通过 | 文档与规格校验不替代业务验收；最终规格同步、其余文档差异核对和归档仍待全部必要门禁完成 |
| 6.3 最终审查与交付 | AGENTS规定的 OCR preview/coverage/终态、修复验证、精确 base/head | 尚无可声明覆盖最终候选全部差异的 OCR 回执 | 待 OCR、核实问题/修复/复审、候选干净且可复现；通过后通知用户并停在 PR 前，不推送、不自动合并 |

## 五能力 delta 对照

| 能力与 Requirement 范围 | 实现/任务映射 | 当前证据边界 |
| --- | --- | --- |
| [durable-http-ingress](specs/durable-http-ingress/spec.md)：旧 Webhook区分、持久身份/状态、原始正文、固定绑定、入口资源、安全重试、当前 token/路由留存、HTTP 独立合并错误 | HTTP、容量/绑定、T1 合并；2.1–2.8、3.7、5.1 | E1/E2/E3/E6 支持明确的功能/小样本范围；原 Webhook、ACL、最终真实故障、完整UI及 HTTP正式10,000待最终核验 |
| [event-delivery](specs/event-delivery/spec.md)：稳定首次输入、各层容量/责任释放、两类有界推进、发布确认、参考目标恢复、T1逐条责任/确认 | 账本、scheduler/outbox、reference Adapter、T1Unit；2.3–2.7、3.1–3.7、5.5–5.7 | 既有单条实测与新定向范围已分列；新 Rabbit SDK、并发资源、控制优先权和准确故障恢复必须单列证明；E4/E8 的60秒失败均保留 |
| [business-event-batch](specs/business-event-batch/spec.md)：显式选择/固定绑定、原子成员集合、strict结果、逐成员处置/共享取消、共享副本保护 | Batch/Member、0052/0053、finish/Admission/Slot、replay/skip/GC、Web声明；4.1–4.3 | E4/E6/E8 证明对应轮次最终逐条正确；全部异常结果和恢复用例的最终回执仍须绑定；共享取消允许逻辑cancelled保留已验证成功事实 |
| [event-subscription-management](specs/event-subscription-management/spec.md)：保存不自动开启、平台/入口权限和生效、被动来源所有权、适配器内普通用户呈现 | management/schema/registry、runtime/lease、Web/i18n；2.2、2.4–2.6、5.1、5.7 | E2/E3 提供部分生命周期/池/HTTP认证回执；当前候选UI、完整ACL/排空锁、开启/停止与文案交互待验 |
| [ingress-capacity-validation](specs/ingress-capacity-validation/spec.md)：B0、同资源、分阶段成本、正式两来源5/60/600、管理/长期责任、准确故障、B3前置 | 测量清单与双oracle、资源/管理/故障探针；3.6、4.1、5.2–5.8、6.1–6.3 | B0/B2路线依据已记录；Rabbit两轮正式接管FAIL，r2管理一致性FAIL；新HTTP小样本与诊断不外推；正式容量、故障及600秒观察仍未闭合 |

## 最终关闭前的必要收口

1. 对当前候选的 RabbitMQ 剩余收集窗口、受检 SDK 资源释放与分页修复完成必要复核；记录实际组大小和阶段成本，预取4不等于每组4条，诊断不作容量证据。
2. 新候选的两来源20/200条及双oracle完成后，分别运行正式10,000条；核对发布/首发窗口、接管60秒和排空600秒，保留首败及所有HTTP拒绝、重试和未知结果。
3. 与正式轮同步完成管理接口逐项p95、分页一致性、资源及队列观察，排空后完整600秒；r1的142.984秒、r2的≤2.065秒均不得补写或累加成10分钟。
4. 完成约定目标中断60秒/1,000条，以及精确故障点20条轮次，核对自动恢复、无重复有效效果、无重叠 active Attempt、无重复释放。
5. 完成适配器内真实UI流程和权限验证。中英文 HTTP 指南现已说明共享 intake 8连接、管理/恢复4连接及 MicroT1/业务批次边界；仍须完成最终文档和实际操作核对，不能据文案存在宣布 UI 通过。
6. 最终规格/文档、OCR覆盖与终态、修复及复审全部绑定最终候选。必要工作未完成时不归档；完成后先通知用户，停在提交PR之前。后续PR四项CI、main集成、部署和用户验收仍是独立门禁。
