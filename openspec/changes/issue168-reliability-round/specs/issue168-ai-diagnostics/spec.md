## Purpose

本合同在 #166/#167 页面临时对话边界内提供足以区分 Provider 传输、响应解包、严格 JSON、Schema 和业务校验的非敏感观测，使实际 AI 失败可归因，预算与共享时限可核对，同时保留用户可见操作及候选人工应用边界。

## ADDED Requirements

### Requirement: Safe stage diagnostics preserve strict validation
AI 调用 SHALL 通过稳定的非敏感阶段和原因标识记录失败，保留统一公开错误码及既有协议/秘密/配置校验。诊断 MUST 不输出或持久化完整 Prompt、Working Copy、Provider 响应、reasoning、工具正文、附件、凭据名称/值或私有端点。

#### Scenario: A provider reply fails validation
- **WHEN** 同一公开 ai_response_invalid 来自解包、JSON、Schema、Unicode 或配置匹配中的不同阶段
- **THEN** 受控诊断 SHALL 区分失败阶段和安全原因，不记录失败正文/字段值；用户请求和人工配置边界不被放宽

### Requirement: Each provider call has observable budget and deadline
初始、工具循环和 finalization 的每次 Provider 调用 SHALL 记录可关联的非敏感 purpose、调用序号、预算/裁剪计数与起止/剩余时限，并共享原端到端 deadline。

#### Scenario: Finalization follows a bounded tool stop
- **WHEN** 工具达到合法停止条件而进入 finalization
- **THEN** SHALL 对该调用重新核对剩余预算和同一 deadline，不递归重试、不重置请求总计时，也不能以先前工具成功代替该次证据

### Requirement: Active cancellation stops waiting and isolates results
#195 SHALL 提供“停止等待”：立即中止本页 HTTP 等待、隔离当前请求的迟到结果并允许再次发送，保留已有可见对话。该操作 MUST 不承诺 Provider 终止/撤销或取消计费，上游继续受现有 deadline 和工具限制约束；不引入后台任务、取消服务或持久会话。在实际实现/验收未完成前，F16-006--cancel 保持待证，刷新/切换不能代替主动取消。

#### Scenario: A stopped request finishes late
- **WHEN** 用户停止本页等待后再次发送，而上一个请求自然完成
- **THEN** 上一个请求的消息、Candidate 或错误 SHALL 不回流到当前对话，不影响新的请求状态，也不自动 Apply/Save/Run
