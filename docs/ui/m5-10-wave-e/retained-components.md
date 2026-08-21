# 保留的 legacy/custom 组件与理由

Wave E 是审计和最小修复，不是重做页面。以下组件均保留，因为它们承载 DLR 已建立
的运行时、ACL、AI 或领域状态契约；没有用通用组件替换它们。

| 组件/模块 | 保留理由 |
| --- | --- |
| `AdapterCatalog` | 负责 Adapter 类型/状态筛选、目录选择、未共享隐藏和领域操作菜单。 |
| `ApplicationShell` | 负责身份、worker 健康、导航、账号操作和响应式布局，不能由普通 Layout 单独替代。 |
| `TaskWorkbenchHeader` / `WebhookWorkbenchHeader` | 分别承载 Task/Webhook 的运行状态、只读边界、tabs 和动作。 |
| `LiveLogWorkspace` / `OutputView` / `ExecutionHistoryPanel` | 绑定 SSE、follow-tail、执行历史/detail/log、maximize/restore 与 runtime 状态。 |
| `@monaco-editor/react` 与 `VersionDiffModal` | 编辑器、代码-only Diff、工作副本和 Apply 具有领域语义；Wave E 只修复 Diff 关闭时的生命周期问题。 |
| `AssistantRuntimeProvider`、`Thread`、`Message`、`Composer`、`AiAssistantPanel` | 延续 assistant-ui External Store、请求快照、上下文片段、Candidate、附件和 Adapter 隔离；Candidate Diff 的 Monaco 宿主由 `App` 保持稳定；没有引入 Ant Design X。 |
| `ai-markdown` / `ai-tool-call` | 保留 Markdown/code、copy、tool-call 以及确定的 AI message 展示边界。 |
| `AdapterPermissionsPanel` / `AdapterSettingsDrawer` | 保留 Owner/edit/read ACL、grantee metadata 和权限拒绝状态。 |
| `CredentialBindingsEditor`、`SystemSettingsDrawer`、`AiModelSettingsPanel`、`UserManagementDrawer` | 保留系统设置、credential metadata、AI 配置、用户角色/批量操作和敏感字段不回显契约。 |
| `AccountLoginPage` / `AccountPasswordPage` / `AccountUserPage` | 保留 account session、首次改密和个人密码流程。 |
| `index.css` 与 `design-system.tsx` | 现有 DLR 领域布局、主题 token、overflow 和 assistant/workbench 样式；没有另建 CSS/UI 系统。 |

审计结果没有发现可安全删除的“已被 Wave A-D 明确替换”的 CSS 或组件。删除证明见
[removed-replacements.md](removed-replacements.md)。
