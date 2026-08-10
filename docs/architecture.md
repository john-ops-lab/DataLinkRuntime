# DLR（DataLinkRuntime）总体架构

> 版本：v1.0（已确认）
> 本文档范围：总体架构、领域模型落库设计、Runtime 设计、部署与里程碑。
> 产品定义见 [product.md](./product.md)；工程规范见 `.qoder/rules/engineering.md`，此处不重复。

## 1. 组件总览

```
┌─────────────┐   HTTP/JSON    ┌──────────────────────────────┐
│  web (React) │ ─────────────► │  control (FastAPI)            │
└─────────────┘                 │  ├─ Adapter/版本/执行 API     │
                                │  ├─ Worker 注册/心跳/任务通道  │
                                │  └─ PostgreSQL                │
                                └──────────────┬───────────────┘
                                               │ Worker 主动外连（HTTP 长轮询）
                                               │ 共享 DLR_WORKER_TOKEN 认证
                                ┌──────────────▼───────────────┐
                                │  worker (Python agent)        │
                                │  ├─ 领任务 → 按版本准备 venv   │
                                │  ├─ 子进程执行 Adapter         │
                                │  └─ 上报结果/日志/状态         │
                                └───────────────────────────────┘
```

| 组件 | 职责 | 是否运行用户代码 |
|------|------|------------------|
| web | React SPA：Adapter 管理、在线编辑、执行与日志查看 | 否 |
| control | FastAPI：API、版本管理、任务下发、执行记录、日志转发 | **否** |
| postgres | 持久化：Adapter、版本、执行记录、Worker 运行信息 | 否 |
| worker | 领任务、准备 version-scoped venv、子进程执行 Adapter、上报 | **是** |

关键决策：

- **通信方向**：Worker 主动外连 Control（HTTP 长轮询拉任务），Control 不主动连 Worker。多机部署时 Worker 可位于 NAT/防火墙后。
- **Manual Test 也下发 Worker 执行**，保证测试与运行环境一致，且 Control 不碰用户代码。
- **日志**：执行期间 Worker 增量上报 stdout/stderr，Control 通过 SSE 转发给前端实现实时查看；执行结束后日志随 Execution 持久化（受大字段策略约束）。不引入日志系统组件。

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
- 数据库不持久化任何真实 Secret。

### 2.3 Secret 注入路径（v1）

- Secret 只存在于 Worker 部署环境：Worker 从环境变量读取，约定前缀 `DLR_SECRET_<KEY>`。
- Adapter 通过 `context.secrets.get(key)` 获取；v1 实现即解析上述环境变量。
- **稳定性约束**：未来引入 Secret Store 时，只替换 Worker 侧解析实现，Runtime Contract 与 Adapter 代码零修改。

## 3. 领域模型与持久化

持久化全部在 PostgreSQL（SQLAlchemy 2.x + Alembic）。v1 共四张表：

### 3.1 Adapter

| 字段 | 说明 |
|------|------|
| id / name / description | 基本信息 |
| language | v1 固定 `python`，为多语言预留 |
| latest_version_id | 每次保存代码产生新版本后更新 |
| published_version_id | 发布时设置 |
| created_at / updated_at | 时间戳 |

### 3.2 AdapterVersion（不可变）

| 字段 | 说明 |
|------|------|
| id / adapter_id / seq | 归属与序号 |
| code | Python 源码（text），入口约定见 §4 |
| requirements | Python 依赖声明（text） |
| runtime_config | 非敏感运行时配置（JSON） |
| created_at | 时间戳 |

版本模型规则：

- **保存 = 新建不可变 Version** 并更新 `Adapter.latest_version_id`；不存在 Draft 实体。
- **发布 = 设置 `Adapter.published_version_id`**。
- Manual 测试默认执行 latest 版本；正式触发以 published 版本为准。

### 3.3 Execution

| 字段 | 说明 |
|------|------|
| id / adapter_id / **version_id（必填）** / worker_id | 关联关系 |
| trigger | 枚举：`manual`（预留 `schedule` / `webhook`） |
| status | `pending / running / succeeded / failed / timeout / cancelled` |
| input / output / stdout / stderr | 见 §3.5 大字段策略 |
| error | 失败摘要 |
| start_time / end_time / duration | 时间与耗时 |

### 3.4 Worker

| 字段 | 说明 |
|------|------|
| id / name | 身份与名称 |
| status | `online / offline` |
| last_heartbeat | 心跳时间 |
| capabilities | v1 固定 `python` |
| created_at / updated_at | 时间戳 |

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

## 4. Adapter Runtime（第一阶段：Python）

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

v1 不做 input/output schema 校验。多语言扩展方式：Worker 侧增加对应语言 runner + Version 声明 `language`，契约语义不变；v1 不建 runner 抽象层。

### 4.2 执行模型

- 每次 Execution = Worker 上一个**全新子进程**：harness 读取 input 文件 → import `adapter.py` → 调用 `handle` → 写出 output 文件；stdout/stderr 独立采集。
- **超时**：默认 300s（可配置），超时杀进程，状态记 `timeout`。
- **并发**：Worker 最大并发执行数可配置（默认 4），超过排队。

### 4.3 依赖与 venv 策略

- **version-scoped venv**：Worker 上每个 AdapterVersion 独立 venv 目录，与版本 requirements 严格一致，首次执行该版本时惰性构建，不被其他版本覆盖。
- 磁盘控制采用最简策略：清理该 Adapter 过期版本的 venv，保留 published + latest；不做跨 Adapter 依赖共享与复杂缓存。
- pip 源可在 Worker 配置中指定（应对内网镜像场景）。

## 5. 部署架构

Docker Compose 四容器（单机最小部署）：

| 服务 | 镜像/构建 | 说明 |
|------|-----------|------|
| postgres | postgres:16-alpine | healthcheck：pg_isready |
| control | backend 代码构建 | 依赖 postgres 健康；注入数据库连接串、DLR_ADMIN_TOKEN、DLR_WORKER_TOKEN |
| worker | backend 代码构建 | 注入 DLR_WORKER_TOKEN、control 地址、DLR_SECRET_* |
| web | 前端构建产物 + Nginx | 托管 SPA，反代 `/api` 到 control |

多机演进：worker 容器拆到其它服务器，指向 Control 地址即可，架构与协议不变（得益于 Worker 主动外连模型）。

## 6. 仓库结构

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
│   │   ├── control/             # FastAPI：api / services / db models
│   │   ├── worker/              # agent：领任务 / venv 管理 / 上报
│   │   └── runtime/             # Contract 与 Python harness
│   └── tests/
├── web/                         # React + TypeScript + Vite
└── docker/                      # control / worker / web 三个 Dockerfile
```

control 与 worker 共享同一 Python 包（`dlr.common` / `dlr.runtime`），避免多仓库与复杂 monorepo 工具链。

技术选型：Python 3.13、uv、FastAPI、SQLAlchemy 2.x、Alembic、pytest、ruff、mypy；前端 React（不固定版本，实现时选与当前工具链兼容的稳定版）+ TypeScript + Vite，Monaco Editor 于 M1 引入。**各阶段只安装该阶段真正需要的依赖。**

## 7. 里程碑（M0–M3）

| 里程碑 | 内容 | 验收口径 |
|--------|------|----------|
| M0 工程骨架 | 仓库结构、四容器 Compose、Health Check、SQLAlchemy + Alembic 空迁移骨架、基础测试、ruff/mypy/eslint、CI、README | `docker compose up` 四服务健康检查全绿；CI 绿 |
| M1 Adapter 管理 | Adapter CRUD、Monaco 在线编辑、保存即不可变版本、发布、requirements / runtime_config | 创建→编辑→保存→发布全通 |
| M2 执行闭环 | Worker 注册 / 心跳、version-scoped venv、Manual 触发、子进程执行、Execution 落库（含 §3.5 大字段策略） | "测试运行"→ Worker 执行 → 状态与结果正确 |
| M3 可观测与体验 | 测试输入面板、Output 查看（对象/数组渲染）、实时日志（SSE）、执行历史 | 第一阶段闭环完整可用 |

## 8. 未来演进（不在第一阶段）

- Schedule Trigger（届时引入调度库）。
- Webhook 统一入口（异步 202 语义）；如需同步调用另设 invoke API。
- Secret Store（仅替换 Worker 侧解析，Contract 不变）。
- 常驻 Adapter（AdapterInstance 落库与进程生命周期管理）。
- JavaScript / Java Runtime。
- Worker 独立凭据认证。
- AI 生成 / 修改 / 调试 Adapter 代码。

## 9. 过度设计检查清单（v1 明确不做）

Draft 实体；join token / enrollment；DB 加密 Secret；APScheduler 与前端状态框架预装；同步 Webhook 转发通道；Adapter 级覆盖式全局 venv；AdapterInstance 表；runner 抽象层；Sink / Connector 框架；MQ / 长连接网关；镜像构建与跨 Adapter 依赖共享；日志系统组件；RBAC；schema 校验；重试与优先级队列。
