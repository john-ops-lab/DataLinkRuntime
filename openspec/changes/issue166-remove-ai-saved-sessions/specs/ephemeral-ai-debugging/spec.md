## ADDED Requirements

### Requirement: Ephemeral current-code debugging

系统 SHALL 仅保留当前页面内存中的 AI 调试消息，继续支持最近消息、当前 Working Copy、附件/摘录、工具、预算、失败重试和冻结轮次重新生成。

#### Scenario: Same-page continuity
- **WHEN** 用户关闭并重新打开同一 Adapter 的 AI 面板
- **THEN** 临时可见消息仍在，Candidate 必须经显式 Diff/Apply 才能修改 Working Copy

#### Scenario: Refresh or scope change
- **WHEN** 页面刷新、组件重新挂载或账号/Adapter 范围变化
- **THEN** 旧消息及 Candidate 不恢复，迟到请求不得提交到新范围

### Requirement: Retire durable conversation access

系统 MUST 移除保存会话入口和持久聊天读写路径，不从浏览器存储、数据库或摘要恢复历史。

#### Scenario: Obsolete client
- **WHEN** 旧客户端调用保存会话路由或在 Assist 中提交 durable 字段
- **THEN** 路由返回 404，durable 字段返回 422，不能读取历史或调用 Provider

### Requirement: Scoped history retirement

迁移 SHALL 只退役 AI 聊天两表，保留 Provider 设置、Adapter、Revision、Execution、日志、Credential 及其他业务资产；已有聊天数据在备份校验确认前 MUST 拒绝删除。

#### Scenario: Existing history without verified backup
- **WHEN** 非空聊天表升级且没有备份校验确认
- **THEN** 迁移拒绝，schema、聊天数据及其他业务数据保持不变

#### Scenario: Verified backup or fresh install
- **WHEN** 已确认私有备份可读，或聊天表为空
- **THEN** 0046 移除两表，其他业务资产不变，fresh install 成功
