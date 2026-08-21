# M5.10 Wave E 可达页面与组件清单

清单以 Wave A-D 后的实际 React 入口和最终 fixture stage 为准。DLR 是 SPA，表中的
“页面”表示用户可到达的入口、工作区或抽屉状态，而不是假设存在独立 URL。

| 可达页面/状态 | 覆盖内容 | 代码证据 | Wave E stage |
| --- | --- | --- | --- |
| Token 登录 | 登录表单、错误反馈、长文案 | `web/src/components/LoginPage.tsx`, `web/src/App.tsx` | `login-error-unshared` |
| Account 登录 | 账号登录、401、错误文案 | `web/src/components/AccountLoginPage.tsx`, `web/src/AccountApp.tsx` | `login-error-unshared` |
| Account bootstrap/loading | session bootstrap loading skeleton | `web/src/AccountApp.tsx` | `loading` |
| 首次登录改密 | 当前密码、新密码、确认、退出登录、改密成功回到登录 | `web/src/components/AccountPasswordPage.tsx` | `force-password`, `force-password-complete` |
| Account user/profile | profile 抽屉、密码表单、长用户名 | `web/src/components/AccountUserPage.tsx` | `profile` |
| Application shell | 顶栏、健康状态、身份、runtime worker、导航、退出 | `web/src/components/ApplicationShell.tsx`, `web/src/components/WorkerStatus.tsx` | `shell-access`, `shell-catalog` |
| Adapter catalog | 类型/状态筛选、搜索、空目录、错误、未共享隐藏、长名称、键盘焦点 | `web/src/components/AdapterCatalog.tsx` | `shell-catalog`, `state`, `shell-access` |
| Task workbench header | Adapter 状态、task tabs、编辑/运行入口 | `web/src/components/TaskWorkbenchHeader.tsx` | `task-ai`, `shared-edit`, `shared-read` |
| Webhook workbench header | Webhook tabs、Calls、Live logs、只读状态 | `web/src/components/WebhookWorkbenchHeader.tsx` | `webhook`, `shared-edit`, `shared-read` |
| Monaco editor | editor accessible name、theme、依赖、credential bindings、View diff | `web/src/editor-setup.ts`, `web/src/components/CredentialBindingsEditor.tsx` | `task-ai`, `candidate-diff` |
| Task runtime settings | manual/schedule、worker、timeout、enable/disabled | `web/src/components/TaskRunSettingsPanel.tsx` | `task-ai`, `state` |
| Webhook runtime settings | URL、credential、worker、timeout、enable/disabled | `web/src/components/WebhookTriggerPanel.tsx` | `webhook-runtime` |
| Live logs/output | run、SSE/log、follow tail、context、maximize/restore | `web/src/components/LiveLogWorkspace.tsx`, `web/src/components/OutputView.tsx` | `task-ai` |
| Execution history | table、detail、log、空历史、maximize/restore | `web/src/components/ExecutionHistoryPanel.tsx` | `task-ai` |
| Webhook Calls | 空调用历史、切换后 state isolation | `web/src/components/WebhookWorkbenchHeader.tsx`, `web/src/components/ExecutionHistoryPanel.tsx` | `webhook` |
| Adapter settings/ACL | owner settings、grantee list、edit/read share、permission denied | `web/src/components/AdapterSettingsDrawer.tsx`, `web/src/components/AdapterPermissionsPanel.tsx` | `owner-settings`, `shared-edit`, `shared-read` |
| System Settings | Credentials、Package Sources、Knowledge Source、AI model tabs/forms、modal/drawer overflow | `web/src/components/SystemSettingsDrawer.tsx`, `web/src/components/AiModelSettingsPanel.tsx` | `system-settings-ai` |
| User Management | search、role/status filter、pagination、selection、bulk enable/disable、create、reset password、敏感字段不回显 | `web/src/components/UserManagementDrawer.tsx` | `user-management` |
| AI assistant | assistant-ui thread/composer、Markdown/code/copy、tool call、attachments、error/loading | `web/src/components/AiAssistantPanel.tsx`, `web/src/components/ai-markdown.tsx`, `web/src/components/ai-tool-call.tsx` | `task-ai` |
| Candidate/Diff/Apply | candidate action、代码-only Diff modal、Apply、关闭、工作副本；宿主跨 keyed AI panel 保持稳定 | `web/src/components/VersionDiffModal.tsx`, `web/src/components/AiAssistantPanel.tsx`, `web/src/App.tsx` | `candidate-diff`, `task-ai` |
| AI maximize/restore and Adapter isolation | 面板和日志 maximize/restore、Escape、切换后不串消息 | `web/src/components/AiAssistantPanel.tsx`, `web/src/components/LiveLogWorkspace.tsx` | `task-ai`, `webhook` |
| Empty/error/disabled/denied | 空目录、503 feedback、loading、disabled primary action、只读/未共享 404 | `web/src/components/AdapterCatalog.tsx`, `web/src/adapter-access.ts`, `web/src/AccountApp.tsx` | `state`, `login-error-unshared`, `shared-read` |

每一项均在四种宽度和两种 locale 中执行；具体 DOM state、截图和异常收集见
[browser-report.json](browser-report.json)。
