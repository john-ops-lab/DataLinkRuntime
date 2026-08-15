# DLR（DataLinkRuntime）总体架构

> 当前基线：M5.4 Workbench
> 本文档描述当前架构；历史阶段合同见 `docs/specs/` 与 Alembic migration。

## 1. 组件总览

```text
┌──────────────┐  HTTP/JSON + SSE  ┌─────────────────────────────┐
│ web (React)  │ ─────────────────► │ control (FastAPI)           │
└──────────────┘                    │ Adapter / Execution / AI API│
                                    │ Schedule poller / Webhook   │
                                    │ PostgreSQL                  │
                                    └──────────────┬──────────────┘
                                                   │ Worker 主动长轮询
                                    ┌──────────────▼──────────────┐
                                    │ worker (multi-runtime agent)│
                                    │ Python / Node.js / Java     │
                                    └─────────────────────────────┘
```

| 组件 | 职责 | 运行用户代码 |
|------|------|--------------|
| web | Catalog、Workbench、Monaco、运行控制、实时日志与历史 | 否 |
| control | API、事务门禁、调度、Webhook 路由、日志 SSE、AI Provider 薄适配 | 否 |
| postgres | Adapter、Revision、Execution、Worker、Trigger 与 Credential | 否 |
| worker | 领任务、准备依赖、独立子进程执行、增量上报 | 是 |

Worker 只主动连接 Control；Control 不反向连接 Worker。所有触发方式都在 Worker 执行用户代码。

## 2. 认证与敏感数据

| 通道 | 凭据 |
|------|------|
| 管理员 Web/API → Control | `DLR_ADMIN_TOKEN` |
| Worker → Control | `DLR_WORKER_TOKEN` |
| 外部系统 → Webhook | Adapter 绑定的 token Credential |

Credential 使用部署级 `DLR_MASTER_KEY` 派生的 Fernet key 加密。浏览器只获得 Credential 元数据；Control 仅在 claim、Webhook 校验或 AI Provider 请求的必要时刻解密。明文不落库、不进入普通日志。

## 3. 当前领域模型

### 3.1 Adapter

关键字段：

| 字段 | 当前语义 |
|------|----------|
| `id / name / description` | 基本信息；运行中仍允许修改 name / description |
| `language` | `python / javascript / java`，创建后不可变 |
| `adapter_type` | `task / webhook` |
| `run_mode` | Task 的 `manual / schedule`；Webhook 不使用 |
| `latest_version_id` | 最新已保存不可变 Revision |
| `runtime_worker_id` | 当前运行节点 |
| `archived_at` | 内部软删除标记；当前 Web UI 只展示活跃 Adapter |

API 响应附带 `runtime_locked` 与 `running_execution_id`，供 Web 直接展示权威运行锁和当前 Execution，不使用浏览器时间或本地推断替代服务端事实。

### 3.2 AdapterVersion / Revision

每次保存创建一条不可变记录：`code / requirements / runtime_config / seq / created_at`，并更新 `latest_version_id`。用户界面只提供“保存”，Revision 序号只作为次级排障信息。

运行入口始终绑定创建 Execution 当时的 `latest_version_id`，因此之后再次保存不会改变已经在运行的 Execution。

### 3.3 Execution

| 字段 | 语义 |
|------|------|
| `adapter_id / version_id` | 固定 Adapter 与 Revision |
| `worker_id / target_worker_id` | 实际领取节点 / 指定运行节点 |
| `trigger` | `manual / schedule / webhook` |
| `scheduled_for` | Schedule 计划点；其他 trigger 为 NULL |
| `status` | `pending / running / succeeded / failed / timeout / cancelled` |
| `input / output / stdout / stderr` | 输入、结果与日志 |
| `cancel_requested` | 运行中取消请求 |

数据库部分唯一索引保证每个 Adapter 同时最多一个 `pending / running` Execution；Manual、Schedule、Webhook 共用同一约束。服务层所有入口使用统一 Adapter 行锁顺序，数据库约束作为并发最终防线。

### 3.4 Worker

Worker 上报 stored status、心跳与 capability。Control 使用数据库时间派生 effective-online：

```text
status == online
AND clock_timestamp() - last_heartbeat <= heartbeat_timeout
```

超时值必须为正且严格大于 Worker 心跳间隔。所有运行入口都要求节点 effective-online 且 capability 包含 Adapter language。

### 3.5 Credential 与绑定

- `credentials`：name、type、Fernet 密文与时间戳；
- `adapter_credential_bindings`：`env_key → credential.field`；
- `package_sources`：Python / npm / Maven 依赖源，可绑定 Credential；
- Webhook 只允许绑定 `token` 类型 Credential。

## 4. Task 执行

### 4.1 Manual

```text
POST /api/adapters/{id}/executions
→ 锁 Adapter
→ 校验最新 Revision、运行节点与统一运行锁
→ 创建 trigger=manual Execution
→ Worker claim / run / report
```

取消复用 `POST /api/executions/{id}/cancel`。pending 可直接进入 cancelled；running 设置取消请求，由 Worker 终止进程组并上报终态。

### 4.2 Schedule

`adapter_schedules` 是每个 Task Adapter 的单例配置：enabled、cron、timezone、input、next_run_at。

Control 使用 PostgreSQL 作为唯一调度状态源，以短事务轮询到期行；多个 Control 实例通过 `FOR UPDATE SKIP LOCKED` 分工。每个 tick 使用 `clock_timestamp()` 判定到期，Cron 在配置时区求值，最终以 UTC 保存。

启用 Schedule 后运行配置锁定。到点时以最新 Revision、当前运行节点和配置 Input 创建 `trigger=schedule` Execution。Worker 离线或 Adapter busy 时不排队；条件恢复后至多补最近一次计划点。停用或修改配置后游标重基准到下一个未来点。

## 5. Webhook

`adapter_webhooks` 是每个 Webhook Adapter 的单例配置：enabled、public_id、token credential 与时间戳。

停止状态允许多个 Adapter 使用相同 `public_id`；PostgreSQL partial unique index 只约束 `enabled=true` 的 path 唯一。开启接收时服务层先返回稳定冲突码，数据库索引负责并发最终防线。

外部入口：

```text
POST /api/hooks/{public_id}
Authorization: Bearer <token>
Content-Type: application/json
```

校验顺序覆盖 body 大小、启用路由、Bearer Token、JSON 合同、运行节点与统一运行锁。成功后以最新 Revision、当前运行节点与完整 JSON Body 创建 `trigger=webhook` Execution，并立即返回 202；Control 不等待 Worker 完成。

每次成功接收等于一条调用记录。Retention 按 Adapter 独立保留最新 100 条终态 Webhook Execution，永不删除 active Execution，也不处理 Task/Schedule 历史。

## 6. Clone 与删除

Clone 在一个事务内复制当前代码、依赖、运行参数、Credential 引用、触发配置和运行节点。新 Adapter 从自己的第一条 Revision 开始，无 Execution，Schedule/Webhook 均 disabled。

运行中的 Webhook A 可以 Clone 出同 path 的 stopped B；只有 A 停止后 B 才能开启，从而保持外部 URL 不变。

删除要求 `runtime_locked=false` 且不存在 active Execution。当前实现写入软删除标记，活跃 Catalog 不返回删除后的 Adapter；Web UI 不提供恢复入口。

## 7. Workbench 与实时日志

Workbench 固定为三类页签：

```text
Task:    编辑 / 运行设置 / 执行记录
Webhook: 编辑 / 运行设置 / 调用记录
```

Header 展示 Adapter 名称、类型、语言、运行状态、运行节点、dirty 状态、保存和类型相关动作。禁用控件使用可聚焦 wrapper 暴露稳定原因，Monaco 与所有运行配置控件同时遵循 `runtime_locked`。

Workbench 层只有一个当前实时 watcher：

1. 创建或发现 active Execution；
2. `GET /api/executions/{id}/events` 建立 SSE；
3. 增量合并 stdout / stderr；
4. 异常断开时有界轮询权威 Execution API；
5. 终态后刷新 Adapter 运行状态。

日志工作区位于页面底部，可切换全屏并恢复。Webhook 尚未产生 Execution 时只展示等待状态。历史详情按用户选择独立打开，不因后台 Schedule 创建而被替换。

## 8. Runtime

三种语言都在全新子进程中执行，并通过文件交换 JSON input/output：

- Python：version-scoped `.venv`；
- JavaScript：version-scoped `node_modules` 与 ESM harness；
- Java：version-scoped Maven deps、classes 与 JVM。

依赖准备统一 offline-first。stdout/stderr 增量上传并脱敏；超限按配置截断。input 超限直接拒绝且不创建 Execution；output 超限只保留大小、截断标记与 preview，不保存破坏的 JSON。

## 9. AI Assistant

浏览器显式提交当前 Working Copy、用户指令与有限最近对话。Control 补充服务端 language、基准 Revision 元数据、Runtime Contract 和 Secret env key 名称。Provider final answer 必须通过 Candidate Schema 校验。

Candidate Apply 只改浏览器 Working Copy；stale Candidate 需要再次明确确认。Credential 真值、平台 Token、Provider reasoning、Prompt 与原始 Response 不进入普通日志或持久化。

## 10. 部署与配置

Docker Compose 运行 `web / control / postgres / worker` 四个服务。关键配置包括：

- `DLR_ADMIN_TOKEN`、`DLR_WORKER_TOKEN`、`DLR_MASTER_KEY`；
- `DLR_WORKER_HEARTBEAT_SECONDS` 与 `DLR_WORKER_HEARTBEAT_TIMEOUT_SECONDS`；
- `DLR_SCHEDULE_POLL_SECONDS`；
- Execution 大字段、日志、超时与 Worker 并发限制。

AI Provider 是部署外部依赖，不进入正式 Compose 拓扑。compose-smoke 只在隔离网络启动本地 fake Provider。

## 11. 验证门禁

- Backend：Ruff、format check、Mypy、full pytest；
- Web：ESLint、TypeScript、Vitest、production build；
- Database：fresh Alembic install 与从当前 main schema upgrade；
- Integration：隔离 Compose smoke，真实运行三语言 Task、Schedule、Webhook、Clone URL 交接与运行锁；
- UI：真实浏览器验证底部/全屏日志、运行锁、Clone/Delete 与 Task/Webhook 主路径。

## 12. 明确边界

当前不引入 MQ、请求队列、自动重试、同步 Webhook、URL takeover、常驻进程模型、RBAC、通用插件系统、工作流编排、独立日志系统或 AI 自动执行循环。
