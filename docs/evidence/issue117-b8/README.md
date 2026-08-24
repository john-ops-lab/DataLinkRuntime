# Issue #117 · Batch 8 账号资料与用户管理 UI evidence

## Scope and boundary

- `DISPATCH_ID`: `issue117-b8-account-20260825-r3`
- `DELIVERY_MODE`: `LOCAL_FAST`
- Base and parent: `b8008db698a5ddaca62e120bc78cedd73b4770d7` (local `main`)
- Branch: `ao/datalinkruntime-93/root`
- OpenSpec scope: tasks `8.1`–`8.3` only. Batch 9+ remains unchecked and was not implemented.
- Contract: 账号资料与用户管理复用 DLR 工作台/settings-panel 基线（标题、说明、分区、操作区、反馈、间距），保持修改用户名、修改密码、创建用户、角色调整、启停、重置密码、批量操作及权限判定的既有 API payload 与结果语义。

## Implementation audit

- `AccountUserPage.tsx`：移除抽屉内的全屏居中单卡片布局（`.account-user-page` 的 `min-height:100vh` 会在 460px 抽屉内制造溢出）和与抽屉标题重复的页内标题；改为 `settings-panel account-user-panel` 基线：说明段落、`settings-panel-error`/`settings-panel-success` 反馈（`role=alert`/`role=status`）、资料表单、`settings-section-title` 的「修改密码」分区（`section[aria-labelledby]`）、底部分隔的退出登录操作区。表单字段、按钮 testid、提交处理函数全部未变。
- `UserManagementDrawer.tsx`：刷新按钮与批量操作合并进同一 `settings-panel-toolbar user-management-toolbar` 操作区（`data-testid="user-management-toolbar"`），创建区、筛选、表格、重置模态框与全部处理函数未变。
- `index.css`：`.account-user-page`/`.account-user-card` 删除，新增 `.account-user-panel`、`.account-user-footer`、`.user-management-toolbar`；`.account-user-password` 保留为分区间隔。无新颜色/字号 token，全部复用既有 `var(--dlr-border)` 等变量与 settings-panel 样式。
- i18n：未新增/修改任何 key；zh-CN/en 资源完全未动。
- API/RBAC/Secret：`api.ts`、后端、权限判断、密码处理零改动；单元与浏览器断言均确认请求 payload 与 `X-CSRF-Token` 合同不变、密码不以文本回显。

## Automated verification

- `npm run test -- --run src/account-management-wave-b8.test.tsx` — PASS，13/13（新增回归基线：改名 PATCH payload、资料冲突错误、密码确认本地校验、只提交 current/new、错误不回显原文、busy 锁定、loading/空状态、403 权限拒绝、创建/角色/启停/重置/批量 payload、本地筛选无新请求、两页结构基线）。
- `npm run lint` — PASS。
- `npm run typecheck` — PASS。
- `npm run test` — PASS，31 files / 357 tests。
- `npm run build` — PASS（仅既有 chunk 体积提示）。
- `uv run --frozen pytest tests/test_account_auth.py tests/test_account_wave_b.py tests/test_account_wave_c.py -q`（backend/）— PASS，25 passed（对隔离的本地 PostgreSQL 16 容器，容器带 `ao.session` 标签并已清理）。
- `npm run test:browser -- tests/e2e/issue117-b8-account.spec.ts` — PASS，10/10 Chromium 用例。
- `openspec validate issue117-manual-test-fixes --type change --strict` — PASS。
- `git diff --check` — PASS。

## Scoped Playwright/Chromium matrix

`auxiliary-matrix/browser-report.json` 含全部 10 条记录与对应 PNG：

- Locales: `zh-CN`, `en`；Viewports: `1280`, `1440`, `1680`, `1920`；Browser: Chromium（Playwright 1.62.1）。
- 每个 admin 用例真实执行：键盘/按钮登录、Catalog 基线对照、账号资料改名（断言 `PATCH /api/users/1` body 恰为 `{"username": ...}`）、密码确认不一致（零请求）、当前密码错误 400 反馈、用户管理创建（断言 POST body）、角色调整（`{"role": ...}`）、禁用（`{"enabled": false}`）、重置密码模态框（可访问名称 + `{"new_password": ...}`）、全选批量禁用（3 笔逐用户 PATCH）、关键字筛选空状态（零新增列表请求）、刷新、修改密码成功终态（返回登录页 + 既有提示）。
- 两个 viewer 用例（zh-CN/en 1280）：非管理员菜单不暴露「用户管理/系统设置」，账号资料仍可用且改名只产生 `PATCH /api/users/2`。
- 全部用例：`document`/`body` `scrollWidth <= innerWidth`，`.account-user-panel`/`.user-management-panel` 无 `scrollWidth > clientWidth`；`console_errors` 与 `page_errors` 为空；网络层 4xx/401 与 antd CSS-in-JS unmount 告警计入 `console_filtered_notices`；`unknown_paths` 为空；页面文本不含任何提交过的密码。
- 报告中的 `payloads`/`structure`/`operations`/`feedback`/`overflow` 字段全部从 fixture 实际捕获的请求体与 DOM/状态采样派生（repair round 1-2），采样前均先经 Playwright 自动重试断言等待目标状态；仅口令类字段值脱敏为 `<redacted>`。viewer 用例受权限门禁不打开用户管理抽屉，其 `user_management_toolbar_unified`、`reset_modal_named`、`user_panel_overflow` 等未执行字段记录为 `null` 而非常量。

## AO Browser session evidence

会话浏览器经 `ao preview`/`ao browser open http://127.0.0.1:4180` 打开，目标是 `web/dist` 构建产物 + 匿名本地 fixture（account 入口模式，进程内内存用户表，无真实凭据）。

- `zh-CN`：键盘输入登录（Enter 提交）、Catalog 基线快照、键盘（focus+Enter）展开账号菜单（四个菜单项可见）、账号资料抽屉结构快照（dialog 可访问名称「我的账号」、说明、单一标题、「修改密码」region、全部控件可访问名称）。证据：`zh-CN-catalog-baseline.snapshot.json`、`zh-CN-user-menu-open.snapshot.json`、`zh-CN-profile-drawer.snapshot.json`、`zh-CN-console.json`（仅 i18next info，无错误级消息）、`zh-CN-errors.json`（空）、`zh-CN-network.json`（脱敏请求元数据）。
- `en`：登录页（含会话过期提示状态）、键盘登录、Catalog 基线、菜单展开、账号资料抽屉结构快照。证据：`en-login.snapshot.json`、`en-catalog-baseline.snapshot.json`、`en-user-menu-open.snapshot.json`、`en-profile-drawer.snapshot.json`、`en-console.json`、`en-errors.json`（空）、`en-network.json`。
- 限制如实记录：本会话 AO Browser 对 antd Dropdown 浮层内 menuitem 的点击/键盘激活不稳定（「Account profile」两次成功激活，「User management」多次尝试未激活）；用户管理抽屉的完整交互、AO Browser 内输入框精确键入（type 出现字符倒序）与全部操作断言由上方 Playwright/Chromium 矩阵承担，该矩阵在同一构建产物语义上 10/10 通过。
- 截图可用性：`ao browser screenshot` 返回 `INTERNAL_ERROR`，无文件产出；2 语言 × 4 宽度的完整截图由 `auxiliary-matrix/browser/` 的 10 张 PNG 承担。

## Privacy and cleanup

- 仅使用匿名 fixture 账号（admin/ordinary/viewer/created-user）与占位口令；无真实 token、Secret、cookie、Provider 凭据或原始响应归档。
- 证据不含本机绝对路径；URL 均为 loopback fixture 地址；请求记录仅元数据。
- 临时进程已全部停止：fixture server（4180）与 Playwright webServer（4173）端口均无监听；本地 PostgreSQL 测试容器 `dlr-b8-test-pg`（带 `ao.session` 标签）已删除。
- 未执行 push、PR、Hosted CI、GitHub Check、Issue 评论、reset、lock 删除或 main 合并；未触碰 `.qoder/settings.json`。
