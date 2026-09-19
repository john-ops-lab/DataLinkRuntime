## Purpose

为已持久化的基础设施 Incident 提供可操作、可追溯的人工恢复或合法终结，继续同一 Execution 并保留冻结材料、派发代次、并发执行与资源责任，同时准确区分死信原因和人工处置计数。

## ADDED Requirements

### Requirement: 旧 Incident 有同 Execution 的人工处置入口
系统 SHALL 在原 Execution 详情提供恢复/终结 UI/API、当前资格和稳定拒绝原因，支持升级前已存在的 Incident。恢复 MUST 继续原 ID/版本/输入/目标/资源/重试快照，不创建新 Execution、不调用业务 Replay 语义、不增加业务 Attempt 次数。恢复已提交派发与最终执行成功 MUST 分别展示。

#### Scenario: 恢复旧 queued Incident
- **WHEN** 有权限用户对符合资格的旧记录请求恢复
- **THEN** 返回原 Execution ID 与可追溯处置记录，原任务随后由正常 Worker 执行，输出、Attempt和代次可核对

#### Scenario: 合法人工终结
- **WHEN** 用户终结符合现有取消状态机的 Incident 关联 Execution
- **THEN** queued/retry_wait 合法取消，running 仅请求协作取消，canonical code 与资源释放遵循既有合同，不直接伪造终态或业务 dead_letter

### Requirement: 提交事务重新核对完整资格
系统 SHALL 在统一锁顺序和当前权威状态下核验 Execution backend/state/generation、active Attempt/Slot、cancel flag、身份和冻结材料。页面可用状态 MUST 不代替提交重检；旧 Incident 不得改变当前有效派发。

#### Scenario: 阅读页面后被 Claim
- **WHEN** GET 显示可恢复但 POST 前 Execution 被 Claim 或进入准备/运行
- **THEN** 恢复返回 active conflict，不创建第二 Attempt；终结只走协作取消

#### Scenario: 取消和重试等待
- **WHEN** Execution 已 cancel_requested 或 retry_wait
- **THEN** 恢复拒绝且不清除取消/提前重试；终结仅按现有状态资格收敛

#### Scenario: 已有终态
- **WHEN** Execution 已 succeeded/cancelled/expired/dead_letter
- **THEN** 幂等反馈原结果，可据事实收敛 Incident，但不重开或改写历史

#### Scenario: 过时代次和身份不匹配
- **WHEN** Incident 指向旧代、未来代或与当前 Adapter/Worker/resource/message 身份不符
- **THEN** 旧代恢复拒绝且只能核实关闭旧 Incident；未来/未知身份保持审查，不重播消息或修改当前有效责任

#### Scenario: 冻结材料失效
- **WHEN** 原文件、Lease、版本、依赖材料或快照引用无法确认有效
- **THEN** 拒绝恢复并给稳定原因，不用当前输入替代；仅在状态机允许时提供合法终结

#### Scenario: 无 Execution 或后端不支持
- **WHEN** 关联记录不存在或不属于支持的机制
- **THEN** 明确拒绝/保持审查，不新建或隐式转换执行

### Requirement: Outbox 根据当前责任决定是否增加代次
系统 SHALL 区分有效同代 pending、活跃 publish lease、可替换 published、已处置和无法验证的 Outbox。有效 pending MUST 沿原 Relay/backoff 推进而不无条件加代；有效当前 published 的人工恢复才创建新 generation/message，旧记录保持审计；活跃 lease MUST 不被抢占。缺失或矛盾身份必须拒绝，不能任意复用旧消息。

#### Scenario: 同代 pending 和 publish 在途
- **WHEN** 人工恢复发现有效 pending 或未过期 publish lease
- **THEN** 前者幂等返回已有责任且不加代，后者返回明确 conflict/retry_after，不双发新代次

#### Scenario: 已投递当前代重新承担责任
- **WHEN** 当前 published 与 Incident 身份匹配且材料/容量合法
- **THEN** 原子增加 generation、创建唯一新 Outbox和审计；旧代到达为 stale ACK_NOOP，Admission不重复计费

#### Scenario: published 实际是终止处置
- **WHEN** row 的 published 状态带既有终止处置标记
- **THEN** 不宣称 Broker 已确认或复活派发；按当前 Execution事实拒绝或幂等反馈

### Requirement: 处置幂等审计和资源收敛原子化
系统 SHALL 使用有界请求和幂等键保存主体、动作、原因码、代次、结果和时间；同 key 同请求返回原 receipt，同 key不同请求返回冲突。审计与 mutation MUST 同事务提交。重复观测 attempts、人工处置数及实际恢复派发数 MUST 分开；读页面和重复消息不得消耗恢复次数。

#### Scenario: 双击、并发恢复和取消
- **WHEN** 同 key 重试或不同 key 的恢复/Claim/cancel 同时发生
- **THEN** 至多一个合法新代和 active Attempt，原子保留可审计结果，Admission/Lease/Slot 不重复释放

#### Scenario: 迟到 Result 与请求终结
- **WHEN** 人工请求终结后旧 Worker 回报或恢复产生了更高 fence
- **THEN** 仅当前合法权威可终结，旧回报不能覆盖结果或释放新 Slot；真实cleanup责任保留

### Requirement: 权限和可见性延续 Execution 边界
Incident 查看 SHALL 需要原 Execution read 权限，处置 MUST 需要原 Execution edit 权限并在服务端重检；跨 Adapter ID、伪造 actor 或隐藏按钮不得绕过控制。API/UI只输出有界非敏感事实并支持中英文。

#### Scenario: 无权限与跨 Adapter 请求
- **WHEN** read-only、无关账号或未认证客户端提交处置
- **THEN** 按既有403/404/401合同拒绝，业务状态与处置次数不变

### Requirement: 死信原因准确且不新增自动恢复
系统 SHALL 根据对应队列的结构化 x-death reason 分类 delivery_limit、rejected、expired、maxlen和unknown；任意非空 x-death、配置头存在或历史链其他原因 MUST 不被当作当前delivery-limit。既有非delivery-limit自动处理继续运行；本轮 MUST 不新增delivery-limit自动恢复，也不凭猜测修改旧Incident kind。

#### Scenario: 不同 x-death 原因
- **WHEN** 接收各原因、混合历史链、缺失/畸形 headers
- **THEN** 分类与当前有效结构化事件一致，unknown不伪称delivery-limit，原非delivery-limit流程按既有合同回归
