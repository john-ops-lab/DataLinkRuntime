# B1–B5 需求与证据覆盖稿

**状态：实施与验收进行中，尚不能归档或提交 PR。** 本稿用于定位已实现路径、已经发生的验证以及剩余门禁，不代替 [tasks.md](tasks.md) 的完成判定。本文未修改任务勾选，不以测试文件存在、代码可构建或 OpenSpec 校验通过宣布功能或容量通过。

记录日期：2026-10-26。最新已测 HTTP 与调度候选为 `e01d8555d0d02428381d84553144367ca808533e`；RabbitMQ r6 的正常容量与完整观察绑定 `d38904f200cbc525019f663738253a5df0e374a6`。后续文档提交不改写运行所绑定的源码 SHA。E0–E17 保留早期核对基线 `68900ca7eebd5275239de4bf93fa554642b8d66b` 及其以前候选的历史证据，各轮首败继续保留，不能将不同候选拼成一次最终通过。RabbitMQ r6、约定七个准确故障切点及 e01 三项真实调度 SLA 已取得各自范围的通过证据；HTTP 正式容量及完整观察仍待取得最终结果。

E36–E42 分别补齐七项既有数据库回归、真实 Worker 负结果、两个合同缺口、执行池关闭拒绝、HTTP 审查补强、提交后唤醒与三项真实调度 SLA。原生工作名额早拒绝和新接管提交后唤醒已在 e01 部署，HTTP20/200 双 oracle 通过；不能借小样本或 RabbitMQ 的容量结果宣布 HTTP 达标。完整 Kimi 覆盖及复审、完整 UI 创建/取消等流程回执和正式 HTTP 结果仍待收口，任务不勾完、规格不归档，B1–B5 不作整体完成判定。

最新 HTTP 正式准备中的来源就绪预检曾在 producer 启动前失败：本轮实际接管为 0、资源采样未启动，来源已正常停用，原预检失败回执保留。这是准备阶段未满足就绪门禁，不是容量失败；后续须使用新独立运行清单，不改首次发送后的计时门槛，也不预填后续结果。

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


| E14 | [B4 恢复记录](b4-recovery-results.md)，候选 `0f8cf7a28ce7332aca87695a4e8a1548e91090c4` | 目标真实关闭至少 60.000263 s；1,000 条两独立 oracle 通过、17 个 Execution 成功、11 次真实连接失败后自动恢复；恢复后完整责任核验上界 25.192795 s，无人工重放/跳过。桌面容量暂停及处理中停止接收后原任务成功也已实际验证 | 不代替正常容量或七个准确退出切点；390 px 原工作台布局拥挤不称移动端通过；扩大 Rabbit 接收组后的受影响边界仍须复核 |
| E15 | 第三轮 RabbitMQ 10,000 条（r3），候选同 E14；原始管理/资源及两独立 oracle 已核验 | 全 10,000 条逐项正确；636 次管理请求均正确，218 个资源样本无采样错误/OOM/限流；持久事务时间戳推断 2,504 个接收组，约 99.52% 满 4 条 | **接管仍失败**：128.382084–129.409112 s；接管后完成上界 62.034332 s，post 管理样本 0、未满 600 s。正式 r2 原已平均约 3.375 条，不能拿独立 200 条诊断的 2.22 代替正式组数或收益；PG wall-clock 有负差，不用于阶段耗时 |
| E16 | HTTP 工具 `671ca842f16608096d0438c31c2c154d6ef29965`，新客户端同预算 loopback 及真实 r2 分别留存 | 0.5 CPU/512 MiB 的 loopback 10,000 条全首发 1.962145 s、全回应成功，无未知。真实链路已接管 1,497 条最终全成功，失败清单与期望 10,000 的两个失败 oracle 均保留、来源已关闭 | **真实突发链路失败**：首发 4,921 条、5,079 条未发；59,300 次 POST、136 次未知、57,696 次拒绝，客户端仅确认 1,468 条。发压约 0–60 s 管理 p95 约 760–921 ms；loopback 资格不等于产品容量，不能缩期望或重置 T0 |
| E17 | Rabbit16 候选的针对性本机回执，实际内容哈希另封私有账本 | HTTP 仍最多 4 条；Rabbit 最多 16 条但 1 MiB/2 MiB owner/64 MiB global、逐项门禁和确认不变。真实 PG Unit19+micro7 均首轮通过；纯 SDK/Mock71 通过（含独立 reader12）。修复预取余量/合法前缀进展、计时与未知关闭上抛；静态及 OpenSpec 检查通过 | 这是代码与针对性验证证据；本稿不把尚未发生的新 20/200、真实 Broker、一万条或600 s观察标成通过，最终候选 SHA 由提交时固定 |

E2 的核心文件 SHA-256 为 `cf4a2c134ee6217d08256333b46d0ce2a1522857c5118c2d682b22108cf65d4e`，测试文件为 `7e03b43d1e60520481d862aa090e61b3a4bbd821e536d80b87030f446cb7af48`。该回执绑定当时工作区内容；执行开始时未另取完整 HEAD，不冒充最终候选的精确 HEAD 审查。E2/E3 套件可能覆盖相同依赖，不能简单相加为独立覆盖数量。

本稿的测试名称只标示验证入口。没有关联已核实回执的项目，仍须按影响范围核对结果；不从函数名推断它已经运行或通过。完整私有回执与原始观测由主 Agent 保留，本文不复制实际接收地址、凭据或本机目录。

## 实现与验证位置索引

下列均为仓库相对位置。

- **HTTP**：`backend/src/dlr/control/api/ingress.py`，`services/http_ingress.py`、`http_ingress_group.py`、`subscription_http.py`；验证入口为 `test_issue222_http_ingress.py`、`test_issue222_http_groups.py`、`test_http_ingress_limits.py`、`test_issue222_http_admission.py`、`test_issue222_http_commit_notification.py`。
- **容量、绑定与账本**：`services/ingress_capacity.py`、`subscription_activation_policy.py`、`subscription_accounting.py`、`subscription_delivery.py`、`subscription_sync.py`；验证入口为 `test_ingress_capacity*.py`、`test_issue222_capacity_queries.py`、`test_issue222_queue_capacity_profile.py`、`test_issue222_activation_policy.py`、`test_issue222_input_ledger.py`、`test_issue222_ingress_upgrade.py`。
- **推进与发布**：`services/subscription_scheduler.py`、`subscription_runtime.py`、`ingress_database.py`、`outbox.py`；验证入口为 `test_issue222_scheduler.py`、`test_issue222_intake_pool.py` 及相关既有运行器回归。
- **业务批次**：`models/subscription_business_batch.py`，`services/subscription_business_batch.py`、`subscription_batch_protocol.py`、`subscription_batch_retention.py`、`subscription_batch_view.py`，以及 `attempt.py`、`reliable_execution.py`、`execution.py`；验证入口为 `test_issue222_business_batch.py`、`test_issue222_batch_protocol.py`、`test_issue222_batch_lifecycle.py`、`test_issue222_batch_declarations.py`、`test_issue222_batch_guard_json.py`。
- **T1 合并**：`services/subscription_t1_unit.py`、HTTP 收集/共享池、`subscription_rabbitmq.py`、`subscription_rabbit_budget.py`、`subscription_rabbit_metadata.py`；验证入口为 `test_issue222_t1_unit.py`、HTTP groups、pool、`test_issue222_rabbit_native_budget.py`、`test_issue222_rabbit_micro_t1.py`、`test_issue222_rabbit16_reader.py`。MicroT1 按来源分限：HTTP 最多 4 条、RabbitMQ 最多 16 条；原文加完整事件仍最多 1 MiB/额外收集等待 5 ms，保留逐条责任与提交后确认；T2 的业务批次上限为 100 条/完整输入 256 KiB，二者不等同。
- **迁移**：`backend/alembic/versions/0051_issue222_durable_http.py`、`0052_issue222_business_batches.py`、`0053_issue222_batch_guard_json.py`；旧单条关联唯一约束保留。后续优化不能以修改已发布迁移取代前向迁移。
- **界面**：`web/src/components/HttpSubscriptionConfig.tsx`、`EventBatchModeFields.tsx`、`EventBatchCodeDeclaration.tsx`、`SubscriptionWorkspace.tsx`、`ExecutionBatchNotice.tsx`，相关中英文 locale、schema/registry/management/ACL；验证入口为 `HttpSubscriptions.test.tsx`、`BusinessBatchSubscriptions.test.tsx`、`EventBatchCodeDeclaration.test.tsx` 及 App 测试。
- **独立夹具**：`tools/ingress/workload.py`、`target.sql`、`reference_adapter.py`、`management_probe.py`、`fault_probe.py` 及其测试/说明；参考目标与平台账本分别核验。工具存在、synthetic 自检或故障点配置不等于真实故障轮次发生。

## Tasks 2.1–6.3 覆盖表

| 任务 | 实现与必要验证映射 | 已发生的证据 | 当前缺口与判定 |
| --- | --- | --- | --- |
| 2.1 HTTP 登记、路由与升级 | HTTP source/schema/registry、`HttpIngressRoute`、0051–0053；旧 pending/failed/control 数据升级、不可复用标记和降级保护 | E1 记录旧责任升级范围；E36 的三个明确 nodeid 已实跑升级和原计数增量迁移，E38 验证保存路由、reset、克隆及删除重建的不同身份 | 这些明确路径已有逐项绑定；不据此宣布全部路由/回退与 B1 完成，最终候选审查仍须归集 |
| 2.2 当前 token、安全查询与 ACL | HTTP `_authenticated` / `receipt_status`、管理 API ACL；错/撤销 token、跨来源、旧地址、删除/重建、原 Webhook 两模式 | E1 的旧候选功能边界；E3 当前 HTTP 定向回执 | 原 Webhook 两模式及管理 ACL 的适用最终回执须逐项归集，当前用户流程仍需 UI 验证 |
| 2.3 持久接管与固定输入 | HTTP `receive` / `receive_group`、T1Unit、Receipt/Delivery/Activation、原始 bytes SHA；提交前后故障和重复 | E2/E3 的原文、202/200/409 与计费回归；E27 四个真实 T1 切点；E40 的提交前后未知 PG；E41 的 e01 HTTP20/200 双 oracle | 明确功能及恢复路径已有实证；HTTP 正式 R5 尚未结束，不用 E4/E8 早期最终正确覆盖其接管超时 |
| 2.4 生命周期及运行锁 | `subscription_management`、runtime/lease、平台错误分类、冻结策略、replay/repair/skip | E2 的 stop/binding/租约回滚；E3 runtime/pool；E27 真实 T1 恢复，E39 原生提交已关闭时的503/释放，E42 重建 Control 后按持久状态正常推进 | 旧 E21 生命周期失败保留；不同 SHA 的通过按受影响路径核对，3 项历史 skip 不倒推为通过 |
| 2.5 部署容量与管理边界 | `ingress_capacity`、fingerprint/audit、管理 limits；无效配置、并发采用、下调保留、入口排空 | E2 的 profile/锁定范围；E36 原计数正常及已有137余量时只追加迁移增量；E38 真实旧 owner 排空后切换 profile、下调保留原计数 | 公开配置说明与最终权限/未排空拒绝审查仍须归集；配置提高或下调保护不等于容量通过 |
| 2.6 资源隔离与拒绝释放 | HTTP 32 请求/8 MiB/8 原生工作；HTTP 与原生来源共享 intake 8 连接、管理/恢复另 4 连接，均无 overflow；64 MiB 来源父预算与磁盘迟滞 | E2/E3 逐项拒绝/释放；E17/E18 的16条受检 SDK 边界；E33 RabbitMQ 完整根资源与600秒观察；E39 的70项纯检查及 E40 的真实共享事务提交未知 | HTTP 正式轮次的同步资源和完整观察仍待验证；历史 E9 管理一致性失败保留，不以旧轮峰值替代最终受影响路径 |
| 2.7 完整计费、T2 与留存 | Receipt 原文/元数据、Delivery 未来输入/批次份额、控制预留、30 天整组 GC；原子 T2、回滚、审计 | E1/E2；E36 真实 PG 核验 7/30 天输入保留、重放新增副本计费、skip 后 TTL 释放、未处置失败不失效、重叠重放各自份额 GC；E37 核验终态共享 Admission 释放而输入/manifest/失败预算仍保留 | E36 按用例推进终态时间，不是连续 30 天观察；E37 的终态账本不证明历史释放写入次数。其余适用的计费/恢复及最终审查继续按影响范围归集 |
| 2.8 两来源 20 条链路与本批审查 | `workload`、真实 Worker/目标、平台与目标独立 oracle、HTTP/事件文档 | E1 旧单条20条；E18 16条Rabbit路线20/200双oracle；E41 的 e01 HTTP20/200双oracle | 各结果绑定实际候选及范围，不能拼成同一 SHA 的全量通过；最终文档/OCR仍未完，小样本不等于容量验收 |
| 3.1 依据实测选择优化 | B0/B1/B2 结果、调度/Outbox、容量重复读、0053 JSON 一次展开、D7.1 | E0/E1；E4/E8 两次正式接管失败；E7 为后续选择提供组大小事实 | 最终差异审查/OCR待完成；不得把旧瓶颈假设或诊断含观测开销的耗时当作收益 |
| 3.2 有界公平推进 | scheduler 的 pending/processing 分离、轮转、轻量头部、补偿通知；10 入口/阻塞页/重启场景 | E42：e01 的 ten、实际失败头+150pending、真实丢10次结果通知三轮；两类持久推进上界均≤2秒，双oracle/Broker0通过 | D389 R3 的2.0318–2.0449秒首败保留；e01 修复后重新测三轮，不复用旧ten。该SLA不外推任意入口数或容量 |
| 3.3 受限发布与确认 | `outbox` 的线程归属连接/channel 复用、确认窗口、return/nack/超时/丢连接；逐行未知恢复 | E1 发布链路与代表性确认测试；E28 真实Outbox确认后退出新轮通过，E31/E34跨Worker恢复时核对原Outbox及历史fencing | 该精确切点已实测，不能扩称全部Broker故障组合；最终源码与适用部分确认/超时回执仍按影响归集 |
| 3.4 单条完整链路比较 | 不变沙箱、执行策略/凭据快照、准备成本与参考 Adapter | E1：B0→B2 200 条比较及逐项 oracle，触发 B3 | 比较支持路线选择，不是最终批次吞吐结论；当前候选的必要回归/OCR待完成 |
| 3.5 参考目标有界恢复 | `reference_adapter`、单条/批次重试白名单、共享 deadline/额外重试预算、独立成员事务 | E14 已真实中断目标60秒并自动完成1000条；E34 目标提交后结果丢失由稳定身份恢复，无重复有效写入 | 两轮均绑定其冻结候选与参考目标；权限/重试预算的适用回执继续核对，不宣称任意Adapter自动恢复 |
| 3.6 B2 路线裁决 | `b1-b2-results`、D9、`b3-contract` 与业务批次 delta | E1 已用完整 200 条真实积压窗口触发 B3 | 路线依据已记录；最终代码/OCR和 B4 门禁未完成，不以分位数倒数裁决容量 |
| 3.7 T1 接管合并 | T1Unit、async HTTP collector、共享 intake 8 连接、Rabbit header 前置预算/最多16 credit、首条工作区保护与受限剩余收集窗口 | E2/E3/E7 支持原合并边界；E18/E25 是后续16条路线实测，E30为启动限流/年龄查询回归，E33为最新RabbitMQ正式容量 | 早期r2失败不改写；HTTP正式容量及完整OCR尚未闭合，不以prefetch16推定每组满16或HTTP也达标 |
| 4.1 B3 专项合同 | [b3-contract](b3-contract.md)、business-event-batch delta、D9；精确 Activation/override、结果/取消/留存规则 | E1 的触发证据；专项合同及 delta 已存在且可核对 | 最终合同与代码/文档的审查仍须收齐；没有用户界面声明或整批进程成功替代成员证明的例外 |
| 4.2 有界执行与逐条结算 | Batch/Member 模型、0052/0053、strict finish、Slot/Attempt、replay/skip、整体 GC | E33 正常10000条、E34 Worker恢复；E36 重叠重放份额GC；E37 三种真实Worker负结果和独立目标效果；E42 两类调度SLA | 明确三种负结果已实测，不扩称全部取消/异常矩阵；显式unknown不是网络提交未知。HTTP容量和完整OCR仍未完成，不整项打勾 |
| 4.3 批次说明与比较 | 中英文业务批次指南、版本 marker、输入/结果 schema、逐条投影与技术详情 | E4/E6/E8 的逐项正确结果；中英文 HTTP 指南已区分 MicroT1 与业务批次 | 最终前后对照、UI 实际操作及 OCR 未完；文档更新不代表用户流程或容量通过 |
| 5.1 界面与原操作习惯 | HTTP来源抽屉、处理方式及代码声明、接收记录、共享执行提示、中英文资源 | E14 桌面容量暂停、处理中停止接收后原责任排空已真实验证；相应组件定向检查已有记录 | 完整创建/取消/保存/明确开启、权限及中英文交互/视觉回执仍待归集；既有390px拥挤布局不称移动端通过 |
| 5.2 正式夹具与预冻结 | `workload` / target / management probe、容量 profile、原文/业务哈希、空闲接口基线 | E0/E1 方法、E23 同预算发生器资格；E33 正式RabbitMQ完整清单；e01 HTTP20/200及后续正式轮次的独立冻结 | 正式HTTP结果尚未产生，不提前填成功；实际profile/输入账单/资源/空闲p95均绑定各轮清单，不运行中加额 |
| 5.3 两来源各一万条 | 独立目标与平台 oracle；5/60/600 秒分别核验 | E4/E8/E15/E18/E25 保留历次Rabbit接管失败；E33 D389 r6 正式10000条通过44.867576秒接管/167.047143秒接管后完成及双oracle | RabbitMQ该轮已通过；e01 HTTP正式结果仍待取得，不能代用Rabbit结果。后继差异按路径核对，不跨SHA拼一次最终通过 |
| 5.4 管理面、资源及10分钟 | management/resource probe、根 cgroup、并发/队列/账本、排空后600 s | E33 的2502次正确管理请求、每接口601个post样本、839个完整根资源样本及完整600秒观察通过 | 早期142.984秒、≤2.065秒及分页一致性首败保留，未跨轮累加；HTTP正式轮次管理/资源和完整观察仍待验证 |
| 5.5 目标中断60秒 | 独立 RabbitMQ1,000 条故障轮次；失败/恢复/逐项幂等、无人工处置 | E14：参考目标真实关闭至少60.000263秒、11次真实连接失败，1000条双oracle通过，恢复后完整责任核验上界25.192795秒 | 约定参考目标范围已有通过；不替代正常容量或任意Adapter恢复，最终候选变化仍须按影响核验 |
| 5.6 精确故障点 | HTTP/Rabbit T1 前后、代表性 Outbox、Claim/journal、目标提交后回报前；每点20条 | E27 四个HTTP/Rabbit T1切点、E28 Outbox、E31 Claim/journal、E34 目标提交后结果前，七个独立20条准确切点均有真实通过 | 各自candidate/切面/120与600秒/最终双oracle均保留；E21/E28/E32旧harness或生命周期失败不改写，后继HTTP变化做影响核对 |
| 5.7 背压、权限与旧来源 | 容量/磁盘、身份/绑定、管理锁、旧来源 ACK/开关、K8s 完成与放弃 | E1/E2/E3；E36 七个逐项PG含旧pending/failed/Kubernetes控制责任与计费升级；E38路由/profile合同；E39有界HTTP拒绝释放 | 这些是明确路径证据，不是所有旧来源SDK/权限/开关全矩阵；旧50/179聚合selectors与skip原因不倒推 |
| 5.8 公开报告与复审 | 本稿、测量报告、中英文指南、运维/故障说明 | 本稿分列已通过Rabbit容量/七故障/SLA与所有首败，中英文指南同步有界资源和持久配额幂等读回边界 | HTTP正式结果、完整UI及最终OCR/复审仍待完成；文档存在不将未完成门槛改成通过 |
| 6.1 需求逐项闭合 | 本覆盖表、五能力 delta、Issue 验收与实际证据 | 本稿已映射E14、E27/E28/E31/E34、E33及E36–E42的实际范围 | 仍为draft：HTTP正式容量/完整观察、完整UI及最终审查未闭合，不申请Issue完结；不再否认已取得的Rabbit/故障/SLA证据 |
| 6.2 规格/文档与检查 | active change、HTTP/业务批次指南、配置/迁移/回退、必要静态检查 | 已登记静态检查与报告；本次仅澄清D3/D6、HTTP delta、双语说明及本覆盖表，保留数据/迁移/回退合同 | 文档与OpenSpec校验不替代业务验收；正式规格同步与归档仍待全部必要门禁完成 |
| 6.3 最终审查与交付 | AGENTS规定的 OCR preview/coverage/终态、修复验证、精确 base/head | 固定OCR1.13.3/Kimi的逐scope审查与已核实修复持续归集；E22/E29 的partial、预算及过滤边界保留 | 尚无覆盖最终全部差异且无未决实质问题的完整回执；最终manifest/复审、干净候选通过后通知用户，停在PR前，不推送或自动合并 |

## 五能力 delta 对照

| 能力与 Requirement 范围 | 实现/任务映射 | 当前证据边界 |
| --- | --- | --- |
| [durable-http-ingress](specs/durable-http-ingress/spec.md)：旧 Webhook区分、持久身份/状态、原始正文、固定绑定、入口资源、安全重试、当前 token/路由留存、HTTP 独立合并错误 | HTTP、容量/绑定、T1 合并；2.1–2.8、3.7、5.1 | E27真实T1切点、E38路由、E39/E40/E41拒绝/取消/未知提交及新hint支持明确范围；完整UI/ACL与HTTP正式容量待最终核验，幂等读回仍受前置有限请求资源约束 |
| [event-delivery](specs/event-delivery/spec.md)：稳定首次输入、各层容量/责任释放、两类有界推进、发布确认、参考目标恢复、T1逐条责任/确认 | 账本、scheduler/outbox、reference Adapter、T1Unit；2.3–2.7、3.1–3.7、5.5–5.7 | E33 RabbitMQ容量与资源、E27/E28/E31/E34准确故障、E36保留账本、E42两类调度SLA已分列证明；E4/E8等接管首败保留，不扩称全矩阵 |
| [business-event-batch](specs/business-event-batch/spec.md)：显式选择/固定绑定、原子成员集合、strict结果、逐成员处置/共享取消、共享副本保护 | Batch/Member、0052/0053、finish/Admission/Slot、replay/skip/GC、Web声明；4.1–4.3 | E33/E34 支持对应正常与恢复轮次；E36 支持重叠重放份额 GC；E37 支持真实 Worker 三种负结果、目标效果与共享 Admission/保留预算分层结算，不扩大为全部异常或取消矩阵通过 |
| [event-subscription-management](specs/event-subscription-management/spec.md)：保存不自动开启、平台/入口权限和生效、被动来源所有权、适配器内普通用户呈现 | management/schema/registry、runtime/lease、Web/i18n；2.2、2.4–2.6、5.1、5.7 | E2/E3提供生命周期/池/认证，E14桌面容量/停止排空、E38路由/profile合同已实际验证；完整创建/取消、ACL及最终双语UI回执仍待归集 |
| [ingress-capacity-validation](specs/ingress-capacity-validation/spec.md)：B0、同资源、分阶段成本、正式两来源5/60/600、管理/长期责任、准确故障、B3前置 | 测量清单与双oracle、资源/管理/故障探针；3.6、4.1、5.2–5.8、6.1–6.3 | B0/B2路线已记录；历史Rabbit接管及分页首败保留，E33 r6容量/600秒和七准确切点已通过各自范围；HTTP正式容量、完整UI/最终审查仍未闭合 |

## 最终关闭前的必要收口

1. RabbitMQ 当前受检路线是最多16 credit，不再以早期4条候选描述现状；E33 已完成真实一万条及完整600秒观察。保留历次失败，按最终差异核对该来源证据的适用资格，不以prefetch上限推定每组满16或按诊断外推吞吐。
2. e01 的 HTTP20/200 双oracle及三项真实调度SLA已通过；正式 HTTP 结果仍待取得，须核对一万条原期望、5/60/600秒及全部拒绝/重试/未知结果核验，不能用小样本或 RabbitMQ 通过替代。
3. E33 的管理p95/分页一致性/根cgroup资源与排空后600秒已有完整证据；HTTP仍须等本轮同步门禁结束。早期142.984秒、≤2.065秒不补写或跨轮累加，已完成的RabbitMQ观察也不再写作未测。
4. E14 的1000条目标中断60秒及E27/E28/E31/E34七个精确20条切点已有各自冻结候选通过；后继改动只按影响复核，保留准确切面、自动恢复、逐条效果、Attempt/fencing/释放边界及所有首败，不能拼成同一SHA的一轮。
5. 完成适配器内完整创建/取消/保存/明确开启等UI与权限回执。容量暂停及停止排空的已有桌面验证与尚缺流程分别记，不据构建或文案宣布全部UI通过。
6. 最终规格/文档、Kimi逐文件覆盖与终态、修复及复审仍绑定最终候选；partial/过滤/预算边界不能忽略。必要工作未完成不归档、不勾完tasks；全部通过后先通知用户，停在提交PR之前。后续PR四项CI、main集成、部署和用户验收仍是独立门禁。

## 2026-10-26 后续核验补记

本节覆盖此前表内的待验事实，不将不同候选合成最终通过；前面的旧轮首败继续保留。

| 编号 | 实际来源 | 已证实结果及边界 |
| --- | --- | --- |
| E18 | RabbitMQ r4，`d6236585a0b1f496fd92155abccee7e635a1410e` | 新16条路线20/200双oracle通过；正式一万条双oracle通过，接管72.215200–73.303734秒，仍FAIL。702管理请求均正确，p95约102/154/120ms；238资源样本无错误/OOM/限流，Control/Worker根峰值319.90/130.85MiB。post管理样本0，不能判600秒观察通过。 |
| E19 | HTTP ASGI候选 `553774929758c3d6583c40db1f3e92f6c87129ab` | 27纯定向检查与20/200真实双oracle通过；正式r3仍失败：首发8235/10000，1765未发，99988次POST，客户端确认2342、实际2371全部业务成功。125次未知；原10000期望两个oracle均失败，未完成600秒观察，不补发或缩小期望。 |
| E20 | 速率等待候选 `4b5ef50fd595151ad0d6cdf3e6e3eca4b53c855c` | 40项针对性验证通过，含真实PG来源/全局余10的10+6及竞争只确认2条前缀；真实Broker20/200双oracle通过。1000诊断仅准备，因部署配置须冻结而在producer前恢复正常Control，未发布输入；尚不称新一万条通过。 |
| E21 | 精确HTTP提交前退出r1，候选同E19 | 真正切点、同CID恢复、20条目标/平台/Broker责任对账成立，保守完整责任上界12.173647秒；但旧被动租约使停用持续stopping，本地owner0，整轮生命周期门禁FAIL。旧数据库/回执保留，不把业务恢复成功改成整轮通过。HTTP逻辑关闭修复已针对性核验，新真实故障待重验。 |
| E22 | Kimi OCR1.13.3 / `k3.1-pro` 首轮，base `1158e413e55d92127d615d666c5d4e4bcc15514f` → E18 head | 149项选中，manifest仅27完成、122预算失败，终态partial，不能按摘要files_reviewed=149称全审通过。另requirements-http.txt未获逐文件完成证据，扩展不支持的边界仍须单独核实；被模型间接读取不等于manifest覆盖。确认磁盘路径、HTTP关闭能力投影及深层无效批次结果三个问题；相关修复与最终覆盖/复审仍是必要门禁。 |
| E23 | 冻结候选 `c6bc1952a13c7d367fbf8c5ee45c3601d93b4529` 的真实发生器资格预检 | 相同0.5 CPU/512MiB下，loopback完整读取10000条正文并响应，首发2.018958秒；真实PrivateLog的10000次逐条编码/写入/flush及结果文件写入、哈希读回均执行。不是fsync承诺，也不是产品容量通过。 |
| E24 | E23候选的容量盘实际挂载读回 | Control默认容量路径与PG实际named volume为同一卷，Control只读、PG原读写；默认路径存在且statvfs可读。本轮已有私有显式路径与默认路径也在同一容量域，未静默改写私有配置。 |
| E25 | E23候选的RabbitMQ 1000条诊断及r5正式10000条 | 1000条双oracle通过；有观察器，未进入rate等待函数，不能声称验证了该等待收益。正式r5真实prefetch16，接管64.289761–65.345895秒，仍FAIL；10000条目标/平台全对账通过，完成上界为接管后148.027431秒。705管理请求均正确，负载p95约82/126/95ms；239资源样本无采集错误。post仅1组，未满600秒。 |
| E26 | E23候选HTTP20/200及r4正式10000条 | 小样本双oracle通过。r4正式首发9352/10000，648未发；29806次POST，客户端确认4053，实际4073全部业务成功，25次未知。原10000期望的目标/平台oracle均失败；所有实际责任排空后协作停止观察、正常停源，未补发、未缩小期望、未判600秒观察通过。 |
| E27 | E23候选四个真实T1故障切点，各20条 | HTTP/RabbitMQ提交前、提交后返回或ACK前均实际退出137、保持停机读取切面，再同CID恢复；完整责任保守上界依次79.690692/33.424353/36.813428/34.892735秒。目标/平台/Broker对账、原120/600边界及正常停源恢复通过；RabbitMQ另核真实redelivered、原文哈希、重建/复用接收记录与ACK。未重置起点。 |
| E28 | E23候选Outbox真实确认后退出，各20条的新轮与首败 | r1真实Basic.Ack切点成立，但私有cut DTO未转换UUID，输出截断；另存仅UUID修正的只读切面后恢复并完成20条双oracle，原轮仍记harness FAIL。新v2工具保留旧证据、完整编码后原子无覆盖输出；全新r2通过实际切点、双oracle、历史fencing/无重叠/admission、Broker0、正常停源恢复，完整责任保守上界64.999101秒。 |
| E29 | E23候选的Kimi小范围复审，OCR1.13.3 / `k3.1-pro` | 3项选中，manifest2完成、1预算失败，终态partial；另有grace未完成warning与一项completed状态存在张力，不作无保留通过。确认启动前rate0仍退避及真实速率测试依赖现实1秒窗口两项问题；两项修复及受影响验证见E30，最终完整覆盖仍待闭合，不把旧头或摘要计数拼成最终全审。 |
| E30 | 启动限流、受控测试时钟及待处理年龄查询修复 | 四个相关测试文件共55项通过，包含真实PG速率窗口、精确五分钟边界、跨订阅、终态及真实replay；独立测试库已清除。纯检查、Ruff及queue严格mypy通过，另有只读代码复核。未改变T1/ACK/资源上限，无新增索引；该节点之后的新正式容量结果见E33，Kimi完整复审仍待闭合；不能用旧一万条结果宣布另一个候选达标。 |
| E31 | E23候选Worker Claim/journal写盘后退出，20条 | 真实Agent/UID、原两槽和两个SDK消费者、实际journal文件及目录fsync切点成立；保持Worker退出137时读取真实journal、平台与独立目标切面，再同CID恢复。历史Attempt/fencing/admission、目标/平台/Broker对账及正常Worker恢复通过，完整责任保守上界74.822318秒，Control CID未变。 |
| E32 | E23候选目标提交后、结果RPC前退出r1及诊断工具修复 | 独立Target确认实际batch已提交，Worker实际退出137，但诊断SHA遗漏落盘追加的newline，原轮记harness FAIL。保留原文件，另存精确匹配原marker的canonical副本，完成切面核验后同CID恢复；20条目标/平台双oracle与Broker0成立，正常停源恢复，不改写原时限。工具修复仅将newline计入实际文件SHA，30项本机工具/SQLite子进程测试通过；新真实故障轮次待验。 |
| E33 | `d38904f200cbc525019f663738253a5df0e374a6` 的 RabbitMQ r6，正式10000条 | 真实prefetch16；同发生器时钟的接管上界44.867576秒，接管后完成上界167.047143秒，目标与平台10000条全对账通过。2502个管理请求正确、负载p95约86/121/92ms，各接口排空后601个样本，完整600秒观察通过。839个完整根cgroup资源样本无错误/OOM/限流，Control/Worker内存高水位324.484/137.246MiB，低于1/2GiB上限；末段进程RSS进入平台期，不外推长期无泄漏。source按版本正常disabled/stopped，源与内部队列均排空；RabbitMQ本轮容量通过，不代替HTTP。 |
| E34 | `d38904f200cbc525019f663738253a5df0e374a6` 的目标提交后、结果RPC前退出r2，20条 | 新实际文件SHA包含newline并精确匹配marker；真实Agent/journal/独立Target提交切面及Worker退出137均成立。读取切面后同CID恢复、旧Attempt worker_lost后由同Execution新Attempt完成；目标/平台/历史fencing/共享Admission/Broker与正常Worker恢复门禁通过，完整责任保守上界75.702595秒，无重复有效写入。Control CID未变，原120/600起点不改写。 |
| E35 | `d38904f200cbc525019f663738253a5df0e374a6` 的 HTTP1000条CPU诊断r1 | 产品1000条目标/平台双oracle通过，全部成功观测上界48.735911秒；发送方无unknown，普通Control配置已恢复。私有cProfile记录受Python3.13多线程机制影响，出现94次观测错误及不可能的CPU时长，诊断记录无效；不使用坏排行归因、不作为正式容量结果，原记录保留。 |
| E36 | `b1-closeout-7`：既有七个 selectors 的独立真实 PG 补证 | 7 passed、0 skip，JUnit 合计 11.998 秒；实际 nodeid 与下列七项逐项一致，运行前后源码集/测试哈希不变，独立主测试库及迁移临时库均正常清理。覆盖旧 pending/failed/Kubernetes 控制责任升级、原计数两种增量边界，以及输入/失败/重放的保留与 GC。是新的逐项证据，不倒推旧 50/179 聚合结果的 selectors 或 skip 原因，也不宣称 B1 全部完成。 |
| E37 | 普通候选 `d38904f200cbc525019f663738253a5df0e374a6`，`b222-negative-d389-r3` 三种真实 Worker 负结果 | 三个三成员批次均经过真实 Worker/沙箱，每组单 Attempt。部分失败、显式 unknown、错误 manifest 的独立 Target 效果分别为 1/2/1 条，逐 ID/业务哈希/值及 attempts 对账；Control 成员分别为 succeeded/failed/not_run、succeeded/unknown/not_run、全 unknown，错误 manifest 不抹去已发生的目标事实。事件 DTO、阻塞项与保留输入/manifest 一致；共享 Admission 已释放，输入/失败/历史预算保留，local/global quota audit 无差异。三个来源均正常停用至 stopped、本地 owner 归零、来源与内部 Broker 排空。显式 unknown 是代码保守报告，不冒称真实网络提交未知。r1 的命令事件参数冲突在产品写入前失败，r2 的稀疏计数读取在首个业务场景完成后失败；两轮 harness FAIL 及 r2 独立业务核验/正常停源证据均保留，全新 r3 才记三场景完整通过。 |

| E38 | `25da92ecaa883ada4a4e1d2e1662eee3f50d3c3b` 下两个明确合同 PG 补证 | 路由保存/reset/克隆/删除重建身份不同，及旧owner排空后profile切换/下调保留原计数，2 passed / 3.39秒；运行中源码/测试哈希不变，独立测试库已清理。不称完整ACL或升级矩阵。 |
| E39 | HTTP原生工作门禁及执行池关闭的局部回归 | 70项纯检查 / 3.06秒通过，含真实executor已关闭后submit抛错返回503、归还原工作/请求/字节预算，以及取消直到实际native结束才释放。纯检查不连PG或Broker，不替代正式HTTP负载；相同检查中已有路径不与其他行简单累加成独立覆盖数。 |
| E40 | OCR B3审查组的HTTP取消/timer/提交不明断言补强，后纳入e01 | 4项纯检查 / 0.65秒通过，核对取消真实未释放及timer不占native/不越收集截止；另2项真实PG / 2.78秒核对提交前失败、提交后结果不明均不提前返回成功。独立库已按大小写折叠核对清理。最初混合大小写库标识在setup阶段失败，发生在业务用例之前；原首败与清理回执保留，改全小写新库通过，无产品修改。不是业务批次全异常矩阵。 |
| E41 | `e01d8555d0d02428381d84553144367ca808533e` 的HTTP提交后唤醒与真实小样本 | 14项纯检查 / 0.05秒通过；只在已知commit成功且有202新项后提示一次，dup/GET/拒绝/失败或未知不提示，提示异常不改变已知响应或native预算。E40的2项真实提交未知PG针对新ingress文件哈希复核；不重复计数。HTTP20/200目标与平台独立oracle均通过，宿主启动至观察成功上界3.102369/7.219195秒，不当正式吞吐。 |
| E42 | e01 `b222-b2-sla-e01-r4` 三个独立新库真实调度轮次 | ten、真实失败头+150pending、丢失10次结果提示三轮，pending持久推进上界最大0.920662693/1.111773483/1.026526534秒，terminal确认上界最大0.240696539/0.188926412/0.985250738秒。29健康ID的独立Target/Control oracle、每轮Broker0、observer0错、正常停10源及恢复原资源/基线均通过；失败头与150pending留存。使用同Control单调时钟包围真实root commit，观察器有界元数据读取开销未扣除。D389先前ten独立通过与blocked R3首败2.031808743–2.044867208秒保留，e01未复用旧结果而重测三轮；不代替容量或完整OCR。 |

### E36 / E37 可复核索引

E36 实际运行的七个 nodeid：

1. `tests/test_issue222_ingress_upgrade.py::test_upgrade_preserves_pending_failed_and_kubernetes_control`
2. `tests/test_issue222_ingress_upgrade.py::test_migration_adds_old_execution_and_control_reserve_deltas_without_reset[0]`
3. `tests/test_issue222_ingress_upgrade.py::test_migration_adds_old_execution_and_control_reserve_deltas_without_reset[137]`
4. `tests/test_issue222_input_ledger.py::test_seven_day_cleanup_keeps_real_execution_input_and_thirty_day_clears_it`
5. `tests/test_issue222_input_ledger.py::test_replay_only_charges_new_execution_copy_and_skip_releases_unused_reserve_at_ttl`
6. `tests/test_issue222_input_ledger.py::test_unresolved_failure_never_expires_its_input_budget`
7. `tests/test_issue222_business_batch.py::test_overlapping_replay_groups_gc_their_own_shares_without_deleting_shared_receipt`

E36 完整 nodeid、命令、逐项结果及哈希索引 SHA-256 为 `d7bc391dbfca63b171ee68b45301d9a06988b0e80523f00f2571d0f0961f621d`，原始结果为 `0d38dd08d7386edced48eed0b21653afb1c0ce95f3a0b92b12000fa6abcfee1d`；本机运行源码集合哈希为 `f42e5142f1783a80f822d2d834b1335074870ec624c0319339b81f6f8dd5d95f`，不把含未部署改动的工作区冒称已部署候选。

E37 三场景收尾索引 SHA-256 为 `7d23826c87b8411509dc8d6a378f3df196b1f65f5eb2a2ae827f99d3ef61ced9`，原始完整 final 为 `e7d58a690af0762c5170ed23cb702c0c9838032f30d2ff173ebfa02004d9785c`；其逐场景的目标/平台 oracle、事件 DTO、Broker 和正常停止回执均由该索引固定。私有完整材料另行保留，本文只披露通用运行标识、哈希和结论；不复制私有路径、地址、凭据或账户。

运行前增加本机空间检查；不足20GiB暂停新增重型轮次。本轮已清理未引用的DLR旧镜像/相关构建缓存并回收稀疏虚拟盘空闲块，原运行容器、数据卷、密钥、首败证据和主目录保留。该宿主保护不改变平台容量profile或5/60/600秒验收标准。

### E38–E42 可复核索引

- E38 两个实际 nodeid 为 `tests/test_issue222_http_ingress.py::test_saved_http_routes_reset_clone_and_recreation_are_distinct`、`tests/test_ingress_capacity_database.py::test_profile_change_waits_for_old_owners_then_lower_budget_preserves_counters`。
- E40 的四项为 `test_cancelled_request_keeps_native_work_and_bytes_bounded`、`test_cancel_one_waiter_keeps_group_and_other_results`、`test_sparse_group_timer_never_waits_in_native_thread`、`test_delayed_timer_cannot_admit_a_request_after_collection_deadline`；提交未知两项为 `test_group_commit_loss_never_exposes_success_before_known_commit[False/True]`。它们是新的明确selectors，不倒推旧聚合回执。
- E41 绑定 `backend/src/dlr/control/api/ingress.py` SHA-256 `e56bb8d6136d0d432b14ddd5188be3b1d7fde18ce4b8f8a9e77fd9af78d18256`、`backend/tests/test_issue222_http_commit_notification.py` SHA-256 `2253467749619a0fc711c76a85f0b35ee408c1a997f0b3037148b904cbb3390b`。
- E42 完整三轮/恢复汇总 SHA-256 为 `7f969161690ead2c374cdcdd093c13513a10bc44389f2604c5e879a0ebfce0c9`，首次producer前scope为 `4a3492bdeb44f1bd44238dd8cf55ff6283c7109b910d75f385c55cd7d5aaef86`，收回原始观察器/请求/Target证据索引为 `3026f60c5a65af61d064392062c07f94d18869e466b19b5fd76232df3a254c71`。一次性PG测试库已按其回执清理；容量、故障及SLA证据库按保留策略留存，不能混称已全部删除。
