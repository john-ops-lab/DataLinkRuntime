# UX-MANUAL-001～018 闭环矩阵（M5.5.14 最终核验）

问题池来源：#53 评论（用户真实浏览器人工验收形成的 18 项问题池，含跨批次拆分）。

归属说明：Wave 1（#53/#54/#55）→ PR #63/#62/#61；Wave 2（#56/#57）→ PR #64/#65；
Wave 3（#58/#59）→ PR #66/#67。全部已 MERGED，main 包含全部 merge commit。

自动测试证据 = 各批次 PR 引入 + 本轮全量重跑；浏览器证据 = 本轮真实浏览器
（Playwright + 系统 Chrome，见验证记录.md）+ 前批次真实浏览器记录。

| # | 问题 | 归属 Issue / PR | 自动测试证据 | 浏览器证据 | 状态 |
|---|---|---|---|---|---|
| UX-MANUAL-001 | 模型服务域名解析失败，默认增加通用公网 DNS | #54 → PR #62 | compose-smoke DNS 断言（默认 1.1.1.1/8.8.8.8、可覆盖/关闭、`.env.example` 路径保持、127.0.0.11 内嵌解析不回归）；pytest test_ai DNS 错误分类 | M5.5.8 批次浏览器记录；本轮 compose 栈 DNS 配置生效 | PASS |
| UX-MANUAL-002 | “外部模型数据边界”说明过于技术化 | #54 → PR #62 | web AiModelSettingsPanel 测试（AI 使用说明文案） | 本轮 17/18（AI 引导文案用户化，无内部术语） | PASS |
| UX-MANUAL-003 | Diff 应用成功后弹窗自动关闭 | #59 → PR #67 | web App.test（Apply 自动关闭、失败不关闭） | 本轮 20~22（Apply 后 Diff 自动关闭、已应用标记） | PASS |
| UX-MANUAL-004 | Task 运行设置单页动态表单 | #57 → PR #65 | web App.test + vitest（单页动态表单、无重复运行一次）；backend execution_timeout 合同 | 本轮 04/05（手动/定时动态切换） | PASS |
| UX-MANUAL-005 | 运行前保存门禁与运行中提示收敛 | #55 → PR #61 | web App.test（dirty 运行阻止「请先保存当前修改，再运行」）；backend 409 门禁 | M5.5.9 批次浏览器记录；本轮运行锁表现（定时 Radio 运行中禁用） | PASS |
| UX-MANUAL-006 | 实时日志独立 Tab、统一日志格式与自动滚动 | #56 → PR #64 | web LiveLogWorkspace.test（Tab 结构、时间前缀、暂停/恢复滚动）；backend 统一日志 | 本轮 06/23（Task/Webhook 实时日志 Tab + 统一时间前缀） | PASS |
| UX-MANUAL-007 | 单次执行超时（Task 部分 / Webhook 部分跨批次） | Task：#57 → PR #65；Webhook：#58 → PR #66 | backend test_migration_m5_5_11 + execution timeout 测试（默认 300s、最大 24h、真实 kill 进程组标记 timeout、Clone 复制）；compose-smoke timeout 链 | 本轮 04（Task 超时字段）/ 08（Webhook 超时字段） | PASS |
| UX-MANUAL-008 | 主界面隐藏内部 Execution ID | #56 → PR #64 | web LiveLogWorkspace.test + App.test（无「执行 #N」）；backend 保留内部主键 | M5.5.10 批次浏览器记录；本轮实时日志标题仅状态（运行中） | PASS |
| UX-MANUAL-009 | Monaco 深色主题偶发不生效 | #55 → PR #61 | web App.test（深色主题刷新/切换/remount 稳定、跟随系统语义） | M5.5.9 批次浏览器记录 | PASS |
| UX-MANUAL-010 | 适配器目录三点快捷菜单 | #55 → PR #61 | web AdapterCatalog.test（菜单仅设置/复制、不误触行点击、键盘可达） | 本轮 02（三点菜单展开：设置/复制） | PASS |
| UX-MANUAL-011 | AI 悬浮入口拖动 + 加入上下文自动展开 + 多代码/日志片段 | #59 → PR #67（#56 实时日志底座、#53 脱敏合同前置） | web App.test + LiveLogWorkspace.test（拖动不持久化、选区加入自动展开、多片段/删除/清空/防串线）；fake Provider 哨兵 | 本轮 19/24（代码 + 日志选区加入上下文）；拖动行为 M5.5.13 真实验证（拖动 120px 不误触、刷新恢复默认位置） | PASS |
| UX-MANUAL-012 | AI 抬头样式与引导文案简化（含凭据绑定安全引导） | #59 → PR #67 | web App.test（顶部蓝色、文案断言：凭据绑定引导、无「工作副本」术语）；backend Secret 不入 Prompt | 本轮 17/18（新引导文案 + composer 区分说明） | PASS |
| UX-MANUAL-013 | 日志统一展示、触发方式收敛、执行详情去版本化 | #56 → PR #64 | web LiveLogWorkspace.test（stdout/stderr 统一、触发方式三种、无 Revision/version 用户概念）；backend 字段保留审计事实 | 本轮 06/23（统一日志）；M5.5.10 批次执行详情记录 | PASS |
| UX-MANUAL-014 | 依赖说明 / 取消运行参数入口 / 默认凭据示例（三处跨批次拆分） | 依赖说明：#54 → PR #62；取消运行参数入口：#55 → PR #61；默认凭据示例：#53 → PR #63 | backend test_m5_5_7_demo_bindings + test_package_sources（三语言依赖说明、runtime_config 退出 UI、demo 绑定）；web 测试 | 本轮 03（demo-passwd + PASSWORD 绑定引导）/ 16（依赖源说明） | PASS |
| UX-MANUAL-015 | 依赖安装源默认国内可达镜像 | #54 → PR #62 | backend test_m5_5_8（seed 默认值、可修改/恢复默认、清空回退策略）；compose-smoke 默认源断言 | 本轮 16（aliyun PyPI / npmmirror / aliyun Maven 默认值展示） | PASS |
| UX-MANUAL-016 | 目录显示类型 + Adapter 名称唯一性 | #55 → PR #61 | backend test_migration_m5_5_9 + 名称唯一（trim、软删除复用、并发 DB 防线）；web AdapterCatalog.test | 本轮 01（`[任务]`/`[Webhook]` 类型标签） | PASS |
| UX-MANUAL-017 | Webhook 运行设置双状态 + 停止接收 active 三选一 | #58 → PR #66 | backend webhook service 测试（path 唯一、认证、busy、202、cancel 复用）；web WebhookTriggerPanel 测试 | 本轮 08~11（停止状态字段、接收中锁定、三选一弹窗、直接结束真实 cancel） | PASS |
| UX-MANUAL-018 | 凭据管理、默认绑定、Starter Code 与 Secret 安全体验 | #53 → PR #63 | backend test_m5_5_7_demo_bindings + test_credentials（四类说明、access_key_id 迁移、demo bootstrap 随机值、Secret 不可回读）；compose-smoke Secret 不入日志 | 本轮 03（绑定引导）/ 12~15（四类说明 + 一次性明文提醒 + 列表仅元数据） | PASS |

## 汇总

- 18/18 全部 PASS；无 BLOCKED；无不处理项。
- 跨批次拆分项（007 Task/Webhook、011 多上下文、012 安全引导、014 三处）均已
  逐部分归属并核验，无遗漏。
- 未验证项仅剩「用户最终人工复测与 ChatGPT 阶段级确认」（#60 合同要求的交接项，
  等待用户）。
