# M5.10 Wave E：全可达页面真实浏览器审计

## 结论

本归档只覆盖 Issue #100 的最终 Wave E，不扩展到下一里程碑。审计基于
`origin/main` 的固定基线 `b26bb39497550f721bc70cae5ead9b6cef9f678`，使用
React 19、Vite、Ant Design 5.29.3、`@ant-design/pro-components` 2.8.10 和
现有 assistant-ui。Playwright Chromium 以确定性内存 fixture 替代 Control API、
AI provider、账号和凭据；没有使用真实凭据、真实模型或破坏性操作。

完整用例为 32 个，覆盖 `zh-CN`/`en` 与 1280/1440/1680/1920 px（高度 900 px）。
最终报告包含 240 条状态记录和 240 张 PNG：

- [browser-report.json](browser-report.json)：每条记录的 persona、宽度、locale、
  状态、截图、console/page error、未知请求和 overflow 数值。
- [browser/](browser/)：按 `locale-width-persona-stage` 命名的可复核截图。
- [browser-matrix.md](browser-matrix.md)：执行矩阵与状态结果。
- [inventory.md](inventory.md)：可达页面/组件清单与代码证据。
- [retained-components.md](retained-components.md)：保留的领域组件及原因。
- [removed-replacements.md](removed-replacements.md)：没有删除替代组件的证明。
- [unhandled-issues.md](unhandled-issues.md)：自动化之外的最终用户验收和非阻塞事项。

自动化结果：`console_errors=0`、`page_errors=0`、`unknown_requests=0`、
`overflow_failures=0`。fixture 中预期的登录未授权（401）和错误状态（503）被单独
允许并记录，不被误报成运行时错误。

## 覆盖边界

- 身份与访问：superadmin、account admin、owner、shared edit、shared read、
  unshared、普通 user；覆盖 Owner/edit/read ACL、隐藏未共享 Adapter、只读提示、
  禁止保存/运行/AI Apply，以及账号登录失败、强制改密和权限拒绝。
- Shell 与目录：顶部状态、导航、长用户名/长 Adapter 名称、目录搜索、类型/状态
  筛选、键盘焦点、可访问名称和主操作可见性。
- 管理面：System Settings 的凭据、依赖源、Knowledge Source、AI model 表单；
  User Management 的搜索、角色/状态筛选、分页、选择、批量启停、新建和重置密码；
  Account profile 与密码表单。
- Workbench：Task 和 Webhook 编辑、Monaco、依赖/credential bindings、运行设置、
  live log、follow/maximize/restore、execution history/detail/log，以及 Webhook URL、
  credential、worker、timeout、enable 和空的 Calls。
- AI：assistant-ui Markdown/code、copy、tool call、attachments、loading/error、
  Candidate、代码-only Diff、Apply、maximize/restore、Adapter switching/state
  isolation，以及 read-only 禁用边界。
- 通用状态：loading skeleton、空目录、API error、disabled primary action、modal/
  drawer overflow、长中文/英文、无横向溢出、console/page error 和 unknown request。

## Wave E 最小修复

审计在关闭 Candidate Diff 以及切换 Adapter 时稳定复现 Monaco 0.56 的
`AbstractContextKeyService has been disposed` page error。最小产品修复包括：

- `web/src/components/VersionDiffModal.tsx` 缓存最近一次非空 panes，使用 antd 的
  `forceRender` 并令 DiffEditor 在 Modal 关闭时保持挂载（`destroyOnHidden={false}`），
  避免 Monaco 在异步关闭路径中使用已释放的 context。
- `web/src/components/AiAssistantPanel.tsx` 与 `web/src/App.tsx` 将 Candidate Diff 的
  稳定宿主放在 keyed AI panel 之外；Adapter 切换仍清空 AI conversation/attachment
  状态，但不会在切换提交期间卸载 DiffEditor。App callback 使用稳定引用，避免新增
  渲染循环。

这些修复保持现有 code-only Diff、Apply、工作副本和 Adapter state isolation 契约不变。

审计 fixture 在切换到 Webhook 前关闭可见 AI 面板并等待稳定，以避免并发的 Monaco
卸载与 Adapter 切换竞争；该行为只存在于测试 fixture，不是新的产品状态模型。

没有重新设计 Shell、没有引入新的 UI 系统、没有升级 Ant Design、没有改变 Issue #90
的 identity/session/role、Owner/ACL、CSRF、webhook 或 Wave A-D 的 AI/runtime 状态契约。

## 验收状态

自动化真实浏览器验收已经通过，但这不等于最终用户视觉 PASS。用户仍需在可复核截图
和运行页面上完成最终视觉确认；本 Wave 不代替该确认，不合并、不关闭 Issue #100。
