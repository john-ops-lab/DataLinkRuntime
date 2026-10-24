# Spec Delta

## Purpose

定义 Kafka 来源在通用事件订阅下的逐条接收、分区进度、来源确认、组内恢复与业务顺序，明确源保留窗口和人工处置边界，使消息接管与目标处理成功可以分别核验。

## ADDED Requirements

### Requirement: Kafka 使用专用消费组和明确首次起点
Kafka 订阅 SHALL 使用专用消费组、明确 topic 列表及 `per_event + partition_serial`。首次启用 MUST 显式选择 earliest/latest 并持久化解析后的分区起点；已有进度 MUST 不因重启重新计算起点。运行期不得静默 offset reset。

#### Scenario: latest 初始化后重启
- **WHEN** 首次 latest 起点已持久化，而接收前进程重启
- **THEN** 从该持久起点继续，不能重新选择更晚的末尾而漏掉中间消息

#### Scenario: 消费组占用冲突
- **WHEN** 本部署另一订阅已绑定同源同组，或检测到不支持的外部组共享
- **THEN** 启用/继续接收被阻止，明确提示使用独立组，不能悄悄分流消息

### Requirement: Kafka 源身份和原始内容可追踪
事件 SHALL 保存 cluster identity、topic incarnation/显式 epoch、partition、offset、时间戳以及原始 key/value/headers。字节 MUST 无损编码并区分 null、空字节和 tombstone；不可验证的 topic 重建检测能力不得宣称已支持。

#### Scenario: 同 topic 名称重建
- **WHEN** 验证到 topic incarnation 改变或无法沿用原身份
- **THEN** 阻塞并要求显式新 epoch，旧责任保留，不能把新消息误归为旧 offset 的重复

#### Scenario: 二进制与重复 header
- **WHEN** 消息包含二进制 value、空 key 或同名多个 headers
- **THEN** Envelope 保留其字节、null 区别和 header 顺序，由 Adapter 决定解码和业务解释

### Requirement: Kafka 只提交安全下一读取位置
连接器 MUST 关闭自动提交及超前 offset store；仅在前序已消费记录均可靠接管后提交安全下一读取位置。压缩/事务造成的 offset 空洞 SHALL 按来源位置解释，不虚构缺失记录，也不能越过已读未提交项。

#### Scenario: offset 存在空洞
- **WHEN** 来源合法返回 offset 10 后的下一条为 14
- **THEN** 系统不等待不存在的 11–13，且只有 14 及此前实际已读记录持久接管后才推进对应安全前沿

#### Scenario: 后序先完成持久化
- **WHEN** offset 14 已提交而此前已读取的 10 尚未提交
- **THEN** 来源 commit 不越过 10，恢复可重新接管未提交数据

### Requirement: 分区顺序屏障跨越重试仍有效
同一 partition SHALL 至多有一个未完成业务 Delivery；只有成功或显式 skip 才放行下一条。retry_wait、失败、取消与过期 MUST 保持阻塞；其他 partition 可继续但不承诺跨分区顺序。

#### Scenario: 前项正在重试
- **WHEN** partition A 的前项处于 retry_wait，后项已接管
- **THEN** 后项不创建 Execution，partition B 的合法责任仍可竞争现有 Adapter Slot

#### Scenario: 显式补处理解除阻塞
- **WHEN** 前项的关联补处理成功
- **THEN** 只解除对应 partition 屏障，原失败执行保持原终态并可追踪

### Requirement: Rebalance 从持久责任恢复
撤销 partition 时 SHALL 停止新接收并仅确认已持久安全位置；新 assignment SHALL 检查所有权并从 DLR 安全 checkpoint 恢复。Broker 提交位置超过 DLR 安全位置 MUST 阻塞诊断，不能据此跳过未证明接管的数据。

#### Scenario: 提交后确认失败再分配
- **WHEN** 数据库已接管但 Kafka commit 失败，partition 被重新分配
- **THEN** 接管者识别原责任并安全补确认，不重复创建业务执行

### Requirement: 位置过期与超限有明确恢复路径
位置超出保留范围 SHALL 报告 `source_history_gap` 并暂停 shard；用户明确处置缺口后才可 reset 并新建 epoch。已接管责任 MUST 保留。容量或毒消息暂停时 SHALL 保持客户端必要组维护，或以可恢复方式退出组，不无限立即重投。

#### Scenario: Kafka 已清理未接收历史
- **WHEN** 保存位置低于来源可读最早位置
- **THEN** 显示缺口和可选择的恢复起点，不默默改为最新位置

#### Scenario: pending 已满
- **WHEN** 当前 pending 数量或字节触限
- **THEN** 暂停相关读取，来源位置不越过尚未接管记录，容量恢复后从安全位置继续
