## Context

现有临时 Assist 已具备冻结请求、重试/重新生成、请求材料和 Candidate 审查。持久会话是可独立移除的旁路。

## Goals / Non-Goals

保留围绕当前 Working Copy 的临时调试；刷新、重新挂载及账号/Adapter 切换后没有历史恢复。保留同页关闭再打开的连续性。非目标为持久会话兼容、自动执行及 Provider 更换。

## Decisions

1. 移除保存会话 UI 和 client；保留内存消息及请求 generation/scope fencing，防止账号/Adapter 切换时迟到结果写入新范围。旧浏览器选择键不再读取或写入。
2. 删除 session 路由及 durable schema 字段，严格 schema 将旧字段作为 422 拒绝，旧路由 404。保留 audit-only conversation_id。
3. 删除聊天 ORM、摘要/状态/rollup 和过期清理，保留全部 Assist 每次调用预算和工具审计。
4. 保留历史 0045；新增 0046 先检查数据存在性。非空表必须由操作员在完成备份与校验后使用 Alembic `-x ai_history_backup_verified=true` 才能退役。仅 drop ai_conversation_messages 和 ai_conversations，不 CASCADE 其他对象。空表可直接升级。降级只重建空的 0045 表结构，历史内容仅能从私有备份恢复，不能自动回滚数据库。
5. 更新当前双语合同并注明 #166 覆盖 #151 的后续历史恢复验收；旧测试报告和历史迁移不伪改。

## Risks / Trade-offs

临时上下文仍限制最近 8 条并按每次调用预算裁剪；不再进行跨刷新长会话摘要。旧客户端必须刷新/升级。删除聊天表前需真实私有备份，公开证据只记录数量和保全结果。

## Migration Plan

停止 AI 写入并确认空闲；记录两表行数和业务资产快照；保存并验证 PostgreSQL custom-format 备份；带上述确认执行向前迁移；核对 schema 和业务快照；启动候选并验收临时消息、刷新空白、API 退役、真实 Provider 与 Candidate 边界。固定预览使用现有私有验收编排与卷。
