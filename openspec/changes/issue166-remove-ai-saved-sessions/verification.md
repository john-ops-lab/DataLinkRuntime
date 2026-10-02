# Issue #166 验证记录

## 当前边界

本变更覆盖 #151 / #161 后续保存会话、刷新恢复和持久摘要验收。原交付记录保留，不改写旧结果。临时连续调试、当前 Working Copy、附件/工具、预算及 Candidate Diff/Apply 继续验收。

## 本地验证

- Web 定向 Vitest 68 tests PASS：临时历史、忽略旧选择键、同页关闭重开、重新挂载清空、账号/Adapter 切换迟到回复，以及原有 Regenerate/附件/工具合同。
- Web ESLint、TypeScript、production build PASS；Ant Design 5.29.3 CLI lint 无发现。
- Backend Ruff / format / Mypy PASS；使用本轮隔离 PostgreSQL 跑相关 API/Prompt/预算/迁移/retention/locale 测试：原有 226 项通过，修正新增测试后 14 项定向复测通过。迁移包含拒绝无备份确认、有确认退役、空表升级和空结构降级，并核对其余业务表行完全一致。
- 初次定向后端执行暴露新增测试的 fixture 名与历史 schema 断言错误，修正后复测；这些为测试编写问题，不能据此宣称业务验收通过。

## PR 与实际运行验证

- PR #167 首个完整提交 `2e3825e` 的 `local-preview`、`backend`、`web`、`compose-smoke` 均成功。页面验收发现并修正中英文旧保存会话提示；最终提交继续以当前 HEAD 的完整 CI 和实际部署回执核对。
- 固定验收环境保存 custom-format 私有备份，并在独立临时数据库实际恢复；全部 50 张表逐行一致后清除验证库。迁移退役 5 个会话、45 条消息所在的两张聊天表，其余 48 张业务表逐行一致。Provider 元数据、原有资产/历史保全检查、PostgreSQL/RabbitMQ 容器身份与验收持久卷保留。
- 真实 MiniMax 三轮：前两轮临时上下文承接标识，第三轮生成完整 Python Candidate；AI 调用未保存版本。人工保存验收 Candidate 后，经真实 RabbitMQ/Worker 执行成功，输出正确，workspace cleanup completed。
- 实际旧 API 5 种请求均 404；6 种旧持久轮次字段均 422。
- 本机 Chrome：保存会话/切换/清空/删除入口不存在；同页收起重开保留临时对话；真实模型生成 Candidate，Diff/Apply 显式进行，Apply 后后台版本与代码完全不变且运行按钮提示先保存；刷新重选 Adapter 后对话、Candidate 清空。
- 初次进入账号入口时既有验收账号处于 disabled 状态，未改变账号状态。本轮实际页面验证使用固定 Token 入口。

备份、原始 Provider 结果、截图、镜像/部署回执及所有本机参数仅保存于私有证据目录，不纳入公开仓库。PR 保持开放待审，不自动合并。
