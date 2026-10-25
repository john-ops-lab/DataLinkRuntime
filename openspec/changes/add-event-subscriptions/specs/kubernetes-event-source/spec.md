# Spec Delta

## Purpose

定义 Kubernetes 当前状态订阅的 LIST/WATCH、对象身份、批次完整性与显式合并规则，使来源恢复和目标对账各自具备明确证据，并避免因不完整读取、历史过期或同名对象重建而误清理目标。

## ADDED Requirements

### Requirement: Kubernetes 范围和对象身份稳定
每条订阅 SHALL 固定logical cluster identity及server/有效CA信任、一个GVR、namespace范围和selectors，不宣称原生全局cluster UUID。server SHALL 为HTTPS origin，路径由已验证Pod/Deployment支持矩阵构造，不将server附带path当任意API入口。对象键 MUST 包含 UID；resourceVersion SHALL 作为不透明字符串保存，不作数值排序或跨对象比较。范围、server或有效CA信任变化 MUST 在停用、原生关闭、结清旧责任后显式新建scope/epoch，不能自动清理旧范围对象；同一credential引用的token值轮换保持epoch和持久RV。

#### Scenario: 同名对象删除后重建
- **WHEN** 相同 namespace/name 出现新 UID
- **THEN** 新旧对象不合并，旧删除不得作为删除新对象的依据

#### Scenario: selector 范围离开
- **WHEN** WATCH 返回一个离开选择范围的 DELETED
- **THEN** 保留事件和范围上下文，不宣称该对象必然从集群物理删除

### Requirement: 默认逐条与状态合并显式区分
默认 `per_event` SHALL 保留全部已接管变化。只有显式 `state_sync` 才可合并尚未创建 Execution 的同 UID WATCH 更新；LIST 成员、批次标记、不同 UID 和删除/离开范围边界 MUST 不合并。两种模式均不得承诺恢复源已过期的全部历史。

#### Scenario: 执行期间又收到更新
- **WHEN** 同一对象已有 Execution，而收到新变化
- **THEN** 旧输入不变，新变化形成后续 pending；合并只能作用于尚未派发的责任

#### Scenario: 合并后的追踪
- **WHEN** 多条可合并更新由最后状态承担处理
- **THEN** 保留起止源身份、合并次数和替代关系，不能将被合并项伪装为独立执行成功

### Requirement: LIST 批次与 WATCH 来源身份可区分
每次全量 LIST SHALL 建立独立 batch ID 和范围指纹，LIST item 身份包含 batch ID，WATCH 身份包含 UID、事件类型和 RV。分页读取 SHALL 保持同一快照；不完整页面不得标记完整集合。

#### Scenario: 新批次含相同 RV 的对象
- **WHEN** 重同步 LIST 再次返回同一 UID/RV
- **THEN** 对象仍作为新批次成员处理，不因旧 WATCH 或旧批次去重而漏记成员

#### Scenario: 分页中途失败
- **WHEN** 继续令牌失效、权限错误或超时导致 LIST 未完成
- **THEN** 该批标记失败，不推断未读取对象不存在，不发送成功完成标记

### Requirement: 目标对账依赖完整成功屏障
系统 SHALL 提供 `sync.started`、`sync.completed`、`sync.failed` 事件。只有所有LIST页面成功、各成员最新责任成功且批次未失效，才允许派发completed；正常有效批次中started成功先于成员派发，失效批次按failed处置后的排空规则收敛。完成事件 MUST 区分来源读取完整与目标对账，目标清理由 Adapter 核对自身写入事实后决定。

#### Scenario: 读取完成但一个对象写入失败
- **WHEN** LIST已读完而一个成员最新责任仍失败
- **THEN** 不派发completed；若完整快照及批次仍有效，显式repair/replay最新责任成功后可重新检查全员成功，历史失败本身不永久废除资格

#### Scenario: LIST 成员或 started 被显式 skip
- **WHEN** LIST成员或sync.started被显式skip
- **THEN** 本批永久失去对账资格并进入唯一failed控制收敛路径，后来成员成功也不复活旧批，只有新完整批次才可重新取得资格

#### Scenario: 完整批次目标确认
- **WHEN** 所有成员处理成功且完成事件被执行
- **THEN** Adapter 收到明确 batch/scope/数量，核对目标写入后才能只在受管范围清理；正常路径中后续WATCH派发等待此完成事件业务成功，只有本规格定义的专用人工放弃可例外解除完成屏障，不能据此声称目标对账成功

#### Scenario: 成功闭合完成责任不得重新执行
- **WHEN** completed 已成功确认且批次闭合后，再请求新的原/repair/current-config replay
- **THEN** MUST 拒绝，不重开清理资格或已结账控制预留；原请求仅按当前权限和原参数只读回原 Delivery，重新全量对账使用新完整批次，普通对象/WATCH补处理不受此限制

### Requirement: 批次失败可排空且控制记录有保留额度
活动批次 SHALL 预留有限的生命周期控制记录额度，覆盖 pending、原文/历史和源身份去重元数据的订阅及全局计数；普通对象、WATCH 和其他订阅不得占用。整批对象/字节或可用历史预算不足时 MUST 明确失败。failed标记处理或显式处置后SHALL打开失败批次派发屏障，让已接管对象/WATCH排空，但该批永远不得恢复清理资格。控制可以越过批次等待及早期普通失败，MUST不越过active Attempt；处置后SHALL重算并恢复普通失败/active屏障及repair入口，不能直接清空checkpoint。

#### Scenario: 满额时结束批次
- **WHEN** 普通数据触及配额且需要记录 completed 或 failed
- **THEN** 使用预留额度完成批次状态记录，不出现既无法关闭又无法释放保护的循环等待

#### Scenario: 失败批次后已有 WATCH 积压
- **WHEN** WATCH 已接管但批次随后因衔接失败失效
- **THEN** 先终结对账资格，失败标记处置后排空原责任，再启动新 LIST，不无限等待旧 completed；sync.failed业务失败可显式generic skip并审计为未处理，不恢复旧批清理资格

### Requirement: 控制预留受相同速率和有界恢复治理
控制落账 SHALL 服从同一每订阅10/s、全局50/s窗口，不豁免rate。持久ready-control意图SHALL按等待时间和稳定ID公平取得下一可用令牌；等待成员成功的completed不算ready。后台MUST专门有界扫描并逐Batch短事务，不持全局quota反取Adapter锁；Admission拒绝时已材料化控制留Inbox等T2。未闭合空Batch、待控制及未结预留MUST保留运行锁/引用，停用或featureoff后仍维护既有责任；有界GC须保护Batch对activation/config的引用。

#### Scenario: 普通数据持续竞争速率令牌
- **WHEN** ready控制与其他订阅的普通接管持续竞争下一窗口
- **THEN** 最老ready控制最终获得令牌而总率不超10/50，暂不可执行的意图有界重试或显式结清，不饿死控制也不以悬空意图永久阻断普通数据

#### Scenario: 空批次停止或进程重启
- **WHEN** Batch已预留但尚无Receipt，来源停用、featureoff或进程退出
- **THEN** 持久意图和预留可恢复，运行锁不因空列表消失；不完整批次走failed，完整有效批次仍可正常完成，未闭合及仍被Batch引用的activation/config不可提前GC

### Requirement: 完成执行与批次失效原子互斥
completed的T2创建SHALL与批次failed/superseded转换互斥，并持久保存独立completion_committed事实；MUST不以可被SET NULL的completed Execution外键推断是否曾冻结。失效先提交 MUST 阻止 completed；completed 先提交后发生的 WATCH 缺口 SHALL 触发下一轮恢复，不追溯撤销既有执行或伪造旧批失败。

#### Scenario: 完成派发与 WATCH 410 竞争
- **WHEN** completed 创建事务和同批失效事务并发
- **THEN** 只有一个合法路径生效；已提交completed保留原输入和冻结事实，正常经原快照受限补处理收敛，或按专用人工放弃结账；新缺口记录为后续恢复，不回写同批failed

### Requirement: 已冻结完成责任有专用人工放弃路径
正常路径 SHALL 在completed业务成功后解除完成屏障。对已冻结但结果仍未确认的完成链，专用 `abandon-completion` SHALL 是唯一人工例外，不是generic skip、成功或同批failed；generic skip已冻结completed MUST拒绝。它保留completion_committed、原Envelope及原失败Delivery/Execution终态，独立记录“放弃后续补处理、业务结果未确认”。请求及条件SHALL满足event-subscription-management的停用/原生关闭/全关联执行终态、ACL、expected version及最新责任/Execution关联绑定；T2先赢冲突，放弃先赢无新Execution/Outbox。最新pending且无Execution的replay可原子记为未执行放弃、释放一次pending并保留历史。

#### Scenario: 人工放弃只解除完成屏障
- **WHEN** 完成链被合法放弃而还有已接管WATCH或普通失败
- **THEN** 旧WATCH按持久原序排空，普通失败及repair入口保留；不自动enable或新LIST，旧责任结清后才可显式启用新完整批次

#### Scenario: 旧完成链不得复活
- **WHEN** 放弃后出现新的原/repair/current-config replay、reset后历史调用、重启或迟到观察
- **THEN** 均不再授予旧completed新的Delivery/Execution/Outbox；仅原请求按同actor、对象、原因和意图幂等读回原结果，冻结及放弃事实不因Execution清理丢失

### Requirement: 完成放弃使用已有预算内单份审计
该动作 MUST 在已有Batch/责任预算的有界槽内原子转换为单份实际审计，不要求额外空位、不免计费或复制原Envelope；审计至多8KiB，30天及引用保护留存，账本可重建占用和剩余预留。未用reserve只结账一次，不提前删原历史，不承诺产品max下新批立刻可启动。

#### Scenario: 完成失败且补处理被历史配额拒绝
- **WHEN** completed已冻结后失败，history_count=4下已用3/预留1，新replay需第五份占用而失败
- **THEN** 合法专用放弃仍能在原预算内结账并留审计，原失败不改成功，既有Envelope及历史保留，无新增完成Execution或同批failed

### Requirement: 目标控制幂等和代次保护阻止旧批误清理
目标示例 SHALL 持久化scope/batch generation及控制幂等结果；generation MUST独立于opaque RV。新批started生效后，晚到或重复的旧completed MUST不得清理新批数据；重复同控制只返回既有业务结果，不重复清理。平台侧放弃不宣称物理撤回远端请求或回滚已发生目标副作用。

#### Scenario: 放弃后新批开始而旧 completed 晚到
- **WHEN** 原完成结果未确认且被放弃，后续显式新批的started和目标写入已生效，旧completed请求才抵达或重投
- **THEN** 目标按持久generation和控制幂等拒绝其清理新批数据，scope外和同名新UID对象继续受保护，人工放弃本身不伪造目标成功

### Requirement: WATCH 恢复如实表达历史缺口
断线 SHALL 从最后安全持久 RV 续接；410 SHALL 记录历史缺口并重新 LIST，不能宣称补齐所有历史。BOOKMARK 只能在此前事件可靠接管后推进位置，不创建业务执行；系统不得依赖固定 bookmark 周期。

#### Scenario: WATCH 历史过期
- **WHEN** 保存 RV 返回 410
- **THEN** 失效尚未冻结完成资格的批次，已冻结批次保留资格；分别按对应路径收敛已接管责任后恢复；正常enabled路径可新建LIST，专用完成放弃后仍保持停用并等待显式启用，追踪保留gap

#### Scenario: 收到 bookmark 前尚有未提交项
- **WHEN** bookmark 已到达但前序变化未持久接管
- **THEN** 持久 cursor 不越过未接管变化

### Requirement: Kubernetes 容量故障不伪装为空集
容量、超大对象、解码或权限错误 SHALL 停止受影响范围、保留诊断并按需要标记重新同步；错误事件 MUST 不作为对象删除。所有已接管责任仍须执行或显式处置。

#### Scenario: 大对象阻塞 LIST
- **WHEN** 一页包含超限对象
- **THEN** 批次失败并保留有界故障摘要，不返回“成功读取零对象”，不触发清理
