# Proposal
## Why
合法 JSON 中真实 U+0000 无法存入 PostgreSQL JSONB，Task 保存输入返回 500。按 #197 用户确认规则，仅在输入保存边界移除空字符并准确反馈。
## What Changes
- 解析后递归清理字符串值和键，统计字符数量；键冲突可定位且拒绝整个写入。
- 统一 InputConfig 服务覆盖 API、Schedule 兼容镜像和 Adapter 导入。
- 前端清理本次提交、提示数量，并按服务端值回显；迟到响应保留新草稿。
- 非目标：迁移、历史清洗、Webhook、代码、凭据、日志、执行输出、#129。
## Capabilities
### New Capabilities
无。
### Modified Capabilities
- `adapter-input-config`: JSON 保存仅移除 U+0000，保留类型与其他字符、原事务及门禁。
## Impact
Backend InputConfig 服务与响应、Web Task 保存和中英文反馈。JSONB 无迁移，无新依赖；回滚代码不会恢复已清理字符。权限、锁、大小限制、revision、绑定和历史保持。
