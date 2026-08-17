# M3.2 Adapter 生产生命周期与运行配置闭环——关键决策记录

> 状态：已实施  
> 基线：M3.1 已合并到 `main`（`0a6cc62`）  
> 实施分支：`feat/m3-2-adapter-production-lifecycle`（对应 Issue #10）  
> 目标：让 Adapter 从"能编辑、能测试、能看日志"升级为"可选择生产 Worker、可发布、可启动/停止生产、可绑定凭据、可管理依赖源"的完整生产形态。  
> 非范围：Schedule / Webhook 触发、MQ / 长连接 / 全局状态框架、Issue §21 清单全部条目、用户账号 / RBAC、多语言 Runtime。

## 1. 数据模型（migration `0003_m3_2_production_lifecycle`）

- `adapters` 新增：
  - `production_worker_id`（FK workers，`ON DELETE SET NULL`，可空）——生产 Worker；测试运行在已设置时默认以其为目标。
  - `production_state`（`idle / running / stopped`，默认 `idle`）——生产入口开关。
  - `archived_at`（可空）——归档时间戳，归档后只读。
- `executions` 新增：
  - `target_worker_id`（FK workers，可空）——指定执行的 Worker；为空时可被任意 Worker 领取（存量兼容）。
  - `cancel_requested`（bool，默认 false）。
  - `trigger` 约束扩展为 `('manual', 'production')`；存量 `manual` 一律解释为"测试运行"，不作为 Production Running。
  - 部分唯一索引 `uq_executions_active_production ON executions(adapter_id) WHERE trigger='production' AND status IN ('pending','running')`：一个 Adapter 同时只有一个 active Production Execution，由数据库强制。
- 新表：
  - `credentials`（name 唯一、type ∈ `password/token/access_key/secret`、字段 schema 按类型固定、Fernet 密文、时间戳）。
  - `adapter_credential_bindings`（adapter_id + env_key + credential_id + field，unique(adapter_id, env_key)，全量替换语义）。
  - `package_sources`（name 唯一、index_url、is_default 部分唯一索引、可绑定 credential 做 basic auth）。

前端展示的生产状态为派生规则（无持久化转换）：`未发布` = published 指针为空；`待启动` = 已发布且 state=idle；`已启动` = state=running；`已停止` = state=stopped；`异常` = 无 active Execution 且最近一次 Production Execution 为 failed/timeout；`已归档` = archived_at 非空。`production_state=running` 表示生产入口已启动，不要求此刻一定存在 active Execution；最近一次执行成功后应展示“已启动 / 空闲”，不能误报异常。`AdapterResponse` 附带 active Production Execution 的 `running_version_id` / `running_execution_id`，以及最近一次 Production Execution 的最小摘要，供 UI 在无 active Execution 时区分成功空闲与失败/超时。

## 2. Publish 门禁

- 规则：目标 Version 在 `adapter.production_worker_id` 上最近一次测试 Execution（`trigger='manual'` 且 `target_worker_id` 相符）必须为 `succeeded`，否则 409 `publish_gate_locked`。
- 存量 manual 记录 target 为空，天然不满足门禁（需重测）；切换生产 Worker 后门禁同样失效，语义一致。
- `GET /api/adapters/{id}/versions/{vid}/publish-gate` 返回 `allowed / reason / last_test`（reason 稳定码：`no_production_worker` / `not_tested_on_production_worker` / `last_test_not_succeeded`），供发布确认框展示；Publish 端点由后端强制门禁，前端仅展示。
- Publish 只更新 `published_version_id`：即使 Production Execution 正在运行，只要目标 Version 通过门禁也允许发布；当前 Execution 的版本、状态与进程均不改变。例如 `Running=v2` 时 Publish v3 后必须保持 `Running=v2 / Published=v3`。

## 3. Start / Stop / 取消

- `POST /api/adapters/{id}/production/start`（202）：校验未归档、已发布、生产入口不处于已启动状态、生产 Worker 已配置且在线、无 active Production Execution，并重新校验当前 Published Version 在**当前** production Worker 上最近一次测试成功；创建 `trigger='production'` Execution（绑定 published version 与 target worker）并置 `production_state='running'`。Publish 后不会自动 Start；只有管理员人工 Stop 且旧 Production Execution 真正进入终态后，才能 Start 新的 Published Version。
- `POST /api/adapters/{id}/production/stop`（body `{mode: wait|terminate}`）：置 `production_state='stopped'`；`terminate` 时对 active Execution 执行取消（pending 直接 cancelled，running 置 cancel_requested）。
- `production_state=stopped + running_execution_id != null` 派生为“停止中 / 等待当前任务完成”：Start、Unpublish、Archive 均保持禁用/后端拒绝，并提示正在等待的 Execution；`wait` 等自然终态，`terminate` 也必须等 Worker 真正上报 `cancelled` 后才解锁，不能把 `cancel_requested` 当成终态。
- `POST /api/executions/{id}/cancel`：幂等。pending → 直接 cancelled；running → cancel_requested=true；终态 → no-op。
- Worker 感知取消：`progress` 上报响应从 204 改为 200 JSON `{cancel_requested}`（ProgressAck），executor 每个轮询片检查，命中则 kill 进程组并上报 `cancelled`；claim 查询过滤 `cancel_requested=false` 且 `target_worker_id` 与自身相符。
- Unpublish 与 Archive 均要求生产已停止（409 `production_running`）；归档禁止 Save / Publish / Test / Start（409 `adapter_archived`）。
- Clone 复制 working copy（latest 版本）为新 Adapter 的 v1，连同凭据绑定引用；新 Adapter 未发布、未启动。

## 4. Secret Store 与安全边界变化

- 采用 `cryptography` 的 Fernet（认证对称加密）做静态加密；Fernet 密钥由部署级 Master Key（`DLR_MASTER_KEY`）经 HKDF-SHA256 派生（salt/info 为固定常量），Master Key 只存在于部署环境、从不落库。未配置时凭据 API 返回 503 `secret_store_unavailable`（不回退明文存储）。这是 M3.2 唯一新增后端依赖（`cryptography`）。
- 凭据 API 保存后任何响应不返回明文与密文，仅元数据；删除被绑定引用的凭据返回 409 `credential_in_use`。
- Adapter 绑定 `env_key → credential.field`；Control 在 claim 时按绑定解析出 `secrets`（仅该 Execution 所需）注入 TaskPayload；Worker 以 `DLR_SECRET_{env_key}` 注入子进程并纳入日志脱敏集合。
- `context.secrets.get()` Runtime Contract 不变（仍解析 `DLR_SECRET_*` 环境变量），Worker 环境变量路径保留为兼容路径。
- 实时日志脱敏的滚动 holdback（扣住末尾 `max_secret_len-1` 字符防止 Secret 跨片重组）增加 2 秒静默宽限：滞留尾部超过宽限且不再可能是某个 Secret 的前缀时随下一次空轮询释放，避免长静默子进程的实时日志停滞在残缺片段；仍可能补齐 Secret 的尾部继续保留到进程退出。
- **安全边界变化（明确声明）**：architecture.md §2.3 v1 "Secret 不经过 Control" 的表述演进为 "密文经 Control 解密后在内网传输给 Worker"。解密只发生在 claim 时刻，明文不落库、不进任何响应与日志。

## 5. Python 包源与 venv 策略

- 包源 CRUD + Control 侧可达性探测（stdlib urllib；任何 HTTP 应答含 401/403 均视为可达，仅传输失败为不可达）。claim 时把默认包源 index URL（如绑定凭据则内嵌 basic auth）放入 TaskPayload。
- venv 准备顺序（offline-first，测试与生产同一策略）：`.ready` 已就绪 → 直接通过不联网；否则先 `uv pip install --offline`（本地 cache）→ 失败且有默认包源 → 用包源安装 → 失败且无包源（含 Worker 环境变量 `DLR_PYPI_INDEX_URL` 兼容源）→ 明确失败并提示管理员。带 Basic Auth 的 effective index URL 只用于安装请求；平台在依赖安装日志进入 `install_log` / Execution stderr 前显式脱敏 URI userinfo 与对应用户名/密码，不依赖第三方工具自行打码。

## 6. 前端交互

- WorkbenchHeader 四层状态（未发布 / 待启动 / 已启动 / 已停止，异常与归档叠加展示）：保存为新版本、发布（确认框含门禁信息与 Diff 入口）、启动、停止（等待 / 终止选择）；Published != Running 时显著提示。发布确认只说明“当前运行不会自动切换；需人工停止后再启动”，不把 Publish 称为“热切换”；真正的切换拦截发生在旧生产入口尚未停止或旧 Execution 尚未终态时尝试 Start。Start 成功后自动切"执行记录"Tab 并打开该 Execution 实时日志（TestRunPanel 的 SSE/fallback 逻辑抽成共享 hook，不引入状态框架）。
- 编辑页次级配置 Tabs：Python 依赖 | 凭据绑定。M5.5.9 起“运行参数（JSON）”退出用户主流程，普通、非敏感配置由代码本身表达（底层 `runtime_config` 作为不可变 Revision 字段保留，Diff/AI 工作副本仍携带）。
- Header 新增"系统设置"抽屉：凭据管理 + Python 包源（含可达性测试）。
- Monaco DiffEditor 两个入口：Working Copy vs 基准版本、发布目标 vs 当前生产版本（覆盖 code/依赖/参数/绑定引用）。
- Adapter 设置提供 production Worker 选择器并明确在线/离线；切换后显示“需重新测试”，测试和 Start 对离线目标明确拒绝。Catalog 状态点 + 活跃/已归档切换，二级信息以生产状态、Running Version、production Worker 与 Published/Running mismatch 为主；Worker 列表一次加载后映射，不能按 Adapter 做 N+1。执行记录区分"测试运行/生产启动"标签。

## 7. 兼容与边界

- 存量 Adapter：新字段全部可空/默认值，migration 可从 main 状态直接 upgrade；存量 manual Execution 视为测试运行，不计入生产状态。
- 单 Worker 部署：未配置生产 Worker 时 Test/Start 自动默认唯一在线 Worker（并回写 production_worker_id）；多 Worker 未配置时 Test/Start 拒绝并提示先选择；已配置但离线的 Worker 对 Test/Start 都直接返回 `worker_offline`，不得创建长期 pending 任务。
- 明确不做：Issue §21 清单全部条目；不新增 MQ/长连接/全局状态框架；Control 仍不执行用户代码，Worker 仍主动外连。
- 回归原则：M1–M3.1 测试保持通过，仅对合同真实变化（progress 响应 204→200 JSON、trigger 值空间、AdapterResponse 新字段）做最小更新。

## 8. 部署配置

- `.env.example` 与 `docker-compose.yml`（control 服务）新增 `DLR_MASTER_KEY`；Compose 不提供任何公开已知的默认 Master Key，部署者必须显式配置。未配置时凭据功能整体不可用（503），其余功能不受影响；`compose-smoke` 在隔离脚本中显式注入当次测试 Key。
- `scripts/compose-smoke.sh` 扩展 M3.2 链路：凭据（断言无明文）→ 包源可达性 → 生产 Worker 设置 → 门禁 409 → 测试成功 → 门禁放行 → Publish → 绑定 → Start → cancel/Stop(terminate) → cancelled → 再 Start → succeeded（绑定凭据以 SHA-256 摘要验证端到端可用）→ Stop(wait) → Clone → Archive（409 拦截写/启动）→ Restore → 删除被绑定凭据 409。
