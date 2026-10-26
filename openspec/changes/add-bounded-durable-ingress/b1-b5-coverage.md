# B1–B5 需求与证据覆盖稿（2026-10-27）

**状态：实施与验收进行中，尚不能归档或提交 PR。** 本稿用于定位已实现路径、已经发生的验证以及剩余门禁，不代替 [tasks.md](tasks.md) 的完成判定。本文不修改任务勾选，不以测试文件存在、代码可构建或 OpenSpec 校验通过宣布功能或容量通过。

记录日期：2026-10-27。最新实测候选为 `d689792b8670af5da99104d2eac52c467d4ae8ec`。RabbitMQ 正式 r6 的容量与完整600秒观察仍绑定 `d38904f200cbc525019f663738253a5df0e374a6`，e01 的三项真实调度 SLA 保留历史记录；d689 的独立 SLA r6 三轮已通过，HTTP 正式 R7 仍失败（E50/E51）。E0–E42 保留各历史候选及首败，后续文档更新或小样本通过不改写其运行源码 SHA，也不能拼成一次最终通过。

e01 HTTP R5 是 producer 前、实际零接管的就绪预检失败；独立 R6 已实际发送并失败，原10000条期望的两个 oracle 均未通过，缺失6390条，未完成600秒观察（E43）。d689 旧 RabbitMQ 200条出现14个结果未知并保留首败（E48）；夹具修正后的 ns1 四个小样本另有双 oracle 通过（E49），不以此裁定原失败根因或宣布正式容量通过。

新增保护、时钟/字节边界、Kubernetes、批次投影/协议、SQL、HTTP16条/25ms及严格数据库拒绝断言的定向证据分列 E44–E47；各项绑定各自回执与源码，不相加成一个总通过数。截至冻结 d689 的161条变更路径，81条已有与当前内容绑定的审查链，80条仍缺覆盖，且已有审查链不等于全部意见已批准。Kimi 审查受服务额度阻塞，完整复审未完成；完整 UI 创建/取消/保存/明确开启流程仍缺回执，访问工具限制已使该次尝试停止。必要门禁未闭合，B1–B5 不作整体完成判定，不归档、不勾完任务、不提交 PR。

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
- **T1 合并**：`services/subscription_t1_unit.py`、HTTP 收集/共享池、`subscription_rabbitmq.py`、`subscription_rabbit_budget.py`、`subscription_rabbit_metadata.py`；验证入口为 `test_issue222_t1_unit.py`、HTTP groups、pool、`test_issue222_rabbit_native_budget.py`、`test_issue222_rabbit_micro_t1.py`、`test_issue222_rabbit16_reader.py`。MicroT1 当前最多16条/完整原文加事件1 MiB；HTTP 额外收集等待最多25 ms，RabbitMQ 仍最多5 ms，保留逐条责任与提交后确认；T2 的业务批次上限为 100 条/完整输入 256 KiB，二者不等同。
- **迁移**：`backend/alembic/versions/0051_issue222_durable_http.py`、`0052_issue222_business_batches.py`、`0053_issue222_batch_guard_json.py`、`0054_issue222_receipt_ttl_guard.py`；旧单条关联唯一约束保留，0054 前向修复空终态时间不能放行 TTL 清理。后续优化不能以修改已发布迁移取代前向迁移。
- **界面**：`web/src/components/HttpSubscriptionConfig.tsx`、`EventBatchModeFields.tsx`、`EventBatchCodeDeclaration.tsx`、`SubscriptionWorkspace.tsx`、`ExecutionBatchNotice.tsx`，相关中英文 locale、schema/registry/management/ACL；验证入口为 `HttpSubscriptions.test.tsx`、`BusinessBatchSubscriptions.test.tsx`、`EventBatchCodeDeclaration.test.tsx` 及 App 测试。
- **独立夹具**：`tools/ingress/workload.py`、`target.sql`、`reference_adapter.py`、`management_probe.py`、`fault_probe.py` 及其测试/说明；参考目标与平台账本分别核验。工具存在、synthetic 自检或故障点配置不等于真实故障轮次发生。

## Tasks 2.1–6.3 覆盖表

| 任务 | 实现与必要验证映射 | 已发生的证据 | 当前缺口与判定 |
| --- | --- | --- | --- |
| 2.1 HTTP 登记、路由与升级 | HTTP source/schema/registry、`HttpIngressRoute`、0051–0054；旧 pending/failed/control 数据升级、不可复用标记和降级保护 | E1/E36 的明确升级及原计数增量路径；E38 的路由身份边界；E44 的0054保护与升级代表回归 | 这些明确路径已有逐项绑定；不据此宣布全部路由/回退与 B1 完成，最终候选审查仍须归集 |
| 2.2 当前 token、安全查询与 ACL | HTTP `_authenticated` / `receipt_status`、管理 API ACL；错/撤销 token、跨来源、旧地址、删除/重建、原 Webhook 两模式 | E1 的旧候选功能边界；E3 当前 HTTP 定向回执 | 原 Webhook 两模式及管理 ACL 的适用最终回执须逐项归集，当前用户流程仍需 UI 验证 |
| 2.3 持久接管与固定输入 | HTTP `receive` / `receive_group`、T1Unit、Receipt/Delivery/Activation、原始 bytes SHA；提交前后故障和重复 | E2/E3 的原文、202/200/409 与计费回归；E27 四个真实 T1 切点；E40 的提交前后未知 PG；E41 的 e01 HTTP20/200 双 oracle | e01 R5 预检失败与 R6 正式失败分别见E43；d689 小样本及旧200条首败见E48/E49，均不替代HTTP正式容量 |
| 2.4 生命周期及运行锁 | `subscription_management`、runtime/lease、平台错误分类、冻结策略、replay/repair/skip | E2/E3 的 stop/binding/租约/运行器范围；E27真实T1恢复、E39关闭执行池拒绝、E42调度恢复；E45 Kubernetes平台错误分类定向回归 | E21旧生命周期失败保留；不同SHA的通过按受影响路径核对，不据定向回归宣布所有旧来源或历史skip通过 |
| 2.5 部署容量与管理边界 | `ingress_capacity`、fingerprint/audit、管理 limits；无效配置、并发采用、下调保留、入口排空 | E2 的 profile/锁定范围；E36 原计数正常及已有137余量时只追加迁移增量；E38 真实旧 owner 排空后切换 profile、下调保留原计数 | 公开配置说明与最终权限/未排空拒绝审查仍须归集；配置提高或下调保护不等于容量通过 |
| 2.6 资源隔离与拒绝释放 | HTTP 32 请求/8 MiB/8 原生工作；HTTP 与原生来源共享 intake 8 连接、管理/恢复另 4 连接，均无 overflow；64 MiB 来源父预算与磁盘迟滞 | E2/E3 逐项拒绝/释放；E17/E18 的16条受检 SDK 边界；E33 RabbitMQ 完整根资源与600秒观察；E39 的70项纯检查及 E40 的真实共享事务提交未知 | HTTP 正式轮次的同步资源和完整观察仍待验证；历史 E9 管理一致性失败保留，不以旧轮峰值替代最终受影响路径 |
| 2.7 完整计费、T2 与留存 | Receipt 原文/元数据、Delivery 未来输入/批次份额、控制预留、30 天整组 GC；原子 T2、回滚、审计 | E1/E2；E36的7/30天、重放新增副本、skip后TTL、失败保留及重叠GC；E37的Admission/保留预算分层；E44空terminal_at保护与升级代表 | 按用例推进终态时间不等于连续30天观察；各自计费/恢复回执不相加，最终审查仍未完成 |
| 2.8 两来源 20 条链路与本批审查 | `workload`、真实 Worker/目标、平台与目标独立 oracle、HTTP/事件文档 | E1旧单条20条；E18的Rabbit路线20/200；E41的e01 HTTP20/200；E48/E49的d689独立小样本 | 保留d689旧Rabbit200首败；新ns1小样本不能替代一万条、600秒观察或最终OCR，不跨候选拼成全部通过 |
| 3.1 依据实测选择优化 | B0/B1/B2 结果、调度/Outbox、容量重复读、0053 JSON 一次展开、D7.1 | E0/E1；E4/E8 两次正式接管失败；E7 为后续选择提供组大小事实 | 最终差异审查/OCR待完成；不得把旧瓶颈假设或诊断含观测开销的耗时当作收益 |
| 3.2 有界公平推进 | scheduler 的 pending/processing 分离、轮转、轻量头部、补偿通知；10 入口/阻塞页/重启场景 | E42：e01 的 ten、实际失败头+150pending、真实丢10次结果通知三轮；两类持久推进上界均≤2秒，双oracle/Broker0通过 | D389 R3首败保留；E42只证明e01三轮。E45的9项SQL PG不代替真实SLA；d689独立r6结果见E50 |
| 3.3 受限发布与确认 | `outbox` 的线程归属连接/channel 复用、确认窗口、return/nack/超时/丢连接；逐行未知恢复 | E1 发布链路与代表性确认测试；E28 真实Outbox确认后退出新轮通过，E31/E34跨Worker恢复时核对原Outbox及历史fencing | 该精确切点已实测，不能扩称全部Broker故障组合；最终源码与适用部分确认/超时回执仍按影响归集 |
| 3.4 单条完整链路比较 | 不变沙箱、执行策略/凭据快照、准备成本与参考 Adapter | E1：B0→B2 200 条比较及逐项 oracle，触发 B3 | 比较支持路线选择，不是最终批次吞吐结论；当前候选的必要回归/OCR待完成 |
| 3.5 参考目标有界恢复 | `reference_adapter`、单条/批次重试白名单、共享 deadline/额外重试预算、独立成员事务 | E14 已真实中断目标60秒并自动完成1000条；E34 目标提交后结果丢失由稳定身份恢复，无重复有效写入 | 两轮均绑定其冻结候选与参考目标；权限/重试预算的适用回执继续核对，不宣称任意Adapter自动恢复 |
| 3.6 B2 路线裁决 | `b1-b2-results`、D9、`b3-contract` 与业务批次 delta | E1 已用完整 200 条真实积压窗口触发 B3 | 路线依据已记录；最终代码/OCR和 B4 门禁未完成，不以分位数倒数裁决容量 |
| 3.7 T1 接管合并 | T1Unit、async HTTP collector、共享 intake 8 连接、Rabbit header 前置预算/最多16 credit、首条工作区保护与受限剩余收集窗口 | E2/E3/E7的原合并边界；E30启动限流/年龄查询；E33的D389 Rabbit正式容量；E44 T1时钟/字节、E47 HTTP16条/25ms定向验证 | HTTP正式容量未通过；d689旧Rabbit200失败及新ns1小样本分列E48/E49，不由prefetch/组上限推定容量 |
| 4.1 B3 专项合同 | [b3-contract](b3-contract.md)、business-event-batch delta、D9；精确 Activation/override、结果/取消/留存规则 | E1 的触发证据；专项合同及 delta 已存在且可核对 | 最终合同与代码/文档的审查仍须收齐；没有用户界面声明或整批进程成功替代成员证明的例外 |
| 4.2 有界执行与逐条结算 | Batch/Member 模型、0052/0053、strict finish、Slot/Attempt、replay/skip、整体 GC | E33/E34正常与恢复；E36共享GC；E37真实Worker负结果；E42两类SLA；E46投影/协议及严格数据库拒绝断言的独立回执 | 不扩称全部异常/取消矩阵；E48结果未知首败原因仍在诊断，HTTP容量和完整OCR未完成，不整项打勾 |
| 4.3 批次说明与比较 | 中英文业务批次指南、版本 marker、输入/结果 schema、逐条投影与技术详情 | E4/E6/E8 的逐项正确结果；中英文 HTTP 指南已区分 MicroT1 与业务批次 | 最终前后对照、UI 实际操作及 OCR 未完；文档更新不代表用户流程或容量通过 |
| 5.1 界面与原操作习惯 | HTTP来源抽屉、处理方式及代码声明、接收记录、共享执行提示、中英文资源 | E14 桌面容量暂停、处理中停止接收后原责任排空已真实验证；相应组件定向检查已有记录 | 完整创建/取消/保存/明确开启流程仍缺回执，访问工具限制后已停止该次尝试；不能据此判UI通过或产品失败，既有390px布局也不称移动端通过 |
| 5.2 正式夹具与预冻结 | `workload` / target / management probe、容量 profile、原文/业务哈希、空闲接口基线 | E0/E1方法、E23发生器资格、E33正式Rabbit清单；E43保留e01预检与正式首败，E49另核新ns1夹具 | 每轮profile、输入、资源和时钟绑定各自冻结清单；夹具资格或小样本不替代正式一万条，运行中不加额 |
| 5.3 两来源各一万条 | 独立目标与平台 oracle；5/60/600 秒分别核验 | 历次Rabbit接管首败保留；E33只证明D389正式r6；E43的e01 HTTP R6失败，E48/E49分列d689旧200首败与ns1后继小样本 | 尚无最终候选两来源各一万条完整通过；实际已接管项成功不缩小原期望，新小样本不覆盖旧首败 |
| 5.4 管理面、资源及10分钟 | management/resource probe、根 cgroup、并发/队列/账本、排空后600 s | E33 的2502次正确管理请求、每接口601个post样本、839个完整根资源样本及完整600秒观察通过 | 早期短观察及分页首败保留；E43管理压力p95约650–683ms不判过，HTTP仍缺正式同步管理/资源及完整600秒观察 |
| 5.5 目标中断60秒 | 独立 RabbitMQ1,000 条故障轮次；失败/恢复/逐项幂等、无人工处置 | E14：参考目标真实关闭至少60.000263秒、11次真实连接失败，1000条双oracle通过，恢复后完整责任核验上界25.192795秒 | 约定参考目标范围已有通过；不替代正常容量或任意Adapter恢复，最终候选变化仍须按影响核验 |
| 5.6 精确故障点 | HTTP/Rabbit T1 前后、代表性 Outbox、Claim/journal、目标提交后回报前；每点20条 | E27 四个HTTP/Rabbit T1切点、E28 Outbox、E31 Claim/journal、E34 目标提交后结果前，七个独立20条准确切点均有真实通过 | 各自candidate/切面/120与600秒/最终双oracle均保留；E21/E28/E32旧harness或生命周期失败不改写，后继HTTP变化做影响核对 |
| 5.7 背压、权限与旧来源 | 容量/磁盘、身份/绑定、管理锁、旧来源 ACK/开关、K8s 完成与放弃 | E1/E2/E3；E36 七个逐项PG含旧pending/failed/Kubernetes控制责任与计费升级；E38路由/profile合同；E39有界HTTP拒绝释放 | 这些是明确路径证据，不是所有旧来源SDK/权限/开关全矩阵；旧50/179聚合selectors与skip原因不倒推 |
| 5.8 公开报告与复审 | 本稿、测量报告、中英文指南、运维/故障说明 | 本稿保留Rabbit正式容量、七故障、e01 SLA与所有首败，新增E43–E52区分独立定向回归、夹具、小样本、SLA及正式HTTP首败 | HTTP正式容量、完整UI及最终Kimi覆盖/复审未完成，报告与规格校验不能改写验收结论 |
| 6.1 需求逐项闭合 | 本覆盖表、五能力 delta、Issue 验收与实际证据 | 本稿映射历史E0–E42及新增E43–E49的实际范围 | 仍为draft：正式容量/完整观察、完整UI和审查缺口未闭合，不申请Issue完结或归档 |
| 6.2 规格/文档与检查 | active change、HTTP/业务批次指南、配置/迁移/回退、必要静态检查 | 本次仅将覆盖稿同步到d689已发生证据；各升级、合同说明与源码仍按独立回执绑定 | 文档和OpenSpec校验不替代验收；正式规格同步、任务完成及归档等待全部必要门禁 |
| 6.3 最终审查与交付 | AGENTS规定的 OCR preview/coverage/终态、修复验证、精确 base/head | 固定OCR1.13.3/Kimi逐scope与组合修复链；冻结d689共161路径，81有当前内容审查链、80缺覆盖 | 服务额度阻塞、完整审查未完成；保留partial/失败/工具局限和未决意见，不把审查链计数当质量批准。门禁闭合后先通知用户，停在PR前 |

## 五能力 delta 对照

| 能力与 Requirement 范围 | 实现/任务映射 | 当前证据边界 |
| --- | --- | --- |
| [durable-http-ingress](specs/durable-http-ingress/spec.md)：旧 Webhook区分、持久身份/状态、原始正文、固定绑定、入口资源、安全重试、当前 token/路由留存、HTTP 独立合并错误 | HTTP、容量/绑定、T1 合并；2.1–2.8、3.7、5.1 | E27真实T1切点、E38路由、E39/E40/E41拒绝/取消/未知提交及新hint支持明确范围；完整UI/ACL与HTTP正式容量待最终核验，幂等读回仍受前置有限请求资源约束 |
| [event-delivery](specs/event-delivery/spec.md)：稳定首次输入、各层容量/责任释放、两类有界推进、发布确认、参考目标恢复、T1逐条责任/确认 | 账本、scheduler/outbox、reference Adapter、T1Unit；2.3–2.7、3.1–3.7、5.5–5.7 | E33 RabbitMQ容量与资源、E27/E28/E31/E34准确故障、E36保留账本、E42两类调度SLA已分列证明；E4/E8等接管首败保留，不扩称全矩阵 |
| [business-event-batch](specs/business-event-batch/spec.md)：显式选择/固定绑定、原子成员集合、strict结果、逐成员处置/共享取消、共享副本保护 | Batch/Member、0052–0054、finish/Admission/Slot、replay/skip/GC、Web声明；4.1–4.3 | E33/E34 支持对应正常与恢复轮次；E36 支持重叠重放份额 GC；E37 支持真实 Worker 三种负结果、目标效果与共享 Admission/保留预算分层结算，不扩大为全部异常或取消矩阵通过 |
| [event-subscription-management](specs/event-subscription-management/spec.md)：保存不自动开启、平台/入口权限和生效、被动来源所有权、适配器内普通用户呈现 | management/schema/registry、runtime/lease、Web/i18n；2.2、2.4–2.6、5.1、5.7 | E2/E3提供生命周期/池/认证，E14桌面容量/停止排空、E38路由/profile合同已实际验证；完整创建/取消、ACL及最终双语UI回执仍待归集 |
| [ingress-capacity-validation](specs/ingress-capacity-validation/spec.md)：B0、同资源、分阶段成本、正式两来源5/60/600、管理/长期责任、准确故障、B3前置 | 测量清单与双oracle、资源/管理/故障探针；3.6、4.1、5.2–5.8、6.1–6.3 | B0/B2路线已记录；历史Rabbit接管及分页首败保留，E33 r6容量/600秒和七准确切点已通过各自范围；HTTP正式容量、完整UI/最终审查仍未闭合 |

## 最终关闭前的必要收口

1. E33 的D389 RabbitMQ一万条及完整600秒观察保留为该候选通过；E48的d689旧200条首败也保留。E49新ns1小样本通过不证明旧ID或inode假设，不覆盖原首败；后继差异的资格仍按路径核对。
2. e01 HTTP R5只在发送前预检失败，R6则实际发送后未达到原10000条期望（E43）。d689 HTTP16条/25ms定向检查和20/200小样本不代替正式5/60/600秒；d689 SLA r5在瞬时队列收尾检查中断，r6三轮真实推进通过见E50；d689 HTTP正式R7仍失败见E51。
3. 不将短观察拼成600秒，不将排空后空闲段的分位数代替压力窗口。E43管理压力p95约650–683ms不判过，最终HTTP同步管理/资源与完整观察门禁仍缺。
4. E14目标中断60秒、E27/E28/E31/E34七个准确切点和E42 e01三项SLA已有各自证据；保留原切面、时限和首败，后继改动仅按影响核验，不拼成同一SHA的一轮。
5. 完整UI创建/取消/保存/明确开启等实际流程尚缺，访问工具限制后已停止该次尝试；已有容量暂停/停止排空证据保持其范围，不据组件测试或构建宣布UI全通过。
6. 冻结d689的161路径中81有当前内容审查链、80缺覆盖，Kimi服务额度阻塞尚未完成；本次或后续内容变化仍须绑定新差异。任务不勾完、规格不归档、不提交PR；全部必要门禁通过后先通知用户。PR四项CI、main集成、部署和用户审查仍是后续独立门禁。

## 后续核验补记（更新至2026-10-27）

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
| E43 | e01 HTTP正式 R5预检与独立R6首败 | R5在producer及资源采样启动前就绪失败，实际接管0，正常停源；不当容量失败。独立R6首发9004/10000，996未发，28560次POST；客户端确认3594、拒绝24949、未知17。实际3610项全部业务成功，但原10000期望的目标/平台两oracle均失败、缺失6390；未完成600秒观察。管理压力窗口p95约650–683ms不判过，不合并空闲段替代压力结果，不缩小期望或抹去任何首败。 |
| E44 | 0054保护与T1时钟/字节边界的独立PG回执 | 0054空terminal_at不能放行批次Receipt TTL：4项guard PG及另1项升级代表通过，旧0053复现首败保留；旧0052/0053文件未被改写。T1另6项PG通过，覆盖受控1秒速率窗口、真实原文加完整Envelope恰1MiB与+1边界。不同回执与源码哈希分别保留，不累加成全体升级或B1通过。 |
| E45 | Kubernetes平台分类与调度SQL的独立定向范围 | K8s回执24项通过，区分平台容量配置不可用、磁盘保护等待与坏消息，保留原配额/内容故障优先级；不称全SDK/全权限矩阵。SQL优化另9项PG通过，核查询预算与推进语义；不以数据库测试替代d689真实调度SLA。 |
| E46 | 批次视图/协议与严格拒绝断言 | view另3项PG通过，核单Session跨事务不增加模式缓存、每次单SELECT、实际重放override/顺序/latest及逐成员展示；protocol另23项纯检查含独立固定ASCII/SHA和内容/顺序变更拒绝原绑定，产品digest未改。既有8个数据库拒绝变体另经真实PG核SQLSTATE23514与各自精确guard消息，既有40001并发检查未改。各回执独立，不重复累加。 |
| E47 | d689所含HTTP16条/25ms收集候选 | 39项纯检查与13项真实PG通过，覆盖有界收集/提交/取消及字节边界，沿用既有请求、原生工作、字节和连接预算；RabbitMQ16条/5ms及普通单条路径不扩大。不是正式一万条、600秒观察或完整UI验收。 |
| E48 | d689初轮小样本与RabbitMQ200条首败 | HTTP20/200及RabbitMQ20的两个独立oracle通过。随后RabbitMQ200条实际接管200、业务成功186、14项unknown，成员错误码result_unavailable；执行288记录workspace_cleanup_unknown。来源正常停用，14项失败与原200期望保留，两个原200 oracle均失败。原因仍在诊断，不将猜测当已证实；后继成功不抹去本轮首败。 |
| E49 | 同d689的新ns1夹具与四个独立小样本 | 仅夹具避免执行标识复用：两条序列使用1000000–1999999号段、NO CYCLE，13项纯检查及fresh ns1序号PG成立，67表含alembic的数据哈希不变，产品源码未改；分配登记作为受保护夹具状态保留。新ns1 RabbitMQ20/200的宿主启动至观察成功上界约2.0454/5.1607秒；HTTP20/200分别2.049059583/6.167612秒。四轮目标与平台full oracle核精确ID/数量/成员均通过、Admission归零且正常停源。旧14项失败离线保留，不据此认定历史inode/ID是原失败根因；不代替一万条、完整观察或正式容量；新的独立SLA结果见E50。 |

| E50 | d689 独立SLA r5首败与r6三轮 | r5普通10入口通过；阻塞场景的目标、平台及原始2秒推进记录通过，但瞬时队列仍有5条待消费而流程attention，后续只读观察为0，原记录不改pass。夹具只将业务完成后的队列检查改为45秒绝对期限等待，保持原T0/2秒/目标与平台判据，不purge或重发；新r6三轮全部通过：pending上界0.691932780/1.103422588/0.677319045秒，完成确认上界0.101641021/0.189308037/1.015495612秒；通知丢失恰10次，observer errors为0。目标独立核10/9/10项及完整平台oracle通过，来源停用、队列排空、正常配置恢复；有意失败头及150项pending保存在独立数据库，不以SLA结果代替容量或OCR。 |
| E51 | d689 HTTP正式R7，原10000期望 | 首发9278，722未发；29215次POST，客户端确认6732、拒绝22454、未知29，Retry-After校验无异常。实际6759项全部成功，目标6759次有效写入/尝试，平台逐项核6759成员、80批次/尝试，原10000期望两oracle仍失败且缺3241；Admission计数/字节与active全0，来源正常停用。管理867次均200、无漏采或超时，实际发压代理窗p95三项约342/390/347ms，不冒充完整10000的正式phase。293个资源样本无新增OOM/限流；Control自身最忙约59秒均0.930核，跨VM窗口不做精确对齐。排空后仅观察约121秒，不算600秒通过；故障标准、原期望和首败均保留。 |
| E52 | R7只读分组与配额边界 | 6759个新项落在1066个Delivery事务时间戳桶，平均6.34、最大16，满16的桶13个；仅为含新项事务的条件估计，不包括纯重复/拒绝事务，也不能排除同微秒碰撞，不称总cohort数。最终来源历史字节占51.53%、全局29.44%，pending/阻塞与控制预留归零；最终快照不能排除先前瞬时门禁。新项状态已经纯内存生成，没有待消除的新项逐条详情SELECT；下一优化须以实际热点与拒绝分类为依据，不增加资源或降低标准。 |

| E53 | d689 HTTP1000项的有界外置诊断及入口模型 | 同配置1000项目标/平台全对账通过，3774次POST精确闭合为1000个202及2774个429，所有拒绝均为`ingress_capacity_exceeded`，无transport error或unknown；该客户端计数及15秒/50Hz/GIL采样只作诊断。419个有效样本中Main153、原生入口142、维护88、AnyIO34，包含调用栈重叠不累加。实际Admission的125并发无网络模型显示BaseHTTP让125个请求都先通过提示再由内层拒绝100个；直接ASGI链让100个在外层被拒绝，均25成功/100拒绝、峰值25/最终0。模型含刻意hold屏障，不推真实吞吐；D7.3须另验认证、取消及原正式容量。 |

| E54 | D7.3入口层等价改写的定向核验 | 106项禁网络纯检查通过，涵盖新边界36项、原Admission42项、大小限制25项及collector/cancel3项；另8项真实PG以账号/CSRF/跨入口、旧Webhook及持久HTTP认证/重复/冲突/状态查询为范围，8 passed、0 skip/0fail、13.492秒，运行前后源码哈希不变，独立测试库已清理。宿主独立差异审查未见确定问题；随后只补强一个既有真实native Future取消代表，实际穿过新边界：取消时字节与work名额保留，实际工作结束才归零，1项纯检查通过。两来源小样本、SLA及新正式HTTP尚未执行，OCR仍有额度与覆盖缺口，不将本轮当最终通过。 |


| E55 | `d7dda63572c206c001b8d97cdfa40059d2abd070`四小样本及三项SLA | 同资源和新0054/独立ID段，HTTP与RabbitMQ各20/200目标、平台与精确成员全对账通过，Admission0且正常停源。随后新三场景均过原2秒：pending/completion上界为0.856370458/0.109554686、0.856771672/0.191452914、0.724987788/1.032036197秒；通知确实丢失10次，observer final/errors0，Target完整报告10/9/10已归档。阻塞旧库1failed+150pending保持；已恢复普通配置，实际11来源全停，资源/凭据/镜像/挂载及PG/RabbitMQ容器身份按回执保持，不代替正式容量。 |
| E56 | 同d7 HTTP正式R8，原10000期望 | 首发5695，4305未发，首发跨度4.984260893秒；23016次POST=5695个202+17318个429+3个503，未知0，Retry-After合法，最大12次尝试；3个503均为首发请求，正式原始日志未保留其机器码，不能从retryable拒绝总数推作429或定因。实际5695全部成功，Target有效写入/尝试均5695，原10000目标与平台完整oracle均缺4305，平台另有3项对应汇总/双射计数失败；Admission0、Broker0且正常停源。858次管理请求均200/无超时漏采，首实际请求后60秒代理窗三项p95约323/300/331ms；实际全完成上界136.531665秒，之后只观察127.033640秒，不能算完整600秒。290资源样本无错误/OOM/配额节流；各自约60秒最忙Control/Worker/PG约0.830/0.374/0.428核，与R7工作量及跨VM时钟差异不做因果归因。D7.3仍未证明容量收益，不据此直接增加资源或重跑正式轮次。 |
| E57 | 旧意见处置与本轮OCR局部复核 | 宿主只读归集17条已完成Kimi原意见：15项有修复依据、1项原缩进guard已保护而为误报、1项原有年龄投影观察保留；不是宿主冒充Kimi或全量质量批准。0054/guard/升级测试3路径现经OCR1.13.3、真实`k3.1-pro`从原完整base复审，3/3完成、0新发现、无429/超时/工具失败；保留一项背景长度建议。当前内容审查链82/162，仍缺80，整体未批准；后续新范围和最终文档须分别核覆盖与质量。 |

| E58 | d7新1000诊断与热点复核 | 3491次POST=1000个202+2491个429，429均`ingress_capacity_exceeded`，unknown/transport0；两oracle和Admission0/正常停源通过，原R8的5695项及完整平台资源前后相同。原外置15秒/50Hz/GIL/threads/no-locals得393有效样本，Main/native/维护/AnyIO为123/120/88/53，原BaseHTTP栈为0；await包含帧不能算边界自身CPU，不能据不同工作量推容量提升或根因。新项事务时间戳桶均值R8为6.30、新1000为6.76，仅条件估计；源码允许同来源native组重叠，具体争用时序尚待D7.4一次200测量，不直接加协调器。 |

| E59 | Kimi发现的大单条公平调度P1及D7.5修复 | scheduler2虽manifest2/2完成，但有1条high及plan180秒超时/压缩取消，原组不qualified且不晋升内容计数。经源码和真实PG核实：合法per_event预付>1MiB在游标前永久break；首次fixture因默认512KiB输入校验失败不算复现，显式既有1MiB测试输入配置后，旧源pending/processing均实际0attempt失败。产品仅两SQL分支LEAST，保留loop/cursor/batch/账本；修复后13项PG通过、0skip/14.94秒，包括原2大项、默认512KiB上限/382KiB实际合法大项及小项同轮累计和原9调度回归。独立测试库按lower(datname)确认清理，静态/宿主peer通过；尚待新候选真实Kimi复审，不把此修复当R8瓶颈或容量通过。 |

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

### E43–E49 最新范围索引

以下是各自独立回执的SHA-256，不将测试次数、旧候选或不同运行合并成最终通过；完整材料留在私有证据目录，本文不披露路径、地址、账户或凭据。

- E43：R5发送前预检关闭回执 `0d60bd91e3c4914eff0a39f4c5e25fde73e75f6439d943e8499b6fb00371f371`；R6发生器结果 `cb371470e7f9b063ffb46e55057a268f90befb6fa5d2c62c46b5a46787bace9b`、原10000期望的Target核验 `460dd07a60f26ef12c10eaeaf9b189c2ab9048a319faf909f6b217c7fb726468` 与平台核验 `d9efd0908f4a11ae0e76ee2c14146f08cdfe315266930024ec968481bcf5835f`；管理原始采样 `f2213049a50b66f02688e1601cd8bbb9bdfcc5e6ac7b3ee8b38d725bf3b8e025`。压力窗口与空闲段分开，不用合并分位数改写压力结论。
- E44：0054四项guard回执 `b7fd7786f11d319b8a550db7715085bf9856f30a340d4788441e6fcafed872eb`、另一个升级代表回执 `f428d915b1e00dcb383a7ea0329fd0ae602c2e031a50d3c5597b2b8f38b16e87`；T1六项时钟/字节PG `83a3433430e0b0df224bcb6dbd710af7302645a76177c9bafafd04a4139bbaad`。旧失败及各轮源码/测试哈希均由对应索引固定。
- E45：Kubernetes平台分类回执 `5b0d073e5b2925f486295cf10b487cdfc66ae41781cc3bace0d4c6eaec20421d`；调度SQL九项PG `75a345c4693c0ed4d06889435d37db058f6084a426eb82b07a8900d8787f4d74`。
- E46：批次view三项PG/protocol二十三项纯检查 `c3316f7266776360ea41190bd9f5eb4e04b6578dd9b27983797b05c2aeb65cdc`；八个严格23514/guard断言PG `7ca8437e2ef4d8c001e96727f7d8735df4bdce663dd6280117e5dc5ab452807c`。一次性测试库均按各自回执清理，不混同需保留的失败证据库。
- E47：HTTP16条/25ms定向检查 `de00a3929f499570b2c124f1e6ffe5b5f6695b46afbac087dc78119f0b4e0725`；39项纯检查与13项PG分开记录，修复后只复跑受影响边界，未据此宣布负载通过。
- E48：旧Rabbit200原期望及两oracle首败 `86081282f06f2b06ca31f803f97b75e4aa799012f7aad22f092f9f9186450de6`、执行错误读回 `30be527f79fc94ed9c124662c9457d2765f8a11fed797f323f12e1c1363ce783`、正常停止与失败保留 `c8d4fb4a1c014690860efef2d476ac69c969a736a694069df920785abe827f41`；不把机器错误码当根因证明。
- E49：ns1夹具独立资格 `de4db1bb1c406e3ae76e8b95fd47bbcf6ebaf03ddf6dfdaedd9a1e918c6fe226`；四个小样本的full oracle与停止读回 `d7ecde7101bcbe8cb012ca3a6ebff04c3ee9617b51068c0b03c68cc1a08e838e`，明确保留旧失败且正式容量未通过。
- 冻结d689的当前内容审查链/缺口清单 `fcc6c7239deb9abe1c37f2d6427a050bc5fb8ad790b5edab2f06c6080cb6d78e`；完整UI流程受工具限制停止的回执 `5b0204f56597627064f4ca5625b8040cba9f5697bd8ce7f377562f361c90aaa0`。两者均不是完成或批准回执。

- E53诊断采样 `8c50ca2c6c66a6f830878138141decd7f384ebbb9f942ee893b0047d337cc250`，宿主离线分析 `8f044ac701edcdaf26a6053191650458bb9bc77fb6a44ad34e8e7526c0f260e3`；E54完整定向回执 `31b729c5123fc30252d23d0aab8adbd7eb4fddd2081ed99acadd593575d8c154`。

- E55完整资格 `568cf3b4635197fe9cf393f276cb26e04c43a0a0cdd0e53e0a0a562dffbf5ee0`，主Agent五个完成/冻结链接复核 `8f3d939748dcafc37ebbe96b3f72abb63391b2f40a8ca5124ea44393c946a641`；E56平台原10000完整报告 `37652cb28d65a993442e8ecc3343991c0524109145897daea9f06b641ed830e0`、资源收尾 `8b8837d1086a9aacaf2b27f29dabf99ffd49c6f103927b332fe778361757cd0c`、离线请求/管理分析 `28be5eea1bde56bd466aaeec318bfe8ce913ed07e4ac69d84a86be2af8e745be`。首次宿主读回脚本错误要求失败oracle返回0而中断，原完整oracle不重跑，续读相同已生成文件闭合，原脚本/日志保留。
- E57原17项处置索引 `98ef43424761856b7a2c0980223766d696c31bdfcc9745094252b6d7780ba4a3`；0054真实复审资格 `2a33962a38b40c41acd7d51933aed3ab19bc3ebb83e706c038baa41f5ab6d968`、随后精确内容链矩阵 `05e30ac774c5b9ad3ee6b0fba107aa5a4e59d07d2524b6fe6a61d4a06bf78a18`。旧审查clone经clean/无进程引用/原报告与trace在外部/当前快照可重建旧Git对象核验后，只退役冗余clone，未删原审查证据或任何数据库。

- E58完整诊断资格 `1693c1ae3700bf24441ce20424f13daabb07142209a62d09d01c025940aabad6`、原profile `e5591b375cd34b56e93502ecab6db4652b28dac9ab7ecf0d94a3ba9b6e9f91ed`、独立宿主分析 `d394964836ff46f99d4a1094c220935487b1c798d6c55ec81b153b0ce6f6470a`、条件新项桶 `0a333053183395435a3ef737f5955c7bf442f3daf597d25dd966e705d6b176d7`。该分析纠正E56旧资源派生报告将重试性拒绝总数17321误标为429的标签；R8原状态事实始终为17318个429和3个503，原报告不覆盖。

- E59原P1与phase/source-input资格 `3e8e130a078d8dc45f2cd22a9f4bf3005e5853c21f497b5beea9fdeee7a85d49`；修复完整定向回执 `4e031f74636e4dd5823a39d61cd61e377e87413b4970004a43c67eabd31ae924`。D7.4诊断门禁初版迟启动漏记与后续完整性不足均在现场前修复，旧准备scope保留失效，不把其纯测试当计时测量成功；后续必须绑定新候选。
