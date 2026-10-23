# Spec Delta

## Purpose

定义 Kubernetes 当前状态订阅的 LIST/WATCH、对象身份、批次完整性与显式合并规则，使来源恢复和目标对账各自具备明确证据，并避免因不完整读取、历史过期或同名对象重建而误清理目标。

## ADDED Requirements

### Requirement: Kubernetes 范围和对象身份稳定
每条订阅 SHALL 固定 cluster identity、一个 GVR、namespace 范围和 selectors。对象键 MUST 包含 UID；resourceVersion SHALL 作为不透明字符串保存，不作数值排序或跨对象比较。范围变更 MUST 显式新建 scope/epoch，不能自动清理旧范围对象。

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
系统 SHALL 提供 `sync.started`、`sync.completed`、`sync.failed` 事件。只有所有 LIST 页面成功、成员处理成功且批次未失效，才允许派发 completed；started 成功先于成员派发。完成事件 MUST 区分来源读取完整与目标对账，目标清理由 Adapter 核对自身写入事实后决定。

#### Scenario: 读取完成但一个对象写入失败
- **WHEN** LIST 已读完而一个成员 Execution 失败或被 skip
- **THEN** 不派发可用于清理的 completed，skip 使本批失效，须新完整批次才能重新取得对账资格

#### Scenario: 完整批次目标确认
- **WHEN** 所有成员处理成功且完成事件被执行
- **THEN** Adapter 收到明确 batch/scope/数量，核对目标写入后才能只在受管范围清理；后续 WATCH 派发等待此完成事件成功

### Requirement: 批次失败可排空且控制记录有保留额度
活动批次 SHALL 预留有限的生命周期控制记录额度，覆盖 pending、原文/历史和源身份去重元数据的订阅及全局计数；普通对象、WATCH 和其他订阅不得占用。整批对象/字节或可用历史预算不足时 MUST 明确失败。failed 标记处理或显式处置后 SHALL 打开失败批次派发屏障，让已接管对象/WATCH 排空，但该批永远不得恢复清理资格。

#### Scenario: 满额时结束批次
- **WHEN** 普通数据触及配额且需要记录 completed 或 failed
- **THEN** 使用预留额度完成批次状态记录，不出现既无法关闭又无法释放保护的循环等待

#### Scenario: 失败批次后已有 WATCH 积压
- **WHEN** WATCH 已接管但批次随后因衔接失败失效
- **THEN** 先终结对账资格，失败标记处置后排空原责任，再启动新 LIST，不无限等待旧 completed

### Requirement: 完成执行与批次失效原子互斥
completed 的创建 SHALL 与批次 failed/superseded 转换互斥并冻结当时完成资格。失效先提交 MUST 阻止 completed；completed 先提交后发生的 WATCH 缺口 SHALL 触发下一轮恢复，不追溯撤销既有执行或伪造旧批失败。

#### Scenario: 完成派发与 WATCH 410 竞争
- **WHEN** completed 创建事务和同批失效事务并发
- **THEN** 只有一个合法路径生效；已提交 completed 保留原输入并按普通重试收敛，新缺口记录为后续恢复

### Requirement: WATCH 恢复如实表达历史缺口
断线 SHALL 从最后安全持久 RV 续接；410 SHALL 记录历史缺口并重新 LIST，不能宣称补齐所有历史。BOOKMARK 只能在此前事件可靠接管后推进位置，不创建业务执行；系统不得依赖固定 bookmark 周期。

#### Scenario: WATCH 历史过期
- **WHEN** 保存 RV 返回 410
- **THEN** 失效尚未冻结完成资格的批次，已冻结批次保留资格；分别按对应路径收敛已接管责任后新建 LIST 批次，并在追踪中保留 gap

#### Scenario: 收到 bookmark 前尚有未提交项
- **WHEN** bookmark 已到达但前序变化未持久接管
- **THEN** 持久 cursor 不越过未接管变化

### Requirement: Kubernetes 容量故障不伪装为空集
容量、超大对象、解码或权限错误 SHALL 停止受影响范围、保留诊断并按需要标记重新同步；错误事件 MUST 不作为对象删除。所有已接管责任仍须执行或显式处置。

#### Scenario: 大对象阻塞 LIST
- **WHEN** 一页包含超限对象
- **THEN** 批次失败并保留有界故障摘要，不返回“成功读取零对象”，不触发清理
