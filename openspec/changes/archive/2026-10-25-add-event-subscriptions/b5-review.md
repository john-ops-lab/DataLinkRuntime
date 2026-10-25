# B5 审查与验收记录

**状态：B5 实现、本地代表业务、浏览器、受影响兼容验证与独立审查材料完成；最终精确 HEAD CI 和部署作为统一 PR 的退出门禁。** 本批开放已声明的 Kubernetes 来源，补齐双语管理与专用完成处置。用户验收、合并及发布仍分别等待授权。

## 提交与实际运行边界

产品入口候选为 `ff50dd2c48dabb00cf2129e9697db3e69825898e`，Web 为 `065c33af5583829baf8a4a7dad1219effefc300a`，最终运行代码为 `6f68df56db25ce062c0fba3ade5c3a7d203e3bf6`。Deployment 首九份业务及部分页 403 在 `ff50dd2` 的匹配部署执行；满额放弃、state_sync、普通浏览器续接、Kafka 和普通触发器复验在 `6f68df5` 执行，不把历史实验改标为最终 SHA 重跑。后者部署了 Control、Worker 和两个 Web 入口；逐文件核对 Control 359、Worker 308 个源码/元数据文件、75 个实际依赖锁版本，以及每个 Web 的 98 个产物文件、代理配置和入口模式。React 19、Ant Design 5.29.3、Pro Components 2.8.10 的版本边界保持不变。

开发数据库保持前向迁移 `0048`。本轮沿用户已授权的私有开发测试编排更新四个应用，保存可读备份并检查实际连接目标、六个持久卷、挂载和密钥等值；原始数据库仍为 `0046`，保留 135 条终态执行和 50 个 Adapter。独立来源、目标及其他服务未因应用部署重建。私有配置和原始证据不进入公开仓库。

本机候选使用锁版本已核对的缓存依赖与精确 Git archive 构建，不冒称重新执行了在线 `npm ci` 或公开 Dockerfile 全量构建。统一 PR 的远端工作流另外验证这些门禁。功能默认关闭，新订阅默认停用；启用后的 capabilities 声明 Kafka 和 Kubernetes，不再使用内部候选入口代替公开管理。

## 代码、事务与静态验证

本批管理界面支持 Pod/Deployment 范围、模式、批次和控制责任、配额预留、历史事件所属批次，以及独立于当前来源类型的历史追踪。专用处置对话框固定原 request ID 和完整责任快照，冲突后不自动替换为新责任；重新审查需要明确开始新请求。显式 nullable Execution 与原失败终态均可见。

跨来源配置审查发现两项后端问题：Kubernetes 切到 Kafka 时，默认生成的 group 可能被旧空值覆盖；旧 Kubernetes LIST 的人工 skip 在切换来源后可能错误改写已成功批次。现分别限制 group 继承来源，并按不可变 Receipt phase 判断历史语义；新增四项真实 PostgreSQL 回归。Web 同时修复旧事件应跳到自身批次，而非最新批次或随当前来源隐藏。

主 Agent 受影响组合为 Issue #219 后端 **230 passed / 11 skipped**、普通订阅兼容 **14 passed**，Web 四文件 **39 passed**。跳过的是没有配置专用外部连接的可选故障测试，不将其计作通过；真实 Kafka 复验另见下文。作者和独立审查的重叠检查不累加为不同用例数。Ruff、格式、相关 mypy、Web lint/typecheck/build 及精确 Ant Design CLI 检查通过；保留既有无关 UI 提示和大 chunk 提示，不扩大修复范围。

## 真实 Kubernetes 与独立目标

实际来源为 Kubernetes `v1.35.9+k3s1`，客户端 `kubernetes==35.0.0` / `urllib3==2.8.0`。来源没有 agent/Node；Pod 和 replicas=0 的 Deployment 是真实 API 元数据对象，不声称它们运行了工作负载。普通 Python Adapter 经 RabbitMQ、实际 Worker Attempt 和 Outbox 向独立 PostgreSQL 目标处理完整 Envelope，业务结果由目标表及内容哈希读回，而非仅看 Execution 状态。

| 代表路径 | 实际结果与边界 |
| --- | --- |
| Deployment 正常业务 / U01、U04 | LIST 成员及两个控制，加六个真实 WATCH，共九份成功责任；包括 annotation、UID/RV 前置条件删除和同名新 UID 重建。目标保留新 UID、移除旧 UID，九份事件身份和目标哈希经独立复算。额外合法 WATCH 与操作产生的事件分别保留，没有只挑预期三条。 |
| 部分页权限失败 / U02、U06、U09 | 真实接管第一页 16 个 Pod 后撤回本任务 LIST 权限；同凭据诊断实际返回 403，应用记录权限错误、16 项/一页和 failed，无 completed、无目标清理。16 个成员及两个控制共 18 份实际成功执行，未遍历对象整行保持。停止并恢复精确 Role 后，显式启用新完整批次，17 项及两个控制共 19 份成功；旧失败批次保留。403 的 HTTP 原始响应来自同凭据诊断，不冒称捕获了 Control 第二页网络报文。 |
| 满额完成失败 / U11、U12、U15 | history=4 时三个责任及一份预留占满预算，新 replay 被 429 拒绝。中文真实页面要求非空原因及显式影响确认；合法放弃将预留转为单份 546 字节审计，history 达 4/4，原失败 Execution/Attempt 和冻结事实不变，本订阅 scope 的五张目标表不变，业务结果仍未确认。 |
| 最新责任竞争及 Execution=null / U12、U13 | 英文页面打开旧非空 Execution 快照后，公开 repair/replay 新增最新 pending、Execution=null。旧框提交被冲突拒绝，保留原请求；重新审查后明确显示未派发。新专用放弃只取消该 pending、释放一次 895 字节 pending 和完成预留，单份审计 570 字节，history 达 5/5；原失败和 repair 暂停保留。 |
| 显式新批与迟到完成 / U14、U15 | 两个满额代表先依法结清旧责任，再于停止且排空状态把 history 限额分别从 4 调到 8、5 调到 9，为新完整批次提供空间；没有靠加额完成原放弃。随后显式启用同 epoch/new activation，各有三个真实成功责任，目标 generation=2。迟到旧 completed 被目标 409 拒绝，所有目标表保持不变。产品最大限额下仍可能须等待合法留存治理，不承诺总能立即重开。 |
| 显式 state_sync / U05 | 一次真实 WATCH 执行失败后连续三个同 UID 更新，各先等 T1。第一份 pending 的 first ID 为自身，后续两次合并保留最早身份/接收时间，coalesced=2；两个 superseded 责任从未创建执行。原失败、已执行输入和显式 replay 不被合并。公开原请求 replay 后目标为最后实际 payload，七个 Receipt、八个 Delivery、六个 Execution（原失败加五个成功），目标五份幂等结果、一对象三次写入；pending/reserve 归零。 |

上述部分批次 37 份完整执行输入/输出、两个满额场景 12 份执行/Attempt/Outbox、state_sync 六份执行，以及不可变失败事实均由另一 Agent 从冻结证据及单独只读补证核对。目标稳定内容哈希与 Envelope 重新计算。Deployment 首九份执行的离线 summary 不含完整 input：实时探针已逐项核对，独立离线审查限于其保存的来源、目标、输出与物理关联，不冒称重新核验九份完整 input。

真实 Pod 分页、scope 外保护、同 epoch RV 续接、原生旧 RV 410 和资源关闭还沿 [B4 记录](b4-review.md)。两向 T2/完成冻结及 abandon 竞争、30 天及引用保护 GC、各层预算和控制公平性沿真实 PostgreSQL 确定性测试；不将时间推进用例写成实际运行了 30 天，也不把代表竞争扩写成全部真实故障交错。普通对象/WATCH 仍可依合同重放。

## 浏览器与幂等处置

真实 Chrome 验证中文/英文配置、诊断、批次事实、放弃影响、缺少确认的拒绝、旧责任快照冲突、Execution=null 及只读权限。普通浏览器配置保存使版本从 1 升到 2，epoch、规范 scope 和有效限额不变；有界连接测试没有新增责任或业务。只读账号只能查看获授权 Adapter 的订阅与历史批次，没有编辑和处置入口；会话已退出，测试账号恢复停用。系统语言恢复原中文。浏览器最后 error/warn 日志为空；这不抹去之前已保存的预期冲突与来源状态提示。

普通浏览器随后显式启用保存后的 Deployment 配置，并在同 epoch、同原批次、无新 LIST 的新 activation 中接收两条实际 MODIFIED（含 Kubernetes controller 的额外合法更新）。两条完整 Worker 输入等于 Envelope，输出与目标缓存及内容哈希一致，实际 Attempt/Outbox 均可关联；原九份责任及其目标幂等记录保持不变。目标仍一对象、事件增至 11、对象 writes=5、完整 payload 等于最后实际 WATCH。实际页面停用后 desired=disabled、connection=stopped、delivery=idle，native/buffer/pending/reserve 均归零；独立只读补证没有再次发送变更。

在配置已升版且新批次完成后，实际 API 再提交两份原 abandon 请求，均只读回原结果；原 pending replay 请求仍只读回已放弃的 null Execution Delivery。对旧放弃 completed 与新成功闭合 completed，各以 original/current/repair 发起新请求，共 12 次实际 HTTP 409，均为 `subscription_sync_completion_conflict`。每次前后同一只读快照核对本地责任、执行、Attempt、Outbox、审计、配额、批次、位置及配置等十二类表，均零副作用。

## 公共路径复验与容量

最终运行代码真实 Kafka 两分区三消息复验通过：一分区头项确因预期代码错误而失败、后项 pending 无 Execution，另一分区成功；显式代码修复和原请求 replay 只重做头项，repair 期间后项仍暂停，退出后按原 Revision 排空。三个目标业务行各一次有效写入，四个实际 Execution/Attempt/Outbox 可关联，原失败和 SavedInput 不变，两分区 DB safe/confirmed 与 broker 位置一致。仅删除本探针自有 topic/group，broker 返回均确认成功，来源已停止、native/buffer 归零。

普通 manual/schedule/webhook 各一条在最终运行代码下重新经真实 Worker 执行，结果分别为 10、31、59；schedule 等待实际 cron 时点，未用模拟触发。原 JSON 输入、Revision、Worker、输入哈希和 trigger 筛选均符合合同，两个临时触发入口随后停用，没有新增订阅。权限、管理事务、配额和留存的相关 C05/C08/C09/C10/C13 确定性回归包含在上述后端组合中。

Kafka 30 分钟及 10 分钟触限门槛保留 [B3 的原始结果和适用配置](b3-review.md)，本批没有无依据重跑全故障矩阵。默认重试队列基数 1 秒时首次 1/s 稳态失败；调整到 0.1 秒的明确实验配置才通过 1,800 条正式窗口，不据此承诺默认所有工作负载。13.91263 kB/事件的实验增长、默认 history=10,000 在 1/s 约 2h46m40 暂停、短突发 API p95 波动及先前 125.897 秒 Control 重试调度延迟均保留。未验 Kubernetes 30 分钟持续吞吐或全规模集群容量。

## 首败与未验范围

- B4 真实 LIST TypeMeta 首败及窄修沿原记录，不覆盖。
- state_sync 首轮探针错误地预期首份 pending 的 first ID 为 null；产品实际保存自身 ID 符合合同。保留首败及该责任后续结清，修正单项期待后另建场景完成三更新合并，未将首轮计作通过。
- 浏览器保存会规范化 PEM 空白。初始续接探针错误比较原始 PEM 文本，修后分别核对实际和原配置的规范 CA/scope，以及其余字段和有效限额；原首败保留。其后在来源尚 Starting 时探针拒绝前进，等待实际 Listening 后再操作，未越过启动门禁。
- Deployment annotation 会触发 Kubernetes 自身 generation 增长，初始探针错误要求不增长；精确 v1.35.9 官方 strategy 与实际 generation 1→2、UID/spec 不变共同确认该行为。保留第一次 patch 与已接管的两条 WATCH，通过只读输入/Attempt/目标补证完成续验，不重复发送变更，未因此修改产品代码。
- 不声明自然过期的持久 RV 在平台内完整恢复、重建 Kubernetes 已丢失历史、任意资源/认证、全语言真实故障矩阵、RSS 硬上界或端到端业务 exactly-once。目标清理和幂等仍依赖业务 Adapter/目标合同。

本批最终文档经独立只读审查通过。统一 PR 首轮精确 `6f68df5` CI 的 Web、预览控制器及 Compose 冒烟通过；后端为 3,282 passed / 28 skipped / 6 failed，六项失败均是旧迁移测试把当前 head 写死为 `0046`，而本次合法前向迁移已到 `0048`。修订仅将这些测试的当前唯一 head 预期集中交由 Alembic 解析；历史升级起点、资产保全和实际数据库版本严格相等的断言均保留，不改应用或旧迁移。六个原失败节点定向复验 6 passed（4.14s），六文件 Ruff/格式检查通过；历史起点和业务断言的精确 diff 另经独立审查。完整 CI 由最终 HEAD 重跑，首败日志保留。

最终完整 HEAD 的四项 CI、实际部署 SHA 及单次真实 Worker 烟测以 [统一 PR #221](https://github.com/john-ops-lab/DataLinkRuntime/pull/221) 的检查项和私有交付记录核对，全部成功后才转为待审查。本文保留各实验实际源码版本，不冒称在后续测试/文档提交重跑了业务故障矩阵；不会自动合并、发布或把开发验证称作用户验收。
