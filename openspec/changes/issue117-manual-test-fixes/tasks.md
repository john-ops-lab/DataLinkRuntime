## 1. Batch 1：Issue #117.7 PostgreSQL 初始化与健康检查（无前置依赖）

- [ ] 1.1 为平台日志目录不存在、目录对容器内 postgres 用户不可写、存在用户但缺少 `dlr` 数据库的半初始化路径补充隔离 Compose 回归，并验证每个路径在预期位置失败且不把 PostgreSQL 标记为 healthy。
- [ ] 1.2 在首次 initdb 前加入目录存在/可写预检，并将 healthcheck 改为以 `dlr` 用户对 `dlr` 数据库执行等价于 `SELECT 1` 的真实查询；验证 `docker compose config --quiet`、定向 Compose 回归和 `./scripts/compose-smoke.sh` 均通过，且 Control 不因虚假 `service_healthy` 启动。
- [ ] 1.3 完成 Batch 1 验收门：检查服务日志不含凭据、数据库无新增 migration/API 变更，并保存缺失目录、不可写目录、目标库可查询三种结果；本批无浏览器变更，自动化/Compose 证据必须完整后才允许 Batch 2 开始。

## 2. Batch 2：Issue #117.9 Tencent ima 字段归一化（依赖 Batch 1 验收通过）

- [ ] 2.1 为 `kb_id/kb_name`、旧 `id/name`、双字段同时存在及两套候选字段缺失的响应增加 fixture 和回归测试，并验证 malformed 响应稳定返回 `ks_response_invalid` 且不回显原始 payload。
- [ ] 2.2 在 ima adapter 边界实现 `kb_id` 优先 fallback `id`、`kb_name` 优先 fallback `name` 的规范化，保持真实 ID 进入 search 链路；验证 `uv run --frozen --project backend pytest backend/tests/test_ai_knowledge.py -q` 及变更文件的 Ruff/类型检查通过。
- [ ] 2.3 完成 Batch 2 验收门：用 fake/fixture provider 验证新旧列表均可用、鉴权失败映射不变、Secret/原始响应不进入日志；通过系统设置知识库“测试连接”浏览器路径刷新列表，记录页面、console、request 和无溢出证据后才允许 Batch 3 开始。

## 3. Batch 3：Issue #117.5 实时日志冻结与恢复（依赖 Batch 2 验收通过）

- [ ] 3.1 为暂停时后台继续收日志、恢复补齐顺序、重复事件/重连去重、窗口上限/截断及暂停期间终态补齐增加 Web 单元/集成回归，并验证既有 SSE 连接没有被暂停动作关闭。
- [ ] 3.2 复用现有 watcher/SSE 数据和权威快照边界实现客户端冻结、暂停期间有界保留、恢复去重合并与自动跟随；验证不新增公共 offset API、不停止服务端采集，且最终 Execution 日志/状态/脱敏结果不改变。
- [ ] 3.3 完成 Batch 3 验收门：运行 `cd web && npm run lint && npm run typecheck && npm run test && npm run build`；用真实浏览器在 zh-CN/en、1280/1440/1680/1920 验证暂停后内容和位置冻结、继续后不丢不重且恢复跟随，并归档 console、page、request、overflow 和截图证据后才允许 Batch 4 开始。

## 4. Batch 4：Issue #117.3 凭据绑定角色提示（依赖 Batch 3 验收通过）

- [ ] 4.1 增加平台管理员、非管理员 Adapter owner、非 owner/无权用户的前后端测试 fixture，验证准确的平台角色决定提示而非 Adapter ownership；补充 `zh-CN/en` 对应 i18n key、精确中文文案和英文文案 `To add a credential, ask an administrator to go to “System Settings → Credentials” and create one.` 的资源 key parity 断言。
- [ ] 4.2 实现管理员保持原提示和系统设置入口、非管理员按 `zh-CN/en` 资源显示对应提示（英文路径标签固定为 `System Settings → Credentials`）且不显示入口；验证组件不硬编码用户可见文案，已有 Credential 绑定/读取/编辑与全局 admin-only CRUD 的 API 权限测试全部通过。
- [ ] 4.3 完成 Batch 4 验收门：运行变更相关 backend/web lint、typecheck、unit tests；真实浏览器分别用管理员和非管理员会话在 `zh-CN/en` 检查精确中文文案及 `To add a credential, ask an administrator to go to “System Settings → Credentials” and create one.`、资源切换、链接、键盘可达性、console 与 1280/1440/1680/1920 无溢出证据，确认未出现 Secret 后才允许 Batch 5 开始。

## 5. Batch 5：Issue #117.2 编辑器布局与最大化（依赖 Batch 4 验收通过）

- [ ] 5.1 为编辑页首次渲染、两个区域独立展开/折叠、最大化/恢复、最大化期间编辑和 dirty 状态增加 Web 回归测试；记录切换前 selection 起始 line/column、selection 结束 line/column 与顶部可见行五项值，并断言最大化和恢复后这五项值全部分别等于记录值，同时验证切换动作不发 Save/Run/Revision/Credential 生命周期请求。
- [ ] 5.2 使用现有 Ant Design/图标和 Monaco 集成实现 Python 依赖与凭据绑定默认折叠，以及带可访问名称的代码编辑区最大化/恢复；在布局完成回调后 MUST 恢复 selection/cursor 起止 line/column 和顶部可见行，验证代码、Working Copy 和保存语义不变。
- [ ] 5.3 完成 Batch 5 验收门：运行 `cd web && npm run lint && npm run typecheck && npm run test && npm run build`；真实浏览器在 zh-CN/en、1280/1440/1680/1920 操作展开/折叠/最大化/恢复并断言 selection/cursor line/column 与顶部可见行保持不变，检查 console、request、键盘访问和水平/垂直溢出，证据通过后才允许 Batch 6 开始。

## 6. Batch 6：Issue #117.1 AI Assistant 单行说明（依赖 Batch 5 验收通过）

- [ ] 6.1 为 AI Assistant 底部说明增加 zh-CN/en 渲染回归，验证附件数量/大小限制和隐私提醒完整存在、未出现主动换行，并覆盖上传/拖拽/校验逻辑未改变。
- [ ] 6.2 使用现有说明容器和 i18n 资源实现单行布局，不修改附件校验、上传、拖拽和隐私处理；验证变更文件的 Web lint、typecheck、unit tests 与 build 通过。
- [ ] 6.3 完成 Batch 6 验收门：真实浏览器在 zh-CN/en、1280/1440/1680/1920 检查单行内容、附件交互、可访问文本、console、request 和 overflow；确认没有 Secret 或 Provider 原始响应后才允许 Batch 7 开始。

## 7. Batch 7：Issue #117.4 Adapter Catalog 布局（依赖 Batch 6 验收通过）

- [ ] 7.1 增加 Catalog 渲染回归，断言标题、新建、刷新、帮助、搜索和筛选仍存在且 `catalog.overview` 不渲染，验证删除说明后无异常空白。
- [ ] 7.2 移除 Catalog 常驻 overview 展示并收紧相邻布局间距；验证搜索、筛选、刷新、新建和帮助操作的既有请求/状态行为不变。
- [ ] 7.3 完成 Batch 7 验收门：运行 Web lint、typecheck、unit tests、build；真实浏览器在 zh-CN/en、1280/1440/1680/1920 检查标题与操作、列表布局、console、request 和 overflow，证据通过后才允许 Batch 8 开始。

## 8. Batch 8：Issue #117.6 账号资料与用户管理 UI（依赖 Batch 7 验收通过）

- [ ] 8.1 先为账号资料和用户管理页面建立操作/状态回归基线，覆盖修改用户名、修改密码、创建用户、角色调整、启停、重置密码、批量操作、加载/空/成功/错误状态及既有权限拒绝。
- [ ] 8.2 仅复用现有 DLR 工作台容器、Ant Design token、卡片/表单/表格/状态组件和 i18n 资源统一两页的标题、说明、层级、操作区、间距、字号、留白和反馈；验证 API payload、权限判断和敏感值处理不变。
- [ ] 8.3 完成 Batch 8 验收门：运行 `cd web && npm run lint && npm run typecheck && npm run test && npm run build`；真实浏览器在 zh-CN/en、1280/1440/1680/1920 逐项完成账号与用户管理操作，检查键盘/可访问名称、console、request、page state 和 overflow，并保存视觉证据后才允许 Batch 9 开始。

## 9. Batch 9：Issue #117.8 本地日志配置与快速开始文档（依赖 Batch 1 和 Batch 8 验收通过）

- [ ] 9.1 为 `.env.example`、README 快速开始和 `docs/deployment/platform-logs.md` 建立一致性检查，验证本地可写示例、Linux 生产区分、`control/`、`worker/`、`web/`、`account-web/`、`postgres/` 五个子目录、postgres 容器用户写权限和禁止 `chmod 777` 均有明确表述。
- [ ] 9.2 更新本地日志根目录示例和快速开始准备步骤，保持生产绝对路径说明、bind mount 合同、日志轮转/脱敏语义和凭据示例安全；验证 `rg` 交叉核对、Markdown/相对链接检查和 `git diff --check` 通过。
- [ ] 9.3 完成 Batch 9 验收门：按文档在隔离目录准备本地五个子目录并运行 `docker compose config --quiet` 与 `./scripts/compose-smoke.sh`；以浏览器打开 README/部署文档检查可读性和链接无误，不启动业务浏览器流程，文档/Compose 证据通过后才允许最终门禁。

## 10. 串行最终门禁

- [ ] 10.1 重新运行全部相关 backend/web 自动化、静态检查、`./scripts/compose-smoke.sh` 和 OpenSpec 严格校验，验证 9 个 capability 与 tasks/proposal/design 一一对应、没有未勾选实施项或无关业务文件。
- [ ] 10.2 汇总每批自动化、Compose、浏览器视觉/交互、console、request、overflow 和安全边界证据，确认 zh-CN/en 与 1280/1440/1680/1920 覆盖完整，并明确记录任何无法验证的项而不是以 build 代替验收。
- [ ] 10.3 仅在 Batch 1–9 全部通过后复核 API/数据库/权限/Secret 兼容性和 clean worktree，创建本 change 的 Candidate commit；验证 Candidate tree 只包含 OpenSpec artifacts（本 planning worker 不实现业务代码、不 push、不创建 PR）。
