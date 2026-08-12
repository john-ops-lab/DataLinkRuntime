# M3.1：DLR Console 视觉收敛设计

> 状态：Approved for implementation
>
> 本阶段是 M3 之后、M4 AI Editor 之前的纯前端视觉与布局收敛。目标不是新增业务功能，而是把现有 Web 从“功能完整的后台页面”收敛为稳定的企业级 Developer Workbench，并为 M4 右侧 AI Assistant 预留长期可演进的布局基础。

## 1. 目标

M3 已完成 Adapter 编辑、测试运行、实时日志、Execution 历史等完整浏览器闭环，但现有实现仍明显偏“Ant Design 表单/卡片堆叠”，与 `docs/ui/m3/` 的视觉基线存在较大差距。

M3.1 要解决的问题：

1. 建立稳定的 DLR Console App Shell，不再让后续 M4/M5 每增加一个功能就推翻页面布局。
2. 让 Adapter 成为左侧 Catalog，让代码编辑、测试运行、执行记录成为中间 Developer Workbench。
3. 降低不必要的 Card 感，提升信息密度、层级和桌面端空间利用率。
4. 让 Monaco、Execution、Output、stdout/stderr 成为页面视觉主角，而不是被大量表单控件挤压。
5. 对照现有 4 张 UI 参考图完成真正的视觉收敛，而不仅是“使用了 Ant Design”。
6. 为 M4 AI Assistant 预留右侧可折叠 Context Panel 的布局能力，但本阶段不实现 AI 功能。

最终产品定位：

> **DLR 是数据适配开发工作台（Developer Workbench），不是普通 CRUD 管理后台。**

## 2. 权威来源与优先级

实施前必须完整阅读：

- `docs/product.md`
- `docs/architecture.md`
- `docs/specs/m1-adapter-management.md`
- `docs/specs/m2-execution-loop.md`
- `docs/specs/m3-observability-ux.md`
- `.qoder/rules/engineering.md`
- `docs/ui/m3/01-登录页.png`
- `docs/ui/m3/02-Adapter编辑页.png`
- `docs/ui/m3/03-测试运行页.png`
- `docs/ui/m3/04-执行记录页.png`

发生冲突时：

1. **业务功能、API、状态和交互合同**：以 M1/M2/M3 已实现合同为准。
2. **布局、视觉层级、信息密度和设计语言**：以本 M3.1 文档 + `docs/ui/m3/` 为准。
3. 参考图中出现但 DLR 实际不存在的菜单、用户体系、资源模块、工作流、连接器等内容一律忽略，禁止为了“还原图片”新增虚构功能。

本阶段不要求逐像素复制图片，但要求：

> 登录页、Adapter 工作台、测试运行、执行记录四个核心页面在第一眼上应明显属于与参考图一致的现代企业 Console 设计语言，而不是仅仅“配色相似”。

## 3. 硬边界

### 3.1 本阶段允许

- React 组件拆分/重组。
- Ant Design 5 现有组件重新组合。
- CSS / Design Token / Layout 调整。
- 纯展示型局部状态，例如 Drawer 开关、Tab、折叠区、搜索过滤。
- 将低频设置从主工作区移动到 Drawer/Modal。
- 为未来 AI Panel 预留 DOM/CSS 布局插槽，但不出现假的 AI 功能按钮或聊天内容。
- 对现有测试做与 DOM 结构变化对应的最小更新，并增加关键视觉结构/交互回归测试。

### 3.2 本阶段禁止

- 修改后端 API、FastAPI、Worker、Runtime Contract。
- 修改数据库表或新增 Alembic migration。
- 修改 Adapter / Version / Execution 的业务语义。
- 修改认证模型、Token 存储方式。
- 新增 AI Editor、AI API、AI Provider。
- 新增 Schedule/Webhook、文件上传、RAG、JS/Java Runtime。
- 引入 Redux/MobX、Router、大型状态框架、微前端、Design System 框架。
- 自研一套组件库。
- 为还原参考图而添加不存在的业务菜单或虚构数据。

如果实施过程中认为必须改变后端/API/数据模型或核心业务行为，立即停止并用中文说明原因，等待确认。

## 4. 总体设计语言

### 4.1 视觉方向

- 中文优先的现代企业级 Console。
- 浅灰应用 Shell + 白色核心工作区。
- 高信息密度、克制、稳定、专业。
- 以边框、层级、留白和 Typography 区分区域，少用大面积 Card 和阴影。
- 圆角克制，避免消费级 SaaS Landing Page 风格。
- 状态颜色一致：成功绿、运行蓝、警告橙、失败红、中性灰。
- 代码、JSON、日志使用等宽字体；业务标题/标签使用系统中文字体。
- Ant Design 大量使用 `size="small"`，但不能牺牲可读性。

### 4.2 Design Token 原则

优先通过 `ConfigProvider` Theme Token + 少量 CSS 变量统一，不在组件中散落大量硬编码颜色。

建议基线：

```text
页面背景：#f5f6f8 一类浅灰
工作区背景：#ffffff
弱边框：#e5e7eb 一类浅灰
主色：Ant Design 蓝色体系
圆角：4–6px 为主
阴影：默认不用，只有 Drawer/浮层等必要位置使用
```

具体色值允许在实现时根据 Ant Design Token 微调；关键是全局一致，而不是每个组件独立配色。

## 5. App Shell

### 5.1 长期布局

M3.1 的桌面布局必须建立为：

```text
┌───────────────────────────────────────────────────────────────────┐
│ Top Header                                                        │
├──────────────┬──────────────────────────────────────┬─────────────┤
│ Adapter      │ Developer Workbench                  │ Future      │
│ Catalog      │                                      │ Context/AI  │
│              │                                      │ Panel slot  │
└──────────────┴──────────────────────────────────────┴─────────────┘
```

M3.1 实际只展示左 + 中；右侧 Panel 默认不存在/收起，但中间布局不能写死成未来无法容纳 360–420px 右侧 Panel 的结构。

### 5.2 顶栏

顶栏从当前“独立白色 Card”改为真正 App Header：

- 左侧：`DLR` / `DataLinkRuntime` 产品身份。
- 右侧：Control 健康状态、Worker 状态入口、Token/退出相关已有能力。
- 不虚构用户头像、租户、超级管理员、RBAC。
- 顶栏高度固定、边框克制，与下方工作区形成清晰 Shell。
- Health 文案继续保持中文。

## 6. 登录页

参考：`docs/ui/m3/01-登录页.png`。

要求：

- 页面应有明确品牌区 + 登录/Token 卡片，不是只有一个孤立输入框。
- 产品标题：DataLinkRuntime / DLR。
- 产品说明：`轻量数据适配运行平台`。
- Token 输入仍保持现有 `sessionStorage` 行为，不改变认证合同。
- 背景保持克制，不使用夸张插画或大渐变；允许非常轻的品牌背景层次。
- 登录错误、加载状态与主 Console 视觉语言一致。

## 7. Adapter Catalog（左侧）

### 7.1 固定导航区

左侧从“Card 中的 List”调整为真正的 Catalog Sidebar：

- 建议宽度 240–280px。
- 顶部标题 `Adapters`。
- 搜索框紧凑。
- `新建 Adapter` 操作明显但不抢主工作区视觉。
- Adapter 列表使用行式导航，不为每条 Adapter 创建 Card。
- selected / hover 状态清晰、克制。

### 7.2 列表信息

每条 Adapter 至少显示：

```text
api-sync
v5 · Published v3
```

根据现有数据真实展示 latest / published 信息；不存在的值显示中性状态，不伪造。

Adapter 名称为第一层，版本/发布信息为第二层弱文本。

### 7.3 新建 Adapter

当前内嵌创建表单可以改为 Drawer/Modal 或更紧凑的局部表单，以不长期占用 Sidebar 空间为目标。

不得改变 Adapter 创建 API 和原有校验行为。

## 8. Adapter Workbench Header

选中 Adapter 后，中间顶部建立稳定的上下文 Header：

左侧：

- Adapter 名称（主标题）。
- 描述（弱文本）。

右侧/次级区域：

- `vN Latest`。
- `vN Published`。
- 保存新版本、发布、更多操作。

版本选择不再突出原生 HTML `<select>` 的工程感；优先使用 Ant Design Select/Dropdown，并明确标记 Latest/Published。

低频 Adapter 名称/描述编辑、删除等操作不要长期占据主工作区。建议放入 `Adapter 设置` Drawer/Modal 或 `更多` 菜单；但必须保留原有编辑/删除能力和确认逻辑。

Working Copy dirty 状态在 Header 附近保持一眼可见。

## 9. 主工作区 Tabs

稳定保留三个核心区域：

```text
编辑 | 测试运行 | 执行记录
```

要求：

- Tabs 是 Workbench 主导航，不再像 Card 内普通表单 Tabs。
- Tab Header 与 Workbench Header 分层明确。
- 切换后中间内容尽可能使用可用高度，减少不必要的外层 padding。
- 不改变现有懒加载、dirty、防串线、SSE 等业务行为。

## 10. 编辑页：Monaco 为主角

参考：`docs/ui/m3/02-Adapter编辑页.png`。

### 10.1 页面层级

编辑页核心结构：

```text
Workbench Header
Tabs
────────────────────────
Monaco Editor（主区域）
────────────────────────
Requirements | Runtime Config | Secrets/说明（按现有能力）
```

要求：

- Monaco 占据明显最大视觉面积。
- 代码区高度应随桌面窗口自适应，不能被名称/描述/版本等表单挤压成小块。
- 编辑器边框、Toolbar 与整体 Console 风格统一。
- Requirements / Runtime Config 作为次级配置区，可使用底部 Tabs、折叠面板或紧凑分栏；不需要两个巨大 textarea Card 并排占据主体。
- 继续保留 Save/Publish、历史 Version 切换、dirty Working Copy 等全部 M1 行为。

### 10.2 为 M4 预留

编辑器区域未来必须能够从：

```text
Adapter Catalog + Editor
```

自然演进成：

```text
Adapter Catalog + Editor + AI Assistant(360–420px)
```

本阶段不实现 AI Panel，但避免用难以拆分的固定宽度/绝对定位布局锁死中间区域。

## 11. 测试运行：Input + Execution 双栏

参考：`docs/ui/m3/03-测试运行页.png`。

桌面端优先使用约 40/60 或 45/55 双栏：

```text
┌────────────────────┬────────────────────────────────┐
│ Test Input         │ Execution                      │
│ Version            │ status / id / worker / duration│
│ JSON Editor        │ Output | stdout | stderr       │
│ Run                │                                │
└────────────────────┴────────────────────────────────┘
```

要求：

- 左侧是 Input 与运行入口；右侧是当前 Execution。
- 当前测试 Version 信息明显，继续显式提交 `version_id`。
- dirty Working Copy 继续禁用运行。
- Input 继续支持任意合法 JSON，包括 `null`、数组、标量。
- Execution 概览突出状态、ID、Version、Worker、耗时；不要依赖大量 Description 小格子堆叠。
- Output / stdout / stderr 使用较大的内容区域。
- SSE、fallback、generation 防串线逻辑不得因视觉重构被破坏。

窄屏时允许上下堆叠，但本阶段主要验收桌面端。

## 12. Output 与日志

### 12.1 Output

- 正常 JSON 使用清晰的格式化代码区。
- 对象、数组、标量均可正确展示。
- `output_truncated=true` 时继续明确显示 `output_size + output_preview`，绝不能将 preview 伪装成完整 JSON。
- Output 区域应更像开发工具结果面板，而不是普通 `<pre>` 填在 Card 中。

### 12.2 stdout / stderr

- 保持 terminal 风格。
- stdout / stderr 分开，不虚构跨流顺序。
- 不虚构时间戳（当前 Runtime 没有逐行时间戳合同）。
- 截断状态必须明显。
- 可以加入纯前端的 `复制` 等体验操作，但不要为视觉目标引入复杂日志功能。

## 13. Execution History：Sentry 式列表 + Detail

参考：`docs/ui/m3/04-执行记录页.png`。

主表格建议列：

```text
状态 | Execution | Version | Worker | Trigger | 耗时 | 创建时间
```

要求：

- 高密度小尺寸表格，状态 Badge 一眼可辨。
- newest-first 和 `before_id` 游标分页保持不变。
- 行点击打开右侧 Drawer。
- Detail Drawer 顶部先展示状态、Execution ID、Version、Worker、Duration、Error；再通过 Tabs 展示：

```text
概览 / Input / Output / stdout / stderr
```

如果“概览”没有足够内容，可保持现有结构，不要为了 Tab 数量制造空页面。

继续复用现有 `GET /api/executions/{id}`，不得新增详情 API。

## 14. Worker 状态

- 顶部只展示轻量状态入口，不让 Worker 列表抢占主工作区。
- 弹层继续展示记录状态 + 最近心跳。
- 必须保留“状态为最近上报值，不代表强实时在线判断”的已有提示。
- 不新增 Worker 认证/管理功能。

## 15. 响应式与桌面验收尺寸

M3.1 重点是桌面 Console。

必须人工检查至少：

- 1440px 宽。
- 1680px 宽。
- 1920px 宽。

基本要求：

- 无横向页面溢出。
- Sidebar 不挤压 Monaco 到不可用。
- Test Run 双栏在常见桌面宽度稳定。
- Drawer/Popover 不遮挡关键操作到不可用。
- 未来增加 360–420px AI Panel 时，中间 Workbench 仍具备可缩放空间。

移动端不是 M3.1 验收重点，不要为移动端引入复杂响应式框架。

## 16. 组件与工程约束

- 继续使用现有 React 19 + TypeScript + Vite + Ant Design 5 + Monaco。
- 优先复用现有依赖；本阶段原则上不新增 UI 框架依赖。
- 可以按职责拆分现有大组件，例如：

```text
AppShell
AdapterCatalog
AdapterWorkbenchHeader
EditorWorkspace
TestRunWorkspace
ExecutionHistoryPanel
ExecutionDetailDrawer
WorkerStatus
```

名称以实现时最简合理方案为准，不要求为了目录漂亮而过度拆分。

- 不引入全局状态框架。
- 保留现有业务层 `api.ts` / SSE 合同，不把网络逻辑散落到纯展示组件。

## 17. 测试与验证

### 17.1 Web 自动化回归

现有 M1/M2/M3 Web 测试必须全部继续通过，尤其：

- Adapter 创建/选择。
- dirty Working Copy。
- 保存新 Version。
- Publish。
- 历史 Version 切换。
- 测试运行显式 version_id。
- dirty 禁运行。
- JSON 校验。
- SSE Authorization Header。
- SSE running/log/terminal。
- fallback 恢复。
- Output truncated。
- Execution 历史分页/详情。
- 异步 generation 防串线。

新增最少量结构测试，证明：

- 登录页品牌区与 Token 卡片存在。
- Adapter Catalog / Workbench Header / 三个主 Tab 存在。
- Adapter 设置移动后原功能仍可达。
- Test Run 桌面双栏结构存在（不要做脆弱像素断言）。
- Execution Detail 仍可访问 Input/Output/stdout/stderr。

### 17.2 完整质量门禁

必须运行：

```text
web lint
web typecheck
web test
web build
backend 现有完整测试（确认纯前端改造无仓库级回归）
compose-smoke
GitHub Actions
```

禁止因为“只是 UI”跳过现有质量门禁。

## 18. 视觉验收

实现完成后必须对照 `docs/ui/m3/` 做四组人工视觉检查：

1. 登录页。
2. Adapter 编辑页。
3. 测试运行页。
4. 执行记录页。

PR 中必须明确说明每页与参考图的主要对应关系和保留差异；差异必须来自真实 DLR 功能，而不是“没时间实现视觉”。

建议 PR 附上真实页面截图。若当前工具无法直接把截图作为 PR 附件，则在最终汇报中明确给出截图生成位置/方式，不得用自动化测试通过替代视觉验收。

最终视觉判据：

- 不再明显是“Ant Design Card + Form + Tabs 堆出来的后台页面”。
- Adapter Catalog、Workbench Header、Monaco、Test Runner、Execution History 形成清晰产品结构。
- 与 `docs/ui/m3/` 在布局比例、层级、密度、边框/背景、状态表达上明显同源。
- 不出现参考图中的虚构业务模块。
- M4 增加右侧 AI Assistant 时不需要重做整个 App Shell。

## 19. Demo 验收

视觉重构后，以下原有闭环必须完整可用：

```text
登录
→ 创建 Adapter
→ 编辑 Python
→ Save v1
→ 切换 Version
→ 测试运行
→ 输入 JSON
→ 看到 pending/running
→ 实时 stdout/stderr
→ succeeded / failed / timeout
→ 查看 Output
→ 执行记录
→ 打开 Execution Detail
```

同时完成视觉验收：

```text
登录页 ≈ 视觉参考
Adapter 编辑页 ≈ 视觉参考
测试运行页 ≈ 视觉参考
执行记录页 ≈ 视觉参考
```

这里的 `≈` 指设计语言与布局明显收敛，不是逐像素复制。

## 20. 完成要求

1. 从最新 `origin/main` 创建 `feat/m3-1-console-design-convergence`。
2. 只做本 M3.1 范围内前端视觉/布局与必要组件整理。
3. 不修改后端/API/数据库/Runtime Contract。
4. 最终检查 diff，禁止混入 M4 或其他业务功能。
5. 运行全部 Web、Backend、Compose Smoke、CI 质量门禁。
6. PR 标题建议：`feat: 完成 M3.1 Console 视觉收敛`。
7. PR 描述、实施总结、验证结果、视觉对照、已知限制全部使用中文。
8. 不自动 merge。
9. 不开始 M4。
10. PR 创建后停止，等待独立 Code Review。
