# Design
## Context
见 proposal.md。现有 InputConfig 服务集中事务，Schedule/导入复用该入口；Web 已有 inputEditEpoch。
## Goals / Non-Goals
目标是窄保存边界修复；不修改执行、其他字符串合同或数据库。
## Decisions
- 递归纯函数返回值与数量，按 JSON Pointer 报告对象路径和冲突键；在大小检查之后和变更之前调用，保留原始输入大小门禁。
- 服务返回对象附加本次保存移除数量，响应添加默认 0 的 removed_null_characters；GET 默认 0，不持久化计数。
- Web 解析后清理，Object.fromEntries 保留 __proto__ 等键；数量由前端本次清理加服务器清理值组成。
- 保留现有编辑 epoch，并以组件请求范围保护响应；成功 Alert 展示普通成功或数量反馈，API 错误定位键冲突。
- JSON 请求可选 original_json_size_bytes 为 strict 非负整数，仅 JSON 分支有效。服务以 max(实际紧凑 UTF-8 大小, 声明原大小) 检查；声明只能提高检查量，未声明仍按实际入参检查。
- JSONB 类型和 JSON.NULL、flag_modified 沿用现有合同。替代方案只改前端会让其他入口继续 500；更换存储不符合确认范围。
## Risks / Trade-offs
空字符不可恢复 → 明确保存反馈；键冲突 → 写入前拒绝；迟到结果 → epoch 和请求范围保护。
## Migration Plan
无迁移、无新依赖；代码回滚不恢复已移除字符。
