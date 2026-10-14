# Proposal

## Why

#203 的已解析文本附件被模型误当成不得采用的需求；#204 存在历史最终 JSON 与传输偶发失败，最新复测已经恢复成功，不能预设会话污染。

## What Changes

- 统一 Prompt 分层，允许按 USER_REQUEST 授权采用附件业务需求与替换 Working Copy，保留系统权限边界。
- 显式区分技术回复与代码任务完成，咨询不强迫 Candidate。
- 按实际模型与端点选择输出策略，严格校验不变；最终格式或 Schema 失败最多一次预算内纠正。
- 回归附件拒绝后新请求与原请求 Retry/Regenerate 身份及错误恢复，中英文一致。
- 非目标：#129、#202 OCR/Office/渲染、永久聊天、通用 Agent 框架、自动 Apply/Save/Run。

## Capabilities

### New Capabilities

- `ai-authorized-reference-tasks`: 用户授权的业务材料与代码任务完成状态。
- `ai-response-recovery`: 严格协议、有限纠正与同会话恢复。

### Modified Capabilities

无。

## Impact

影响 backend AI Prompt、Provider 薄适配、响应校验、脱敏审计及 Web AI 恢复提示。新增可选请求与响应元数据保持旧客户端兼容；无数据库、凭据或部署迁移，回滚代码即可。真实 MiniMax/Kimi 与 Chrome 验收由主 Agent 单独记录首次结果。
