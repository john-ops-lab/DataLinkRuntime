# M5.10 Wave A：Playwright 真实浏览器基线

## 固定环境与运行方式

基线使用项目锁定依赖和 Chromium，避免依赖开发者本地浏览器状态：

| 项 | 固定值 |
| --- | --- |
| Runner | `@playwright/test 1.62.1` |
| Browser | Playwright Chromium（`npx playwright install chromium`） |
| antd | `5.29.3` |
| ProComponents | `2.8.10` |
| React | `19.2.8` |
| Color scheme | `light` |
| Locale | `zh-CN`, `en` |
| Viewport widths | `1280`, `1440`, `1680`, `1920`；height `900` |
| Worker policy | `1`，retries `0`；默认由 Vite dev server 提供 `4173` |
| API | Playwright route fixture；不写入真实账号、Token 或后端数据 |

首次准备和重复运行：

```sh
cd web
npm ci
npx playwright install chromium
npm run test:browser
```

也可以用 `DLR_BASELINE_OUTPUT_DIR` 指向另一个复核目录；默认输出到本目录。测试入口为 `web/tests/e2e/m5-10-wave-a-baseline.spec.ts`，配置为 `web/playwright.config.ts`。

默认基线通过 `npm run dev` 启动 Vite dev server，截图和 console 记录反映
React dev build，不是 production bundle。若设置 `DLR_BROWSER_BASE_URL`，Playwright
使用该外部/预览地址并不会启动本地 `4173` webServer。相对的
`DLR_BASELINE_OUTPUT_DIR` 基于测试文件目录解析，而不是基于当前工作目录解析，
因此从 `web` 目录或仓库根目录运行都写入同一仓库内基线目录。

## 覆盖与记录

测试执行 8 个 locale × width 组合，每个组合依次覆盖：

1. Token 登录 → Adapter Catalog →健康状态→管理员 Task Workbench；
2. Account 登录 →账号 principal→ Adapter Catalog →只读权限 Task Workbench。

每个场景均验证：

- 登录标题、Catalog、健康状态、Workbench header、Monaco 编辑区的关键可见性；
- 管理员运行操作或只读权限状态；
- 未处理请求；
- `document.documentElement.scrollWidth`、`document.body.scrollWidth` 不超过 viewport；
- page error 和 console error；
- 全页截图。

`baseline/baseline-report.json` 当前应包含 16 条记录和 16 张 PNG，截图命名为 `<locale>-<width>-<scenario>.png`，可由报告中的相对路径追踪。报告还写入实际 Chromium 版本、Node 版本、平台和 viewport height，便于判断两次基线是否来自同一执行环境。

## Error 处理说明

Account 场景在未登录初始化时会按现有会话合同请求 `/api/auth/account/me`，fixture 有意返回两次 `401 account_session_required`，浏览器会将这两个预期的未认证挑战记录为原始 console error。报告同时记录：

- `console_errors` / `http_errors`：原始浏览器证据；
- `expected_console_errors`：与 `/api/auth/account/me` 的 `401` 一一对应的预期项；
- `unexpected_console_errors` / `unexpected_http_errors` / `page_errors`：基线门禁项，当前均为空。

因此测试没有静默吞掉浏览器错误；任何不是已知会话挑战的错误都会使测试失败并进入 `unexpected_*`。

## 复核边界

该基线证明 Wave A 固定场景在四个宽度和两种语言下可重复加载、交互、截图且无意外错误/溢出。它不替代 Wave B-D 的页面重构，也不声称已完成 Wave E 的全状态人工视觉验收。所有 API、身份、Session、Owner、ACL 行为仍由现有实现和后续针对性测试负责；本轮仅通过受控 fixture 检查入口和权限呈现没有回退。
