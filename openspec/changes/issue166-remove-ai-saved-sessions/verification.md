# Issue #166 验证记录

## 当前边界

本变更覆盖 #151 / #161 后续保存会话、刷新恢复和持久摘要验收。原交付记录保留，不改写旧结果。临时连续调试、当前 Working Copy、附件/工具、预算及 Candidate Diff/Apply 继续验收。

## 本地验证

- Web 定向 Vitest 68 tests PASS：临时历史、忽略旧选择键、同页关闭重开、重新挂载清空、账号/Adapter 切换迟到回复，以及原有 Regenerate/附件/工具合同。
- Web ESLint、TypeScript、production build PASS；Ant Design 5.29.3 CLI lint 无发现。
- Backend Ruff / format / Mypy PASS；使用本轮隔离 PostgreSQL 跑相关 API/Prompt/预算/迁移/retention/locale 测试：原有 226 项通过，修正新增测试后 14 项定向复测通过。迁移包含拒绝无备份确认、有确认退役、空表升级和空结构降级，并核对其余业务表行完全一致。
- 初次定向后端执行暴露新增测试的 fixture 名与历史 schema 断言错误，修正后复测；这些为测试编写问题，不能据此宣称业务验收通过。

## 后续交付

当前 HEAD CI 与固定本机预览验收仍须完成。运行时备份、原始 Provider 结果与部署参数保存在私有证据目录，不纳入公开仓库。最终验收结果见 PR 与交付回执。
