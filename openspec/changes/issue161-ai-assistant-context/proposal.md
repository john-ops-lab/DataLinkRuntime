## Why

当前 AI Assistant 将稳定规则、运行合同、动态 Working Copy 和本轮参考材料拼在同一个 system Prompt；最近八条消息之外的明确约束也会丢失。#128 与 #151 需要在保持人工确认、严格输出和凭据隔离的前提下，让普通回答、长对话与恢复会话具有可验证的行为。

## What Changes

- 将静态规则拆为随包发布的 `system.md`、`adapter.md`、`tools.md`，引入请求级深层快照和唯一 Prompt Builder；先做等价迁移，再将动态状态与非可信材料放入当前 user 消息，按真实能力加载规则。
- 为普通 Assist、工具后续/收尾及专用摘要调用建立统一输入预算和共享截止时间；必需内容超限时在相应 Provider 调用前拒绝，不截断当前请求或 Working Copy。
- 增加有来源与修订号的会话任务状态、有覆盖游标的有界摘要，以及按服务端认证身份和 Adapter 授权的会话持久化、恢复、清空与删除。
- 保留现有五语言 Runtime Contract、Tool/Knowledge 状态机、Strict JSON、Candidate 人工 Apply、附件与 Managed Input 的安全边界；真实 Provider 长对话和浏览器验收单独留证。

## Capabilities

### New Capabilities

- `ai-assistant-prompt`: 静态规则、请求级快照、消息角色、条件组装、资源发布及普通回答行为。
- `ai-conversation-context`: 各类模型调用的整体预算、任务状态与摘要、会话归属、持久化和恢复。

### Modified Capabilities

无；当前 OpenSpec 主规格没有对应 AI Assistant 能力。历史 `docs/specs/` 只作基线证据。

## Impact

- Control 的 AI service/schema/API、Prompt 与 Provider 组装；Web AI 面板的会话身份和恢复交互；PostgreSQL 新增受限会话表及 Alembic 增量迁移。
- 不引入通用 Agent 框架、新 Provider、向量库、Prompt CMS、自动保存/运行/发布或跨用户语义记忆。现有 Assist 请求保持兼容，新增会话能力需要明确选择；旧客户端可继续不带会话状态调用。
- 迁移保留现有业务库、运行历史、审计与文件卷。新数据表可独立回退应用代码，但不能通过旧二进制或数据库降级来假设恢复新会话；部署和回退遵守现有同卷升级合同。
