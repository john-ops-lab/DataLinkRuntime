# Issue #117 Batch 10 最终门禁

## 1. 结论与边界

- `DISPATCH_ID`: `issue117-final-gate-20260825-r5`
- `DELIVERY_MODE`: `LOCAL_FAST`
- exact base / parent: `ca8810f6fc1e5d5532aba2557fc16b49d18c77f2`
- branch: `ao/datalinkruntime-102/root`
- 本次 final-gate Candidate 只允许改变本文件、`gate.json` 和 OpenSpec `tasks.md`；集成 `main` 已包含 Batch 1–9 的实现。
- Candidate 的最终 SHA/tree 由提交后的 `REVIEW_READY` 回执记录；不在提交内容中自引用 Git commit 元数据。

本次 `MODE=REPAIR` 只补充交付后跟踪元数据；下表 10.1 的 Web/backend/Compose 结果继承自 reviewed Candidate `7212b5a4c27ecdbb3808ef77bb44c4784fef7290`，未声称在本 repair SHA 重跑。repair 实际只运行 OpenSpec strict、JSON/schema/links/task counts、`git diff --check` 与敏感信息/绝对路径扫描。

开始前已确认：工作区无 tracked/untracked 修改；仅执行 `git merge --ff-only main` 对齐，未执行 `reset` 或删除 lock；对齐后 `HEAD=main=merge-base=ca8810f6fc1e5d5532aba2557fc16b49d18c77f2`。native 主 Worker 首个 `turn_context` 已独立复核为 `model=gpt-5.6-luna`、`effort=max`、`collaboration_mode.settings.reasoning_effort=max`；独立低 effort review transcript 未用于该判断。

`.qoder/settings.json` 在本 worktree 不存在；只核对其路径元数据，未读取内容、修改、暂存或提交，未修改 ignore 规则。

## 2. 10.1 门禁命令

| 命令 | 实际结果 |
| --- | --- |
| `openspec validate issue117-manual-test-fixes --type change --strict` | PASS；`Change ... is valid` |
| `openspec validate --all --strict` | PASS；`1 passed, 0 failed` |
| `cd web && npm run lint` | PASS，exit 0 |
| `cd web && npm run typecheck` | PASS，exit 0 |
| `cd web && npm run test` | PASS；31 test files，357 tests |
| `cd web && npm run build` | PASS；6119 modules transformed，Vite build exit 0 |
| `cd backend && uv run --frozen ruff check .` | PASS；`All checks passed!` |
| `cd backend && uv run --frozen mypy` | PASS；84 source files，`Success: no issues found` |
| `cd backend && DATABASE_URL=postgresql+psycopg://dlr:EXAMPLE_POSTGRES_PASSWORD@127.0.0.1:55439/dlr uv run --frozen pytest -q` | PASS；671 passed，8 warnings |
| `./scripts/check-platform-log-docs.sh` | PASS；`platform-log documentation consistency: PASS` |
| `docker compose --env-file /dev/null ... config --quiet`（default 与 DNS example） | PASS |
| `COMPOSE_SMOKE_PROJECT=dlr-i117-r5-smoke COMPOSE_SMOKE_WEB_PORT=8894 COMPOSE_SMOKE_ACCOUNT_WEB_PORT=8895 COMPOSE_SMOKE_TIMEOUT=300 ./scripts/compose-smoke.sh` | PASS；含四种 PostgreSQL 回归路径、全服务 healthy、认证/账号、DNS fallback、fake AI/ima、M5.4.4 chain 与 secret log redaction |
| `git diff --check` | PASS；baseline 与 Candidate staged diff 均无 whitespace error |

Backend 使用独立 `postgres:16-alpine` 容器和独立端口 `55439`；测试完成后容器已移除。`npm ci` 仅作为本地依赖准备，未改 manifest/lockfile；Node 23 的 engine warning、依赖审计的 5 条 advisory 和 build 大 chunk advisory 均原样记录，未执行 `npm audit fix`。

## 3. 9 capability / Batch / 实现 / 证据矩阵

| capability | tasks | 集成实现提交 | 主要实现/测试 | 固定证据 |
| --- | --- | --- | --- | --- |
| `ai-assistant-layout` | 6.1–6.3 | `3d3bbe1`, `eb1ea37`, `657838a` | `web/src/components/AiAssistantPanel.tsx`、`web/src/assistant-wave-b*.test.tsx`、`web/tests/e2e/issue117-b6-ai.spec.ts` | [`issue117-b6`](../issue117-b6/README.md)、[`browser-report`](../issue117-b6/auxiliary-matrix/browser-report.json) |
| `workbench-editor-layout` | 5.1–5.3 | `30eff0b`, `5bedf11` | `web/src/App.tsx`、`web/src/App.test.tsx`、`web/tests/e2e/issue117-b5-editor.spec.ts` | [`issue117-b5`](../issue117-b5/README.md)、[`assertions`](../issue117-b5/auxiliary-matrix/assertions.json) |
| `credential-binding-permission-hints` | 4.1–4.3 | `666084c` | `web/src/components/CredentialBindingsEditor.*`、`backend/tests/test_account_wave_c.py`、双语 settings resources | [`issue117-b4`](../issue117-b4/README.md)、[`matrix`](../issue117-b4/playwright-browser-matrix.json) |
| `adapter-catalog-layout` | 7.1–7.3 | `cb0959e`, `0a3284f`, `b8008db` | `web/src/components/AdapterCatalog.*`、`web/tests/e2e/issue117-b7-catalog.spec.ts` | [`issue117-b7`](../issue117-b7/README.md)、[`browser-report`](../issue117-b7/auxiliary-matrix/browser-report.json) |
| `live-log-follow-freeze` | 3.1–3.3 | `7a3fe37`, `ad56b4c` | `web/src/components/OutputView.tsx`、`web/src/hooks/useExecutionWatcher.*`、`web/tests` | [`issue117-b3`](../issue117-b3/README.md)、[`matrix`](../issue117-b3/playwright-browser-matrix.json) |
| `account-management-ui` | 8.1–8.3 | `a1fce3b`, `77df9c8`, `3070959` | `web/src/components/AccountUserPage.tsx`、`UserManagementDrawer.tsx`、`web/tests/e2e/issue117-b8-account.spec.ts` | [`issue117-b8`](../issue117-b8/README.md)、[`browser-report`](../issue117-b8/auxiliary-matrix/browser-report.json) |
| `postgres-init-health` | 1.1–1.3 | `36361f7`, `8cf1d5c`, `49128bd` | `docker/postgres-entrypoint.sh`、`docker/postgres.Dockerfile`、Compose regression script | [`issue117-b1`](../issue117-b1/compose-postgres-init-health.md) |
| `platform-log-local-development` | 9.1–9.3 | `0deb171`, `fc15836`, `47a847e`, `6139631`, `c8f4809` | `.env.example`、README/docs、`scripts/check-platform-log-docs.sh` | [`issue117-b9/compose`](../issue117-b9/compose.md)、[`doc check`](../issue117-b9/doc-consistency.txt) |
| `ima-knowledge-base-normalization` | 2.1–2.3 | `bec1998`, `7d11355` | `backend/src/dlr/control/ai/ima.py`、`backend/tests/test_ai_knowledge.py` | [`issue117-b2`](../issue117-b2/ima-browser-validation.md)、[`fixture`](../issue117-b2/ima-api-fixture.json) |

`proposal.md` 的 9 个 capability 名称与 `specs/*/spec.md` 的 9 个目录精确一致；`design.md` 按同九个域描述目标、非目标、决定、迁移和风险；`tasks.md` 的 Batch 1–9 共 27 项均已勾选，无未勾选实施项。Batch 10 的 10.1–10.3 已在本 Candidate 中勾选。

## 4. 浏览器 / Compose / 安全证据汇总

- B1：隔离 Compose 覆盖缺目录、不可写目录、目标库缺失和 healthy 四路径；无浏览器变更。
- B2：fake/fixture provider 覆盖新旧字段、双字段与 malformed 响应，并保存页面、console、request、overflow；未使用真实 Tencent ima 凭据或环境。
- B3：Playwright/AO Browser 固定证据覆盖 `zh-CN/en × 1280/1440/1680/1920`，含暂停、恢复、SSE/request、console、page 和 overflow；README 记录了一个无关历史 test asset failure。
- B4：管理员和 owner 固定证据覆盖双语与四个目标宽度；reader 只有 `zh-CN` 的受限证据，未形成 reader 的英文/全宽度矩阵。
- B5：AO Browser 与 Playwright 固定证据覆盖双语与四个目标宽度，含展开/折叠/最大化/恢复、selection 五字段、dirty、request、console、overflow。
- B6：Playwright 固定证据覆盖 `zh-CN/en × 1100/1180/1280/1440/1680/1920`，目标四宽度均包含；AO Browser 记录了截图能力限制。
- B7：Playwright 固定证据覆盖双语与四个目标宽度，含搜索/筛选/刷新/新建/帮助、request、console、overflow；AO Browser 截图不可用及帮助 popover 点击限制已记录。
- B8：管理员固定证据覆盖双语与四个目标宽度；viewer 只有双语 `1280` 证据；AO Browser 菜单激活不稳定，Playwright 保存了主要交互和截图。
- B9：只打开 README/部署文档，验证双语 heading、相对链接、文档一致性、静态页面与 Compose；按任务不启动业务浏览器流程。

因此，主要合同路径的双语及四个目标桌面宽度已有固定自动化/截图矩阵；reader/viewer 人群的额外矩阵缺口如上如实保留。AO Browser/Playwright 自动化和无 overflow 不能替代最终人工视觉验收，人工视觉验收仍为 pending。

### 4.1 交付后非阻塞跟踪

后续跟踪载体已写入 [`tasks.md` 第 11 节](../../../openspec/changes/issue117-manual-test-fixes/tasks.md)，明确不计入 OpenSpec 实施完成度：

| ID | 状态 | 责任方 | 触发条件 | 验收出口 |
| --- | --- | --- | --- | --- |
| F1 | `PENDING_POST_DELIVERY_NON_BLOCKING` | Product/QA + final human reviewer | 集成 Candidate 后、发布或归档前的人工验收窗口 | 完成双语四目标宽度的统一人工视觉记录，并核对 console/page/request/overflow/security 后回填 evidence |
| F2 | `PENDING_POST_DELIVERY_NON_BLOCKING` | Web QA / Batch 4 owner | 下一次 reader 角色人工浏览器回归 | 补齐 reader `en`、`zh-CN` × 1280/1440/1680/1920 及键盘/console/request/overflow 证据 |
| F3 | `PENDING_POST_DELIVERY_NON_BLOCKING` | Web QA / Batch 8 owner | 下一次 viewer 角色人工浏览器回归 | 补齐 viewer `zh-CN/en` × 1280/1440/1680/1920 及主要交互/console/request/overflow 证据 |

F1–F3 均为交付后非阻塞条目；本 repair 不修改产品实现，也不取消 10.1–10.3。

Issue117 B1–B9 evidence 语义扫描结果：221 个文件（106 个文本文件）中，机器绝对路径 marker `0`、credential value pattern `0`、实际 request/response body/header 字段 `0`。B9 `rg-cross-check.txt` 有一处仅用于审计规则的 `request body` 文字，不是 payload；B4 的 `secretValuesCaptured`/`secretExposed` 仅为布尔断言字段，无 Secret 值。未读取或归档真实凭据、raw provider response、request headers/bodies。

## 5. 兼容性与候选差异

- API：历史 Batch 1–9 diff 未新增 Control API path；本次后端变更只在 ima adapter 边界归一化，旧 `id/name` fallback 保留。
- 数据库：无新 migration；PostgreSQL 只增加 init 前目录门禁与目标 `dlr` 数据库真实查询 healthcheck。
- 权限：Credential 提示继续按平台角色；已有 admin-only CRUD、绑定/读取/编辑和账号权限测试通过；未扩大 RBAC。
- Secret：不改变 Secret 解密、浏览器、Prompt、普通日志和 adapter raw response 边界；Compose smoke 的 redaction 检查通过。
- i18n：`adapter.json` 130/130、`ai.json` 176/176、`common.json` 375/375、`settings.json` 190/190，`en` 与 `zh-CN` key parity 通过；Web 基线仍为 React 19、Ant Design 5.29.3、ProComponents 2.8.10。
- Candidate delta 不包含业务代码、测试代码、proposal、design、spec 或 ignore 规则；只包含本文件、`gate.json` 和 `openspec/changes/issue117-manual-test-fixes/tasks.md`。不要将整仓 tree 误报为只含 OpenSpec：整仓 implementation 已由集成 `main` 携带。

## 6. 资源、禁止操作与剩余风险

- 已清理本 Worker 创建且无引用的 `dlr-i117-r5-backend-pg`、smoke project 的 containers/volumes/network/temp directory，以及本次创建的五个 `dlr-i117-r5-smoke-*` image tags；共享的历史 image tags 未删除。
- 未执行 push、PR、Hosted CI/GitHub Checks、Issue 操作/关闭、远端 main 操作或全局 Docker/AO 清理；未 force-push、未 reset、未删除 lock。
- 未验证项/风险：最终人工视觉验收仍 pending；B2 无真实 Tencent ima 环境；B4 reader 与 B8 viewer 的额外宽度/语言矩阵缺失；既有 AO Browser 截图/交互限制以固定证据中的说明为准；Node 23/依赖 advisory、build chunk 与测试 deprecation warnings 未在本门禁中修复。
- 全仓历史中非 Issue117 evidence 的本机路径 marker 属于 pre-existing out-of-scope，本次未修改且不作为 Issue117 Gate blocker；Issue117 evidence 自身的语义扫描为零命中。

相关链接：[`tasks.md`](../../../openspec/changes/issue117-manual-test-fixes/tasks.md)、[`proposal.md`](../../../openspec/changes/issue117-manual-test-fixes/proposal.md)、[`design.md`](../../../openspec/changes/issue117-manual-test-fixes/design.md)、[`gate.json`](./gate.json)。
