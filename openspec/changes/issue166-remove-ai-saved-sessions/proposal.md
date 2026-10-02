## Why

AI Assistant 用于围绕当前 Adapter Working Copy 调试代码。保存和恢复历史聊天引入了额外界面、服务端状态及迁移负担，偏离当前产品目标。

## What Changes

- 移除保存会话、列表、切换、读取、清空、删除和刷新恢复入口；仅保留同页内存态连续调试。
- **BREAKING** 移除持久会话 API 与 Assist durable 字段；旧调用不得返回历史或偷偷退回普通请求。
- 移除持久消息、任务状态、摘要、保留清理实现；向前迁移退役两张聊天表，已有数据先备份再清理。
- 保留当前 Working Copy、最近消息、Prompt/预算、附件/工具、严格输出校验和 Candidate Diff/Apply。

## Capabilities

### New Capabilities

- `ephemeral-ai-debugging`: AI 临时调试及持久聊天退役边界。

### Modified Capabilities

无。Issue #151 和 #161 的旧交付证据保留，本变更覆盖其后续保存聊天验收边界。

## Impact

Web AI panel/client/types/双语文案，Control API/schema/模型/retention/摘要代码，新增 0046 迁移、双语产品和架构文档及对应测试。不改 Provider 配置、其他业务数据或工具审计。非目标：自动执行、自动 Apply、调试能力扩张。
