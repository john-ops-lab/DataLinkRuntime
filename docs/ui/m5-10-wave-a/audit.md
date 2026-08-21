# M5.10 Wave A：Web Console 现状审计清单

本清单是 Wave A 的代码审计与后续 Wave 映射，不是本轮页面重构计划。Wave A 只建立官方知识入口、依赖边界、薄主题入口和可重复的真实浏览器基线；下表列出的现状问题不在本轮顺手修复。

## 审计边界与方法

- 可达入口：`web/src/entry-mode.ts` 选择 Token 或 Account 入口，`web/src/App.tsx` 负责控制台壳层；账号入口由 `web/src/AccountApp.tsx` 及其账号页面组成。
- 代码面：`web/src/components/` 下的 Catalog、Workbench、设置/权限抽屉、日志、历史、Diff 和 AI Assistant 展示层；`web/src/index.css` 的布局、固定尺寸、截断与响应式规则。
- 真实浏览器面：`web/tests/e2e/m5-10-wave-a-baseline.spec.ts` 使用 Chromium、固定 API fixture 和真实交互覆盖 Token 登录、Account 登录、Catalog、健康状态、Workbench、管理员运行操作及只读权限可见性。
- 结果：`baseline/baseline-report.json` 记录 2 个 locale × 4 个 viewport × 2 个场景共 16 条记录；每条记录包含截图、原始 console/page error、HTTP error 分类、未知请求、横向溢出和可见性检查点。

## 可达页面与通用组件清单

| 可达面 | 现有实现/证据 | Wave A 结论 | 后续映射 |
| --- | --- | --- | --- |
| Token 登录 | `components/LoginPage.tsx`、`.login-*` CSS | 使用 Ant Design 表单控件；登录行为未改 | Wave B：登录壳层、按钮/反馈、长文案 |
| Account 登录与账号页 | `AccountApp.tsx`、`AccountLoginPage.tsx`、`AccountPasswordPage.tsx`、`AccountUserPage.tsx` | 只由同一 `ConfigProvider` 提供 antd locale/token；不改身份、Session 或账号 API | Wave B/C：页面容器、账号表单、抽屉/反馈 |
| 控制台壳层 | `App.tsx:1087` 起；`.app-shell`、`.app-header`、`.console-body` | 保留现有 DLR 产品色和布局结构 | Wave B：ProLayout/PageContainer 评估、导航与全局状态 |
| Adapter Catalog | `components/AdapterCatalog.tsx:256` 起；`Space.Compact` 搜索/类型/状态筛选 | 只记录固定宽度和原生行选择，不迁移组件 | Wave C：ProTable/QueryFilter 或最小官方适配 |
| Task / Webhook Workbench | `TaskWorkbenchHeader.tsx`、`WebhookWorkbenchHeader.tsx`、`App.tsx` Tabs | 基线验证编辑器主区和 header 可见；不改变运行/保存/权限行为 | Wave D：Workbench 空间、工具栏、图标动作 |
| Runtime / History / Live Log | `TaskRunSettingsPanel.tsx`、`ExecutionHistoryPanel.tsx`、`LiveLogWorkspace.tsx`、`OutputView.tsx` | 仅纳入现状清单；未改变实时日志、SSE、follow-tail 或运行状态 | Wave D/E：状态、滚动、最大化、长日志 |
| Adapter 设置与权限 | `AdapterSettingsDrawer.tsx`、`AdapterPermissionsPanel.tsx` | 未触碰 #90 Owner/ACL 合同；仅记录通用 Drawer/Form 入口 | Wave C：DrawerForm/表单/权限状态的呈现 |
| 系统设置 | `SystemSettingsDrawer.tsx`：Credential、Package Source、Knowledge Source、AI Model | 依赖已锁定但没有引入 ProComponents 页面迁移 | Wave C：表格、筛选、表单、长 URL/状态 |
| 用户管理与账号 Profile | `UserManagementDrawer.tsx`、`AccountUserPage.tsx` | 未修改 #90 用户、Session、Owner 或 ACL 行为 | Wave B/C：标准操作、表单反馈、权限态 |
| AI Assistant / Candidate / Diff | `AiAssistantPanel.tsx`、`ai-markdown.tsx`、`VersionDiffModal.tsx` | 保留现有 assistant-ui/业务状态模型；不接入 Ant Design X | Wave D：面板尺寸、图标、Markdown、Candidate/Diff/Apply |

## 固定尺寸与响应式风险点

以下是现有值的可追踪记录，不代表 Wave A 要把所有固定值删除。

| 位置 | 当前规则 | 风险/验收关注点 | 后续 Wave |
| --- | --- | --- | --- |
| `index.css:132-173` | Catalog `flex: 0 0 260px`，`max-width: 1400px` 时为 230px；类型/状态筛选分别为 96px/88px | 中文长选项、搜索框与相邻控件的可用宽度 | Wave C/E |
| `index.css:539-585` | Workbench header context `flex-basis: 360px`；controls 允许 wrap | 长 Adapter 名、长英文动作、顶部状态组合是否拥挤 | Wave B/D/E |
| `index.css:789-825`、`2103-2115`、`2368-2372` | AI Assistant 展开宽度 390px，较窄视口为 360px | 与 Catalog/Workbench 的空间分配、长内容阅读 | Wave D/E |
| `index.css:1673-1694` | 历史日志高度 `clamp(180px, 42vh, 360px)`；最大化使用 fixed inset | 视口高度、滚动容器、恢复状态 | Wave D/E |
| `index.css:1775-1841` | Schedule/Webhook/Task 配置面板 max-width 720/760px | 长表单标签、窄宽度可读性 | Wave C/E |
| `index.css:1983-1985`、`2283-2296`、`2340-2348` | locale field 220px；账号登录卡片 420px；账号用户卡片 640px | en/zh-CN 文案与表单按钮不被挤压 | Wave B/C/E |
| `index.css:2047-2053` | Package Source test status 宽度 56px | 状态文本截断且无法理解 | Wave C/E |

## 截断、滚动与重复 CSS

- `index.css:258-293`：Catalog 名称、访问级别、描述均使用 `overflow: hidden` + `text-overflow: ellipsis` + `white-space: nowrap`。
- `index.css:564-570`：Workbench 标题同样截断；`index.css:1332-1360`：AI 附件名称/错误消息截断；`index.css:1496-1510`、`1588-1600`：Worker/执行摘要截断；`index.css:2028-2053`：Package Source 单元格和状态截断。
- `index.css:1644-1715`：Output/Terminal 使用内部滚动和 `word-break`；需要在长日志、最大化和跟随状态下继续验证，而不是用全局 `overflow` 补丁掩盖问题。
- 主题色、边框、间距和按钮尺寸同时存在 `index.css` 自有变量、组件级颜色值和 antd 默认 token。Wave A 只把现有背景色/圆角收进 `ConfigProvider`，不建立第二套 Token 或大范围删除 CSS。

## 工具栏拥挤与非标准控制审计

- 顶部 `.app-header-status`（`index.css:71-76`）是单行 flex；用户身份、健康状态、用户管理、系统设置、Worker 状态同时出现时需在长文案下验证。
- Workbench header controls（`index.css:573-580`）可换行，但其动作集合、权限禁用原因、运行状态仍需统一层级和可访问名称。
- Editor toolbar（`index.css:739-745`、`App.tsx:1233` 起）把主题 Segmented、Diff、Context 操作放在同一工具条；应在后续 Wave 验证窄宽度和长英文。
- Catalog 搜索使用 `Space.Compact` 将两个固定宽度 Select 与搜索框放在一行；这是 Wave C 的直接收敛对象。
- 生产代码中显式原生 `<button>` 主要出现在 `AdapterCatalog.tsx:342`（Catalog 行选择）和 `ai-markdown.tsx:71`（代码复制）。两者目前都有可访问行为/名称约束，Wave A 不擅自替换；后续 Wave B/D 决定是否采用 antd 官方图标按钮/Tooltip，并保留领域语义。
- 其余主要动作已使用 antd `Button`，但操作文案、图标化、Tooltip 和 destructive/disabled 层级仍需 Wave B/D 按全站状态审计，不能在 A 只修两个例子。

## Wave 映射与未处理项

| 现状类别 | 进入的后续 Wave | Wave A 明确不做 |
| --- | --- | --- |
| Shell、顶部栏、页面留白、通用按钮/反馈/Tooltip/键盘焦点 | Wave B | 不迁移 Umi，不替换路由/i18n/API |
| Catalog 搜索、筛选、列表、设置/账号表单和抽屉 | Wave C | 不为固定宽度添加临时像素补丁，不改 #90 权限合同 |
| Monaco、Workbench、日志、AI Assistant、Markdown、Candidate/Diff | Wave D | 不重写 assistant-ui，不引入 Ant Design X，不改变业务状态 |
| 全部可达面、长文本、空/加载/错误/禁用/权限状态、全尺寸截图与人工验收 | Wave E | A 的 16 条基线不是全站最终验收，不把基线通过描述成 Wave E 完成 |

本轮没有因审计发现而修改页面 CSS、按钮、布局或业务路径；这些项保留在此清单中，等待对应 Wave 的明确授权。
