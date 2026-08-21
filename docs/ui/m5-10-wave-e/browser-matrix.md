# M5.10 Wave E 浏览器矩阵

## 固定环境

| 项目 | 值 |
| --- | --- |
| Base | `b26bb39497550f721bc70cae5ead9b6cef9f678` |
| Browser | Playwright Chromium 1.62.1 |
| Locale | `zh-CN`, `en` |
| Viewport | 1280、1440、1680、1920 × 900 |
| UI | React 19 + Vite + antd 5.29.3 + ProComponents 2.8.10 |
| Fixtures | 确定性 route fixture、fake AI provider、内存数据 |
| Real credentials | 否 |

## 用例矩阵

| Suite | 用例数 | 每个用例覆盖 |
| --- | ---: | --- |
| `m5-10-wave-a-baseline.spec.ts` | 8 | 登录、locale、宽度基线、基础响应式和导航 |
| `m5-10-wave-b-shell.spec.ts` | 9 | Shell、目录、搜索/筛选、键盘/可访问名称、管理入口 |
| `m5-10-wave-c-data-forms.spec.ts` | 9 | 表格、分页、批量操作、所有设置表单/抽屉/权限边界 |
| `m5-10-wave-d-workbench-ai.spec.ts` | 12 | Monaco、运行/日志/历史、AI Markdown/code、Candidate/Diff/Apply、附件、tool call、Adapter 隔离 |
| `m5-10-wave-e-audit.spec.ts` | 32 | Wave E 全页面、全状态、全角色和最终宽度/语言矩阵 |

Wave E 最终报告的 32 个用例按以下四组执行，每组均为两种 locale × 四种宽度：

1. 8 个 superadmin 场景：Shell/目录、System Settings、User Management、Task AI、
   Webhook runtime/history、Candidate Diff。
2. 8 个账号角色场景：account-admin、owner、shared-edit、shared-read；验证 Owner、
   edit/read ACL、禁用主操作、AI Apply 和 Adapter state isolation。
3. 8 个状态场景：empty、API error、loading skeleton、force-password 及改密完成回到
   account login；同时检查 disabled workbench。
4. 8 个登录/权限场景：account login 401、直接访问 unshared Adapter 的 404 和明确的
   permission-denied UI。

## 记录与硬性检查

- 240 条记录、240 张截图，见 [browser-report.json](browser-report.json) 和
  [browser/](browser/)。
- 每条记录均记录 persona、locale、width、stage、visible state 和 screenshot。
- 每页收集 console error、page error、未知请求；401/503 只作为 fixture 预期错误
  收集，不计入 bad error。
- 每页断言 `document.scrollWidth <= innerWidth` 与 `body.scrollWidth <= innerWidth`；
  modal/drawer 另行测量并在截图中保留。
- 键盘场景验证目录筛选焦点、主要控件焦点和 Escape restore；按钮/输入使用可访问名称。
- 宽度矩阵中包含长中文/英文用户名、Adapter、worker、URL 和说明文字。

## 可复现命令

```bash
cd web
DLR_WAVE_E_OUTPUT_DIR=../docs/ui/m5-10-wave-e \
  npm run test:browser -- tests/e2e/m5-10-wave-e-audit.spec.ts
```

该命令只能使用本地确定性 fixture，不需要 Control API、数据库、AI provider 或任何
真实凭据。

\n