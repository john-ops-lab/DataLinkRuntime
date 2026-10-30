# Event Delivery Specification

## Purpose

定义跨来源共享的可靠接收与交付行为，使每条已确认数据都有可恢复、可追踪且容量受限的处理责任，并明确去重、人工重处理、失败和留存各自的边界。

## Requirements

### Requirement: 队列平台错误不得污染消息诊断
RabbitMQ/RocketMQ 的 HTTP 与原生失败 SHALL 按错误含义统一分类。平台开关、所有权、激活/配置及未知运行错误 MUST NOT 写入消息 `blocked_unaccepted`；明确内容/身份冲突保持阻塞，容量摘要保留既有低水位恢复规则。仅提交 T1 后才可 ACK，ACK 失败 MUST NOT 新建未接管坏消息诊断。

#### Scenario: 在途接收遭遇功能关闭后恢复
- **WHEN** 消息已拉取但 T1 遭遇平台关闭，或关闭后又在客户端退出前恢复
- **THEN** 系统不新增 Receipt、Delivery、消息屏障或 ACK，确认关闭后可沿原 epoch、激活及配置继续接收

#### Scenario: 真实内容冲突保持原诊断
- **WHEN** 稳定消息 ID 对应的内容发生冲突
- **THEN** 系统保留原已接管事实和明确的消息阻塞，不把平台恢复当作跳过或修复内容冲突

### Requirement: 新队列订阅保持稳定消息身份与持久确认
RabbitMQ SHALL 使用生产方稳定 message_id，RocketMQ SHALL 使用原生稳定 message_id，结合来源/范围形成身份。临时 delivery tag、receipt handle、重投次数 MUST 不作为身份或稳定内容。正文和业务属性 SHALL 无损且有界；两种来源均 MUST 在 T1 整体提交后才明确确认，源重投 SHALL 命中原责任且不覆盖首次输入。

#### Scenario: 消息已保存但确认丢失
- **WHEN** RabbitMQ/RocketMQ 在 T1 提交后、ACK 生效前断线并重投
- **THEN** 使用原事件/版本/待处理责任，仅增加重复接收计数，不创建第二条责任或业务执行

#### Scenario: 消息缺少稳定身份或超限
- **WHEN** RabbitMQ 消息没有 message_id，或任一新来源的正文/属性超过边界
- **THEN** 拒绝接管且不 ACK，保存有界诊断并阻塞来源，不以正文哈希去重、不截断、不无限分配或热重试

### Requirement: 新队列来源只承诺实际持久接收顺序
RabbitMQ/RocketMQ SHALL 各使用一个本地串行 shard，沿用失败屏障和显式补处理/跳过规则；MUST 不把 Broker 消费顺序、单槽执行或原生重投解释为跨重启全局 FIFO。平台只控制专用队列/组内已实际接管责任，不承诺恢复上游已经过期或由其他消费者取走的消息。

#### Scenario: 当前业务失败而后续已接收
- **WHEN** 当前 Delivery 最终失败且后续事件已保存
- **THEN** 后续业务不越过该屏障；停用仅停止新接收，旧责任继续保持并允许受限修复

### Requirement: 来源扩展不得改变旧输入和完成资格
新增 RabbitMQ/RocketMQ MUST 继续使用 dlr.event.v1、subscription trigger 和原 JSON Worker 协议；旧 Kafka/Kubernetes 身份和稳定内容哈希 MUST 保持。来源 ACK 与业务成功 SHALL 分开，Kubernetes 已闭合 completed 不可新 replay、专用完成放弃的有界审计和业务未确认事实不得因公共抽象改变。

#### Scenario: 新旧来源共存
- **WHEN** 注册四种来源并读取旧事件或旧批次
- **THEN** 旧输入身份/哈希和追踪结果不变，旧失败仍失败、已放弃完成责任不复活；未知来源无法借新配置绕过原运行锁

### Requirement: 持久接管先于来源确认
系统 MUST 在完整可处理输入、责任、对应安全位置和容量占用原子提交后才确认来源；内存接收不等于接管。对按序读取的来源 SHALL 不越过已读但尚未提交的前序记录。

#### Scenario: 提交前崩溃
- **WHEN** 进程在接收事务提交前退出
- **THEN** 没有来源确认或安全位置前移，来源可重复投递；数据库不存在部分接管责任

#### Scenario: 提交后确认前崩溃
- **WHEN** 责任已提交但来源确认未完成
- **THEN** 恢复后继续处理原责任，来源重投命中原记录，不创建第二条处理责任

### Requirement: 待处理转执行具有原子责任交接
每条可派发责任 SHALL 在同一事务中完成 Execution、Admission、Outbox 与事件关联并退出 pending 占用。失败或重启 MUST 保留原责任；重复调度不得创建多条同一 Delivery 的 Execution。新缓冲 MUST 不绕过既有执行容量限制。

#### Scenario: 执行入口暂不可用
- **WHEN** RabbitMQ readiness 或 Execution Admission 拒绝新派发
- **THEN** 原 pending 保留、调度有界退避，来源接收受自身额度控制

#### Scenario: 执行创建事务中断
- **WHEN** Execution 写入后而统一 commit 前失败
- **THEN** Execution、Outbox、计数与关联全部回滚，恢复后至多成功交接一次

### Requirement: 稳定身份与首次输入不受重投元数据影响
源去重 SHALL 使用订阅、source epoch 和源事件 ID，内容冲突只比较稳定源内容。重投 MUST 保留首次 Envelope、接收时间和版本；已接管去重不得依赖新的 Admission 或 RabbitMQ 可用性。HTTP SHALL 使用调用方稳定 ID 和原始正文哈希；HTTP 未接管冲突仅明确拒绝当前请求，不新增永久来源屏障。旧来源身份、稳定内容哈希和冲突屏障 MUST 保持。

#### Scenario: 接收时间变化且内部队列故障
- **WHEN** 同一事件在新时间重投且 RabbitMQ 不可用
- **THEN** 系统仍识别原责任，不误报内容冲突、不建立新执行

#### Scenario: 同身份不同内容
- **WHEN** 同一稳定身份携带不同内容
- **THEN** 报告 `event_identity_conflict`，不覆盖原内容；旧来源仍阻塞受影响 shard，HTTP 明确拒绝冲突请求并保留其他合法请求的接收能力

### Requirement: 重试层次和顺序失败可解释
系统 SHALL 将来源重连、平台调度退避和 Execution 有界重试分开。业务失败、取消或过期 MUST 不被自动视为成功；其后续是否可执行由来源顺序合同控制。普通单槽串行不得被宣称为重试顺序保证。

#### Scenario: 业务错误耗尽重试
- **WHEN** 当前事件进入 dead_letter
- **THEN** 保留失败事实和处置入口，未按来源合同补处理或显式处置前相关顺序屏障保持阻塞，平台不无限新建执行

### Requirement: 人工重处理创建可追踪新责任
replay SHALL 仅为来源合同仍允许执行的责任创建保留原事件与原执行关系的新 Delivery/Execution，按请求 ID 幂等并经过容量检查。已专用放弃，或已成功确认且批次已闭合的 K8s completed MUST 拒绝任何新的原/repair/current-config 补处理；旧请求只可幂等读回原责任。重新全量对账 SHALL 使用新完整批次，不重开旧清理资格或预留。尚未确认完成的失败链和普通对象/WATCH历史补处理仍按原合同处理。默认使用原输入、Revision、Worker 和凭据引用；更换为当前配置 MUST 显式选择且只在停用、无其他未完责任时允许。显式 repair 下可在后续 pending 保留时仅指定修复 Revision，仍须满足受限修复合同。

#### Scenario: 重复提交补处理请求
- **WHEN** 同一 request_id 被重复提交
- **THEN** 返回同一补处理责任，不创建额外执行，也不修改原终态

#### Scenario: 原版本不可用
- **WHEN** 默认 replay 依赖的版本、Worker 或原内容不可用
- **THEN** 返回明确不可重放原因，不静默使用当前版本或不完整输入

#### Scenario: 重放已被后续越过的历史记录
- **WHEN** 用户重放来源合同仍允许补处理的已完成或已跳过、且已有后续业务的旧事件
- **THEN** 必须先停用排空，记录带外修复，不回退来源位置或承诺原业务顺序

#### Scenario: 已成功闭合的完成控制再次补处理
- **WHEN** completed 已成功确认且批次闭合，随后提交新的原/repair/current-config replay
- **THEN** 拒绝且不新增责任、执行或审计；原请求先按当前权限和完整原参数只读回原 Delivery，合法配置升版或 featureoff 不阻断该幂等读回；重新对账使用新完整批次

### Requirement: 冻结完成责任可通过专用人工放弃有界结账
K8s 已冻结但业务结果未确认的 completed MAY 使用专用 `abandon-completion`；正常路径仍 SHALL 等业务成功。此动作 MUST 与 generic skip、成功和同批 sync.failed 区分，保留 completion_committed、原 Envelope、原失败 Delivery/Execution及其终态。最新 replay 若仍 pending 且无 Execution，SHALL 在同一事务明确终止该未派发责任、释放一次实际 pending并保留已用历史/去重；不得创建 Execution/Outbox。请求 MUST 绑定期望最新 Delivery及其Execution关联快照，含null，与T2/新replay/终态观察在既定锁序内原子互斥。

#### Scenario: 放弃与 pending 补处理 T2 竞争
- **WHEN** 已冻结完成链的最新 replay 尚无 Execution，专用放弃与 T2 并发
- **THEN** T2先提交则放弃冲突，即使执行已快速终态；放弃先提交则该 Delivery无新Execution/Outbox，pending只释放一次且历史保留，任一事务回滚不留下半处置

#### Scenario: 已放弃完成责任的历史重处理
- **WHEN** 放弃后发生dispatch、原/repair/current-config新replay、reset后的历史调用、重启或迟到观察
- **THEN** 持久放弃事实阻止旧completed复活；原请求仅按原参数与权限幂等读回既有结果，不追加Delivery、执行或副作用

### Requirement: 完成放弃审计计入现有预算且保留原责任事实
专用放弃 MUST 使用已有 Batch/责任预算中的有界槽原子转换为单份实际审计占用，至多8KiB并受既有元数据30天留存及引用保护；不得要求额外空位、免计费追加审计、复制原Envelope或提前删除原历史。账本/audit/显式修复 SHALL 可重建该审计实际占用及remaining reserve，未用预留只结账一次。该动作不放宽产品max，也不承诺下一批立即获得额度。

#### Scenario: 历史满额但完成责任需要人工退出
- **WHEN** history_count=4、已用3份且余1控制预留，completed冻结后失败，新replay因需要第五份占用被拒绝
- **THEN** 合法专用放弃在既有预算内留下单份有界且已计费审计并结账预留，原历史不删；重复请求不再占用或归还额度，新批仍服从产品max和合法留存治理

### Requirement: 显式跳过保留损失事实
skip/reset SHALL 要求 edit 权限、原因与幂等请求 ID，并记录操作者、原事件/位置和影响。系统 MUST 将跳过显示为未处理处置，不能伪装为业务成功，不能隐式清除其他已接管责任。

#### Scenario: 操作员跳过毒消息
- **WHEN** 有权限的用户明确跳过一个阻塞来源记录
- **THEN** 仅被审查位置按来源合同前移，记录未处理原因；后续责任仍可追踪

### Requirement: 容量覆盖内存待处理执行和历史
系统 SHALL 为连接、预取、单项大小、pending、接管速率、Execution、原文历史和去重元数据设置数量/字节上限及最老时间告警。触限 MUST 暂停新接收或按来源标记需同步，不能静默丢弃、无限缓冲或立即热重试；恢复须满足明确低水位。接收容量 SHALL 支持正式部署上限和受其约束的入口配额，缺省保持现有值；不得通过测试脚本绕过限制。

#### Scenario: 历史额度已满
- **WHEN** pending 很少但保留窗口内的原文历史已达上限
- **THEN** 停止新接管，不通过提前删除窗口内数据腾空间，页面显示具体额度原因

#### Scenario: 超大消息
- **WHEN** 单项超出来源或最终 Envelope 限制
- **THEN** 旧来源记录有界 `blocked_unaccepted` 摘要且不确认来源；HTTP 明确拒绝未接管请求且不建立永久来源屏障；均不将截断内容当成完整接管

#### Scenario: 降低全局容量时已有责任超限
- **WHEN** 合法部署配置生效后，既有实际占用高于新的全局上限
- **THEN** 停止新接管，保留全部旧责任、计数和恢复数据，允许原责任继续结算，满足低水位后再恢复接收

### Requirement: 去重和重放窗口有限且不删除未完责任
系统 SHALL 默认保留可重放原文 7 天、源去重元数据 30 天，并保护未完责任、未解决屏障及批次引用。内容过期的 replay MUST 返回 `event_payload_expired`；源无永久进度保护时不得承诺窗口外永久去重。

#### Scenario: 保留时间已过但仍阻塞
- **WHEN** 失败事件仍阻塞顺序屏障或属于未完成批次
- **THEN** 自动清理保留其必需内容和关联，不以 TTL 抹去责任

#### Scenario: 已完成事件原文过期
- **WHEN** 无保护引用的已完成事件到达原文清理窗口
- **THEN** 可清理原文并保留独立去重摘要，查询说明原文已过期，不能提供虚假可执行 replay

### Requirement: 各层容量仅在责任合法交接后释放
接收、内存、执行、历史、去重、重试/补处理及控制/诊断预留 SHALL 分层计账。T2 MUST 在同一事务中完成 Admission、Execution、Outbox、关联写入及退出 pending/扣减相应占用，全部随事务提交整体生效；失败回滚保持原责任。历史和去重保持各自保护。重复回报、重启或处置 MUST NOT 重复释放、伪造余量或静默删除旧责任；原生工作未结束不得提前归还资源。

#### Scenario: T2 事务失败后重新调度
- **WHEN** 执行/Outbox 创建或责任交接在提交前失败
- **THEN** pending 与占用保留或随事务完整回滚；后续至多成功交接一次，不留下未计费执行或丢失责任

#### Scenario: 原文与控制预留共同接近上限
- **WHEN** 实际历史加控制/诊断预留达到上限
- **THEN** 一并计入容量拒绝，不把预留当免费空间，也不通过删除失败事件为新请求腾额度

### Requirement: 可运行责任和完成确认获得有界推进
调度 SHALL 对可运行分片头部与执行终态确认分别给予有限条数、字节和处理时间预算；候选查询及其连接等待受既有有限超时约束，不挤占处理阶段的 50 ms；非空轮在首个可处理候选进入处理会话后开始计时，处理中的数据库等待计入预算，避免持续慢查询或首项前暂停导致永不处理。整轮包含查询、连接等待与处理，耗时可超过 50 ms；阻塞 pending 不得占满全部处理机会。固定资源、至多 10 个活动入口的验收集合内，在顺序及容量满足时，完成回访和后继开始派发处理各 SHALL 不超过 2 秒；通知只作唤醒，丢失或重启仍由持久状态和有界扫描恢复。

#### Scenario: 大量前序阻塞消息与已完成执行共存
- **WHEN** 一条来源的后继 pending 被失败屏障阻塞，其他来源有可派发消息或待确认终态
- **THEN** 后者在约定预算内继续推进，保留前者失败事实，报告各类等待原因和回访延迟

#### Scenario: 候选查询耗时超过处理预算
- **WHEN** 每轮候选查询均超过 50 ms，但数据库仍在有限超时内返回可运行候选
- **THEN** 首个候选进入处理会话后开始计时并获得推进机会；处理预算到期后不得启动下一笔事务，整轮耗时不得伪称不超过 50 ms

#### Scenario: 完成通知丢失或调度进程重启
- **WHEN** 唤醒提示丢失或调度进程退出后恢复
- **THEN** 按持久执行结果恢复责任并补偿推进，不依赖内存事件、不重复创建有效 Execution 或 active Attempt

### Requirement: 发布提速保留逐条确认与有限资源
发布优化 MUST 保留持久消息、mandatory、publisher confirms、路由校验、数据库待发布责任和逐条确认关联。连接/channel 复用或并行发布 SHALL 有有限窗口与超时；确认不明时保留可重发责任，不能释放不存在的成功。派发批量 MUST NOT 被解释为业务批次或改变逐条执行身份。

#### Scenario: 一批发布中部分确认后连接丢失
- **WHEN** 有限发布窗口内部分消息确认，其他消息结果未知
- **THEN** 逐条结算确定结果，未知项保留责任并经原身份安全重发；不整批宣称成功、不重复有效业务效果

### Requirement: 暂时故障恢复使用显式且有界策略
新增下游恢复能力 SHALL 由明确启用的参考 Adapter 策略负责，固定可重试错误、最大次数、总时限、取消与结果不明处理；保持平台默认 business_error 边界。参考目标策略 MUST 使用稳定幂等身份，在目标实际不可用 60 秒后自动继续；错误耗尽形成明确持久失败，不能伪装成功或无限重试。

#### Scenario: 参考目标恢复但原提交结果不明
- **WHEN** 目标提交可能成功而响应丢失，随后参考 Adapter 重试
- **THEN** 通过原稳定键和内容核验恢复唯一业务效果；保留失败/尝试记录，不更换消息身份或执行绑定

#### Scenario: 权限错误或重试预算耗尽
- **WHEN** 目标返回非白名单权限/数据错误；或当前成员仍需重试但单条处理已尝试 20 次、业务批次已用尽整次调用共享的 19 次额外重试，或自本次处理开始的 120 秒时限先到（N 个批次成员最多 N + 19 次目标事务尝试）
- **THEN** 形成明确失败与对应顺序屏障，不自动改成 platform_transient；无关可运行入口仍获得处理机会

### Requirement: 接管合并保持逐条责任和确认边界
显式 business_batch 模式 MAY 在事务外按来源分限收集：HTTP 最多 16 条/额外等待 25 ms、RabbitMQ 最多 16 条/额外等待 5 ms，原文加 Envelope 合计仍为 1 MiB，再在一个事务内复用同 scope/lease/checkpoint/精确 Activation/CV/profile。截止后 RabbitMQ MAY 仅以零等待排出已 complete ready 的消息，不再原生 I/O 或等待，无 ready 即结束，仍受 16 条/1 MiB 限制；准备 CPU 耗时计入真实接管时间。每条 MUST 保留独立身份、Receipt/Delivery、完整计费及首次绑定；无跨事务缓存，RETURNING 按身份映射，原单条路径不变。每个新项仍检查磁盘、租约时间、速率和控制优先权，不得留下拒绝项的局部扣额。

#### Scenario: HTTP原生单元只取已有就绪前缀
- **WHEN** HTTP获得原native名额，同public_id、同loop已有未过期ready成员
- **THEN** MAY 将其FIFO前缀组成最多16条/保守估算1MiB的实际原生单元，不等待新项、延长25ms收集或重置任何成员的原1秒就绪期限
- **AND** 部分转交后的剩余条目保留原顺序及期限，所有原Future与字节/请求预算恰一次转交和释放；MQ收集与确认规则保持


#### Scenario: HTTP机会性早seal仍先建立ready所有权
- **WHEN** 同owner-loop中已有ready候选未满，且同路由collector当前已有可填前缀，原非预留名额提示可用
- **THEN** MAY 先在ready锁外用原flush把collector整个组封入ready，再由原_take/repack按16条/估算1MiB与FIFO裁决；不能在持ready锁时调flush，也不能直接借未seal项或把提示当一次claim
- **AND** 已有ready的任何nonfit/partial尾不能越过，collector首项不fit也不促封；本轮任意到期ready先由原_take处置，不能为后面较空路由提前flush；已有deadline不变，新提前封组从当次seal起原1秒，部分余项保原deadline和逐成员预算；关闭/取消/提交未知、逐项认证/fresh clock/计费与MQ确认规则不变

#### Scenario: RabbitMQ 第二条不能接管
- **WHEN** 同组第二条发生内容故障或容量不足
- **THEN** 仅合法连续前缀可提交，确认提交后才 ACK 该前缀；故障及后继不 ACK，沿原分类处置；数据库或提交未知时整组不确认，重投不重复记账

#### Scenario: 接收资源与有限预取
- **WHEN** RabbitMQ 多条模式授予最多 16 个原生 credit 或收到 content header
- **THEN** 固定帧及逐正文装配峰值在分配前计入原 owner/global 预算，不能可靠安装受检门禁则保持旧单条读取；不足时停止并退避而非生成 poison，关闭未确认不提前回收，原大小及总资源上限不扩大

#### Scenario: 合法大消息的首次准备及下一窗口
- **WHEN** 一个新收集窗口首次收到完整消息，或提交后的 ACK 补充下一窗口
- **THEN** 在首条准备确实取得工作区之前暂停该专用 SDK reader 与后继解码；已有合法连续前缀可进展，不能因原文预取占满 owner 而永久零进展，最多 16 个 retiring 身份与新预取共用原 2 MiB owner/64 MiB global，其他连接不受 reader 控制影响

#### Scenario: 后继读取预算不足时先确认已提交前缀
- **WHEN** 已有准备完成的合法连续前缀，而下一次帧读取或正文分配不能取得预算
- **THEN** 保留未解码帧并暂停后继读取；只有前缀确认提交后才在接收仍暂停时 ACK，实际释放预算后才恢复读取，不能先关闭连接使同一前缀永久重投；预算不足不写坏消息屏障

#### Scenario: 瞬时速率窗口已满而持久容量仍可用
- **WHEN** RabbitMQ 多条模式的只读快照显示本入口或全局速率余量为零，所有原硬门禁仍满足
- **THEN** 可在保留原 owner、暂停 reader 和字节归属的情况下按可取消等待重读，连续等待 deadline 最多 1 秒；有余量时收集最多 min(16, 本入口余量, 全局余量) 条，实际 T1 仍逐项校验，快照后竞争沿原有效前缀与关闭路径处置；硬拒绝或等待耗尽不无限维持未服务的 SDK 连接


### Requirement: 合法大单条可独占调度工作轮
系统 SHALL 区分持久责任预付账与单轮逻辑工作字节预算。对已按原输入限制合法接管的不可拆per_event单条，pending和processing候选的保守工作计费 SHALL 不超过整轮工作预算；实际预付账、容量占用和保留输入 MUST 保持。

#### Scenario: 大预付账单条后仍有健康订阅
- **WHEN** 合法单条的多副本/元数据预付账超过1MiB，且后续还有健康订阅的可运行项
- **THEN** 该单条可独占原工作轮并推进公平游标，后继正工作量项在后续轮继续派发或完成确认
- **AND** 单轮仍受原100候选/1MiB逻辑工作和处理阶段50ms限制，小项按原工作计费累计，business_batch整轮计费不变
- **AND** 调度不得改写持久账本、配额或输入上限，也不得通过读取大JSON来估计候选成本
