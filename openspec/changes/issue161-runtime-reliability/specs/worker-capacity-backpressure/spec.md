## Purpose

保证 Worker 的消息接收与实际执行槽占用协调，正常容量等待不转化为基础设施投递失败，同时保留 durable Claim 和私有 journal 后确认、短未确认窗口、连接响应性及故障恢复语义。

## ADDED Requirements

### Requirement: 接收预留和业务占用共享有限槽预算
Worker SHALL 在接收前预留真实执行槽，所有接收中、暂停取消中、Claim 中和运行中的责任总数 MUST 不超过配置 S；无槽时让未接收消息保留在 Broker。ACK 不释放执行槽，容量等待 MUST 不关闭连接、触发重投、使用无界本地缓冲或线程池排队。

#### Scenario: 任务占满槽并超过 consumer timeout
- **WHEN** S 个任务 ACK 后运行超过现有 300000 ms consumer timeout，新的合法 Execution 到达
- **THEN** 新消息保留 ready，既有连接/心跳继续，容量原因不产生新失败 delivery count 或 DLQ；释放槽后原 Execution 继续

#### Scenario: 暂停和槽释放竞态
- **WHEN** 暂停接收请求、在途 delivery、多个槽释放和重新接收同时发生
- **THEN** 每条在途消息均有已预留槽，责任总量不超过 S，无消息丢失、双释放或第二 active Attempt

### Requirement: 未 ACK 窗口仅覆盖握手
Worker SHALL 保留 `deliver → durable Claim → durable private journal → ACK send → Sandbox start` 顺序。ACK MUST 不等业务终态，原 consumer timeout/delivery limit 不得放宽以掩盖容量问题；握手和取消注册必须有真正可执行的有界截止。业务、HTTP 和落盘不得阻塞连接事件处理。

#### Scenario: CancelOk 延迟或丢失
- **WHEN** Broker 继续心跳但不完成接收取消握手
- **THEN** IO仍响应，真实 deadline 触发明确协议故障和有界清理/重连，尚未 Claim 的消息不启动业务且槽只释放一次

#### Scenario: journal 或 ACK 边界失败
- **WHEN** journal 失败、ACK 发送调度失败或原 channel 已关闭
- **THEN** 未满足顺序的任务不进入 Sandbox；已 durable Claim 保留 journal/Lease 恢复责任，不把 ACK 排队当成 Broker 已确认

### Requirement: 真实故障和正常容量分别诊断
Worker SHALL 区分容量等待、Control/auth 故障、Broker 故障和协议超时；诊断可关联 message/Execution/connection epoch，MUST 不记录敏感 payload、URI userinfo 或 Token。断线、重复消息、迟到回报继续遵循原 Claim、Lease、fencing 和 Slot 合同。

#### Scenario: 老连接回调到达新连接
- **WHEN** 断线后原任务完成并回调 ACK 或释放
- **THEN** 不以旧 delivery tag 操作新 channel，不重复释放槽，新连接仅接收真实空槽数的消息

#### Scenario: 业务入口覆盖
- **WHEN** 分别执行计划自动重试、Webhook 容量等待和手动排队
- **THEN** 每项验收核对原 Execution ID、Attempt、输出及 generation，不能用新建后继任务成功代替
