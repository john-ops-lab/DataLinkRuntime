# B1–B5 需求与证据覆盖稿（2026-10-27）

**状态：同候选两来源正常容量及完整600秒观察已通过；统一OCR仍未完成，尚不能归档或提交PR。** 本稿用于定位已实现路径、已经发生的验证以及剩余门禁，不代替 [tasks.md](tasks.md) 的完成判定。本文不修改任务勾选，不以测试文件存在、代码可构建或 OpenSpec 校验通过宣布功能或容量通过。

最新正式结果：产品候选`6e77f16c58e1c41bffe91217cded34231a1909b2`的HTTP R17与RabbitMQ R8各自原10000条、5/60/600秒、独立Target＋Control对账、同步管理/资源及完整排空后600秒观察均已通过（E102/E104）；MQ的主机操作水位暂停及保持原T0的Root续接收尾单独披露，不称自动序列无中断通过。原普通环境已恢复，测试数据库和证据保留（E106）。早期a966的HTTP R12/RabbitMQ R7及其规格校验记录保留在E92–E93，不能代替后继候选。当前增量审查覆盖和未完成范围见E107；统一OCR与交付总门仍待，不以容量通过代替最终质量批准。简明结果见[B4容量与恢复记录](b4-recovery-results.md)。

**实施与历史快照（a966正式验收前；本段“仍待/未放行”指该时点，最新容量与审查范围见E102/E104/E107）：**

记录日期：2026-10-27。历史产品基线为 `557baa1f3d45819ac7e712a777f8b640a999d633`；原测试补强至 `af287387a915d500948d824e2275d4d99c3a5b18` 的证据保留原SHA。D7.10产品/测试候选为 `643265c5e7826cccc03bf0ede74496b7f816b845`，文档与小链候选为 `e1fd145e0f5969b3cf661242f40c6aeaffbdad39`；相对557仅HTTP ReadyQueue产品源改变，迁移/Web/tools及其他保护源不变，19纯/3融合PG和三个HTTP小链见E78–E82，D7.11额外200条metadata诊断见E84，D7.12已在`c9da9bb931225a00017e4ca98a355ac3220369d3`实施，36纯/真实collector三项PG与同候选HTTP20/native200/忙闲代表链见E85–E86；C9相对557新增API初始化接线及ReadyQueue两处产品差异，迁移/Web/tools与其余保护源保持，正式容量与最后候选门禁仍待闭合；关闭池修复已完成针对性纯检查、真实PG和Kimi七文件增量复审，最终三项调度SLA实测已通过（E68），HTTP正式R9/R10/R11均失败并已收口（E69/E83/E87），后继RabbitMQ正式轮次未放行；D7.13仅raw预留缩额已在`17f74ee482a40fedb4be3293b0a9b27885284c6d`完成实施和针对性功能资格：14纯/旧25项反例及GPT审查见E88；publisher的新增测试、原7纯及真实PG4见E89；同候选新HTTP20/native200/忙闲代表链见E90。两组实际Worker输出边界及正常恢复见E91，保留ControlPG与真实Worker的不同层级。原正式容量、完整600秒观察和最终统一OCR仍待，不把预算算式、小链或Worker代表当容量通过。D7.6的四轮HTTP正常链路实际绑定afcd候选，两轮新HTTP提交前后故障绑定0112候选（E64/E65），组件转移范围另列E67。RabbitMQ正式r6的容量与完整600秒观察仍绑定 `d38904f200cbc525019f663738253a5df0e374a6`；HTTP正式R8失败仍保留（E55/E56）。各历史候选及首败不因后继文档或小样本通过而改写，不拼成一次最终容量通过。

e01 HTTP R5 是 producer 前、实际零接管的就绪预检失败；独立 R6 已实际发送并失败，原10000条期望的两个 oracle 均未通过，缺失6390条，未完成600秒观察（E43）。d689 旧 RabbitMQ 200条出现14个结果未知并保留首败（E48）；夹具修正后的 ns1 四个小样本另有双 oracle 通过（E49），不以此裁定原失败根因或宣布正式容量通过。

新增保护、时钟/字节边界、Kubernetes、批次投影/协议、SQL及HTTP16条/25ms的定向证据分列E44–E47，入口ASGI、完整链路及后继调查见E53–E67；各项绑定自身回执与源码，不相加成总通过数。实际Kimi在ec97测试复审、reader及拆分首组冻结点已核127/165条精确内容链，仍有38条缺覆盖，且内容覆盖不等于全部意见批准；后继源码或文档改动另核复审范围。三份规划文档本轮仅审D7.7增量，最终全文审查未完成。原UI访问受限首败保留；用户恢复权限后，已由实际浏览器完成新建/取消、HTTP来源取消、保存并刷新仍未开启、明确开启/停止及本轮零消息测试项清理（E72），不冒称移动端或未测场景通过。必要门禁未闭合，B1–B5不作整体完成判定，不归档、不勾完任务、不提交PR。

审查顺序已按用户要求调整：中间阶段用GPT独立审查，完整开发与验证后才统一最终OCR。下文127/165及各中途Kimi回执均为历史范围记录；未完成/配额停止/实质意见未关闭的结果不能算最终通过，不继续为每个中间阶段调用OCR。

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
| 2.2 当前 token、安全查询与 ACL | HTTP `_authenticated` / `receipt_status`、管理 API ACL；错/撤销 token、跨来源、旧地址、删除/重建、原 Webhook 两模式 | E1旧功能边界；E3/E38/E54 HTTP定向范围，E74同epoch跨订阅反例，E72实际用户流程 | E54/E75分别支持旧Webhook accepted/completed；管理ACL由同源旧兼容回执支持隐藏404/只读403，最终按实际源码范围核对，不由聚合数推断 |
| 2.3 持久接管与固定输入 | HTTP `receive` / `receive_group`、T1Unit、Receipt/Delivery/Activation、原始 bytes SHA；提交前后故障和重复 | E2/E3 的原文、202/200/409 与计费回归；E27 四个真实 T1 切点；E40 的提交前后未知 PG；E41 的 e01 HTTP20/200 双 oracle | e01 R5 预检失败与 R6 正式失败分别见E43；d689 小样本及旧200条首败见E48/E49，均不替代HTTP正式容量 |
| 2.4 生命周期及运行锁 | `subscription_management`、runtime/lease、平台错误分类、冻结策略、replay/repair/skip | E2/E3 的 stop/binding/租约/运行器范围；E27真实T1恢复、E39关闭执行池拒绝、E42调度恢复；E45 Kubernetes平台错误分类定向回归 | E21旧生命周期失败保留；不同SHA的通过按受影响路径核对，不据定向回归宣布所有旧来源或历史skip通过 |
| 2.5 部署容量与管理边界 | `ingress_capacity`、fingerprint/audit、管理 limits；无效配置、并发采用、下调保留、入口排空 | E2 的 profile/锁定范围；E36 原计数正常及已有137余量时只追加迁移增量；E38 真实旧 owner 排空后切换 profile、下调保留原计数 | E75补齐普通edit用户停止仍pending时合法改额拒绝及无变更，部署profile写入另核静态边界；这些功能保护不等于容量通过 |
| 2.6 资源隔离与拒绝释放 | HTTP 32 请求/8 MiB/8 原生工作；HTTP 与原生来源共享 intake 8 连接、管理/恢复另 4 连接，均无 overflow；64 MiB 来源父预算与磁盘迟滞 | E2/E3 逐项拒绝/释放；E17/E18 的16条受检 SDK 边界；E33 RabbitMQ 完整根资源与600秒观察；E39 的70项纯检查及 E40 的真实共享事务提交未知 | HTTP 后继正式轮次的资源和完整观察见E92；历史 E9 管理一致性失败保留，不以旧轮峰值替代最终受影响路径 |
| 2.7 完整计费、T2 与留存 | Receipt 原文/元数据、Delivery 未来输入/批次份额、控制预留、30 天整组 GC；原子 T2、回滚、审计 | E1/E2；E36的7/30天、重放新增副本、skip后TTL、失败保留及重叠GC；E37的Admission/保留预算分层；E44空terminal_at保护与升级代表 | 按用例推进终态时间不等于连续30天观察；各自计费/恢复回执不相加，最终审查仍未完成 |
| 2.8 两来源 20 条链路与本批审查 | `workload`、真实 Worker/目标、平台与目标独立 oracle、HTTP/事件文档 | E1旧单条20条；E18的Rabbit路线20/200；E41的e01 HTTP20/200；E48/E49的d689独立小样本 | 保留d689旧Rabbit200首败；新ns1小样本不能替代一万条、600秒观察或最终OCR，不跨候选拼成全部通过 |
| 3.1 依据实测选择优化 | B0/B1/B2 结果、调度/Outbox、容量重复读、0053 JSON 一次展开、D7.1 | E0/E1；E4/E8 两次正式接管失败；E7 为后续选择提供组大小事实 | 最终差异审查/OCR待完成；不得把旧瓶颈假设或诊断含观测开销的耗时当作收益 |
| 3.2 有界公平推进 | scheduler 的 pending/processing 分离、轮转、轻量头部、补偿通知；10 入口/阻塞页/重启场景 | E42：e01 的 ten、实际失败头+150pending、真实丢10次结果通知三轮；两类持久推进上界均≤2秒，双oracle/Broker0通过 | D389 R3首败保留；E42/E50为历史候选，最终557三项真实SLA见E68。E45的9项SQL PG不代替真实SLA |
| 3.3 受限发布与确认 | `outbox` 的线程归属连接/channel 复用、确认窗口、return/nack/超时/丢连接；逐行未知恢复 | E1 发布链路与代表性确认测试；E28 真实Outbox确认后退出新轮通过，E31/E34跨Worker恢复时核对原Outbox及历史fencing | 该精确切点已实测，不能扩称全部Broker故障组合；最终源码与适用部分确认/超时回执仍按影响归集 |
| 3.4 单条完整链路比较 | 不变沙箱、执行策略/凭据快照、准备成本与参考 Adapter | E1：B0→B2 200 条比较及逐项 oracle，触发 B3 | 比较支持路线选择，不是最终批次吞吐结论；当前候选的必要回归/OCR待完成 |
| 3.5 参考目标有界恢复 | `reference_adapter`、单条/批次重试白名单、共享 deadline/额外重试预算、独立成员事务 | E14 已真实中断目标60秒并自动完成1000条；E34 目标提交后结果丢失由稳定身份恢复，无重复有效写入 | 两轮均绑定其冻结候选与参考目标；权限/重试预算的适用回执继续核对，不宣称任意Adapter自动恢复 |
| 3.6 B2 路线裁决 | `b1-b2-results`、D9、`b3-contract` 与业务批次 delta | E1 已用完整 200 条真实积压窗口触发 B3 | 路线依据已记录；后继两来源 B4 容量与观察见E92–E93，最终OCR仍待，不以分位数倒数裁决容量 |
| 3.7 T1 接管合并 | T1Unit、async HTTP collector、共享 intake 8 连接、Rabbit header 前置预算/最多16 credit、首条工作区保护与受限剩余收集窗口 | E2/E3/E7原合并边界；E44/E47定向验证；E85/E88/E90受影响功能与小链；E92/E93同a966两来源正式容量 | 两来源原10000与完整观察已有E92/E93；最终OCR仍待。d689旧Rabbit200及ns1分列E48/E49，不由prefetch或组上限推容量 |
| 4.1 B3 专项合同 | [b3-contract](b3-contract.md)、business-event-batch delta、D9；精确 Activation/override、结果/取消/留存规则 | E1 的触发证据；专项合同及 delta 已存在且可核对 | 最终合同与代码/文档的审查仍须收齐；没有用户界面声明或整批进程成功替代成员证明的例外 |
| 4.2 有界执行与逐条结算 | Batch/Member 模型、0052/0053、strict finish、Slot/Attempt、replay/skip、整体 GC | E33/E34正常与恢复；E36共享GC；E37与E91真实Worker负结果/输出边界；E46协议/数据库守卫；E92/E93正常容量 | 明确Worker/ControlPG/pure分层，不扩全部异常/取消矩阵；旧E48首败及未实证根因保留。正常容量已过，最终OCR仍待 |
| 4.3 批次说明与比较 | 中英文业务批次指南、版本 marker、输入/结果 schema、逐条投影与技术详情 | E1逐条200次执行处理200条；E93以159次执行处理10000条，原2KiB正文、参考目标及既定资源预算对照见B4报告 | 对照只证明执行生命周期按实际成员数摊薄，轮次数量与部分配置不同，不计算跨口径速度倍数或归因单点；UI沿E14/E72范围，最终OCR仍待 |
| 5.1 界面与原操作习惯 | HTTP来源抽屉、处理方式及代码声明、接收记录、共享执行提示、中英文资源 | E14桌面容量暂停、处理中停止后原责任排空；E72实际创建/取消/来源取消/保存刷新未开/明确开启停止及临时项清理 | 原访问限制已由用户恢复；上述桌面流程已有回执。E72为零消息操作，不代替E14的真实排空；既有390px布局未通过，不扩称移动端已验收 |
| 5.2 正式夹具与预冻结 | `workload` / target / management probe、容量 profile、原文/业务哈希、空闲接口基线 | E0/E1方法、E23发生器资格、E33正式Rabbit清单；E43保留e01预检与正式首败，E49另核新ns1夹具 | 每轮profile、输入、资源和时钟绑定各自冻结清单；夹具资格或小样本不替代正式一万条，运行中不加额 |
| 5.3 两来源各一万条 | 独立目标与平台 oracle；5/60/600 秒分别核验 | E92 HTTP R12和E93 RabbitMQ R7，同a966各自原10000/5/60/600及完整Target/Control逐项oracle通过 | 同候选两来源正常容量已实际闭合；历史接管/发生器/对账首败不删、不缩原期望，不把本轮成功归因单项优化 |
| 5.4 管理面、资源及10分钟 | management/resource probe、根 cgroup、并发/队列/账本、排空后600 s | E92/E93同轮827/826个完整资源样本，各三接口p95均≤500ms，完整post600.979/600.017秒，Root独立资源审核 | 只支持本轮固定资源和观察窗口，不外推长期无泄漏；不将压力与空闲阶段混作一个p95，也不跨VM时钟相减 |
| 5.5 目标中断60秒 | 独立 RabbitMQ1,000 条故障轮次；失败/恢复/逐项幂等、无人工处置 | E14：参考目标真实关闭至少60.000263秒、11次真实连接失败，1000条双oracle通过，恢复后完整责任核验上界25.192795秒 | 约定参考目标范围已有通过；不替代正常容量或任意Adapter恢复，最终候选变化仍须按影响核验 |
| 5.6 精确故障点 | HTTP/Rabbit T1 前后、代表性 Outbox、Claim/journal、目标提交后回报前；每点20条 | E27 四个HTTP/Rabbit T1切点、E28 Outbox、E31 Claim/journal、E34 目标提交后结果前，七个独立20条准确切点均有真实通过 | 各自candidate/切面/120与600秒/最终双oracle均保留；E21/E28/E32旧harness或生命周期失败不改写，后继HTTP变化做影响核对 |
| 5.7 背压、权限与旧来源 | 容量/磁盘、身份/绑定、管理锁、旧来源 ACK/开关、K8s 完成与放弃 | E1/E2/E3；E36 七个逐项PG含旧pending/failed/Kubernetes控制责任与计费升级；E38路由/profile合同；E39有界HTTP拒绝释放 | 这些是明确路径证据，不是所有旧来源SDK/权限/开关全矩阵；旧50/179聚合selectors与skip原因不倒推 |
| 5.8 公开报告与复审 | 本稿、测量报告、中英文指南、运维/故障说明 | 历史首败/准确故障保留；E88–E91分层功能证据；E92/E93本候选两来源正式结果及正常恢复 | 本稿和B4报告已草拟最新实际结论；最终全文OCR与本批文档一致性/交付总门仍待，UI仅沿E14/E72实际范围 |
| 6.1 需求逐项闭合 | 本覆盖表、五能力 delta、Issue 验收与实际证据 | 本稿按E0–E93各自候选/层级覆盖；两来源正式容量与完整观察由E92/E93闭合 | 功能与正式证据映射已核；全文OCR和PR前通知仍是独立未完门禁，不申请Issue完结或归档 |
| 6.2 规格/文档与检查 | active change、HTTP/业务批次指南、配置/迁移/回退、必要静态检查 | 五份能力已同步主规格，17项主规格校验通过；升级、合同说明与源码按独立回执绑定 | 本批报告和任务并入后的规格一致性与严格校验已通过；统一OCR及归档仍待完成；规格校验不替代业务验收 |
| 6.3 最终审查与交付 | AGENTS规定的 OCR preview/coverage/终态、修复验证、精确 base/head | 固定OCR1.13.3/Kimi历史逐scope与修复链；旧d689的81/161及后继127/165仅为各自冻结统计 | 最终候选及覆盖范围尚未冻结，按用户顺序待全部开发验证后统一OCR；保留历史配额/partial/失败与未决意见，不把审查链计数当质量批准。门禁闭合后先通知用户，停在PR前 |

## 五能力 delta 对照

| 能力与 Requirement 范围 | 实现/任务映射 | 当前证据边界 |
| --- | --- | --- |
| [durable-http-ingress](specs/durable-http-ingress/spec.md)：旧 Webhook区分、持久身份/状态、原始正文、固定绑定、入口资源、安全重试、当前 token/路由留存、HTTP 独立合并错误 | HTTP、容量/绑定、T1 合并；2.1–2.8、3.7、5.1 | E27真实T1切点、E38路由、E39/E40/E41拒绝/取消/未知提交及新hint支持明确范围；E72支持上述桌面流程，ACL/旧Webhook精确补证见E74–E75，HTTP正常容量见E92已通过，幂等读回仍受前置有限请求资源约束 |
| [event-delivery](specs/event-delivery/spec.md)：稳定首次输入、各层容量/责任释放、两类有界推进、发布确认、参考目标恢复、T1逐条责任/确认 | 账本、scheduler/outbox、reference Adapter、T1Unit；2.3–2.7、3.1–3.7、5.5–5.7 | E33 RabbitMQ容量与资源、E27/E28/E31/E34准确故障、E36保留账本、E68最终557两类调度SLA已分列证明；E4/E8等接管首败保留，不扩称全矩阵 |
| [business-event-batch](specs/business-event-batch/spec.md)：显式选择/固定绑定、原子成员集合、strict结果、逐成员处置/共享取消、共享副本保护 | Batch/Member、0052–0054、finish/Admission/Slot、replay/skip/GC、Web声明；4.1–4.3 | E33/E34 支持对应正常与恢复轮次；E36 支持重叠重放份额 GC；E37 支持真实 Worker 三种负结果、目标效果与共享 Admission/保留预算分层结算，不扩大为全部异常或取消矩阵通过 |
| [event-subscription-management](specs/event-subscription-management/spec.md)：保存不自动开启、平台/入口权限和生效、被动来源所有权、适配器内普通用户呈现 | management/schema/registry、runtime/lease、Web/i18n；2.2、2.4–2.6、5.1、5.7 | E2/E3提供生命周期/池/认证，E14桌面容量/停止排空、E38路由/profile、E72新建取消与明确启停已实际验证；管理ACL及双语组件的精确回执仍按范围归集 |
| [ingress-capacity-validation](specs/ingress-capacity-validation/spec.md)：B0、同资源、分阶段成本、正式两来源5/60/600、管理/长期责任、准确故障、B3前置 | 测量清单与双oracle、资源/管理/故障探针；3.6、4.1、5.2–5.8、6.1–6.3 | B0/B2路线已记录；历史Rabbit接管及分页首败保留，E33 r6容量/600秒和七准确切点已通过各自范围；同a966两来源正常容量与完整观察见E92/E93已闭合，最终审查仍待，桌面UI范围见E14/E72 |

## 最终关闭前的必要收口

1. E33 的D389 RabbitMQ一万条及完整600秒观察保留为该候选通过；E48的d689旧200条首败也保留。E49新ns1小样本通过不证明旧ID或inode假设，不覆盖原首败；后继差异的资格仍按路径核对。
2. 同a966的HTTP R12/RabbitMQ R7已按原10000/5/60/600及完整600秒观察通过（E92/E93）；E87的R11与旧R5–R10/夹具首败原样保留，未重发补齐或重置T0。正式结果不倒推某次200诊断、小链或单个优化的因果收益。
3. E92/E93各自同步管理/资源及完整600秒观察已通过，压力与排空阶段p95分开核验；不将短观察拼成600秒，不用空闲p95替代压力窗口。E43约650–683ms的旧管理首败仍不判过；本轮通过不外推长期无泄漏。
4. E14目标中断60秒、E27/E28/E31/E34七准确切点、E65受影响HTTP两切点以及E68最终557三项SLA已有各自证据；保留原切面、时限和首败，后继改动仅按影响核验，不拼成同一SHA的一轮或重跑全MQ矩阵。
5. 桌面新建/取消、来源取消、保存刷新仍未开、明确开启/停止已有E72；容量暂停和处理中停止后排空已有E14。原访问失败保留，390px布局未通过，不将它变成未经约定的新测试矩阵；后继代码有影响时才针对补验。
6. 旧d689的81/161、后继127/165是历史冻结统计，不代表当前最终覆盖。全部开发验证完成后统一冻结最终候选、执行OCR并修复复审；新差异不得复用不匹配回执。当前任务不勾完、规格不归档、不提交PR；全部必要门禁通过后先通知用户。PR四项CI、main集成、部署和用户审查仍是后续独立门禁。

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

| E59 | Kimi发现的大单条公平调度P1及D7.5修复 | scheduler2虽manifest2/2完成，但有1条high及plan180秒超时/压缩取消，原组不qualified且不晋升内容计数。经源码和真实PG核实：合法per_event预付>1MiB在游标前永久break；首次fixture因默认512KiB输入校验失败不算复现，显式既有1MiB测试输入配置后，旧源pending/processing均实际0attempt失败。产品仅两SQL分支LEAST，保留loop/cursor/batch/账本；修复后13项PG通过、0skip/14.94秒，包括原2大项、默认512KiB上限/382KiB实际合法大项及小项同轮累计和原9调度回归。独立测试库按lower(datname)确认清理，静态/宿主peer通过；6e0候选真实Kimi源码及两测试路径的核心审查随后完成、无新发现，保留辅助规划超时，资格边界见E61；不把此修复当R8瓶颈或容量通过。 |

| E60 | D7.4真实200条计时及D7.6设计依据 | 6e0首次控制器因启动输出尾部空行在发送前attention，原始失败保留；冻结PID/CID、源码、输入、配置和接管0等检查点，只续接首次200条，未重建、重发或修改T0。28个真实native组的新202合计200，两完整oracle、Admission0、正常停源及恢复普通环境通过；同路由native重叠，逐组锁入口路径去重累计3.886194833秒/Σnative墙钟5.161815719秒=75.28736%，组级p95为544.719020ms。该路径包含SQL/ORM/网络与等待，只满足原调查阈值，不证明纯DB锁或吞吐收益。源计时卷在完整证据收回、普通环境恢复且无容器引用后清理，数据库、首次失败和ID分配登记保留。 |
| E61 | D7.5真实Kimi复审与辅助规划错误核实 | 固定OCR1.13.3、真实k3.1-pro，完整base到6e0源码主任务DONE、完整diff/规则/背景均在成功请求中、26次注册工具全成功、压缩成功、预算及逐文件终态正常，无新发现；辅助plan请求仍300秒超时。核对工具1.13.3官方调用链及独立宿主复核，确认该失败仅跳过可选规划、未丢主任务输入后，单独追加核心审查资格，保留原失败报告及warning，不要求零辅助告警，也不泛化为允许关键任务/工具/覆盖失败。两测试路径按相同严格边界完成复审。另纠正3份旧派生回执漏读原JSON retry_report的元数据，原报告不覆盖；这些局部结果不等于整候选批准或完整覆盖。 |
| E62 | D7.6有界原生就绪队列的定向核验 | 38项受影响纯检查通过，新增文件最终8项纯检查复核通过，两者有重叠不相加；覆盖真实Thread/Future、同路由FIFO及跨路由并行、取消保留所有权、GET/旧单条释放后无新POST唤醒、32请求/8MiB/8native上限、单到期计时器、关闭和submit/早完成回调。过期完成回调关闭时不得预先claim后继的窗口已修复并验证。独立宿主peer核四文件哈希及原T1/单条/Admission函数不变；3项真实PG以新ready API核401/422/202/200/409独立响应和两账本差异0，加原提交前/后不明两个参数，3 passed/0skip/5.18秒，临时库已独立按lower(datname)确认删除。静态通过；新候选真实小样本、故障、Kimi及容量均另待验证。 |
| E63 | HTTP16故障夹具上限对齐 | 产品故障探针仅将HTTP接收组识别上限4改16，保留第17条、未知来源及输入/结果身份不匹配拒绝；7个定向无网络/临时SQLite提交回滚用例通过，私有新v4控制器3项纯检查通过，旧v3字节及旧故障证据不变。Ruff/AST/差异检查通过；额外strict mypy的9项既有错误与HEAD逐行相同、单独保留，不扩修。该夹具资格不等于已发生新的真实故障切点。 |

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
- E60检查点续接资格 `118260266ed0ff28139a1e64fc9da86c2c0466c46c0953cf85b18b6714214bdc`，原计时trace `53240d1182ca4eb64785792d3bd01202e775abbc3bd12e3d60cd0c8ed253f8ca`、分析 `f87965145d36bf2f17affac429fe2cff30bfaeed447566f108559566ec13b166`、Target `5bccb581a78585412c2b039a8e8d8afda897a60a96c2269a2001b98203d98feb`、Control `361d5146f924ce8bde2a3bdd02f80ef3f20f4cfbf59cc81caea947354bbfc658`；计时卷退役 `111388af9c2208ccb4b8b21f4af0ea134fc7e8fa51f022c858376755f8c735c9`。
- E61源码核心资格 `0275bcefcff584bfa78c0ee80665f83ff7e1c7811debea9d74d9dd11909d5c8d`，独立宿主复核 `b0edbbb633a3d20d6f6e5de6985deceb217036f89900c4fc7ed444d55de6bb11`，两测试路径资格 `31857fc4617c18c94c1f16ff2573df3c50bf354becb9c2be9142a95bce2866dd`，retry_report派生元数据纠正 `fe2891d73bca1016dbfe1e6c8ccab8757463de68c8fdd1ad5c0829773ddc339c`。只核实指定内容链，不将旧SHA结果复用于后继新改动。
- E62实现冻结 `579b52f31f2b9ea91fcba509b5d97de20b0bfae6e26dac0d785369b8537380eb`，独立peer `9d5dae48ae808b4ddbf866cf1b3b2c5dc92d278a4cfed4dc7eb5ec924f8f9d29`，真实PG/清库完整回执 `803f73b8b3216754b852cd00462bb7c460b4f5cafc2dbd44b6feb9a5372c867f`，JUnit `5ecdb297be4dd367fa3cd27fea2460ad406c8da4c8e135e313d77dd9478a9235`；这些回执不包含新的真实发压或部署。
- E63完整夹具资格 `002e0d0e328b32bee1dbee8c4537c466992bc8790bf8afadf3ed821be627c6dc`；探针源码 `d5a610621f6095367099a8f7f9006ec68f74f05d33211c83c8ee5aa92b3da460`、直接测试 `2e1294a40df9b509ba7fc1a0102067fcd86d3fb12c1c44d72d7d52b27df14b94`。后继新候选与新故障scope必须另行固定，不混用旧现场回执。

## 后继实测与修复证据

| 编号 | 已核实来源与范围 | 能支持的结论 | 不能支持的结论 |
| --- | --- | --- | --- |
| E64 | `afcd591ad36e4822f6463500ecb0392fda9482f7` 的 HTTP 20/200 条及同一发生器忙路由200/闲路由20条；每轮 Target 与平台完整 oracle 通过，正常停源、Admission 归零 | 完成观测上界分别3.104148/6.168425/10.349216/3.125409秒；忙闲实际 POST 区间重叠1.001505秒。四轮共440条真实正确 | 不是正式10000条或公平时限验收；其后旧夹具实际16条切点被旧4条上限误拒，原20条未接管且原失败保留，不能改写为成功或重发补齐 |
| E65 | `0112faef7d28558be2c1299154b3911c4c3b02bd` 的两轮全新 HTTP T1 提交前/后，各20条；修复的实际切点验证接收组16条 | Target/Control 各20条逐项正确，原始提交权威、同容器恢复与历史 Attempt 不重叠已核；完整责任恢复上界33.264986/31.097580秒，均满足120/600秒。正常停源、Admission/Broker归零，恢复原普通环境 | 每轮5个故意停机期间采样错误保留；不是零错误容量/600秒观察，也不改写旧夹具失败 |
| E66 | 实际 Kimi 审查发现并修复的证据缺口：Worker journal标记缺字节绑定、提交失败断言过宽，以及激活测试漏核重试策略冻结 | 探针对同一文件描述符有界完整读取并记录SHA/长度；提交异常及零行反例增强，32项纯检查通过，修复实际Kimi复审闭合。原请求重放/旧历史各1项真实PG通过，0 skip；新增四行重试冻结断言及实际Kimi复审闭合 | 这是夹具/断言缺口；不能据此认定原产品丢数据或否定已有独立 journal/目标故障证据。旧回执保持原SHA，新增代码须进入最终审查链 |
| E67 | `557baa1f3d45819ac7e712a777f8b640a999d633` 的关闭池修复：原生接收关闭超过15秒时不再无条件重置共享池 | `aclose` 仅在原生 owner/probe/start 及 supervisor 真正结束后返回显式成功；HTTP 仍实际排空，仅成功才在同一锁内 dispose/清空池与 factory。旧源码12个反例失败、修后12项纯检查及2项真实PG通过（0 skip），独立审查及真实Kimi七文件增量复审无新问题 | 未观察到的超8连接或数据丢失不冒称已复现；该修复只涉及关闭分支。旧正常/故障证据保持原SHA，新候选调度时限及正式容量须另外实测；三份规划文件本轮只审D7.7增量，最终全文审查尚待完成 |
| E68 | 最终产品候选 `557baa1f3d45819ac7e712a777f8b640a999d633` 的三项真实调度SLA：十入口、1失败+150pending后的健康入口、真实丢失十次结果通知 | 健康输入分别10/9/10条，两独立oracle逐项通过；pending至T2 / 终态至结清的最大上界分别1.749814/0.141000、0.924608/0.102592、0.879936/0.866753秒，均≤2秒。各轮Admission/Broker归零，正常停源；阻塞源的1失败+150pending保留原独立库，不人工跳过/重放 | 该轮测量修复了仅恢复期接口就绪等待的夹具接线，原r1未执行并保留。三项SLA不能代替一万条、资源/管理面与600秒观察，也不代表全部OCR或UI通过 |
| E69 | `557baa1f3d45819ac7e712a777f8b640a999d633` 的HTTP正式R9，原10000条及5/60/600+600秒判据未改 | 5952条首发（302个202、5650个429），4048条未首发；总24435次POST（3528个202、20881个429、26个未知）。实际持久接管3553条全部成功，其中25个客户端未知已接管；Admission/Broker归零，来源正常停用，原owned进程全部结束 | **正式容量失败**。完整目标/平台oracle保持原10000期望，均缺6447条；未完成原正常容量600秒观察，不向实际3553条缩小期望，不重发/改T0，不放行MQ。本轮不支持D7.6带来收益，也未据请求机器码认定瓶颈 |
| E70 | `ec97d71da62d8184778faabc521489d6d1cba6f4` 的RabbitMQ属性预解码测试补充，仅改一个测试文件 | 实际Kimi核实encoder样本未覆盖部分合法定宽tag，新增15个手写wire正例及14个截断先拒解码反例，使用实际Pika decoder/assembler核边界和释放；31项纯检查通过。只拒f/d的运行时mutation使旧样本仍通过、新两例失败；精确增量Kimi复审无新问题 | 原产品解析宽度与上游一致，此为保护性覆盖缺口，不冒称产品错误或新增来源能力。产品/迁移/Web/工具树与557相同，本项不能抵消HTTP R9失败或宣称全部OCR通过 |
| E71 | R9已封存原始逐请求、资源、管理及完整平台报告的独立宿主分析，无新压力或原件改写 | 576个资源完整样本、0采集错/断尾/OOM/限额突破，管理1716次请求全200；Control最高60秒代理窗均0.834核，不能据整窗低均值判空闲。首轮/重试429的p95分别165.744/24.273ms，不支持多数为1秒ready到期；26未知集中整体截止附近，原完整平台映射实际25存在、1缺失 | 未采线程/GIL及外部发生器CPU，跨VM时钟未硬对齐；代理窗管理p95低于500ms不替代官方load/post与600秒门禁。未知原日志无异常类型，不认定具体网络/超时原因；当前数据仍不足定位根因，仅支持D7.8有限诊断 |
| E72 | 用户恢复本地URL权限后，普通557产品环境的真实Chrome DevTools独立会话UI验收，1200×900 | 原已登录会话有效；取消新建保持9个适配器，HTTP来源取消后仍未配置；新建测试项保存来源/节点/代码，刷新仍“尚未开启”；明确确认后才“正在接收”，停止后各责任0。显式解除其绑定并删除本次零消息测试项，恢复原9个，原5695+1000成功数据保持 | 未向该测试项发消息或执行用户代码，不用本项代替完整业务/停止排空/移动端验收。额外发现有绑定时删除仅显示技术错误码，保护本身生效，另记易用性后续而不扩本轮修复。MCP输出文件受路径策略限制，实际AX工具输出与观测回执保留，不冒称保存了截图 |
| E73 | `5db739f8a3312bdb44b7f50db65f8c6bcbda5a0a`（产品树与557相同）的唯一D7.8真实200诊断；实际配置、同容器PID/start/boot/时间命名空间、源码及来源摘要绑定，621条/75,242字节观测完整 | 30组/200新接管、0重复/原生拒绝，Target与平台逐项200正确；native无重叠、墙钟合2.884085秒/线程CPU合1.039915秒，23次已有后继交接合28.7666ms/最大5.1971ms。封组均6.67条、仅1组满16，无ready到期；正常停源、Admission/Broker归零，恢复普通环境及旧5695+1000，临时卷导出后双环境全容器0引用才删除 | 没有证明原10000风暴复现；墙钟减线程CPU不能定成数据库锁时间，观测含hook开销。组外263个429不等于ready到期；分位数沿冻结分析器的floor定义。不能由小样本或交接占比判容量、排除大突发机制或直接选择产品优化 |
| E74 | `6e9fe87720b9ecfe62f45bf0762f587192a95dac`仅增强HTTP来源隔离测试，第二订阅使用独立有效凭据并故意取同一合法epoch | 原订阅可查原Receipt，第二订阅有效token查它得404，第二token用于原路由得401。唯一真实PG用例1 passed/0 skip；运行时仅移除订阅谓词后新增404断言准确失败为200，原产品与fixture文件不改，临时库前后均确认不存在；GPT独立复核无阻断 | 原产品已有Receipt ID、订阅ID和epoch联合过滤，这是旧测试可能漏检的覆盖缺口；不是产品越权已发生。未调用新的中途OCR，最终候选仍待全部开发验证完成后统一审查 |
| E75 | `01b7b524c81b136caf16729d97302e1972a1d8f0`的精确14项PG补验，及`af287387a915d500948d824e2275d4d99c3a5b18`仅修RabbitMQ原生替身入口后的3项复验；均实际0054/独立测试库/0 skip | 首轮11通过：旧Webhook completed四结果、功能关闭仍T2排空、RocketMQ开关恢复两例/提交后ACK失败一例、新跨mode重放两方向及停止仍pending改额拒绝；后三例核精确409与原绑定正对照/责任及额度不变。Rabbit三例首轮尚未进入真正断言：旧fixture只patch open，当前open_context绕开替身；仅对Rabbit补真实工厂patch后，原三例7.748秒全部通过，5秒截止与产品代码不改。两测试库前后缺失均已只读确认并清理 | 这是首轮11通过加3项修后补验，不改写为一次14项全过；原39.114秒的三项启动失败保留。受控原生Transport、实际Runtime/T1/PG，不冒称真实Broker/Worker测试或Kafka三个未配置隔离集群的历史skip通过；不替代容量或最终OCR |
| E76 | 参考目标55项、批次lifecycle15项及另1项取消race的原始回执与当前源码只读对照；E34真实丢结果恢复、E37三个真实Worker负结果另列 | 四个参考测试文件及参考代码与原55项回执完全相同，静态参数计数闭合；含7个实际PG目标用例，覆盖权限/认证拒绝、真实提交后客户端结果丢失与成员事务。原lifecycle五个函数AST及核心服务保持，原15项加后来取消race1项支持明确生命周期范围；本次跨mode精确409缺口由E75两项补齐 | 原55/15日志没有逐nodeid XML，归集明确为整文件及参数数核对，不伪造参数ID。15项无真实Worker，E34/E37只证明各自实际切点/结果，不扩大成真实Worker全部取消/权限/预算矩阵；完整映射留私有索引，不据旧聚合数量宣布最终候选通过 |
| E77 | `21fa0a0168275ef3d7d20f00739e16c956d7b70f`的唯一D7.9原生分段200诊断；实际六源/观察器/同进程身份及SQLAlchemy2.0.51、未包装commit身份绑定，原始696行/117,995字节完整 | 实际27单元/200新接管、双oracle200正确；27个成功单元构造/receive/commit均恰一次正常返回。内部native wall1.669926秒/CPU0.496639秒，exclusive scope入口0.671075/CPU0.041655秒，persist0.273303秒，clock200次合0.100202秒；cold27/hit373只是方法次数。原事件16次已有后继ready条目可填，支持D7.10试验。正常停源/Admission/Broker归零，原Control停止后先导出final，再恢复普通环境/旧5695+1000/资源/ID段，临时卷零引用后删除 | 外层native包含摘要flush，内部阶段不含该flush，不能混分母；原残差和unknown规则保留，scope与wall−CPU不等纯DB等待。重复快照不能相加为可省条目/事务或外推吞吐；D7.8与本轮同产品已存在明显波动，不能据时间差宣称优化、复现万条风暴或正式容量通过 |
| E78 | `643265c5e7826cccc03bf0ede74496b7f816b845`的D7.10就绪前缀重包，仅改ReadyQueue及其直接测试；独立GPT产品/纯与最终融合PG函数审查无确定阻断 | 旧源码两个整组/部分前缀反例准确失败；新19项受影响纯检查通过，0 skip，保留真实Thread/Future、FIFO/字节/原期限/取消关闭/submit与提前完成的恰一次所有权。新3项真实PG均先两ready组形成一个真实native：正常混合401/422/202/200/409/202；真实提交前抛错或提交后抛错时原6项均503，恢复factory后原ID/原文再经融合安全重试，原绑定/原文/Envelope/2Receipt2Delivery及计费不重复。0054与逐例源码绑定、临时库前后缺失及清理已核 | 不是旧两个直呼native的未知提交用例，也不是只验证正常融合；没有启动Broker/Worker。19项纯与3项PG不外推20/200/实际融合次数、容量或最终OCR通过；新交付仍须实测 |
| E79 | D7.10私有实际native观察器的宿主冻结资格及独立GPT只读复核；后继现场另列E81 | 实际native入口分配unit id/人数/13阶段与结果；有限take前后真实head身份及人数关联可证明融合/部分尾组，WeakKey与未知null防误判过期头和泄漏。冻结v1的6项及v2的4项新纯边界各自通过，增量复验不相加；输出/原始日志与helper全局不变，不伪造旧collector/ready一对一事件 | 该宿主冻结点尚无新候选运行身份、20/200或融合次数证据，后继E80–E82不改写原回执范围。独立审查未执行这些测试，不冒称Kimi；实际200须核精确source/进程/配置/路由且至少1个确认融合单元，partial另报，未知不能当0。E68健康输入每路由一条不触donor，E65原16项cut接口不变，仅按原SHA范围转移，不冒称新SHA重跑 |
| E80 | `e1fd145e0f5969b3cf661242f40c6aeaffbdad39`的唯一HTTP warmup20真实小链，复用98份已验证前端资源及原固定资源，未运行额外plain200 | 20条按原输入与T0发送，Target和平台两个完整oracle各20条通过；producer退出0，首发窗0.003678849秒/并发峰值20/放大1.0；宿主启动至观测完成上界3.127363791秒。版本校验正常停用、Admission/Broker归零，普通环境原5695+1000条责任、资源/凭据/挂载/DataCID及ID段保留，原配置/source557和原context字节恢复 | 这是20条正常功能资格；上界包含启动/轮询，非精确T_accept或2秒SLA，不证明万条容量、最终OCR或新原生融合次数 |
| E81 | 同一`e1fd145e0f5969b3cf661242f40c6aeaffbdad39`的唯一实际native200诊断，实际进程/配置/源码/路由与原始日志绑定已核 | 原200条新接管与双完整oracle均通过。实际22个native单元，22个provenance均已知，7个确认跨ready融合单元，其中1个取donor部分前缀；provenance未知0、duplicate200/拒绝/raised单元均0。真实commit22次正常返回、fresh clock200次；native内部墙钟合1.556837614秒/同线程CPU0.481104516秒，宿主观测完成上界8.486198417秒。正常停源/Admission/Broker归零、停止原Control后先导出原始final、普通环境恢复及零引用临时卷清理均完成 | 带观察器且仅200条，未测ready排队时延；内部阶段与外层统计分母不同，wall−CPU不等数据库等待。不把旧27与本轮22直接换算成吞吐收益，不外推正式容量或最终OCR |
| E82 | 同一`e1fd145e0f5969b3cf661242f40c6aeaffbdad39`的忙HTTP200/闲HTTP20代表链；原Generator总配置并发100+25=125，原输入/T0与时域保留 | 两条链Target和平台oracle分别完整通过200/20条，producer各退出0。实际POST区间相交1.171506125秒；一次顺序只读见证先观察到闲来源accepted=succeeded20，随后忙来源accepted149/succeeded56/pending29/processing64。宿主观测完成上界分别7.234125041/4.167025秒。两来源版本校验正常停用、Admission/Broker归零，普通环境原事实/ID段/资源和原context恢复 | 真实请求重叠及该次闲来源先完成不等于全负载公平性、2秒调度SLA或正式万条容量；没有为凑见证加量、延时或重跑，也未新增MQ/SLA/故障矩阵或调用OCR |
| E83 | `e1fd145e0f5969b3cf661242f40c6aeaffbdad39`的HTTP正式R10，原10000条输入/T0、失败原件和实际逐条目标/平台oracle均保留 | 首发10000条在4.878964219秒内实际开始，5秒门通过；29534次POST=客户端截止前确认8212个202、21298个429及24次未知，未知24项逐ID均可映射到平台记录。最终实际8236条全部业务成功，98个批次/Attempt；不能据未知项最终存在推定其均于60秒内提交。正常停源、Admission/Broker归零。399个完整资源样本无采样错误，并有停止通知后的2个尾样本 | **60秒接管失败**：原10000期望的两个oracle均缺1764条，不缩小期望或重置T0。未完成完整600秒观察，容量仍false、最终OCR未完成；429原日志无machine code，不能由状态码裁定具体资源/配额门，未知结果不等同未接管 |
| E84 | `6fdca919273ec98b405acb5c71ee71f2e8f64a1a`的唯一D7.11 collector-claim metadata200诊断；六运行时源码仍同e1fd/Ready26bfc，先6项新纯边界及GPT独立审查，再核实际owner/config/route/零发送 | 21个实际native/200新接管，21次commit正常返回及fresh clock200；ready融合6/其中partial1，provenance与collector未知均0。5个单元有可容纳collector前缀，prefix1三次、5和3各一次；heads8/7/8/5/5及pending1/1/1/5/3，剩余期限约24/24/25/4/2ms。open6/absent15、full5/更早ready挡头1分维度记录。双完整oracle200、正常停源/Admission/Broker归零、停止原Control先copy final、原普通环境精确恢复与单临时卷零引用删除均通过 | 仅观测而未flush/seal/转交或改产品；full/blocked等边际类别可重叠，不能相加，重复观察也不能当独立可省消息/事务。正机会支持D7.12专项设计，不证明主要根因、吞吐收益或万条容量；不把旧22与本轮21之差当优化效果，未新增正式轮/OCR/SLA/MQ矩阵 |
| E85 | `c9da9bb931225a00017e4ca98a355ac3220369d3`的D7.12机会性整体提前seal；仅API初始化接线、Ready准备步骤与直接测试改变，Root/B2/cap独立GPT差异审查无确定阻断 | 旧源码两真实pool反例首native仅8/9条而非16，失败保留；新17项＋受影响原19项共36项纯检查通过、0 skip，覆盖FIFO/字节/期限/hint与关闭竞争/恰一次所有权及prepare副作用前后异常；Ruff/格式/编译与两源码严格mypy通过。真实collector＋ready的normal/before/after三PG首轮3 pass、0 skip/7.733秒，0054及源码绑定、专库前后缺失/清理已核；正常混合响应及提交前后未知后原ID原文重试不重复2Receipt/2Delivery/计费 | 不是只验证两个已ready组或单纯构造参数失败，也未重跑旧ready-only三PG；纯与PG不证明实际早seal频次/吞吐/正式容量或最终OCR |
| E86 | 同一C9候选的唯一HTTP20、实际native200及忙HTTP200/闲HTTP20代表链；原输入/T0/资源与98份已有前端资产保留 | HTTP20双oracle通过，producer0、首发0.003897859秒/峰20/放大1.0，完成观测上界2.096785333秒。实际native200双oracle通过，19单元/commit19/fresh clock200、ready融合11/partial0/未知0，claim后collector机会0；原始175行/53,961字节完整。忙闲两套双oracle分别200/20通过，实际POST相交0.285137730秒，自然顺序见证先闲20完成、后忙accepted78/succeeded15/pending8/processing55，完成上界5.195345458/2.135002375秒。各轮正常停源/Admission/Broker归零、原事实/ID段/资源/凭据/挂载/DataCID/Generator及普通环境精确恢复，200零引用临时卷清理完成 | 小链及带观察器诊断不是受控吞吐比较；claim后机会0不等于早seal次数0，未测实际促封调用次数，旧21→新19或机会归零不能推出因果/收益。完成上界不是精确T_accept/2秒SLA，顺序见证也不替代SLA；旧SLA/故障/MQ只按历史SHA与未变组件范围引用，未在C9重跑整套；原正式万条、完整观察和最终OCR仍待 |
| E87 | `5ba374ea98285a3f7a2ecab6d3662d443eacc70c`的HTTP正式R11，原10000条输入/T0及5/60/600标准保持 | 实际首发9417条、未首发583条；已首发跨度4.996324606秒，不能以该跨度替代完整10000条/5秒门。29123次POST=客户端截止前8409个202、20688个429与26次未知，查询/503/永久拒绝均0、并发峰值125。最终实际8434条全部succeeded，100个Batch/Attempt/Outbox，配额audit为{}；来源正常停用并排空、Admission/Broker归零。291个完整资源样本无采样错误，含停止通知后2个尾样本；后继已恢复原source557/普通R8与精确原context，原5695+1000/ID段/资源/凭据/挂载/DataCID/Generator保持，R11失败库未改 | **正式容量失败**：首轮漏发583条，原10000目标/平台双oracle缺1566条；不能称最终8434条都在60秒内接管，未满完整600秒观察，容量仍false。保留原失败收口及后继恢复为分开的原件，不重发/缩期望/重置T0；429无机器码证据不能直接定性某个预算为本轮主因，D7.13待实现和验证，最终OCR仍待 |
| E88 | `17f74ee482a40fedb4be3293b0a9b27885284c6d`的D7.13 raw预留缩额；仅`_request`合并路径完整读完正文后归还未用raw的4行产品改变，Root/B2独立GPT审查无确定阻断 | 旧源码真实小正文反例在25个held请求处失败、期望31，首败保留；新14项纯代表14 pass、0 skip/1.33秒，核2KiB持有31项及第32项读前拒绝、64KiB仍25项、EOF前足额/读取取消、真实Future取消/关闭/submit拒绝/原生错误及stalehint回退的恰一次实际held归还。Ruff/格式/编译/API严格mypy通过；其他13个顶层函数及两个类AST、Ready/T1/SQL/资源上限不变 | 256KiB完整Envelope预留不缩，32请求/8MiB/8native不扩；网络审计阻止一次依赖能力探测的bind尝试，无成功网络调用，不写成零尝试。纯验证和31项算式不证明吞吐、真实PG/Worker/Broker或最终OCR通过 |
| E89 | 同一17f候选的publisher测试补证；产品publisher未改，新增已确认前缀/未确认后缀的一个测试函数 | 原7项同测试内容纯检查通过；真实ControlPG四项首轮4 pass、0 skip/7.995秒，含新增部分发布确认后仅重试未确认后缀的用例。四项均核0054和同一实际OID1709927，专库执行前后均缺失、清理已证；原纯检查setup首败与后继通过分开保留 | 使用fake Pika与真实ControlPG，未启动真实Broker故障、Worker或来源集群；7纯与PG4不相加成独立产品/恢复矩阵，新增测试不改变publisher产品或替代原真实切点及最终OCR |
| E90 | 同一17f候选的新HTTP20、实际native200、忙HTTP200/闲HTTP20代表链；原输入/T0/资源与98份前端资产保留 | 三条小链均完成各自Target＋平台完整oracle：20、200及忙200/闲20。实际200为16个native/16次真实commit正常返回/fresh clock200；386次POST含200个202、176个429及10次客户端unknown，provenance与collector观测unknown均0，两个口径分开。忙闲总并发100＋25=125，实际POST相交3.193299953秒；自然顺序见证先闲accepted=succeeded20，后忙accepted200/succeeded174/processing26。各轮正常停源/Admission/Broker归零并精确恢复普通环境/原事实/ID段/资源/凭据/挂载/DataCID/Generator；200原Control停止后先copy final，唯一临时卷导出后零引用清理 | 客户端10次unknown约5秒但无error_type，保留未知原因，不能写成HTTP无未知；观测unknown0只证明其metadata范围。小链及带观察器的16单元不证明相对旧19单元的因果收益，不测earlyseal调用次数；顺序见证不等2秒SLA。原正式10000/5/60/600及完整600秒观察与最终统一OCR仍待，不重复未变Ready/PG3/SLA/故障矩阵来扩大结论 |
| E91 | 同一17f候选两组真实Worker/沙箱输出边界，合计6条消息、各1个Execution/Attempt；原harness首败和同输入窄续接保留 | 第一组独立Target有3次效果/3次目标尝试；实际Worker文件524884字节大于冻结524288字节，truncated=true、完整output为None、预览8176≤16384字节，Control三成员均unknown／`batch_output_truncated`。第二组独立Target同为3次效果/3次目标尝试；Worker输出未截断、512字节且原报告三成员succeeded，实际脱敏输出哈希与预期吻合；Control严格schema校验将三成员均记unknown／`batch_result_invalid`。两组正常停源/Broker0，共享Admission释放，input/manifest/失败责任保留，local/global audits均{}。后继实际正常恢复与metadata导出/零引用stage清理已单独核验，原普通环境/历史事实/资源/ID段保持 | 不把目标3次尝试混作平台3个Attempt；两组总计仅2个平台Attempt，第一组未重新发布。字符串/字节、排他输出旧stage、错误Member字段及续接缺订阅参数的harness失败原件保留，不改成产品首败或隐藏重发。功能final冻结时恢复尚未完成，后继真实恢复另有回执；仅证明本两种输出代表，不冒称全部协议/取消矩阵、网络提交不明、正式10000/完整600秒或最终OCR通过 |
| E92 | `a9667a42f1a6999632eab18d4842863c2ad6a25a`的HTTP正式R12，原10000条×2048字节、同资源及原输入/T0保持，Root独立完整核验 | 10000条实际首发4.816880112秒，首轮500个202/9500个429，按合法Retry-After重试；总29331次POST=最终10000个202＋19331个429，unknown0、未首发0。管理同域时钟接管上界58.861912276秒，最后接管后全部完成上界142.058056399秒，随后完整观察600.979349762秒。独立Target原10000行/有效写入/尝试均10000且0fail；Control原输入raw hash、Receipt/Delivery/成员逐ID一一对应，119个Batch各1个Attempt/Outbox，audit与Admission/Broker均归零并正常版本停源。827个完整资源样本，固定资源限制/OOM/节流及管理审核通过；压力与排空阶段三个接口p95约159.616/241.642/181.254ms，均低于各500ms限值，排空后各601样本 | 本候选的HTTP正常容量及完整观察通过；60秒接管上界仅余1.138087724秒，首轮成功率5%及2.9331倍请求量完整披露，不称首轮全收下或任意负载都有余量。p95门不等每个请求最大延迟低于500ms；不由R11→R12直接归因单个4行改动。MQ R7另由E93独立核验，本行只证明HTTP本轮；不代表B1–B5整体或最终OCR通过；E87原R11首败及其后继验证保持原样 |
| E93 | 同一a966候选的RabbitMQ正式R7，原10000条输入、持久/mandatory/confirm及固定资源不变；Root分别读取Target/Control原件和资源审核 | 原10000条发布至最后Broker确认3.782332890秒，nack/return/unknown均0；接管上界46.968527716秒，最后接管后全部完成上界150.014999461秒，完整post观察600.016937491秒。Target原10000行/有效写入/目标尝试各10000、0fail，Control原逐ID/raw hash/Receipt/Delivery/成员10000一一对应，159个Batch各1个Attempt/Outbox，无重复，audit/Admission/Broker0且正常版本停源。826个完整资源样本及原限制/OOM/节流审核通过，压力/排空期三管理接口p95约124.452/162.333/98.092ms，均≤500ms，post各600样本。后继实际恢复普通source557/R8及精确原context，原5695＋1000/namespace/资源/凭据/挂载/DataCID/Generator保持，MQ本轮库保留 | 本轮RabbitMQ正常容量与完整观察通过；与E92合起来证明同a966两个独立正式轮次，不是同时混合来源负载或任意Adapter性能保证。恢复实际重建Control/Worker/Web如实保留，不称容器未重启；未回滚/删除本轮数据。历史D389及各首败保持原SHA，主规格已实际同步，最终OCR及本批文档一致性/交付批准仍待 |

- E64四轮实测回执 `4ad2d2bed2ecb62590d700a384b244fa0966c9f51fdc1cdb47a68252b5de52bf`；旧失败安全收口 `a16e22812a1180e5a49107acf9c4d04a67a29a6e3d8534da57a4e2559cedd9cc`。旧夹具失败没有重置输入或T0；原失败与后继全新两轮分开保留。
- E65实际16条切点夹具资格 `97a300ca37fd1c83f31970d0f26fde464d7532868253d8fe870bf5cd8ce825cd`；两轮完整收口 `3d4538a9e6b7020d1496bb373be8089be02fbeee9a671c50442ee1215d21d2f1`，前/后切点最终回执分别 `258e3f1543661c5eb50793935663c9d5dc7bd02c1d3ada9c7f6e76d9690f0649` / `f73dc43f3e40fbed6ed7e5e33a83a8dd99cd525763aeed1e721d10d8378d1710`。
- E66探针审查闭合 `44235441eda07caf3353592ea579d5a1938b1e68fe5f8645a7a61fd72376f018`；两项激活真实PG资格 `83d1cf565e6f6d62cf98f209855eb887572c75f9157ea7a6f473dbf223bc6bf6`，激活审查闭合 `ce0dcb695c7980f987a0d85d5273e69985aeb226a5ac4cab76bc1ee309578a4a`。这些内容链不代表完整候选已经批准。

- E67本地提交与验证 `5cf08bf82ff5a26337fbe8de6d12d5ddbfa9ad75820da655eb17b12d59ee0a1a`；两项真实PG/清库 `c4533b74b477aa52c348106c6aff0ec8b7c4312ef0d0c552889c7cc1f07d84d8`；Kimi七文件增量核心资格 `2339167cccd75e225f48194c81d20d7922c931ffccaf8beb0b55ceaa1da08127`（13次实际响应、工具全完成，无实质意见/阶段错误，保留背景超过推荐长度提示）；旧证据的只读组件转移范围 `a09839b234d6e6ad30a818c896744cf53989be0004630bad25fd9b508551c1d8`。

- E68原三项实测完整回执 `fdd8d707582a9f905df38c6d525767b49b85f726e287ae112c42a04fd91722bd`；主Agent独立逐项核两个oracle、两类实际时限、Admission/Broker、正常停源及原始十次丢通知trace的资格 `57266ea097156d3490f120496a153f3816a7bd265535df7ccced735072e7364f`。

- E69完整失败收口 `218248c117925b483afd4014bd12c8556167663b35afeef98792dc816938bd6f`；原HTTP逐请求 `2b00396bb4479bce7f1ef17094b262cd7de58621e635bbee6c05b9fdfa7e653e`，原10000平台/目标失败报告分别 `311406872506b678c5a1951a3edaf253eee587bed4c1affd71a3344031cfe06e` / `30875034f021229d6c250620c85d5f5798a15c879e185858dcce3f8ae2ad61ea`；普通环境恢复 `d06c92628960ab3b2f48129037ba3d60a90a11274529563f147f9e02b11fcedd`。平台oracle本身按实际CLI返回1，外围reader误假设退出码的首败单独保留，原oracle未重跑。
- E70实际Kimi原三路径与精确测试修复的组合闭合 `83bafc52e11e44d1c6f11bd6f58c215ca1e3b672f2e49f3f7291c573918d239d`；保持原medium及三次辅助规划超时，源码不变、测试增量复审的完整输入和Main终态另核，不因退出0直接称全审通过。

- E71原请求分析完整回执 `d4d62fa9c687a6066c3361d42085f8da2c2343253b6f6b8d723071f3e30fbf91`；资源/管理/平台宿主分析完整回执 `95b971a820a74378c515ac4b31ecf45fb895c5348e32bef385231961c3ca6915`。均明确R9失败与原10000期望，不据无资源越界推导容量通过。
- E72真实浏览器步骤与限制回执 `d9820a6ccf67761b0f6880bdd2cf3a3abfeb2ba683b182ad123ff4ed91de05b8`；清理后的原数据/来源/目录和当前配置最小保护读回 `f1dae0db8a5e13903719803e350ed6e39b045de7f4775f195022718964799845`。首轮有绑定删除被正确拒绝及其技术错误提示保留，未抹掉新建来源的审计或路由墓碑。
- E73原始观测 `d739e3e5fc3d1acb587f2baf487316cc2bed8abfcdc99dc9b13b0f69e885f421`、冻结分析 `de155da2ad49726a1fafc6543f43cfa1ea5318ec72006e65260bfb2504346f80`、完整收尾资格 `da456e35927efa91ba45dc7ab6243636aaec506e12631e1cf2a318bd31abcbbf`；主Agent独立重新分析及来源摘要核验 `f843f22b673cb19724ac8f600f79ee6c8ea23fbf23534df83c10d81df394cb87`。实际配置检查点在首次发送前另行审查，不用host-only冻结冒充已运行。
- E74真实PG与反例结果 `0b1a746f90de162ec60e4a128dfabd0f2b5cd3a378b2e9f1a72146c99b9a4728`；私有测试提交回执 `6d83fd4407852cbd21f12bd1deb313f21b9edd7cdf8288c89e9b3d91a6cac282`。原运行时mutation失败作为预期反例保留，不能混写为修后产品失败或Kimi复审通过。
- E75首轮实际结果 `9480e014b641c9dec16585fbc104b8e3c5b1c7471d56f24e19e5b54962652479`、JUnit `c7b3d4fd592b4c22a0501a2f92c7a59bef925fc65072a26b7d36760eb81c2270`；仅Rabbit三项补验结果 `6e2dbb04c6fcfec9b19ef07fdc38eeaec58c8bec45fc9fd274a2427f98eadeec`、JUnit `4747b02f19f3640ad8de3fee6e0f1e14423d84815936fb97484231e2ae74d14f`。普通编辑者不能改平台上限另由部署profile唯一输入/采用写入、无平台写API、请求extra-forbid及入口limits已知键/部署上限的源码边界核定，不编造不存在路由的404测试。
- E76只读逐合同/实际函数及参数集合索引 `9ec6261a1873b4be6bd005a9eb8659cf40717dd26bc7eb0aa1a3099c6c8cf95a`；真实Worker与纯/PG边界在索引中分开，原回执与首败不覆盖。
- E77原始观测 `5256cd67817bdab2f0a8017ce905cfe77c21d935690819abd4e0a25366403bdd`、阶段分析 `77447297d12d12fae4458f555d1455ae2c03174b5c53cb77fdd83fc096119d02`、完整收尾资格 `c3d09c5359bf68f2f73c937fa58b059ca0c2941b1d0cb90c98c81be1345dde40`；主Agent重新分析、成功gid/实际身份核验及packing快照限制 `75990eb8d4197bea8bd238f5ce67c9b1b5b093bef742c910a260a22cbea83f6f`。宿主冻结元数据中的系统Python命令未执行，实际prepare/run使用已有3.13虚拟环境，单独保留操作回执，未改scope或原证据。
- E78代码/纯回执 `be617eb5455342859b65f6789951295676d91c4b726e1ea5b344a0953269ac51`；最终三参数PG函数与静态回执 `d1ab5e86e528c2a88a34bbf827bc7f7280d6447fa38584e4846b6b720c0d041e`，真实PG结果 `688c3b07ee332f26108672f0ae28566a614624b82e975c4f4d3f6e0c16d2ccca`、JUnit `85e73d403ba60d8eb26b51554c8ebb5e28b4626530e6320de7553647a2d27d6e`。原计划两项未经过融合的范围差额在执行前已收敛成当前3项，不扩测试矩阵。
- E79观察器v1/v2宿主回执分别 `432aa748978d4b9c0bc65535c4bd30941b49790dbc0a23c5cb4de2efeefc5f75` / `b40c44a9b4c229c89dc41e94e87547b935940d91ab447b4a67551798a663a249`；主Agent捕获真实独立GPT消息并核10个旧Ready方法AST/7个保护源字节不变的回执 `77e2a0580c3f2519314c2e001bb00b7ce273636ee2d7d8d4dff703c0b7121b2f`。该回执明确审查者未执行测试或现场，也不代替实际运行资格。

### E80–E83 小链与正式首败索引

- E80唯一HTTP20完整收尾 `119107f1ebaf0163322d3bde2b8ab8c9d4bfaf6fcff7aeb170880b3dc091d5d2`；目标与平台oracle分别 `39194ddabc2b54cf275ac988d4dea5e3312673928de4b7b8ae60fd33a2042e96` / `9dc7135a3e59d800e9ea6edba072b2c63564cd8851da32c4db29e16c886070f9`。
- E81实际native200资格 `9db26b856754f95f7c2a9fe6a1e27156bcdfc63a779270b5fb658b12fe9bcde2`、完整收尾 `4fb0112e71ac30f99930bbf23fe68dc6a1466adf7a164abdf968632d1c90cbf9`、原始观测 `31b9a91cc9237dd3480283837d315964e743f59d8ef153bd9d4bbabe9c100020`、分析 `d8c45283435a8441df28f6118c66cdf50b57b15082ce8d8c1479846d5962ce8e`。
- E82忙闲链完整收尾 `1fa86017c39dba3ff8402e7482e8e23ddf3c224d5d0bf0a6de8f942069633a4b`、真实请求相交证明 `1832390a4d2666ac6ce29dc3d3c97b693f2ce327050bc57b989ebc5b2950301b`、闲完成且忙未完的顺序见证 `3ae4484e4dd2577dc90789e97ea8cc7d9350ba3ccf5624ec291fcf875aaec510`。
- E83正式首败收尾 `4bb5e9100bf240854dbd41a0100a5f98d1704ee20f78ec9ba9831a6180843d80`；宿主只读统计 `168c595d701a6b6f408b2dc39080917de16f9f864928c2c7ac9c56ec7e12dedd`、解释边界 `09c65cac6a5aa66c0c0f6bac40ac8b5c87e07bb6b1bf056dfac8fb7ebc77441d`。677个server-default创建时间条件桶只涵盖新持久项，不包含纯拒绝/重复事务且未排除时间戳碰撞，不作actual native/fusion计数。 普通环境恢复的首次readback键错误保留，有限续接完整收尾 `7a3d3365f62cedbd8ea2309b7d8a3226aa6007f079e306cb8ab9d806e447026b`、续接资格 `88a3fd73f1a43c038c6467f5f965a30a22fc6e6f02c28e6dc916b8892fcda7db`；原成功责任、资源与配置恢复，未重跑正式输入或改写首败。

### E84 机会观测与后继设计索引

- D7.11观察器宿主回执 `ebcf3355b3361ab14faa23a1b5db9a50cf9320e4ccb2cb163a6450c3e1d64f0c`；6项新纯边界最终0.062秒通过，原v2/v1/D79/base字节保留，初次动态fixture imports的静态F401与修正元数据另存，不是产品失败，不重复旧观测套件。
- 唯一实测完整收尾 `31dbee3df11515b0ed776baa91e5fa7e64ae8128eb3aaf1af76c5faaec25e491`、分析 `6d343b61e664cd9cd562eef77f2e423200058298d1650c9cd81da2b0a9d64583`、资格 `e3464baefc335d6f2e8349830e3f1d00042c35f79d22e53c7139d830aec855eb`。这是额外200条诊断，不修改R10首败、原10000期望或5/60/600及完整600秒门槛。
- 3.17仅诊断已完成；其冻结点的3.18/D7.12为待实现专项，后继实际实施与小链另列E85–E86，不回写E84为产品优化。T1/SQL/资源保持；最终统一OCR与两来源正式门槛仍待。

### E85–E86 D7.12实施与小链索引

- E85作者纯/静态及旧反例回执 `e417eaffa289c560bdf46725555d9f1587cf7c3747a1ed2610f62e7cc277ccbb`、独立GPT审查捕获 `8511c179f407f032b3a613c2a5515d6a9ce544a659b8299a886efdbb6429a7bf`；实际三项PG完整收尾 `8615f29acaacf92f32abcbbe3cc7d9a5861cd8abfd2d954462c46834bc59b6a6`、JUnit `72c736c54e5353a35b723e4bba6f7ea4a08816d9d24b9132e4621ab55e967b19`。作者回执中“PG尚未运行”仅指其冻结时点，后继真实PG单独记载，不覆盖原件。
- E86 HTTP20完整收尾 `e27f8b1f27f4e2aa9d1485b7bfb86964617e09fa052ce649e29650bcfded7f56`、资格 `2a0851c2e4d52eed4050c45a2d8620626ac2bdfbaf49f460f273a223c7f0291d`；实际native200完整收尾 `74d82d3910948cd9ff1daf37d056ddcce6501dfa4e48dc817f2789dd05decf7d`、资格 `7ce92afa11b331994d15c4a43870c79d4e0ecbcf93a206a05b38c067b12aed00`、分析 `7c203e4aa79648cf062bdedc9d860fbac68d8e84140bba4b4716be51455171a9`、原始观测 `3ed83f929a87c15fb98a5801df03bf33e8893a7a8204e4e49ac790b970eb0455`。
- E86忙闲完整收尾 `9610a78eeb3c4cc634da7bcca6b67b909df4f3e04179ca0819e02eb625ef4f5c`、资格 `7a0a4345949f86941d1257a62ebf6df348b4f1753dfd701597c33fa94ff85374`、POST相交证明 `3c568c50a4c95bbd1b4caf9fa69c8747deba1b11ec436f41457bccb6ecabff66`、自然顺序见证 `fa83e1f4e420e27fc27b3fc09b061941af887d18840a7fdd464f338bc2e736d9`。四份逐ID目标/平台oracle各自哈希在完整收尾中固定，不以单条健康或汇总成功数代替。
- 组件证据转移仅限已核内容：原Ready的`_take/_repack/_settle/close`等10方法及资源对象除初始化外6方法AST保持；T1、SQL、调度、Execution/Attempt、旧来源、迁移/Web/tools未改。E68调度时限、E64/E65切点及既有MQ轮次保留原SHA/样本范围，不改记为C9整候选重验。C9的新collector交接由本轮36纯、真实PG3及小链单独证明；最终候选的容量和最终OCR仍须独立闭合。

### E87 正式R11首败与后继恢复索引

- 原失败收口 `cabdb1d8294220d1fa0f4c55fab9a5c5fbf2c8f3adcafd4625e6070a7c68b072`；原10000目标/平台失败报告分别 `27a76ee43b02d1dec92a53ea1ca5a71f0fbb1d1e78918ddf981bbbfe6fef74ce` / `d52ed0f650830261fe9d038c29a76ece4834daec78c17c917201e27aaf10b7ec`。不能以实际8434条全成功覆盖原10000期望的失败。
- 原HTTP汇总/逐请求分别 `e0533ca72d02c1ac76942f5aca48d1beb5f9bfcc1581800585e22e4df10c500d` / `0fe2a254724fefbb0be4f50eb6b89693aea030c2ea77b2767f096f571297774b`；资源原始 `e57c46548397c9b33f1f0cb0c4a22deb429b0f31e586255325c7766d36130147`、停止证明 `f772ada4a779ff8a15f767ff456271780faa2b8d68e3c6b6efeba1976ff8952b`。原首败时fresh context尚保留未恢复，后继恢复独立记载，不改原回执。
- 后继恢复完成 `ee2d250c2d71831207ea280166742d6fd11c7c0e78c5d61029e608b56a67d932`：原普通source557/R8/context、原事实/资源及ID段恢复已核，R11失败数据库保持。该失败收口时D7.13仍是计划，后继实施与功能资格另见E88–E90；不因正常恢复、预算边界或小链勾完3.18/3.19或B1–B5。

### E88–E90 D7.13实施、publisher补证与新候选小链索引

B3按实际执行层分列：E33/E34的正常与准确恢复、E37的三种负结果经过真实Worker/沙箱；E89是ControlPG＋fake Pika的发布确认/重试验证，未经过真实Broker故障或Worker，不能替代前者。E91另证同17f两组真实Worker输出超限/实际脱敏后schema失效的代表及正常恢复；错/漏/重复成员等其余协议、取消与恢复断言仍按各自ControlPG/纯或既有真实切点范围引用，不扩成全部协议破坏与取消场景均经过Worker。

- E88作者完整交付 `0cce29bbf94233b716ae655f410c339f9055617f7a063669d3554b2f88dbe64f`、旧25项反例JUnit `f0917666eee07696f7065efa67d5ab3a15e5923968ac6d0960ab842872669349`、最终14项JUnit `8092550df02d2585815483dc3d145bea298993dd1108bd2a52dda778d3dd2790`、Root/B2独立GPT审查捕获 `50062857152dad4e4dec080a7482a669d6fa508a450f40e7e22ca342df4cab43`。审查冻结时publisher PG4尚未运行，后继E89单列，原回执不改；组件转移 `e4f07d50a33ccd51cfbceeb3b01bea548eb5111ce5a6bca60efa0fc5daf68422`仅限未变内容，不把旧C9小链改记为17f通过。
- E89新增测试交付 `8528e4f834fc54c92d241d67a50211e02a1189f361aa65c9908b4d0415731a6e`；原7纯回执 `e2f663d27c36a9f485d208fe43233120bb9ac41efb48e69f05f965e02b021763`。真实PG4完整收尾 `8cf8f563eca40b7b81b8acf072bfca5890e854c3d1c5579cee16242f323550c9`、JUnit `1cfa82bb84bcd0987c0f87d11fafac64ed06888d9ba000b3915017d4ba5deae8`；各nodeid、0054、同OID及专库前后缺失在原件逐项固定。
- E90新HTTP20完整收尾 `60e86adeb46efeb274600058eae5932fdbb8abb7889806d3bb13d5ed8ba4c100`、资格 `d9e458f30a3afe18b7e539981c5a360e1b056dcd45214320e339ab798ffbf11a`；实际native200完整收尾 `52c7538b9380e44c72e7c8eb32459eeab373b7e8b8af196eb302738e394e2d6e`、资格 `84c9c70710c73881609fb67aa4a8a7fa2f3bf4999c9c0875714cf30428a0a687`、分析 `100a63ee6c2a51d9db14d596ec7fcad2d2aabc951c19a8081d0de4627e95ca9d`、原始观测 `08418cb0580f0bdf9243f9ee15ba17914e55d56675acded1c5322e2163ff615d`。148行/46,145字节与实际进程/配置/六源码/四helper绑定，客户端unknown见原HTTP回执，不与观测完整性合并。
- E90忙闲完整收尾 `43da0c29bf910660c4d79d6cd41999a9c12bcc138b7c699395d42cd1a3cd7fbc`、资格 `e1d7741cebd5d49a5e7faabe1f73edafd02803c9717df0c861607fa125daa27f`、POST相交证明 `3d36a96309883c603b6cee5f6c5db563f088541167f7f9a1d2d68cabe1b0572f`；四份逐ID oracle及自然见证各自hash在完整收尾/资格中固定。三小链宿主启动至完成观测上界为20项2.089442083秒、native200项7.531953375秒、忙200/闲20各6.196030750/5.184202334秒；包含启动/轮询或观察开销，不是精确接管时刻或容量比较。

### E91 真实Worker输出边界与独立恢复索引

- 两组功能final `f38f3023e5b2c12868ca6449fa97e5c16356fb52fa093932a959e0b40636e799`；其`normal_worker_restore_qualified=false`保留为当时事实，不覆盖原件。第一组/第二组独立功能final分别 `4baef1195b68f82d8160dcb2aea560c1ebe21be4a7448644915b8d3358628c62` / `a560dbe77f80032896bfcc9a5b4808f0519fe5f7f7a951984385d5516d2511fe`，真实Worker metadata资格分别 `ae89a016db0492b2c9f54efeec1a27eb967655e1827bdabf9ee55988cded16a8` / `d112164fd4cdde26bbd16316111f272cece0220b7594391eecf98a521dbc7c74`。第二组实际脱敏输出及预期均为 `be2fa92af7090507d11e53c685757041cd1b9c52567e65961a79f4ded4c3589a`，不公开脱敏前内容。
- 后继实际恢复 `84d4e00c61e94b7f84e68c11c6742a2eb78bdead998628296268faef7ae7a4ca`、metadata导出 `73fcbc30343cdb944b5007e4c92772e6f0fb22291f7319ae48327cd169b929c2`；8个文件原字节hash读回一致，零bind引用后只删除本owned stage。正常恢复后，两组保留Control事实再次只读核对与原件一致；普通source557/R8/原context、原5695＋1000、namespace/资源/凭据/挂载/DataCID/Generator保持。Root逐件实际资格 `077fffed4f2f725d6662521de33b43792277491af8985f76e48e1f054c3be20e`只批准该功能及恢复，不批准正式容量或最终OCR。

### E92–E93 同候选两来源正式验收与恢复索引

- E92 HTTP R12 Root完整资格 `0ce284a5f0fd51f0700b405aabcca63694262d21facbf32d15a2f0e76f22ab94`、独立资源/管理审核 `127c6eb5ef3bd2d91876e0d9f9119bb8157665be2b9c393bb39d22e49ea61d3d`、controller final `58eed66d4bb0f8bf1075c6fef409f0200af7a6039a544d82faa9384a00b52513`、独立原件核验 `c612f5917dca64ea0349c9c3167cd2c72acfb9231eb96205a1694d0078766bd3`、原10000平台逐项报告 `bf5fce026ffc1711d548aec6d767eb0c636d1f65d868febdbe32cbb4a898cdae`。Root另读独立Target原verify、原输入/生产者回执及119批次关系，不以final成功替代oracle。首轮500/9500与最终10000/19331/unknown0分别保留，60秒上界仅余1.138087724秒。
- E93 MQ R7 Root完整资格 `6e009d7a1443f8ce97a01bbbdfd80b0ce703b8a7a58126cbdbe11deea0fdb671`、独立资源/管理审核 `2a956c17ecb8d6ab94b948b0e67c862e7c13139ba55a67814eccbf50a1c6b3d1`、controller final `11c2b1b364037eb17587ba92ccbb61ddb68b64771adf459f64a62d2f877024f7`、原10000平台逐项报告 `aacce5cd280e2e5b8d3f15f46639764d8974c342adff6d49cfab6d4ec68ef28b`；独立Target原verify及159批次关系另核；独立GPT原件核验 `5f6c12d874a1f11a799fbfd0605e33ec0871bc48e734eb945c021c549ca2ff3e`另核20000条发布/ACK逐tag、原输入/回执、159批及三端点818个slot/各600个post样本。两份资源工具仅负责其资源/管理资格，完整容量结论由Root组合两份独立业务oracle与实际门禁得出。
- MQ后实际恢复 `3fc9bfe26287cc562790bae21aaf7520bf9e6c292c95f4b012ae9be462c209a5`：正常重建Control/Worker/Web并等待API就绪，精确恢复原普通context，原source557/R8、5695＋1000、namespace及五角色资源/凭据/挂载/DataCID/Generator保持，Admission/Broker0，MQ本轮数据库未改。管理与业务计时按已核同域单调时钟，资源时钟不跨VM相减；两轮完整观察不拼接、不外推长期无泄漏，主规格已由Root同步并17项校验通过，本批报告、任务与规格一致性已核，统一OCR仍待。

### E94 OCR发现的请求长度边界

- 首轮 Kimi 审查发现超长十进制 `Content-Length` 可触发 Python 整数转换限制；实际 HTTP 解析器接受超过 4300 位的全零长度，旧实现会抛出未处理异常。该路径发生在读取正文和接管之前，不代表已接管数据丢失。
- 后继修复按去除前导零后的数字字符串比较 64 KiB 上限，保留非法长度的 422、超限的 413 和正文读取时的实际大小检查。相关测试修复前复现失败，修复后 34 项通过，包含实际解析器前导零、正常 2048 字节及上限边界；独立代码复核与 Ruff 检查通过，最终 Kimi 复审仍须完成。
- E92–E93 的正式容量实际测量对象仍是 `a9667a42f1a6999632eab18d4842863c2ad6a25a`；截至 `676c842bdf54003da67694d266b22441f2ac8189`，产品代码相比该版本仅变更上述长度校验。正常 2048 字节条件已作针对性验证，但没有重新进行后继候选的万条容量测量，不把旧测量记成新 SHA 的实测。

### E95 批量接收意外异常的诊断

- Kimi 复审指出批量原生任务的意外异常仅返回可重试的 503，缺少服务端故障线索。后继修复只记录异常类型、批次条数及代码位置，不记录异常消息、正文或凭据；日志本身失败也不影响原有 503 和资源释放。针对性故障、脱敏和资源释放检查通过，Kimi 对新修复的复审仍待。
- 相比 E92–E93 正式容量所测版本，后继产品代码另有本条异常分支日志；正常成功路径不执行该日志。正式容量仍只记为原版本的实测，不称后继候选已重测万条。

### E96 单成员重放、受控删除与隔离数据库回归

- Kimi 对正式规格的审查指出单成员重放、共享取消及清理边界不够明确。主规格和 delta 已同步澄清：重放只含目标成员，取消按可证事实投影，普通清理从最后一个成员终态起计算保护期，受控永久删除仍须先排空责任。
- 进一步核实发现重放可能与后续新事件混批；已限制重放独占批次，同时普通前缀遇后续重放即停止，保留原顺序。永久删除入口与数据库批次保护的归档条件原先不相容；后继修复在原锁序内仅于事务中临时标记，恢复标记后才进入可能提交的等待路径，保留未完成责任与数据库保护。
- `0ecf82f3a8e28da6378b5fb23b6f57e2937062a3` 的隔离 PostgreSQL 八项回归通过，覆盖重放与后续普通批次、近期终态批次删除及配额、未完成责任拒绝、等待提交和业务/SQL 错误回滚。第一轮 `72de85579639c8972ff6deb8a3a9d8cc04069da3` 因测试夹具配置版本为 0 而 7 通过 1 失败，原件保留；后继仅将夹具版本改为合法值后 8 项通过。两轮测试库均在原回执的结束时点确认删除，使用真实迁移和独立数据库；每轮有一条 SQLAlchemy 故障回滚提示，不能称无告警。独立核验索引 `af90e526e5b572cf505aecd4e05f3fb3e56cefee742531116989f667c742fe09`。
- 上述数据库回归不等于真实 Worker 链或新候选万条容量实测。E92–E93 仍是 `a9667a42f1a6999632eab18d4842863c2ad6a25a` 的结果；新普通批次选择增加 ordinal 读取与判断，需按最终候选另行核对适用性能门槛，不能将旧 SHA 的计时记为新 SHA 实测。最终 Kimi 审查和相关复审仍待。

### E97 拒绝及等待时保护批次历史

- Kimi 对删除修复复审发现：清理后若删除因运行中任务被拒绝，调用方捕获错误后提交会保留提前清理；等待 Worker 的正常路径也会提交清理。`7d45d0225f3be31e1019becc114eb63016faada8` 的隔离 PostgreSQL 回归真实复现前者，原失败和测试库清理证据保留；先前仅确认标记恢复的静态结论不能覆盖这一问题。
- 后继采用有界两阶段保存点：整个删除尝试在同一保存点内按原配额到执行的锁顺序完成或回滚；需要等待时先回滚清理，再重新锁定最新任务并提交取消请求。状态在重新锁定期间变化时先释放锁再从原顺序重试，最多两轮。`a2ff7f9c1b7b62326a3c56518c95255dbd301edc` 的 13 项隔离 PostgreSQL 回归通过，覆盖拒绝后提交、等待保留、状态变化、有界重试、异常回滚、真实批次删除及单成员重放；无跳过，测试库及其数据库标识在结束时确认不存在。
- 独立代码审查索引 `f6004409496a9472bd401b0b91ed8396c501e076a286379f99f2276315262263`；两轮隔离库及逐项结果独立核验索引 `269ea034c6376b301b0d5dbcba306190eac3c5914a1d9da67519c79a06597fac`。首轮有一条平台日志目录不可写提示，后继 13 项日志无告警。
- 本项只证明上述隔离数据库场景；不替代真实 Worker 业务链、最终候选容量测量及 Kimi 复审。两轮首败、先前 8 项通过与本次 13 项结果各按实际版本保留。


### E98 D7.14 分阶段预算与直接边界

- 当前d92f正式HTTP回归没有通过：首发8983/10000，1017未首发；客户端确认6668个202，Control实际接管6699条并已处理。未完成原600秒观察，RabbitMQ后继轮未启动；保留原轮及真实SLA三场景结果，不用历史a966通过覆盖当前失败。
- 本次按用户批准的D7.14调整读取/等待和native工作区，在原8MiB内分为1.5/6.5MiB，最多128请求；工作前记账、大小双维切组、同route单active/FIFO及真实完成后释放保持。没有改变T1/T2、Worker、发送方、CPU/内存总额、数据库连接或正式验收数字。
- 既有5份直接相关测试中143项通过，包括真实PostgreSQL组内独立错误、提交前后不明、原ID重试和collector/ready事务对账；另25项核对收集边界及新增大正文ready顺序、声明长度不匹配（其中2项是重复确认，合计166个不同测试）。7类复杂/近限/非法JSON的实际分配峰值小于工作区模型；账本和tracemalloc不是进程RSS证明。首轮旧常数断言、测试入口PYTHONPATH错误及既有测试Loop缺少is_closed的失败分别保留，后者仅补既有假时钟必要接口，没有新增辅助框架。
- 4个改动源码类型检查通过；严格OpenSpec校验通过。真实代表链、原正式容量/观察及新差异OCR尚未完成，不据此勾选3.20–3.22或宣布交付。私有日志/数据保留，原557/R8环境此时未切换。


### E99 真实200及按服务窗口补齐准入

- `78314f25f56df26cb305d4228898b31d89796845`完成同资源HTTP200：200个202，0拒绝/未知，首发跨度0.9643秒，Control与独立目标逐ID/原文/结果/责任双oracle均通过；来源正常停用并关闭，实际资源采样70项、无OOM，Control RSS峰值347914240 bytes。此为代表链，不是正式10000容量或600秒观察。
- 客户端平均响应806.7ms、p50 623.9ms、p95 2228.8ms，说明只放开全局128等待者会占住同一发送方的125个连接。按D7.14服务能力要求补同来源32个未结束POST窗口（一个完整active组和下一完整组），包括读取到真实native结束，超额在读前429；原128总上限、8MiB两阶段预算、1秒ready、同来源串行T1/FIFO及所有验收数字保持。新窗口不是另一个队列或责任账本，空闲来源键立即删除。
- 观测复用现有admin-only runtime-status，增加固定、无路由/消息/凭据内容的进程计数/字节峰值；不依赖未启用的info日志来获取结果，不新增观测服务。旧200及其DB/目标数据保留，不能追认为补窗口后的新候选通过。
- OCR第一轮因预估预算未开始文件审查，用777 token；后继4项入口/预算范围终态complete、无发现、实际172240 token，带预算宽限及一次规格路径读取失败限制。用户追加40万中的剩余73306；其他差异与后继修复仍待复审，不擅自加额或称全审通过。


### E100 D7.14正式首发通过但接管仍缺398条

- `d2396c2dbb24b642df429b4019b069fb363205ac`的新200双oracle通过：首发0.13084秒、200条最终正确，417次POST含217次429，无未知；18个实际native组、200成员、最大16，累计native1.105秒、队列等待最大189ms。原文/工作区峰值200704/4194304 bytes，总峰值4395008；真实结束后请求和两类预算归零。原783的200不追认为本候选结果。
- 首次正式准备使用了旧未绑定配置，management在任何POST前拒绝；接管0，保存NO_COUNT，正常停源/停止观测，未修改测量标准或新增框架。改用已有受检bound prepare建立新轮，旧证据保留。
- 正式R15首发10000/4.52339秒通过；原60秒客户端9570个202、20774个429、32个未知（总30376次POST），数据库最终9602条全部结清。独立目标按原10000核验只有398项missing_result，无其他失败类型；不能称容量通过，32未知不当成丢失。实际组及字节门禁计数表明拒绝仅来自同来源窗口，未触发内存/工作区/native/ready到期门禁。原8MiB与资源/顺序约束保持。
- 按实际9602责任全部结清、在途0和正常停源退出，协作停止probe/sampler；原10000/60及未完成600秒观察均保持失败，没有等待未接管398条也成功才退出。原环境DB与前述数据全部保留。
- 后继D7.15只等价优化字符串检查和SQL结构复用：25项相关纯边界/实际分配峰值、23项真实PG T1回归（含每成员新控制状态与fresh数据库时间）通过，0 skip；不缓存DB时钟/结果/配置，不改变责任或原验收标准，新的真实容量仍待。新增差异与剩余OCR覆盖保持pending。

### E101 等价优化后的首败与最小入口调整

- `19508abd0e1bba5de47b73958b24881e287d83ab`的HTTP200代表链双oracle通过，17个native组、200成员，累计native0.8748秒。正式R16仍失败：原10000中只有9928条在5秒内开始首发，72条未发；实际开始的9928条最终全部接管并成功，28823次POST含18895次429、0未知。不能因实际责任全部结清，把原10000首发门槛改成9928。正常排空、停源和协作停止观测后保留该失败、数据库与原输入。
- D7.16仅将两项只读忙提示的检查顺序调整为先route、后global；拒绝条件的并集、权威准入、账本、期限和所有权均保持。既有入口/准入82项针对性检查通过；独立忙ASGI微测只能说明该提示路径成本下降，不用于外推整链吞吐。
- 现有admin-only runtime-status的真实数据库/API权限回归1项通过。固定聚合只用于解释实际拒绝、分组和资源峰值，不作为接管或完成的权威事实。

### E102 最终候选HTTP正式容量

- 实测候选`6e77f16c58e1c41bffe91217cded34231a1909b2`。HTTP R17在原资源、正文、发生器与5/60/600及600秒观察标准下通过：10000条首发跨度4.517414497秒，全部持久接管保守上界58.198614709秒，最后接管后全部完成上界126.059066845秒；原10000逐ID/原文哈希/预期值和责任终态的目标、平台双oracle均通过。独立20条预热单列，不混入正式计数。
- 总28589次POST：10000个202、18589个429、0未知；首轮576条202、9424条429。保留这一实际拒绝比例、重试成本与60秒余量，不保证任意负载达标，不覆盖R15/R16首败。
- 完整排空后600秒观察通过，809个完整资源样本且无采样错误、五角色OOM/OOM-kill增量均0。Control RSS峰值308166656 bytes；HTTP原文/工作区/总账本峰值200704/4194304/4395008 bytes，实际结束后请求及字节归零。同来源请求峰值32、最大native组16。账本与RSS分别核验，不把二者混作同一上限。
- 管理压力及排空期adapters/events/backlog的p95分别172.388/207.180/172.123ms，均小于500ms；各接口有601个post样本。来源正常停用并确认关闭，内部Broker ready/unacked均0。固定平台4CPU/8GiB、Control1GiB、Worker两槽及所有原限制保持。
- Root完整资格原件hash为`bf3dc933269e24a939fa4e5939a444fbc4034d025aa92dff4ee166ddd70ad7ab`，硬件和管理时延补充v2为`7f55e75f51fb70b923cd02eaf1e894df5c7765ec923dc675e6bc4edcd6dffd2b`，v2绑定原件，原件保持。本项不代替后继同候选RabbitMQ正式轮或最终OCR。

### E103 OCR累计预算与未覆盖边界

- 本次追加40万token的可核实累计用量351882，剩余48118，包含前轮153677、本轮预分派失败777、4项初始差异审查172240及collector/ready审查失败25188；其余预算拒绝调用实际0。历史缺失用量仍按用户先前接受的未知保留，不重置为零。
- collector/ready该组因上下文压缩阈值失败；官方resume预估下一组50112，超过剩余48118，因此停止追加调用，没有提高预算或绕过工具门禁。初始4项审查的终态complete、无发现，仅覆盖其原base/head；后继API、测试、SQL/字符优化及规格差异没有完整最终OCR资格，不能转记成最终候选全审通过。
- 保留OCR1.13.3、实际gpt-6-astra订阅、原规则/报告/失败与预算限制。业务验证、宿主代码核对和既有179项旧范围回执均不能替代此次未覆盖的OCR；6.3继续未完成，不提交PR或归档。

### E104 最终候选RabbitMQ与操作暂停后的原轮收尾

- 同`6e77f16c58e1c41bffe91217cded34231a1909b2`的RabbitMQ R8原10000条全部得到Broker确认，首条发布至最后确认2.053734105秒，nack/return/unknown均0；持久接管保守上界47.181734184秒，最后接管后全部完成上界157.064137938秒。目标与平台独立oracle逐原ID/哈希/预期值和责任核验均通过，目标10000行、10000次尝试与10000次有效写入，不以Broker确认代替持久接管。
- 管理及计时分析通过，无失败代码，各接口601个post样本和完整600秒观察；压力/排空期adapters/events/backlog的p95为92.987/138.101/103.893ms。926个完整资源样本、0采样错误和五角色0 OOM/OOM-kill增量；Control采样RSS峰值308183040 bytes、cgroup采样峰值316026880 bytes，内核记录峰值347750400 bytes，保持1GiB上限。Worker保持2GiB/两槽，平台保持4CPU/8GiB与swap0。
- 排空观察期间主机可用空间曾降至约19.2GiB，触发现有控制器20GiB操作水位并留下attention；这不是正常无中断的自动收尾。原持久责任已全部完成，独立probe和资源采样按保留owned handles规则继续运行。只清理本项目确定无用的旧构建缓存874MB及生成的mypy缓存后，主机回到20GiB以上；未删除镜像、容器、卷或业务数据。
- Root保持原输入、配置、工具哈希和T0，确认原probe自然完成完整观察，再停止同一owned sampler并调用原closeout方法完成双oracle和正常停源；未重发、重跑或新增框架。暂停、首次收尾读取碰到结束JSON尚未写完的失败及原件均保留；后继仅读取已完成原件，没有重复信号或覆盖失败。原平台持久存储及容量判据未放宽；主机操作水位的暂停与修复单独披露。
- 来源正常版本校验停用后，来源和内部Broker ready/unacked为0，来源consumer为0；Root组合资格hash为`ab3ebc5c0d9a9d7acd908db88292640cedecf4aa8a70e4d79bf9a9b6f6e1ea35`。HTTP E102的进程runtime聚合包含独立20条预热，正式业务oracle仍严格只计原10000条。

### E105 追加预算后的OCR执行错误与停止边界

- 用户随后另授权最多20万token，当前这一段授权合计60万。Root启动5项collector/ready及相关测试的语义组时遗漏显式provider/model参数，实际误用本机默认Kimi `k3.1-pro`，没有使用原先约定的`chatgpt/gpt-6-astra`。这是执行错误，不写成模型自动降级或用户授权变更。
- 发现后只向本次owned OCR进程发送SIGINT。原报告终态failed/cancelled、5项全部失败、0项完成；首组遇上下文压缩阈值，后组超过单次130000软预算，其报告实际用量167347。报告中的`files_reviewed=5`不能解释为5项审完。该调用没有合格审查意见或可复用的完成范围。
- 当前60万段可核实累计下限为519229，名义差额80771；另有两次`context canceled`请求没有用量回执，实际剩余额度无法核实。因此按项目规则停止追加OCR，不把缺失请求计为0，也不擅自增加预算；历史已获用户接受的未知仍单独保留。本次用量/停止回执hash为`05b719b8da4f18838f1b6efac9a2c6f96eb931e8a81653dc41e8c44255850797`。
- 最终未完成范围仍包括collector/ready及其测试、783后新增入口/状态/API差异、字符/SQL优化与相关测试，以及后继文档/规格增量；既有未变内容保留各自旧SHA资格，不冒称最终整候选OCR通过。6.3以及带该依赖的3.18/3.19/3.22/3.23/5.8保持未完成，禁止据本轮功能通过提交PR或归档。

### E106 原环境、数据与工作区恢复

- 已正常重建Control/Worker/Web，恢复原普通557/R8及原canonical context，字节hash仍为`6bb53f4e440d54488044492ab41972613746a32b8db0b908ea6a92f5cf12cedd`；原5695＋1000条接管/成功事实、ID段与数据库OID、资源上限、镜像、挂载和凭据环境摘要均核验保持，Admission和active Attempt为0。PostgreSQL、RabbitMQ与发生器CID保持，未回退或迁移数据库。
- 本轮HTTP/MQ成功库、R15/R16失败库与所有原始输入/证据保留；只删除已无用途的本次临时编辑脚本和明确生成缓存。主目录用户未提交规则文件未修改，未推送、建PR或合并。恢复回执hash为`0a7aac6fbffa7a54bcf4c2a2e8f3708b0aeb2e8cf314e759781615f7fd506017`；该时点主机可用约21.3GiB，后续值仍须按实际读回报告。

### E107 后继源码复审与仍未完成的交付范围

本节保留当时逐段续审及预算停止的快照；后继文档复审进度见E108，不将旧未完成清单当成后继当前范围。

- 用户接受两次取消请求用量未知后，后继使用固定OCR1.13.3及显式`chatgpt/gpt-6-astra`，按实际范围续审。到`e79d033b1096ab939160090b1e6c7d8c0c79c975`，以已完成原生报告的Git内容区间逐段核对，本轮相对d92f的9份改动业务源码全部有有效覆盖；8份改动测试已有6份覆盖。所有已完成范围均为0发现；入口8文件一轮带预算宽限完成提示，原警告保留，不称无警告审查。
- 改动范围静态检查发现3处会阻塞现有CI的格式差异，使用锁定Ruff0.16.2修复；3份文件修改前后完整Python AST相同，17份改动Python文件的format/lint通过。该格式增量另行低风险OCR完成；业务和资源断言仍按medium审查。没有新增测试或辅助框架，没有因格式修复重跑已有效的真实链路。
- 字符/数据库时钟3项及就绪队列/FIFO/最大正文工作区进展的测试增量已完成原生复审。运行过真实容量的产品源码仍按E102/E104的6e77候选记录，后继格式等价证据另存，不把历史测量改记为新SHA现场实测。原代码、故障、资源和独立oracle的有效范围继续保留。
- 在上述e79时点，两份测试增量`test_issue222_http_collector_bounds.py`、`test_issue222_http_groups.py`以及本轮10份规格/说明/验收文档未完成OCR；当时按用户授权预算边界停止追加调用。后续用户追加额度后，两份测试在`f7dd466619afbc9df4bbd7be2ec0f9110160b486`以固定OCR及显式`chatgpt/gpt-6-astra`完成medium审查，终态complete、两项完成、无失败/警告/发现；因此当前9份改动源码及8份改动测试均已有有效覆盖。中英文使用说明随后按原30e7范围官方resume完成medium审查、无发现，带“预算宽限轮完成”警告，文件内容与后继候选相同。当前仍有8份OpenSpec规格/设计/验收文档未完成OCR；7份合同组曾在派发前因估算超过预算失败，该失败不计为覆盖。原生失败、续接、完成回执与累计用量留在仓库外；未提交PR、推送、合并或归档，不能把已完成范围组合当成整候选审完。
- e79时点源码/测试组合资格hash为`ca52b02b3cd7665d9708514d5600d5d7455bc7a36495b21ae6eae33e07897069`，当时累计和未完成清单hash为`f0e8dbe6387db2a0b750557e65bbcab0ee99cd15ceef84e77dc9ef5e73074e41`；后继两份测试及中英文说明的原生回执另保存在私有审查记录，不改写旧清单。3.18/3.19/3.22/3.23、5.8和6.3的最终统一审查依赖仍未闭合，不提前勾选或宣布交付通过。

### E108 最终文档复审进度

- 两处过期导语已在`698cd2d7016bc4d2b272078689094833bb3b1b6d`纠正：最新容量指向6e77/E102/E104，D7.12/D7.13明确为历史，不改变Requirement或原验收数字。主规格与delta的11条Requirement正文相同，change及主规格strict通过；产品、测试、迁移、tools与Web内容均未改变。
- 相对d92f的五份合同与设计文档（proposal、design、B3合同及主/delta HTTP规格）已在698cd以OCR1.13.3、显式`chatgpt/gpt-6-astra`、medium完成审查：五项完成、无发现/失败/警告。先前E107的源码、测试和中英文说明仍按各自原区间及相同内容复用，预算宽限警告保持。
- 当前仍待三份事实记录的增量审查：本覆盖表、B4容量恢复记录与tasks。用户已明确追加额度，实际用量和回执在私有记录累计核算；原两次已接受未知仍保持未知。完整审查及任务收口尚未完成，不提交PR、推送、合并或归档。
