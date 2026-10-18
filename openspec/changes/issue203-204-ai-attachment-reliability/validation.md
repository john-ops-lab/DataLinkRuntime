# 确定性验证记录

基线：`2e878f3383b0d80ec6a6d9cbd4779837aa104977`。本记录不替代 MiniMax/Kimi 真实模型与 Chrome 业务验收。

- Backend：`test_ai.py`、`test_ai_attachments.py`、`test_issue203_task_status.py`、`test_issue204_format_correction.py`、`test_issue204_output_strategy.py`、`test_ai_prompt_builder.py`、`test_issue180_ai_diagnostics.py`、`test_issue183_provider_budget_audit.py`、`test_ai_context_budget.py`、`test_ai_tools.py`、`test_ai_tool_audit.py` 共 **384 passed**。任务专用 PostgreSQL 16 数据库，固定合成 Provider，无公网模型调用。
- Backend：修改范围 Ruff check 与 format check 通过；项目 `uv run mypy` **176 source files** 通过。
- Web：typecheck、lint、build 通过。9 个 AI Assistant／临时会话／i18n 回归套件全部通过，覆盖原冻结 Retry/Regenerate、Adapter/scope/generation fencing、业务状态和中英文错误恢复提示。
- OpenSpec：`openspec validate issue203-204-ai-attachment-reliability --strict` 通过。
- 附件恢复分别覆盖损坏 PDF 与类型不支持拒绝后同 conversation_id 正常 TXT 新发送；验证坏文件未调用 Provider，新请求仅包含正常文件。
- 纠正回归覆盖 malformed JSON、额外字段、Schema mismatch、一次成功和二次失败、上下文不足、总时限耗尽、Secret/Unicode 不纠正、上游传输失败不换 Provider。安全停止后的最终答复维持原一次调用合同。
- 原生输出回归仅确认精确官方 OpenAI 支持快照的请求形状及 code-only closed Schema，本地旧配置 echo 兼容与权限校验保持；Kimi Code 能力未知的组合采用保守 prompt_only，不断言其上游不支持 JSON mode。

## 真实验收剩余项

#203：固定 MiniMax/Kimi 模型、端点、文件、原请求和 Working Copy，保留首次 Candidate/Diff 与独立预期，人工 Apply/Save/Run 验证 `asset_label` 和 Marker；混合注入与普通咨询回归。

#204：固定真实 Chrome 轮数，坏附件后同会话正常 TXT 首次恢复、原冻结 Retry、传输错误恢复及 scope/generation fencing；分别记录首次失败与显式重试，不用后续成功覆盖首败。
