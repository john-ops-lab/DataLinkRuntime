## Context

本 change 的动机和 9 项外部行为见 `proposal.md`，逐项可验证合同见 `specs/*/spec.md`。当前 DLR 是 React 19 + Vite + Ant Design 5.29.3 的 Web 工作台，Control 通过现有 SSE/watcher 提供实时日志，PostgreSQL 是 Compose 的权威状态服务，ima 集成在 adapter 边界完成响应校验与知识库工具映射。

设计必须遵守当前 product/architecture 与历史 Specs 的稳定边界：保存仍显式产生不可变 Revision，AI Apply 只改浏览器 Working Copy，Credential 真值不进入浏览器/普通日志/Prompt，Control 不执行 Adapter 代码，实时日志的最终结果仍由服务端权威结果覆盖。除本 change 明确的展示、启动门禁和边界归一化外，不新增生命周期、RBAC、队列、日志服务、数据库表或公共 API。

## Goals / Non-Goals

**Goals:**

- 用现有 DLR/Ant Design 组件和 i18n 资源完成 6 项 UI 行为调整，并以真实浏览器检查证明布局、交互、响应式和溢出结果。
- 在不停止日志采集或断开 SSE 的情况下实现前端可见日志快照冻结、恢复补齐、去重和窗口上限兼容。
- 在 PostgreSQL initdb 前阻断不可写日志目录，在 healthcheck 中验证目标 `dlr` 数据库实际可查询，并用隔离 Compose 回归覆盖半初始化故障链。
- 在 ima adapter 边界保留严格 schema 校验，同时以 `kb_id/kb_name` 优先、`id/name` fallback 兼容两套上游字段。
- 以 9 个严格串行批次交付，每批独立可验证、可回滚，后批只依赖前批已通过的合同。

**Non-Goals:**

- 不改变已有 API 路径、Credential CRUD/共享权限、Adapter 保存/运行锁、Execution 状态、SSE 认证、日志脱敏或 ima 搜索鉴权语义。
- 不建设新的客户端状态框架、日志中间件、消息队列、offset 重传协议、数据库迁移或新的运行时依赖。
- 不把 UI 美化扩展为全站设计系统重构；账号页面只收敛本 Issue 指定页面和可验证的交互状态。
- 不在本 change 实现 AI 自动保存/运行、全局 Credential 非管理员管理、知识库写操作或真实凭据回归。

## Decisions

### 1. 用 capability 分域而不是一份混合规格

9 个 capability 分别覆盖 AI 助手、编辑器、凭据提示、Catalog、实时日志、账号页面、PostgreSQL、平台日志文档和 ima。这样每个行为能由对应的 Web/backend/Compose 回归独立证明，避免 UI 细节掩盖第 7/9 项高优先级可靠性问题。替代方案是单一“大 UI 与部署修复”规格，但会让权限、日志和启动契约无法分别验收，因此不采用。

### 2. UI 只复用现有工作台、Ant Design 和 i18n

单行说明使用现有说明容器的非主动换行布局；折叠使用现有页面的折叠/面板模式；最大化使用现有图标按钮模式并保留编辑器实例状态；账号页面复用主工作台的页面容器、卡片、表格、表单、状态标签和间距 token，并以同视口 Adapter Catalog/Workbench 的区域位置和 `scrollWidth <= clientWidth` 作为对照基线。所有新增用户可见文案及凭据角色分支进入既有 `zh-CN/en` 资源，资源 key 集保持一致，非管理员文案按 specs 固定；不新增第二套 UI 框架或状态管理。这样可保持 React 19、Ant Design 5.29.3、ProComponents 2.8.10 边界。

### 3. 日志暂停是客户端显示层的冻结，不是传输层暂停

现有 watcher/SSE 继续接收后台日志；客户端在暂停时冻结已渲染快照，并记录暂停边界。恢复时优先利用现有事件顺序/游标或权威快照边界合并缺口，使用稳定边界避免重复；合并后继续现有自动跟随。不得停止服务端采集、关闭连接或引入新的公共 offset API。合并逻辑必须把截断/窗口上限当作既有事实，不把前端窗口误称为完整历史。

### 4. PostgreSQL 采用“入口前置检查 + 目标库查询”双门禁

在不改 PostgreSQL 数据模型的前提下，把目录存在/可写检查放在首次 initdb 之前，复用容器内已有 shell 与 postgres 用户权限；healthcheck 使用现有客户端工具对 `dlr` 数据库执行只读 `SELECT 1` 等价查询。`pg_isready` 可作为连接辅助信息但不能单独决定 healthy。Compose 继续依赖 PostgreSQL 的真实 `service_healthy`，不把 Control 的 `/api/health` 作为替代探活。替代方案是只增强 healthcheck，但无法阻止首次 initdb 形成半初始化，因此不采用。

### 5. ima 采用边界归一化而不是上游字段硬替换

在 ima adapter 的响应边界先验证成功 envelope 和列表项，再按 `kb_id`→`id`、`kb_name`→`name` 的优先级映射到统一内部项；任一规范化字段为空或缺失即稳定返回 `ks_response_invalid`。不把兼容逻辑下沉到 Web，也不删除旧字段支持。凭据仍只在请求所需时解密，原始响应只在内存中处理，不写日志或响应。

### 6. 文档与运行配置以同一目录合同为源

平台日志文档统一描述相对本地根目录、Linux 生产绝对路径、五个子目录和 postgres 容器用户写权限；`.env.example` 只提供匿名路径示例，README 负责首次准备步骤，部署文档负责完整运行说明。三处内容在自动化文档检查中交叉核对，避免只改 README 造成运行时仍失败。`chmod 777` 不作为修复路径。

## Risks / Trade-offs

- [风险] 客户端暂停期间日志仍在后台增长，缓冲量可能接近现有前端窗口上限。→ [缓解] 复用既有上限/截断标记，以边界合并而非无界缓存；恢复和终态场景必须做长日志回归。
- [风险] 旧 SSE 事件缺少显式唯一序列时，重连去重容易误判。→ [缓解] 先确认现有事件/快照边界；使用已有顺序或单调长度边界，无法证明时以权威快照替换缺口，不改变公共 API，并增加重复事件回归。
- [风险] `postgres` 容器启动用户、bind mount owner 与本地主机用户不同。→ [缓解] 预检必须在容器内以 postgres 有效身份执行；文档明确不要用宽泛放权，Compose smoke 同时覆盖不存在与不可写目录。
- [风险] 过度压缩 AI 说明或账号页面可能造成文案/操作不可见。→ [缓解] 保留完整限制与隐私文案；以 zh-CN/en、目标桌面宽度、键盘/可访问名称、页面溢出和实际操作浏览器证据验收。
- [风险] 上游 ima 响应同时提供新旧字段但值不一致。→ [缓解] 明确新字段优先并对规范化结果做非空校验；fixture 覆盖双字段和 malformed 情形，搜索回归只使用规范化真实 ID。
- [风险] 账号页面重排引入权限或请求回归。→ [缓解] UI 批次只改展示层，保留现有 API/授权测试；逐项验证创建、角色、启停、重置和批量操作，不使用截图作为唯一验收。

## Migration Plan

1. 按 `tasks.md` 的 Batch 1 至 Batch 9 串行实施；每一批必须先完成依赖批次的自动化与人工验收，失败则停在当前批次，不进入后批。
2. Batch 1 预检/healthcheck 与 Batch 2 ima 归一化先以新增回归验证现有行为，再接入最小实现；两者不需要数据库 migration 或公共 API 版本切换。
3. Web 批次按现有 i18n、组件和测试结构逐批落地；每批保存 zh-CN/en、1280/1440/1680/1920 浏览器截图/交互/控制台/溢出证据，并运行对应 lint、typecheck、unit/browser tests。
4. 平台日志文档批次在 Compose 行为稳定后同步 `.env.example`、README 与 `docs/deployment/platform-logs.md`，再运行链接/文档检查和 Compose smoke。
5. 回滚时按批次撤回当前批次文件/配置并重新运行该批次的失败回归；不执行数据迁移回滚。若发现必须变更数据库 schema、权限模型、公共 API 或 Secret 处理，立即停止并重新评审 proposal/specs。

## Open Questions

无。日志去重策略、Compose 前置检查位置和 ima 字段优先级均已在本设计与 specs 中固定；剩余仅是实现时对现有代码结构的局部适配，不应改变外部合同。
