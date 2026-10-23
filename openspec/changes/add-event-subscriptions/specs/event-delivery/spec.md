# Spec Delta

## Purpose

定义跨来源共享的可靠接收与交付行为，使每条已确认数据都有可恢复、可追踪且容量受限的处理责任，并明确去重、人工重处理、失败和留存各自的边界。

## ADDED Requirements

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
源去重 SHALL 使用订阅、source epoch 和源事件 ID，内容冲突只比较稳定源内容。重投 MUST 保留首次 Envelope、接收时间和版本；已接管去重不得依赖新的 Admission 或 RabbitMQ 可用性。

#### Scenario: 接收时间变化且内部队列故障
- **WHEN** 同一事件在新时间重投且 RabbitMQ 不可用
- **THEN** 系统仍识别原责任，不误报内容冲突、不建立新执行

#### Scenario: 同身份不同内容
- **WHEN** 同一稳定身份携带不同内容
- **THEN** 报告 `event_identity_conflict`，不覆盖原内容并阻塞受影响 shard

### Requirement: 重试层次和顺序失败可解释
系统 SHALL 将来源重连、平台调度退避和 Execution 有界重试分开。业务失败、取消或过期 MUST 不被自动视为成功；其后续是否可执行由来源顺序合同控制。普通单槽串行不得被宣称为重试顺序保证。

#### Scenario: 业务错误耗尽重试
- **WHEN** 当前事件进入 dead_letter
- **THEN** 保留失败事实和处置入口，相关顺序屏障保持阻塞，平台不无限新建执行

### Requirement: 人工重处理创建可追踪新责任
replay SHALL 创建保留原事件与原执行关系的新 Delivery/Execution，按请求 ID 幂等并经过容量检查。默认使用原输入、Revision、Worker 和凭据引用；更换为当前配置 MUST 显式选择且只在停用、无其他未完责任时允许。显式 repair 下可在后续 pending 保留时仅指定修复 Revision，仍须满足受限修复合同。

#### Scenario: 重复提交补处理请求
- **WHEN** 同一 request_id 被重复提交
- **THEN** 返回同一补处理责任，不创建额外执行，也不修改原终态

#### Scenario: 原版本不可用
- **WHEN** 默认 replay 依赖的版本、Worker 或原内容不可用
- **THEN** 返回明确不可重放原因，不静默使用当前版本或不完整输入

#### Scenario: 重放已被后续越过的历史记录
- **WHEN** 用户重放已完成或已跳过、且已有后续业务的旧事件
- **THEN** 必须先停用排空，记录带外修复，不回退来源位置或承诺原业务顺序

### Requirement: 显式跳过保留损失事实
skip/reset SHALL 要求 edit 权限、原因与幂等请求 ID，并记录操作者、原事件/位置和影响。系统 MUST 将跳过显示为未处理处置，不能伪装为业务成功，不能隐式清除其他已接管责任。

#### Scenario: 操作员跳过毒消息
- **WHEN** 有权限的用户明确跳过一个阻塞来源记录
- **THEN** 仅被审查位置按来源合同前移，记录未处理原因；后续责任仍可追踪

### Requirement: 容量覆盖内存待处理执行和历史
系统 SHALL 为连接、预取、单项大小、pending、接管速率、Execution、原文历史和去重元数据设置数量/字节上限及最老时间告警。触限 MUST 暂停新接收或按来源标记需同步，不能静默丢弃、无限缓冲或立即热重试；恢复须满足明确低水位。

#### Scenario: 历史额度已满
- **WHEN** pending 很少但保留窗口内的原文历史已达上限
- **THEN** 停止新接管，不通过提前删除窗口内数据腾空间，页面显示具体额度原因

#### Scenario: 超大消息
- **WHEN** 单项超出来源或最终 Envelope 限制
- **THEN** 记录有界 `blocked_unaccepted` 摘要且不确认来源，不将截断内容当成完整接管

### Requirement: 去重和重放窗口有限且不删除未完责任
系统 SHALL 默认保留可重放原文 7 天、源去重元数据 30 天，并保护未完责任、未解决屏障及批次引用。内容过期的 replay MUST 返回 `event_payload_expired`；源无永久进度保护时不得承诺窗口外永久去重。

#### Scenario: 保留时间已过但仍阻塞
- **WHEN** 失败事件仍阻塞顺序屏障或属于未完成批次
- **THEN** 自动清理保留其必需内容和关联，不以 TTL 抹去责任

#### Scenario: 已完成事件原文过期
- **WHEN** 无保护引用的已完成事件到达原文清理窗口
- **THEN** 可清理原文并保留独立去重摘要，查询说明原文已过期，不能提供虚假可执行 replay
