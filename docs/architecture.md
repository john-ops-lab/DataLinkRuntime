# DLR（DataLinkRuntime）总体架构

> 版本：v1.0（已确认）
> 本文档范围：总体架构、领域模型落库设计、Runtime 设计、部署与里程碑。
> 产品定义见 [product.md](./product.md)；工程规范见 `.qoder/rules/engineering.md`，此处不重复。

## 1. 组件总览

```
┌─────────────┐   HTTP/JSON    ┌──────────────────────────────┐   HTTP(S)
│  web (React) │ ─────────────► │  control (FastAPI)            │ ─────────► 管理员配置的 AI Provider
└─────────────┘                 │  ├─ Adapter/版本/执行/AI API  │
                                │  ├─ Worker 注册/心跳/任务通道  │
                                │  └─ PostgreSQL                │
                                └──────────────┬───────────────┘
                                               │ Worker 主动外连（HTTP 长轮询）
                                               │ 共享 DLR_WORKER_TOKEN 认证
                                ┌──────────────▼───────────────┐
                                │  worker (multi-runtime agent) │
                                │  ├─ 领任务 → 按版本准备 venv   │
                                │  ├─ 子进程执行 Adapter         │
                                │  └─ 上报结果/日志/状态         │
                                └───────────────────────────────┘
```

| 组件 | 职责 | 是否运行用户代码 |
|------|------|------------------|
| web | React SPA：Adapter 管理、在线编辑、执行与日志查看 | 否 |
| control | FastAPI：API、版本管理、任务下发、执行记录、日志转发、AI Provider 薄适配 | **否** |
| postgres | 持久化：Adapter、版本、执行记录、Worker 运行信息 | 否 |
| worker | 领任务、准备 version-scoped venv、子进程执行 Adapter、上报 | **是** |

关键决策：

- **通信方向**：Worker 主动外连 Control（HTTP 长轮询拉任务），Control 不主动连 Worker。多机部署时 Worker 可位于 NAT/防火墙后。
- **Manual Test 也下发 Worker 执行**，保证测试与运行环境一致，且 Control 不碰用户代码。
- **日志**：执行期间 Worker 增量上报 stdout/stderr，Control 通过 SSE 转发给前端实现实时查看；执行结束后日志随 Execution 持久化（受大字段策略约束）。不引入日志系统组件。
- **AI 只产生 Candidate**：Control 将当前 Working Copy 与最小上下文发送到管理员配置的
  Provider，严格校验最终回答后返回浏览器；不创建 Version / Execution，不触发任何生产动作。

## 2. 认证与安全边界（v1）

### 2.1 认证

| 通道 | 凭据 | 说明 |
|------|------|------|
| web / 管理员 → control API | `DLR_ADMIN_TOKEN` | 最简单的单管理员 API Token，部署配置注入。避免任何能访问内网 Control API 的人都可以执行 Adapter。**不扩展为账号、角色、权限系统** |
| worker → control API | `DLR_WORKER_TOKEN` | 共享 Worker Token，部署配置注入（Compose 环境变量）。**Worker 领域模型中不保存 token/token_hash**；每 Worker 独立凭据留待未来需要时再设计 |

协议层约定：token 以请求头传递、服务端单点校验，未来升级为独立凭据时不改变协议结构。

### 2.2 v1 安全边界（明确声明）

- v1 是 **trusted-code model**：Adapter 只允许可信管理员创建和运行。
- Adapter 代码在 Worker 主机权限内运行，**v1 不做代码沙箱**。
- 子进程与 version-scoped venv 用于**运行隔离与依赖隔离，不构成真正的安全沙箱**。
- `context.secrets` 是 **Runtime API 抽象**（保证 Adapter 代码与凭据来源解耦），**不是安全隔离边界**。
- 数据库只持久化凭据的 Fernet 密文（M3.2 Secret Store，见 §2.3）；**明文从不落库**。

### 2.3 Secret 注入路径（M3.2 演进）

两条路径并存，`context.secrets.get(key)` 的 Runtime Contract 完全不变：

- **Worker 环境变量路径（v1，保留为兼容路径）**：Secret 只存在于 Worker 部署环境，约定前缀 `DLR_SECRET_<KEY>`；`context.secrets.get(key)` 即解析该环境变量。
- **Secret Store 路径（M3.2）**：凭据以 Fernet（认证对称加密）密文存入 `credentials` 表，Fernet 密钥由部署级 Master Key（`DLR_MASTER_KEY`）经 HKDF-SHA256 派生，Master Key 只存在于部署环境、从不落库。Adapter 绑定 `env_key → credential.field`；Control 在 Worker claim 时只解密该 Execution 绑定的字段并注入 TaskPayload；Worker 以 `DLR_SECRET_{env_key}` 注入子进程并纳入日志脱敏集合。未配置 Master Key 时凭据 API 返回 503（不回退明文存储）。
- **安全边界变化（明确声明）**：v1“Secret 不经过 Control”的表述演进为“密文经 Control 解密后在内网传输给 Worker”；解密只发生在 claim 时刻，明文不落库、不进任何响应与日志。

### 2.4 外部 AI 数据边界（M4）

- Provider / Base URL 由管理员明确配置，它决定 Working Copy、requirements、
  runtime_config、基准 Version 元数据、Secret `env_key` 名称及有限最近对话发送到哪里。
- AI API Key 复用 `token` Credential；Control 仅在请求时于内存解密并以 Bearer Token
  使用。浏览器只得到 `credential_id / credential_name` 等元数据。
- Credential 真值、密文、`DLR_MASTER_KEY`、管理员/Worker Token 永不进入 Prompt。
- Prompt、完整 Working Copy、Provider 原始 Response 与 reasoning 不落库、不进入普通日志；
  若记录调用元数据，只允许 provider、model、耗时与成功/失败等不敏感字段。
- Provider 返回值先隔离 reasoning，再对最终 JSON 做本地 Candidate Schema 校验。无法明确
  分离或校验失败均返回稳定错误，不猜测截取代码。

## 3. 领域模型与持久化

持久化全部在 PostgreSQL（SQLAlchemy 2.x + Alembic）。v1 共四张核心表（M3.2–M5.2
另增平台表，见 §3.6–§3.8）：

### 3.1 Adapter

| 字段 | 说明 |
|------|------|
| id / name / description | 基本信息 |
| language | `python / javascript / java`；创建后不可修改，数据库 CHECK 约束 |
| latest_version_id | 每次保存代码产生新版本后更新 |
| published_version_id | 发布时设置；Published Version 是下一次 Start 的目标 |
| production_version_id | M5.1：Start 时锁定为当时的 Published Version（FK adapter_versions，可空）；Stop 清空 |
| production_worker_id | 生产 Worker（FK workers，可空，ON DELETE SET NULL）；测试运行默认以此为目标；`production_state=running` 时不可修改（409） |
| production_state | `idle / running / stopped`（生产入口开关，默认 idle） |
| archived_at | 归档时间戳（可空）；归档后只读 |
| created_at / updated_at | 时间戳 |

生产状态派生规则（纯展示层）：`未发布` = published 指针为空；`待启动` = 已发布且 state=idle；`已启动` = state=running；`已停止` = state=stopped；`已归档` = archived_at 非空。`production_state=running` 表示生产入口已开启，与当前是否恰有子进程执行分离；M5.1 起 Start 不再创建 Execution，因此 `running` 且无 active Execution 就是合法的“已启动 / 空闲”，不因上一生命周期的 failed/timeout 派生出需要 Stop→Start 的“异常”生命周期状态。最近一次 Production Execution 的 failed/timeout 如展示，只作为独立的“最近一次生产执行失败”结果提示（指向执行记录），不影响入口状态。Adapter API 同时返回 active Production Execution 指针与最近一次 Production Execution 最小摘要，前端不靠猜测状态。

Publish 与 Start 是两个独立动作，术语全平台统一：**Published Version** 是下一次 Start 的目标；**Production Version** 是当前 Start 生命周期锁定、供后续生产 Trigger 使用的版本。Publish 只更新 `published_version_id`，不会修改 `production_version_id`。M5.1 起 Start 不再创建 Execution，而是开启生产入口并锁定 `production_version_id = published_version_id`，同时锁定 production Worker；Start 是同步状态变更，成功返回 200。运行期间 Publish 新版本不会改变已锁定的生产版本。只有管理员人工 Stop 关闭生产入口并清空 `production_version_id` 后，再次 Start 才锁定新的 Published Version。

### 3.2 AdapterVersion（不可变）

| 字段 | 说明 |
|------|------|
| id / adapter_id / seq | 归属与序号 |
| code | Adapter 对应语言源码（text），入口约定见 §4 |
| requirements | 按 Adapter.language 解释的依赖声明文本 |
| runtime_config | 非敏感运行时配置（JSON） |
| created_at | 时间戳 |

版本模型规则：

- **保存 = 新建不可变 Version** 并更新 `Adapter.latest_version_id`；不存在 Draft 实体。
- **发布 = 设置 `Adapter.published_version_id`**，只改变下一次 Start 的目标。
- Manual 测试默认执行 latest 版本；生产类触发（M5.2 Schedule / M5.3 Webhook）执行当前 Start 生命周期锁定的 Production Version（`production_version_id`），而不是发布时的最新版本。

### 3.3 Execution

| 字段 | 说明 |
|------|------|
| id / adapter_id / **version_id（必填）** / worker_id | 关联关系 |
| target_worker_id | 指定执行的 Worker（可空）；为空时可被任意 Worker 领取（存量兼容） |
| trigger | 枚举：`manual`（测试运行）/ `production`（历史兼容值：M3.2 的 Start 曾创建它；M5.1 起 Start 不再创建 Execution）/ `schedule`（定时触发，M5.2 实现）/ `webhook`（事件触发，M5.3 实现） |
| scheduled_for | M5.2：trigger=schedule 时该次执行对应的计划点（UTC，timestamptz）；其余触发为 NULL；`(adapter_id, scheduled_for) WHERE trigger='schedule'` 部分唯一索引防重复创建 |
| cancel_requested | 取消请求标志：running 的执行由 Worker 在下次进度上报时感知并 kill |
| status | `pending / running / succeeded / failed / timeout / cancelled` |
| input / output / stdout / stderr | 见 §3.5 大字段策略 |
| error | 失败摘要 |
| start_time / end_time / duration | 时间与耗时 |

一个 Adapter 同时只允许一个 active 生产类 Execution（trigger IN ('production','schedule','webhook')），由部分唯一索引 `uq_executions_active_production ON executions(adapter_id) WHERE trigger IN ('production','schedule','webhook') AND status IN ('pending','running')` 在数据库层强制。

### 3.4 Worker

| 字段 | 说明 |
|------|------|
| id / name | 身份与名称 |
| status | Stored Status：Worker 在 register / heartbeat / graceful offline 时主动写入的 `online / offline` |
| last_heartbeat | 心跳时间 |
| capabilities | Worker 启动时检测并上报 `python / javascript / java` 子集 |
| created_at / updated_at | 时间戳 |

Control 不通过后台调度器把心跳过期记录的 Stored Status 改写为 `offline`。所有需要判断
Worker 当前是否可用的 Control 路径共用一套派生语义：

```text
effective_online =
    status == "online"
    AND
    now - last_heartbeat <= DLR_WORKER_HEARTBEAT_TIMEOUT_SECONDS
```

Worker 默认心跳间隔为 10 秒（`DLR_WORKER_HEARTBEAT_SECONDS`），Control 默认有效在线
超时为 30 秒。超时值必须为正数且严格大于心跳间隔；部署方调整间隔时应同步调整超时，
建议不少于间隔的约 3 倍。同一 HTTP 请求或业务操作使用一致的当前时间基准，恰好位于
超时边界（`age <= timeout`）仍视为在线。

Admin Worker API 的 `status` 返回 Effective Status，`last_heartbeat` 保持原值供排障；
Test / Start 只选择有效在线且 capability 兼容的 Worker，Web 直接展示 API 状态而不使用
浏览器时间重复计算。过期 Worker 后续 heartbeat 会更新 `last_heartbeat` 并自动恢复有效
在线，不需要 Restore API、Worker lease 或后台 heartbeat scheduler（M5.2 的 Schedule
轮询循环是独立的调度状态源，见 §3.8，不改写 Worker 状态）。

**不保存 token / token_hash**：共享 Worker Token 属于平台部署配置，不是领域数据。

AdapterInstance：长期领域概念，v1 不建表。

### 3.5 Execution 大字段策略

阈值集中在一处配置，可调整；默认值：input 上限 512 KB，output 上限 512 KB，stdout / stderr 各 1 MB。

| 字段 | 超限行为 |
|------|----------|
| input | **直接拒绝**（返回错误，不创建执行），不允许截断后执行 |
| stdout / stderr | 按文本截断，附加截断标记 |
| output | 小于上限：保存完整 JSON。超过上限：**不持久化完整 output、不产生被破坏的 JSON**，记录 `output_size`、`output_truncated` 与有限的 `output_preview`（供人阅读的文本摘录，字段命名以实现时最简方案为准） |

不引入对象存储或独立日志系统。

### 3.6 平台表（M3.2–M3.3）

| 表 | 说明 |
|------|------|
| credentials | 凭据：name 唯一、type ∈ `password/token/access_key/secret`（字段 schema 按类型固定）、Fernet 密文、时间戳 |
| adapter_credential_bindings | Adapter 绑定：env_key → credential.field，unique(adapter_id, env_key)；全量替换语义 |
| package_sources | 依赖源：kind ∈ `pypi/npm/maven`、name 唯一、index_url；每种 kind 至多一个 default，可绑定密码凭据，npm 也可绑定 Token |

### 3.7 AI 模型设置（M4）

M4 只持久化一条全局活动 `AiModelSetting`，包含 provider、base_url、明确选定的
model id、可空的 token Credential 引用、reasoning 策略与可选强度、时间戳。API Key
不存入该表；模型列表刷新结果、Prompt、Response、Candidate 与对话历史均不持久化。

### 3.8 Schedule Trigger（M5.2）

一个 Adapter 至多一条 `adapter_schedules` 记录（单例）：adapter_id 唯一、enabled、
cron（固定 5 字段表达式）、timezone（IANA 名称）、input（JSON，与 Execution input
同大字段合同，超限零持久化）、`next_run_at`（调度游标，UTC timestamptz；enabled 时为
下一个未来计划点，disabled 时为 NULL）、updated_at。

配置 API：`GET /api/adapters/{id}/schedule` 未配置时返回稳定 404
`schedule_not_configured`；`PUT` 全量替换（create-or-replace），cron / timezone / input
校验失败一律不持久化（422 / 413），已归档 Adapter 拒绝写入（409 `adapter_archived`，
GET 仍可查看）；保存后游标总是重基准到下一个未来计划点，因此编辑、禁用、启用都
不回放历史点。

调度循环：Control 进程内一个轻量后台任务按 `DLR_SCHEDULE_POLL_SECONDS`（默认 5s）
轮询 PostgreSQL；PostgreSQL 是唯一调度状态源，不引入 APScheduler / Celery / Redis。
每个 tick 先纯读发现 `enabled AND next_run_at <= now` 的行，再逐行在独立短事务中
处理：按平台统一锁顺序先 `FOR UPDATE SKIP LOCKED` 锁 Adapter 行、再锁 Schedule 行，
在最终锁内复查到期条件（enabled 且 `next_run_at <= now`）后执行“领取→门禁→游标
更新→创建 Execution”；多个 Control 实例并发时自然分区不重复。行处理结果分三类：
创建 Execution（CREATED）、消费计划点仅推进游标（CONSUMED）两者提交；临时阻塞
（HELD）不写任何数据并回滚，计划点保持 due。

门禁分两类：结构性失败（已归档、`production_state ≠ running`、缺锁定的 Production
Version / Worker 指针、Worker 记录缺失或 capability 不兼容、input 超限）消费该点：
游标推进到下一个未来计划点，跳过但不排队；临时性失败（生产 Worker 有效离线、存在
active 生产类 Execution）保持 due：游标不动、不排队，条件恢复后立即补截至当前最近
一次计划点。通过门禁才创建 `trigger=schedule` 的 pending Execution（version_id =
锁定的 `production_version_id`，target_worker_id = 锁定的生产 Worker，input =
Schedule 配置值，scheduled_for = 到期计划点）。

补跑语义（单次最新补跑，绝不排队）：Worker 离线 / 生产 busy 错过的窗口保持 due，
恢复后至多补跑窗口内最近一次计划点，绝不逐周期回放；Control 停机期间无 tick，
恢复后同样至多补最近一次。显式 Stop / 禁用 / 编辑 cron / 启用时游标重基准到未来
点，关闭窗口内的补跑。锁顺序全平台统一为 Adapter 行先于 Schedule 行（Start / Stop /
PATCH / PUT Schedule / scheduler tick 一致），不会交叉死锁；`(adapter_id,
scheduled_for)` 部分唯一索引与 `uq_executions_active_production` 是最终的重复创建
防线，竞争失败只回滚 savepoint，游标推进照常提交。

时间语义：cron 在配置的时区内求值，结果统一以 UTC 落库，从不使用服务器本地时区。
DST 行为固定为 croniter 语义：被跳过的墙钟时间在转换边界触发一次，歧义的墙钟时间
两次都触发。

### 3.9 Webhook Trigger（M5.3）

一个 Adapter 至多一条 `adapter_webhooks` 记录（单例）：adapter_id 唯一、enabled、
public_id（随机不可猜测字符串，全局唯一，创建后稳定不轮换，只负责路由、不视为
认证 Secret）、credential_id（外键指向 token 类型 Credential，RESTRICT，被引用时
不可删除）。

配置 API（需 Admin Token）：`GET /api/adapters/{id}/webhook` 未配置时返回稳定 404
`webhook_not_configured`；`PUT` create-or-replace（enabled + credential_id），非 token
凭据 422 `webhook_credential_type_invalid`，已归档 Adapter 拒绝写入（409
`adapter_archived`，GET 仍可查看）；响应只含 public_id / hook_path / credential 名称
与时间戳，从不返回 token 真值或密文。

外部入口（不要求 Admin Token）：`POST /api/hooks/{public_id}`，携带
`Authorization: Bearer <token>` 与 JSON Body。校验顺序固定：未知 public_id → 404
`webhook_not_found`；已禁用 → 409 `webhook_disabled`；Token 缺失或错误 → 401
`unauthorized`（解密后 constant-time 比较，不区分失败细节）；生产门禁 → 409 稳定
错误码（已归档 / `production_not_running` / `worker_offline` /
`worker_capability_missing` / `production_busy`）；body 超过
`execution_input_max_bytes` → 413 `execution_input_too_large`（流式分块读取，超限
立即中断）；非法 JSON → 400 `webhook_body_invalid_json`。

与 Schedule 的门禁差异：Webhook 拒绝即结束，**不排队、不补跑**（Schedule 的临时性
失败保持 due 待补）。通过全部门禁后创建 `trigger=webhook` 的 pending Execution
（version_id = 锁定的 `production_version_id`，target_worker_id = 锁定的生产
Worker，input = 整个 JSON Body），立即返回 `202 + execution_id`，Control 不等待
执行结果。锁顺序与平台一致：Adapter 行先于 Webhook 行（PUT / hooks 接收一致）；
`uq_executions_active_production` 部分唯一索引是最终的重复创建防线，竞争失败
（`production_busy`）只回滚不产生副作用。

## 4. Adapter Runtime（M3.3：Python / JavaScript / Java）

### 4.1 Runtime Contract

```python
# adapter.py，入口固定为 handle
def handle(context, input):
    # input : JSON-compatible（dict/list/标量，可为空）
    # return: JSON-serializable，允许对象、对象数组等常见形式
    ...
```

`context` 保持最小面：

| 成员 | 说明 |
|------|------|
| `context.config` | 该版本的 runtime_config（dict） |
| `context.secrets.get(key)` | 凭据读取（v1 来自 Worker 环境变量，见 §2.3） |
| `context.logger` | 日志输出，写入 stdout 由平台采集 |

JavaScript 使用 ESM `export async function handle(context, input)`（同步返回与 Promise
均支持）；Java 使用单文件固定类 `Adapter` 和
`public Object handle(Context context, Object input) throws Exception`。三种语言共享
`context.config / context.secrets.get / context.logger` 与 JSON input/output 语义。

### 4.2 执行模型

- 每次 Execution = Worker 上一个**全新子进程**：Python 启动 Python harness，
  JavaScript 启动 Node.js，Java 启动 JVM；三者都通过文件交换 JSON input/output，
  stdout/stderr 使用相同的增量采集、SSE、截断、timeout、cancel 与进程组终止实现。
- **超时**：默认 300s（可配置），超时杀进程，状态记 `timeout`。
- **并发**：Worker 最大并发执行数可配置（默认 4），超过排队。

### 4.3 Version-scoped 依赖环境

- Python 使用 `.venv/`，JavaScript 使用 `node_modules/` 与生成的最小
  `package.json`，Java 使用 `deps/`、`classes/` 与生成的最小 `pom.xml`。Java 首次执行
  同时编译不可变源码；`.ready` 只在依赖和编译全部成功后写入。
- 依赖格式分别为 requirements.txt 行、`package@version` 行（支持 scoped package）、
  `groupId:artifactId:version` 行；不接受任意 package.json、pom.xml、Gradle 或脚本。
- 三种语言统一 offline-first：ready 直接复用，否则先本地 cache/repository，缺失时再用
  对应 kind 的平台默认源；测试和生产走同一路径。依赖源认证与 URI userinfo 在持久化
  stderr 前统一脱敏。

## 5. AI Assistant（M4）

### 5.1 请求与结果

浏览器每轮显式提交当前 Working Copy（code / requirements / runtime_config）、用户指令
和最多 8 条最近可见对话。Control 根据持久化的 `adapter_id` 补充 Adapter.language、
基准 Version、已绑定 Secret 的 `env_key` 以及对应 Python / JavaScript / Java Runtime
Contract。浏览器不能覆盖这些服务端事实。

Provider 的 final answer 必须是 `{message, candidate}`。`candidate` 可空；非空时必须包含
完整 code、requirements、runtime_config、summary 与 required_secret_keys。Candidate
不包含 language、Version / Execution 指针或生产状态。前端用 Monaco Diff 展示完整快照，
Apply 只更新浏览器 Working Copy。

请求发出时前端保存 base snapshot；响应回来后若 Working Copy 已改变，Candidate 标为
stale，只有管理员明确选择“仍然应用”才可覆盖当前编辑。Adapter 切换使用 request
generation 防护，旧响应不得进入新 Adapter；已归档 Adapter 不允许 Apply。

### 5.2 Provider 薄适配

M4 支持 `openai / deepseek / kimi / minimax / custom_openai_compatible`，共同主协议为
`POST /v1/chat/completions`，模型发现为 `GET /v1/models`。模型刷新失败不阻止手工输入
Model ID，已保存 ID 不会自动跟随厂商更新。

`reasoning_mode=default` 时不发送 override；显式开启/关闭或设置 effort 只在 Provider
明确支持时映射，否则返回 `ai_reasoning_unsupported`。Provider Adapter 只输出
`final_text`，不把 `reasoning_content`、`reasoning_details` 或明确 thinking 容器传给
浏览器。无 LangChain / LlamaIndex / Agent Framework、tool calling、模型路由或 fallback。

## 6. 部署架构

Docker Compose 四容器（单机最小部署）：

| 服务 | 镜像/构建 | 说明 |
|------|-----------|------|
| postgres | postgres:16-alpine | healthcheck：pg_isready |
| control | backend 代码构建 | 依赖 postgres 健康；注入数据库连接串、DLR_ADMIN_TOKEN、DLR_WORKER_TOKEN、DLR_MASTER_KEY（M3.2 Secret Store，Compose 无默认值，必须显式配置）、DLR_WORKER_HEARTBEAT_TIMEOUT_SECONDS（默认 30）、DLR_SCHEDULE_POLL_SECONDS（M5.2 调度轮询间隔，默认 5） |
| worker | backend 代码构建 | 注入 DLR_WORKER_TOKEN、control 地址、DLR_WORKER_HEARTBEAT_SECONDS（默认 10）、DLR_SECRET_* |
| web | 前端构建产物 + Nginx | 托管 SPA，反代 `/api` 到 control |

AI Provider 是部署外部依赖，不加入正式 Compose 拓扑。`compose-smoke` 仅在隔离测试网络中
临时启动本地 fake Provider，测试结束后随 smoke 资源清理，且不访问公网模型 API。

多机演进：worker 容器拆到其它服务器，指向 Control 地址即可，架构与协议不变（得益于 Worker 主动外连模型）。

## 7. 仓库结构

```
DataLinkRuntime/
├── README.md
├── docker-compose.yml
├── .github/workflows/ci.yml
├── docs/                        # product.md / architecture.md
├── backend/                     # Python 3.13，uv 管理
│   ├── pyproject.toml
│   ├── alembic.ini + alembic/
│   ├── src/dlr/
│   │   ├── common/              # 共享：协议 DTO、枚举、常量、配置
│   │   ├── control/             # FastAPI：api / services / db models / AI Provider 适配
│   │   ├── worker/              # agent：领任务 / venv 管理 / 上报
│   │   └── runtime/             # Contract 与 Python harness
│   └── tests/
├── web/                         # React + TypeScript + Vite
└── docker/                      # control / worker / web 三个 Dockerfile
```

control 与 worker 共享同一 Python 包（`dlr.common` / `dlr.runtime`），避免多仓库与复杂 monorepo 工具链。

技术选型：Python 3.13、uv、FastAPI、SQLAlchemy 2.x、Alembic、pytest、ruff、mypy；前端 React（不固定版本，实现时选与当前工具链兼容的稳定版）+ TypeScript + Vite，Monaco Editor 于 M1 引入。**各阶段只安装该阶段真正需要的依赖。**

## 8. 里程碑（M0–M5.2）

| 里程碑 | 内容 | 验收口径 |
|--------|------|----------|
| M0 工程骨架 | 仓库结构、四容器 Compose、Health Check、SQLAlchemy + Alembic 空迁移骨架、基础测试、ruff/mypy/eslint、CI、README | `docker compose up` 四服务健康检查全绿；CI 绿 |
| M1 Adapter 管理 | Adapter CRUD、Monaco 在线编辑、保存即不可变版本、发布、requirements / runtime_config | 创建→编辑→保存→发布全通 |
| M2 执行闭环 | Worker 注册 / 心跳、version-scoped venv、Manual 触发、子进程执行、Execution 落库（含 §3.5 大字段策略） | "测试运行"→ Worker 执行 → 状态与结果正确 |
| M3 可观测与体验 | 测试输入面板、Output 查看（对象/数组渲染）、实时日志（SSE）、执行历史 | 第一阶段闭环完整可用 |
| M3.1 Console 视觉收敛 | 不改后端与业务合同，仅收敛 Web UI：Console Shell（App Header + 左侧 Adapter Catalog + Developer Workbench）、Monaco 作为编辑页主视觉、测试运行 Input + Execution 双栏、高密度执行记录表格与详情抽屉、登录页品牌区 | 四个核心页面（登录 / 编辑 / 测试运行 / 执行记录）对照 docs/ui/m3/ 视觉基线收敛；1440/1680/1920 宽度布局正常；全部既有业务测试保持通过 |
| M3.2 Adapter 生产生命周期与运行配置闭环 | 生产 Worker / Publish 门禁 / Start / Stop(wait-terminate) / Execution 取消、target Worker 调度、归档与 Clone、Secret Store（Fernet + 凭据绑定，§2.3）、Python 包源（offline-first）、前端四层状态 / 发布确认 Diff / 系统设置 | 测试→门禁→发布→启动→实时日志→停止→cancelled/succeeded 全链通；绑定凭据只以摘要出现在 output；M1–M3.1 测试保持通过 |
| M3.3 多语言 Runtime | Python / JavaScript / Java 合同、version-scoped venv/node_modules/classes、PyPI/npm/Maven 依赖源、Worker capability 硬调度、Web 语言体验 | 三语言分别真实完成 Save→Test→Publish→Start→succeeded，M3.2 生命周期零回归 |
| M4 AI Editor | 单一全局模型配置、OpenAI-compatible Provider 薄适配、三语言上下文、完整 Candidate、Diff / Apply、stale 与 Adapter 切换防护 | 本地 fake Provider 完成设置→模型刷新→连接测试→三语言 Assist，且 Version / Execution / published / production 事实不变 |
| M4.1 Worker 有效在线判定 | Stored Status 与基于心跳超时的 Effective Status 分离；API / Test / Start 共用有效在线语义 | 心跳新鲜时可用；超时后不创建 Test / Production Execution；恢复心跳后自动恢复在线 |
| M5.1 Production Entry 收敛 | Start 开启生产入口并锁定 Production Version / Worker，不再创建 Execution；Stop 后才切换到新的 Published Version | Start / Stop / 版本轮换全链通；运行期 Publish 不改锁定版本 |
| M5.2 Schedule Trigger | Cron + IANA 时区 + 固定 input 的单例 Schedule；PostgreSQL 轮询调度循环、统一生产门禁、单次最新补跑、scheduled_for 唯一约束 | Compose smoke 真实到点执行锁定的 Production Version；Publish 新版本后未 Stop/Start 仍执行旧版本；Manual 零回归 |
| M5.3 Webhook Trigger | 单例 adapter_webhooks（稳定 public_id + token Credential）；统一入口 POST /api/hooks/{public_id} 异步 202；固定校验顺序与稳定错误码；生产门禁拒绝不排队 | Compose smoke 真实完成配置 → Start → POST → 202 → 异步执行锁定版本；未知 / 未授权 / 禁用 / Stop 稳定拒绝；Publish 新版本后未 Stop/Start 仍执行旧版本；Schedule 零回归 |

## 9. 未来演进（不在第一阶段）

- AI 自动调试循环 / Agent Loop（需单独设计执行权限与审计边界）。
- 同步 invoke API（与 Webhook 解耦，如未来确有同步调用需求）。
- 常驻 Adapter（AdapterInstance 落库与进程生命周期管理）。
- Worker 独立凭据认证。

## 10. 过度设计检查清单（v1 明确不做）

Draft 实体；join token / enrollment；APScheduler 与前端状态框架预装；同步 Webhook 转发通道；Adapter 级覆盖式全局 venv；AdapterInstance 表；runner 抽象层；Sink / Connector 框架；MQ / 长连接网关；镜像构建与跨 Adapter 依赖共享；日志系统组件；RBAC；通用 Adapter I/O schema 校验；AI Provider Plugin/SPI；RAG / Embedding / Vector DB；多模型路由与 fallback；重试与优先级队列。
