# Validation

## 当前统一 PR 交付

用户已追加授权将 B9–B14 更新到现有 PR #221。下述各批“不推送/不运行远端 CI”是当时范围，保留其历史事实。当前最终完整 HEAD 必须通过 `local-preview`、`backend`、`web`、`compose-smoke` 四项远端必要 CI，并与同一私有开发编排的实际版本一致；本地仅执行与改动直接相关的选择集及代表消息链路，不增加无关全量矩阵。

部署后分别核对 RabbitMQ、RocketMQ 和开启 ACL 的 RocketMQ 代表消息经既有 Worker 成功，独立目标每条写入一次；已有拒绝/纠正恢复证据保持原版本含义。产品逐文件及前端产物一致时复用原浏览器和故障矩阵证据，实际入口、运行版本、卷/凭据、原始库和最终停止接收/零积压仍须新核对。CI、部署、功能结果及用户审查分别记账，不自动合并或发布。

首次统一候选 `3d3ba05` 的 Web、Compose 冒烟及本地预览 CI 通过；后端为 2 failed、3331 passed、37 skipped。两项失败分别是历史来源合同仍硬编码仅 Kafka/Kubernetes，以及 i18n JSX 文本正则把 `=> ... <= 0` 和端口大小比较误判为英文界面文案。仅更新测试中的四来源/能力预期，并让扫描识别实际标签或 fragment 边界；补充比较表达式不误报及真实文案仍能被检出的五项保护。两项相关范围本地共 10 passed，Ruff 与格式检查通过，没有修改产品代码、界面文案、依赖或迁移。首轮 CI 原件保留；最终后继完整 HEAD 仍须重新通过四项必要 CI，结果记录于 PR 和私有账本。

## B14 真实 RocketMQ 账号权限补验

使用独立、任务所有的 Apache RocketMQ 5.3.3 Broker/Proxy，并实际开启 ACL 2.0 认证与授权。所有账号及服务地址只读私有配置，不将凭据写入测试源码、日志或公开证据。协议服务器的签名模拟不计为真实 Broker 权限通过。

| 编号 | 针对性通过条件 |
| --- | --- |
| A01 | Normal 账号仅有目标 Topic/Group 订阅权限；可接收真实消息并 ACK，不能以 Super 账号或关闭认证通过 |
| A02 | 错误密码和未知账号由真实服务端拒绝，DLR 分类为认证失败；没有被误当成网络暂态或成功 |
| A03 | 密码正确但缺少 Topic 或 Group 订阅权限，由真实服务端拒绝；已获范围的签名请求及独立 ACL 读回证明账号有效。5.3.3 两类异常均返回 UNAUTHORIZED，DLR 按实际 typed code 分类，不解析原始文本猜原因 |
| A04 | 部署后的 DLR 正常账号经 Receipt/Delivery/Worker 写入独立目标并 SQL 读回；拒绝路径无 Receipt/Delivery/Execution、目标写入或 ACK，不推进消费组 |
| A05 | 诊断仍不消费，失败确定性阻塞并有界关闭，无凭据/服务器原始错误泄露；最终停用、pending=0、native=0、应用缓冲=0 |

仅运行新增真实 ACL 用例及直接受影响的 RocketMQ/错误分类测试和静态检查；没有产品/UI 改动则不跑其他来源、Web、全语言或全量测试。自审如实标记，不冒充独立审查或远端 CI。最终提交、实际部署版本、源码/产物一致性和私有来源/目标事实分别记录。

实际结果：原生真实 ACL 6 passed；恢复缺陷新增回归在旧代码上 5 failed，修复后相关选择集 9 passed；原有签名/ACK/TLS 协议服务器 3 passed。实际 DLR 四类拒绝无接管/执行/目标写入，纠正账号后同配置/周期处理原消息，目标 writes=1。只修复公共队列故障分类与首故障保护，其他来源和 Web 不扩测；首败、架构限制、旧测试周期审计恢复及精确交付边界见 [B14 审查记录](b14-rocketmq-acl-review.md)。

## B9–B13 针对性验证增量

2026-10-24 本轮仅新增 RabbitMQ、RocketMQ 和公共扩展规则；不运行全量 backend/Web 回归或远端 CI。历史用例仅选受影响代表项，实际运行清单和首败/修正记入各批审查。

| 编号 | 必须验证的事实 |
| --- | --- |
| X01 | 注册和配置拒绝未知来源/字段，能力接口与已实现前端表单取交集；新来源无默认 fallback |
| X02 | Kafka/K8s 旧 Envelope 的事件 ID、内容哈希、批次控制及闭合/放弃规则不变 |
| X03 | 新来源 T1 rollback 不 ACK，T1 提交但 ACK 丢失的重投复用首次责任 |
| X04 | 临时确认句柄、重投次数及连接代次改变不产生新事件或内容冲突；同 ID 不同业务内容仍拒绝 |
| X05 | 原始消息/属性/Envelope/SDK 入站超限在有界内存下阻塞；Rabbit 无 message_id 不确认 |
| X06 | 一个未确认预取/一次接收一条；容量满不继续取数、不提前删除历史、不热重试 |
| X07 | 旧 owner、停用和关闭竞争不接管新责任；实际连接/线程可回收或明确 close_unconfirmed |
| X08 | 专用组/队列冲突、范围/信任变更、新 epoch、凭据引用及只读权限经过管理 API 验证 |
| X09 | Rabbit passive 诊断、Rocket 路由诊断不消费、不 ACK、不派发执行；稳定错误码不泄露 SDK 文本/凭据 |
| X10 | 两种来源实际接收顺序失败屏障、原 replay/repair/skip、原终态和普通输入隔离保持 |
| X11 | 真实隔离 RabbitMQ 消息→持久 Receipt→Worker→业务结果读回，原生重投/停止代表路径 |
| X12 | 真实隔离 RocketMQ 5.x Proxy/SimpleConsumer 的同一链路及手动 ACK/重投代表路径 |
| X13 | 新建/编辑来源、保存及测试不启用、启停、只读、双语摘要和原始字节可查看；四种来源无错误跳转 |
| X14 | 迁移仅前向扩展来源约束，旧订阅/责任投影保持；精确本地提交的依赖/源码与实际部署一致 |
| X15 | 原有 Kafka/K8s 各一个代表性实际/确定性路径，以及普通 manual 路径不受新来源影响 |

每批还执行改动文件的格式/类型/静态检查；Web 做生产构建与实际浏览器操作。新来源 Broker 为隔离资源，绝不使用 DLR 内部调度队列充当来源。公开材料只留通用结论，真实 endpoint/凭据/环境原件仅留私有目录。

本文件保留实施验收合同，逐项执行状态见 [tasks.md](tasks.md) 及各批次审查记录。B0/B1 已完成对应公共范围，B2 起继续验证实际来源与业务结果；用例列出不代表全部通过。用户已授权原本机验收栈改作隔离开发测试，来源服务、目标数据和故障注入仅在该授权范围内执行。

## Evidence Rules

- 每批记录 exact source SHA、客户端与来源版本、迁移版本、启用来源、实际配额、步骤、预期/实际、首次失败及修复后重验；CI、部署、功能结果与用户验收分别标记。
- 真实链路必须读回目标业务数据。Kafka 示例是带稳定业务 ID 的消息写入独立测试表；K8s 示例是按 cluster_id + UID 管理的目标清单，包含本范围外保护数据。来源记录和目标查询是独立 oracle，不能只看 DLR 状态。
- 故障注入仅使用隔离的测试部署/来源和任务所有的目标数据；现有主项目、共享验收和业务环境不作为可任意重启或清库的测试场。连接信息与凭据通过私有配置提供，不进入提交或公开证据。
- 默认一个 Python 示例 Adapter 足以覆盖共享事件交付路径；其他语言用现有 JSON 协议回归/契约证明。只有发现语言相关改动才增加对应真实运行，不展开全语言 × 全来源矩阵。
- UI 改动须真实浏览器检查双语文案、禁用原因、配置/启停/错误恢复/追踪交互；构建通过不能替代。现有强制 CI 仍须完成，不以本矩阵免除项目门禁。

## Common Cases

| ID | 场景与输入 | 可观察通过条件 | 首次批次 |
| --- | --- | --- | --- |
| C01 | 同一事件两次投递，received_at/generation 不同 | 同一原责任/原输入；不误报冲突，不重复 Execution | B1 |
| C02 | 同身份内容不同；Rabbit 不可用时重投已接管消息 | 前者明确冲突且不覆盖；后者仍识别原责任、不做新 Admission | B1 |
| C03 | T1 提交前、提交后 ACK 前、ACK 后 T2 前分别崩溃 | 每个窗口责任归属明确；来源位置不越未接管项；已接管最终可处理 | B1→B2 |
| C04 | T2 写 Execution/Outbox 中断及重复恢复 | 原子回滚或完整关联；每 Delivery 至多一条 Execution；计数交接一致 | B1 |
| C05 | lease 过期接管、旧 owner 迟到提交、stop 与接收竞争 | 旧写拒绝；迟到 ACK 不超安全前沿；无普通 Slot 被 listener 占用 | B1→B2 |
| C06 | stop、drain、改代码/Worker/范围、再 enable | 旧快照不变，未排空写入拒绝；新 epoch/activation 行为可追踪 | B1→B3 |
| C07 | retry_wait/dead_letter/cancel/expired，随后 replay/skip | 屏障符合来源合同；补处理新责任、原终态不变；重复 request_id 幂等 | B1→B2 |
| C08 | 代码错误阻塞且后续有 backlog，进入 repair 保存修复版本 | 无需 skip 即可补处理前项；新派发暂停；旧 activation/后续 pending 不被替换；成功后可退出 repair | B1→B3 |
| C09 | 内存、pending 数量/字节、速率、Execution Admission、历史/去重额度分别触限 | 对应层有界背压；不能靠另一层绕过；无热循环；低水位恢复 | B1→B3 |
| C10 | 原文/元数据到期且存在未完、失败屏障或批次引用 | 保护责任不清理；普通过期原文明确不可 replay；配额/引用不泄漏 | B1→B5 |
| C11 | 超大内容、二进制/null、冲突内容、身份无法验证 | 无静默截断/清洗；blocked_unaccepted 不被标成接管成功；错误聚合有界 | B2 |
| C12 | 目标已写但响应丢失，恢复重试 | 读回同业务 ID 仅一次有效副作用；解释依赖目标幂等，不能只凭 Execution 数证明 | B3 |
| C13 | 管理员/view/edit/无权限、跨 ID 查询、双语切换 | 权限、列表计数、详情、replay/skip/reset/repair 符合合同，Secret 不进入响应/日志 | B3 |
| C14 | 普通 manual/schedule/webhook 各一代表链路，Task JSON/文件输入契约 | 原入口结果、InputConfig revision、筛选和运行锁保持兼容，新 trigger 不破坏序列化 | B1→B3 |
| C15 | feature off、向前迁移、Control/Web/Worker 兼容与回退演练 | 不生成默认订阅；未完责任保全；旧应用不兼容时明确阻止裸回退 | B1→B3 |

## Kafka Cases

| ID | 场景 | 可观察通过条件 |
| --- | --- | --- |
| K01 | 单 topic、两 partition，真实业务事件到目标读回 | 每消息正确目标结果；源身份→Receipt→Execution→Attempt 可关联 |
| K02 | partition A 前项失败、后项已接管，B 正常；含一次 retry_wait | A 后项不越过，B 可继续；修复/skip 后 A 恢复；无跨 partition 顺序承诺 |
| K03 | 首次 earliest/latest、重启、合法 offset 空洞、新 partition | 起点只解析一次；实际已读未提交项不被跳过；新 partition 起点显式沿用首次配置并持久化 |
| K04 | rebalance、Control 崩溃、commit 失败、组位置与 DB 不一致 | 从安全位置恢复；broker 超前阻塞，落后重复确认；旧 assignment 不推进新位置 |
| K05 | 缩短来源 retention 形成缺口、topic 重建、组冲突 | 不静默跳最新；保留旧已接管责任，明确 gap/reset/epoch 和身份检测限制 |
| K06 | 长目标故障、pending 满/原文历史满、暂停后恢复 | 不跨过未接管消息，无内存持续增长或热重试；组维护/退出可恢复 |
| K07 | TLS/SASL 错误与凭据轮换，消费事务隔离 | 错误脱敏且停止确定性重试；同源轮换保留 epoch；read_committed 按锁定矩阵验证 |

B2 验证协议和恢复；B3 完成真实业务读回、管理交互、容量适用范围及交付证据。

## Kubernetes Cases

| ID | 场景 | 可观察通过条件 |
| --- | --- | --- |
| U01 | 分页 LIST → started/items/completed → WATCH → 目标读回 | complete 在全部成员成功之后；目标只在本 scope 对账；已有 scope 外对象保留 |
| U02 | LIST 页失败/403/continue 失效/超大对象 | 无 completed、无缺失对象清理；有 failed 和可操作的恢复原因 |
| U03 | WATCH断线、410、bookmark；含已入账WATCH的失败批次 | cursor不超未提交项；failed处置后旧责任排空，已冻结资格的gap归下一轮；恢复正常路径可新批，专用abandon后仍须显式启用；旧批不复活 |
| U04 | 同名不同 UID、离开 selector、旧删除与新对象 | 不合并新旧 UID；目标新对象不被旧删除误清理；不误报物理删除 |
| U05 | per_event 与 state_sync、执行中新更新、删除边界 | 默认不合并；合并有审计；不改既有输入；后续变化有责任；LIST/删除边界不被吞掉 |
| U06 | LIST读完而成员失败，分别显式repair成功或skip；另含started skip | 未解决失败无completed；完整快照仍有效且无失效时，最新责任修复成功可重新检查全员成功；LIST/started skip令旧批永久失效，后来成功也不复活，须新完整批次 |
| U07 | 新LIST返回相同UID/RV；范围、server或有效CA信任变化；同引用token轮换 | 新成员不被旧去重漏掉；范围/信任无显式new_epoch拒绝，合法切换保留旧历史且不清理旧范围；token值轮换仍沿原epoch/RV |
| U08 | local/global pending数量/字节、历史/去重、整批上限及控制rate竞争 | 预留覆盖各层且其他订阅不能抢占，仍可落合法控制；ready按持久since/ID公平且同窗口不超10/50；T2只释放实际pending，超大批明确failed；空批/featureoff意图可维护，余reserve可核对且只结账一次 |
| U09 | sync.started/failed/completed各一次目标响应丢失或业务错误 | 控制身份稳定；failed业务失败可显式generic skip且审计未处理，不恢复清理资格，处置后重算普通失败并排空；正常completed错误阻塞且可受限修复，另按U11–U15验证专用人工例外，均不改原终态或重复清理 |
| U10 | completed创建与WATCH410/失效并发，分别控制提交先后 | 失效先赢则pending completed明确superseded且无Execution；T2先赢则独立freeze保持，新gap进入后续恢复；执行外键清空不丢冻结事实 |
| U11 | history_count=4，started/item/completed已用3且余1预留，完成失败后新replay需5被拒绝 | 专用abandon在原预算内将有界槽原子转换为单份≤8KiB实际审计；无额外空位/免计费/Envelope副本，原历史不删，余reserve只结账一次；30天及引用保护，账本可重建 |
| U12 | view/edit/隐藏ID、监听未关闭、queued/running/retry_wait、过期版本/Batch/epoch/Receipt/latestDelivery/执行关联、同request重试 | 非法条件零副作用；latest Execution已succeeded而Receipt/Delivery投影滞后也拒绝；合法重复原请求只读回同结果，改actor/reason/对象/期望责任复用ID冲突；UI明确业务结果未确认、取消pending补处理及不可回滚 |
| U13 | 已冻结完成链latest replay为pending且execution_id=null，与T2/新replay竞争及事务rollback | 两向固定提交顺序：T2先赢即使快速终态也冲突；新replay改变最新责任则冲突；abandon先赢无新Execution/Outbox，只释放一次pending而保留history/dedupe，失败回滚无半状态 |
| U14 | abandon后及completed成功确认且批次闭合后的新原/repair/current replay；reset历史、restart、迟到observe及旧请求重试，含合法config升版/featureoff | 放弃与completion_committed持久保留，原失败/成功终态不变，两类已结完成链均拒绝新增Delivery/Execution/Outbox；旧replay/abandon原请求按当前权限和完整原参数先只读回，不重复结账或新增审计；再次全量对账使用新完整批次，普通对象/WATCH历史补处理不变 |
| U15 | 放弃完成屏障后仍有普通failed/旧WATCH；结清后显式新批，旧completed晚到 | 重算普通失败并保留repair入口，WATCH原序排空；不自动enable/LIST；显式新批started后目标generation/控制幂等阻止旧清理，新批/scope外/新UID数据保留；放弃不算目标成功 |

B4验证来源、批次和专用完成放弃机制；B5完成Pod/Deployment两个已声明范围的真实读回、目标保护及双语UI。U10/U13的两向事务交错由确定性并发测试覆盖，真实环境各复核代表路径；U11–U15采用收紧预算和代表目标，不扩成全资源/全故障组合。

## Capacity Gate

B0 建立方法和基线，B3/B5 使用实现复测。此处是最低实验合同，不是商用吞吐承诺：

1. 固定一个不超过 4 KiB 的代表性输入、一个带目标幂等的短执行 Adapter、实际硬件预算和版本。记录 HTTP 请求、Control RSS/客户端缓冲、pending/Execution/Outbox 及 PostgreSQL 实际大小基线。
2. Kafka 以 1 条/秒运行 30 分钟，目标正常时记录可持续完成率、延迟和增长；不达此基线则不得宣称该配置适用。再以 10 条/秒有限突发并注入目标故障，验证背压而非要求全部立即完成。
3. 用降低后的测试配额分别触发数量、字节、历史与控制预留边界；达到后继续观察 10 分钟，应用缓冲不超过设置预算，RSS 不持续随消息增长，健康/API 请求不因监听阻塞。记录客户端额外 RSS 上界；API p95 不高于无监听基线的 2 倍且无资源耗尽 5xx，否则 B0/B3 返回修订。
4. 目标恢复后确认按持久顺序排空、所有已接管责任可解释。按实测每事件数据库/日志增量估算每天和 7/30 天窗口用量，连同默认配额预计何时暂停一起交付。
5. K8s使用缩小配额复现完整批次、超限失败和U11满额完成失败的专用abandon，核对单份实际审计、pending/reserve及历史占用；分别验证正常失败恢复和人工放弃后旧责任结清、显式新批次与目标代次保护。产品max下新批可继续等合法留存治理，不以临时加额/删原文通过；无需全规模集群或扩资源矩阵。

## Stop Conditions

- 丢失已接管责任、来源位置超前、重复造成未受控副作用、旧owner可写、错误全量清理、人工放弃伪造成功/免计费，或已有原责任无法通过合法修复/显式处置结账：阻止当前批次合并/交付，只重验受影响范围。原责任依法结账后新批因产品max/保留窗口等待额度，属于已声明治理边界，不得误写为旧责任仍无法退出或新批已成功。
- 无法满足来源必要 API、客户端有界行为或 JSON/trigger 兼容：停在 B0/B1，修改设计并重新审查；不靠省略确认/恢复保证推进。
- 不能控制的来源/目标环境故障：记录“未验”，不能以 mock、绿 CI 或容器健康代替；其他独立批次可继续已授权工作。
- 出现无关问题：单独记录，不自动扩大本项实现范围。

## Review Checklist

本稿供用户重点审查的决策是：首期一 Adapter 一订阅且触发互斥；Control 内监听；统一有界 Inbox；Kafka 分区失败阻塞；K8s 显式合并与批次屏障；受限代码修复；正常完成成功屏障及用户批准的专用人工放弃例外；同预算有界审计/目标代次；初始容量/留存；Kafka与K8s分开交付。表中的用例是这些决策的验证方式，不代表已通过。
